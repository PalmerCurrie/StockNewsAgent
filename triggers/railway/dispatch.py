#!/usr/bin/env python3
"""Fire the Stock News Agent workflow at an exact local time.

WHY THIS EXISTS

GitHub's `schedule` trigger cannot hit a target time. It queues scheduled
workflows and creates the run when it has capacity; measured on this repo the
delay ran from 50 minutes to just over two hours, and moving a cron 50 minutes
later moved the actual fire time only ~15. It is a queue, not a schedule, so
the delay cannot be subtracted out. A `workflow_dispatch` run, by contrast, is
created the instant the API call lands -- measured 16:07:12 dispatch, 16:07:12
run created.

So: this dispatches, and passes `deliver_at`. The agent fetches immediately and
then holds the finished alert until that exact wall-clock minute, which absorbs
the 2-8 minute spread in how long a run takes. News is gathered at 07:45; the
message arrives at 08:00:00.

WHY THE ODD CRON

Railway's cron expressions are UTC, which would reintroduce exactly the
twice-yearly DST edit that `--deliver-at` was built to remove. Rather than
accept that, the cron fires on *every* UTC hour that could be the right local
hour and this script decides which one is real: it dispatches only when it is
genuinely a scheduled local time in TRIGGER_TZ, and exits quietly otherwise.
Exactly one fire per half-day acts, year-round, with no seasonal edit at either
end. The rest are deliberate no-ops and say so in the log.

To build the cron for your own zone, take each TRIGGER_SCHEDULE fire time and
list both of its UTC hours -- standard time and daylight time. The shipped
`45 11,12,17,18 * * 1-5` is 07:45 and 13:45 America/New_York (EDT: 11:45 and
17:45 UTC; EST: 12:45 and 18:45).

CONFIGURATION (environment variables)

  GITHUB_TOKEN     required. Fine-grained PAT, scoped to the one repo holding
                   the workflow, Actions: Read and write. Nothing else.
  GITHUB_REPO      required. owner/name of the repo holding the workflow.
  GITHUB_REF       branch to run the workflow from.
  WORKFLOW_FILE    workflow filename, not a path.
  TRIGGER_TZ       IANA zone the schedule below is written in.
  TRIGGER_SCHEDULE comma-separated `fire_time=deliver_at` pairs, local to
                   TRIGGER_TZ. Fire early enough that the run is certainly
                   finished before deliver_at.
                   Keep the UTC cron above in sync with these times.
  TRIGGER_WINDOW   minutes after a fire time during which a late cron still
                   counts as that fire. Must stay well under the gap between
                   entries, or two of them could match the same moment.
  TRIGGER_DRY_RUN  set to 1 to log the decision and skip the POST.
  TRIGGER_FORCE    set to an HH:MM deliver_at to dispatch regardless of the
                   clock. For verifying a fresh deploy.

EXIT CODES

  0  dispatched, or deliberately did nothing because it is not a fire time
  1  misconfigured, or GitHub rejected the dispatch
"""

from __future__ import annotations

import json
import os
import sys
import time as clock
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone
from typing import List, NoReturn, Optional, Tuple
from zoneinfo import ZoneInfo

API_ROOT = "https://api.github.com"

#: A failed dispatch is not a catastrophe -- the workflow's own fallback crons
#: still deliver, just late -- but a transient 5xx is cheap to ride out.
ATTEMPTS = 3
BACKOFF_SECONDS = 5

#: The PAT expiring is the one failure mode that is silent by design: alerts
#: quietly fall back to the late crons rather than stopping. GitHub returns the
#: expiry on every response, so warn well before it bites.
EXPIRY_WARNING_DAYS = 14

