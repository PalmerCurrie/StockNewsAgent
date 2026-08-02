# Railway dispatch trigger (example)

One way to get an alert at an **exact** time. This is an example, not a
requirement — the agent runs fine from `cron`, Task Scheduler, or anything else
that can start a process. See "Scheduling" in the root README for the simpler
options first.

Use this one if you want the workflow to run on GitHub Actions but refuse to
accept GitHub's scheduling delay. It is a tiny cron service that calls the
`workflow_dispatch` API at 07:45 and 13:45 local time, passing `deliver_at`, so
the message lands at 08:00 and 14:00 on the dot.

The service holds exactly one credential — a GitHub PAT. Every other secret
stays in GitHub Actions where the workflow already reads it, so nothing is
duplicated across two providers where the copies could silently drift.

Why the workflow can't just use a cron, and why the cron below is UTC while the
schedule is local, are both explained at the top of `dispatch.py`.

> Railway is a paid host (~$5/mo). Nothing about this project requires it; the
> same decision logic works on any scheduler that can run a Python script,
> including a free Cloudflare Worker or a machine you already own.

## Deploy

**1. Create the PAT.** GitHub → Settings → Developer settings → Personal access
tokens → Fine-grained tokens.

| | |
|---|---|
| Repository access | Only select repositories → the repo holding your workflow |
| Permissions | Repository permissions → **Actions: Read and write**. Nothing else. |
| Expiration | Note the date — see "When the PAT expires" below |

**2. Create the Railway service.** New → GitHub Repo → your repo, then in
Settings set **Root Directory** to `triggers/railway`. `railway.json` in this
directory already declares the start command, the cron and a never-restart
policy (this is a job that exits, not a server), so a fresh deploy from that
root directory picks them up without any UI edits.

**3. Set the variables.** The first two are required; the rest have defaults in
`dispatch.py`.

| Variable | Value |
|---|---|
| `GITHUB_TOKEN` | the PAT from step 1 |
| `GITHUB_REPO` | `<owner>/<repo>` holding the workflow |
| `GITHUB_REF` | `main` |
| `WORKFLOW_FILE` | `agent.yml` |
| `TRIGGER_TZ` | your IANA zone, e.g. `America/New_York` |
| `TRIGGER_SCHEDULE` | `07:45=08:00,13:45=14:00` |
| `TRIGGER_WINDOW` | `15` |

**If you change `TRIGGER_TZ` or the fire times, update the cron in
`railway.json` to match.** It is UTC and must list *both* possible UTC hours for
each fire time — standard and daylight — because `dispatch.py` picks the real
one at runtime. The shipped `45 11,12,17,18 * * 1-5` covers 07:45 and 13:45
`America/New_York`.

**4. Verify before waiting on a real fire time.** In the Railway shell, or
locally with the PAT exported:

```bash
TRIGGER_DRY_RUN=1 python dispatch.py    # decision only, no POST
TRIGGER_FORCE=08:00 python dispatch.py  # real POST; expect "dispatched: HTTP 204"
```

The forced run should appear in Actions immediately — a dispatched run is
created the instant the call lands, so if it is not there within seconds
something is wrong with the token, not with the queue.

## Reading the logs

Four executions fire per weekday and three of them do nothing — that is the
design, not a fault. A quiet one logs:

```
not a scheduled fire time (07:45, 13:45 America/New_York) -- nothing to do
```

The one that acts logs the matched fire time and `dispatched: HTTP 204`.

If *no* execution ever logs a dispatch, the local-time check is rejecting all
four: check `TRIGGER_TZ` against the cron hours. If two dispatch in one day,
`TRIGGER_WINDOW` is wide enough to swallow two entries — it must stay far below
the gap between them.

## When the PAT expires

This is the failure mode worth designing around, because it is silent: if you
kept the workflow's fallback crons, alerts do not stop — they quietly fall back
to those and arrive late. **A fallback cron run that actually delivers something
is the signal that the trigger has died**; on a healthy day the primary has
already sent everything and dedup makes the fallback a no-op.

Two things shorten the gap between expiry and noticing:

- Railway's execution log goes red on expiry — the script exits 1 with
  `GitHub rejected the token (HTTP 401)` rather than failing quietly.
- Every successful dispatch checks the PAT expiry that GitHub returns in a
  response header and warns for the last 14 days.