DEFAULTS = {
    # GITHUB_REPO and GITHUB_TOKEN have no defaults on purpose -- they are the
    # two values that must be yours.
    "GITHUB_REF": "main",
    "WORKFLOW_FILE": "agent.yml",
    "TRIGGER_TZ": "America/New_York",
    "TRIGGER_SCHEDULE": "07:45=08:00,13:45=14:00",
    "TRIGGER_WINDOW": "15",
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def fail(message: str) -> NoReturn:
    log(f"ERROR {message}")
    sys.exit(1)


def env(name: str, required: bool = False) -> str:
    """Empty and unset mean the same thing.

    Railway renders a variable you have defined but left blank as an empty
    string, and treating that as "configured" would dispatch against an empty
    repo name.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        value = DEFAULTS.get(name, "")
    if not value and required:
        fail(f"{name} is not set")
    return value


def parse_schedule(raw: str) -> List[Tuple[str, str]]:
    """`"07:45=08:00,13:45=14:00"` -> `[("07:45", "08:00"), ("13:45", "14:00")]`."""
    schedule: List[Tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        fire, sep, deliver = entry.partition("=")
        if not sep:
            fail(f"TRIGGER_SCHEDULE entry {entry!r} is not `fire_time=deliver_at`")
        fire, deliver = fire.strip(), deliver.strip()
        for label, value in (("fire time", fire), ("deliver_at", deliver)):
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError:
                fail(f"TRIGGER_SCHEDULE {label} {value!r} is not HH:MM")
        schedule.append((fire, deliver))
    if not schedule:
        fail("TRIGGER_SCHEDULE is empty")
    return schedule


def due(
    now: datetime, schedule: List[Tuple[str, str]], window_minutes: int
) -> Optional[Tuple[str, str]]:
    """The schedule entry `now` falls into, if any.

    The window opens a minute early to absorb clock skew and closes
    `window_minutes` later so a cron that fires a few minutes late still counts.
    It must stay far narrower than the gap between entries; otherwise a very
    late morning fire could be mistaken for the afternoon one and deliver the
    wrong target time.
    """
    for fire, deliver in schedule:
        hour, minute = (int(part) for part in fire.split(":"))
        # combine() resolves the wall time against the zone's offset for that
        # date, matching how agent/time_gate.py builds its targets.
        target = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
        elapsed = (now - target).total_seconds() / 60
        if -1 <= elapsed < window_minutes:
            return fire, deliver
    return None


def warn_if_token_expiring(headers) -> None:
    """Surface an approaching PAT expiry while there is still time to act.

    Best-effort by design: this runs on the success path, and a header format
    change must never turn a working dispatch into a failed one.
    """
    raw = headers.get("github-authentication-token-expiration")
    if not raw:
        return
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            expires = datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = expires - datetime.now(timezone.utc)
        if remaining < timedelta(days=EXPIRY_WARNING_DAYS):
            log(
                f"WARNING the PAT expires in {remaining.days} day(s), on {raw}. "
                "Renew it: when it lapses, alerts fall back to the late crons "
                "silently rather than stopping."
            )
        return
    log(f"note: could not parse the token expiry header ({raw!r})")


def post_dispatch(repo: str, workflow: str, ref: str, token: str, inputs: dict) -> None:
    url = f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": ref, "inputs": inputs}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "stock-news-agent-trigger")

    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                # The dispatch API answers 204 with an empty body. It reports
                # only that the request was accepted -- there is no run id to
                # confirm against, which is part of why the workflow keeps its
                # fallback crons.
                log(f"dispatched: HTTP {response.status}")
                warn_if_token_expiring(response.headers)
                return
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace").strip()
            if error.code in (401, 403):
                fail(
                    f"GitHub rejected the token (HTTP {error.code}): {detail}. "
                    "The PAT has expired, or it is missing Actions: Read and "
                    "write on this repo."
                )
            if error.code == 404:
                fail(
                    f"HTTP 404 for {repo} / {workflow}. Either the workflow "
                    "filename is wrong, or the PAT cannot see the repo -- a "
                    "fine-grained PAT returns 404 rather than 403 for a repo "
                    "outside its scope."
                )
            if error.code == 422:
                fail(
                    f"HTTP 422: {detail}. Usually the ref {ref!r} does not "
                    "exist, or an input name does not match the workflow."
                )
            if error.code < 500:
                fail(f"HTTP {error.code}: {detail}")
            last = f"HTTP {error.code}: {detail}"
        except urllib.error.URLError as error:
            last = f"network error: {error.reason}"

        if attempt == ATTEMPTS:
            fail(f"dispatch failed after {ATTEMPTS} attempts -- {last}")
        log(f"attempt {attempt}/{ATTEMPTS} failed ({last}); retrying")
        clock.sleep(BACKOFF_SECONDS * attempt)


def main() -> int:
    token = env("GITHUB_TOKEN", required=True)
    repo = env("GITHUB_REPO", required=True)
    ref = env("GITHUB_REF", required=True)
    workflow = env("WORKFLOW_FILE", required=True)
    tz_name = env("TRIGGER_TZ", required=True)
    dry_run = os.environ.get("TRIGGER_DRY_RUN", "").strip() == "1"
    forced = os.environ.get("TRIGGER_FORCE", "").strip()

    try:
        tz = ZoneInfo(tz_name)
    except Exception as error:  # ZoneInfoNotFoundError, or a missing tzdata
        fail(f"TRIGGER_TZ {tz_name!r} is not a usable IANA zone: {error}")

    schedule = parse_schedule(env("TRIGGER_SCHEDULE", required=True))
    try:
        window = int(env("TRIGGER_WINDOW", required=True))
    except ValueError:
        fail("TRIGGER_WINDOW must be a whole number of minutes")

    now = datetime.now(tz)
    log(f"now {now:%Y-%m-%d %H:%M:%S %Z} ({tz_name})")

    if forced:
        log(f"TRIGGER_FORCE set -- dispatching with deliver_at={forced}")
        deliver_at = forced
    else:
        # Weekday in local time, not UTC. The UTC cron's own `1-5` is a
        # different question from whether it is a weekday where the market is.
        if now.weekday() > 4:
            log("weekend -- nothing to do")
            return 0
        match = due(now, schedule, window)
        if match is None:
            times = ", ".join(fire for fire, _ in schedule)
            log(f"not a scheduled fire time ({times} {tz_name}) -- nothing to do")
            return 0
        fire, deliver_at = match
        log(f"fire time {fire} matched -- deliver_at={deliver_at}")

    inputs = {"deliver_at": deliver_at, "deliver_tz": tz_name}
    if dry_run:
        log(f"TRIGGER_DRY_RUN=1 -- would POST {repo} {workflow} ref={ref} {inputs}")
        return 0

    post_dispatch(repo, workflow, ref, token, inputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
