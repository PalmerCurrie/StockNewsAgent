"""Alert formatting and delivery (Requirement 7).

Each channel is an adapter that knows its own character limits and its own
markup dialect. The Alert is rendered once into ordered *blocks* (header, one
per event, earnings) and each adapter packs those blocks into messages without
ever splitting an event across a boundary.
"""

from __future__ import annotations

import os
import smtplib
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as html_escape
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import requests

from .alert_builder import group_events
from .logger import Logger
from .models import Alert, ChannelConfig, DeliveryResult, Event

COMPONENT = "Notifier"

TIMEOUT_SECONDS = 30
RETRY_BACKOFF_SECONDS = 2
MAX_SUBJECT_CHARS = 200
TELEGRAM_LIMIT = 4096
DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBED_FIELD_LIMIT = 4096
DISCORD_EMBED_TOTAL_LIMIT = 6000


class TransientDeliveryError(Exception):
    """Timeout, 5xx, or 429 -- worth exactly one retry."""


class PermanentDeliveryError(Exception):
    """4xx (other than 429) or a configuration problem -- do not retry."""


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def escape_mdv2(text: str) -> str:
    return "".join("\\" + ch if ch in _MDV2_SPECIALS else ch for ch in str(text))


def _bold(text: str, style: str) -> str:
    if style == "plain":
        return text
    return f"*{text}*"


def _link(label: str, url: str, style: str) -> str:
    if style == "plain":
        return url
    if style == "markdown_v2":
        # Inside a MarkdownV2 link destination only ')' and '\' are special --
        # escaping the rest would corrupt the URL.
        destination = url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{escape_mdv2(label)}]({destination})"
    return f"[{label}](<{url}>)"


def _esc(text: str, style: str) -> str:
    return escape_mdv2(text) if style == "markdown_v2" else str(text)


def _sep(style: str) -> str:
    """The ' | ' field separator.

    ``|`` is a MarkdownV2 special. It sits between two ``_esc``'d fields rather
    than inside them, so it has to be escaped here -- an unescaped one makes
    Telegram reject the entire message with a 400, which is a permanent error
    and therefore never retried.
    """
    return " \\| " if style == "markdown_v2" else " | "


def _code(text: str, style: str) -> str:
    """A monospace chip. Inside a MarkdownV2 code span only '`' and '\\' are
    special -- escaping the rest would render the backslashes literally."""
    if style != "markdown_v2":
        return str(text)
    body = str(text).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{body}`"


def _italic(text: str, style: str) -> str:
    return text if style == "plain" else f"_{text}_"


#: Impact score -> severity dot. A 9 and a 6 rendered identically before, which
#: made the most important line on the screen indistinguishable from the least.
SEVERITY_HIGH = 8
SEVERITY_MEDIUM = 7


def _severity_icon(score: int) -> str:
    if score >= SEVERITY_HIGH:
        return "🔴"
    if score >= SEVERITY_MEDIUM:
        return "🟠"
    return "🟡"


#: Below this absolute move the arrow reads as flat rather than directional.
FLAT_MOVE_PCT = 0.15


def _direction_arrow(movement: Optional[float]) -> str:
    if movement is None:
        return "•"
    if movement > FLAT_MOVE_PCT:
        return "▲"
    if movement < -FLAT_MOVE_PCT:
        return "▼"
    return "▬"


CATEGORY_LABELS = {
    "earnings_release": "Earnings",
    "regulatory_ruling": "Regulatory",
    "product_launch": "Product",
    "analyst_rating_change": "Analyst",
    "macroeconomic_announcement": "Macro",
    "other": "Other",
}

#: Domains worth naming properly. Anything absent falls back to the second-level
#: domain, title-cased, which is right often enough ("bloomberg" -> "Bloomberg").
PUBLISHER_NAMES = {
    "reuters.com": "Reuters",
    "cnbc.com": "CNBC",
    "finance.yahoo.com": "Yahoo Finance",
    "yahoo.com": "Yahoo",
    "ft.com": "FT",
    "wsj.com": "WSJ",
    "barrons.com": "Barron's",
    "bloomberg.com": "Bloomberg",
    "marketwatch.com": "MarketWatch",
    "investors.com": "IBD",
    "seekingalpha.com": "Seeking Alpha",
    "fool.com": "Motley Fool",
    "benzinga.com": "Benzinga",
    "businesswire.com": "Business Wire",
    "prnewswire.com": "PR Newswire",
    "globenewswire.com": "GlobeNewswire",
    "sec.gov": "SEC",
    "finviz.com": "Finviz",
}


def publisher_name(url: str) -> str:
    """A human label for a source link, derived from its domain.

    The Event carries only URLs -- the Story's ``source`` field names the
    *fetcher* (yfinance / google_news / finviz), not the publisher, so the
    domain is the only thing that identifies who actually wrote the story.
    """
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return "source"
    host = host.removeprefix("www.").split(":")[0]
    if not host:
        return "source"
    if host in PUBLISHER_NAMES:
        return PUBLISHER_NAMES[host]
    parts = host.split(".")
    # news.google.com -> Google, not News
    stem = parts[-2] if len(parts) >= 2 else parts[0]
    return stem.replace("-", " ").title()


def pretty_group(group: str) -> str:
    """'core_holdings' -> 'CORE HOLDINGS'. The label is whatever `group:` the
    user typed in the watchlist file, so it arrives in arbitrary case."""
    return group.replace("_", " ").replace("-", " ").upper()


def format_local_time(moment: datetime) -> str:
    """'Tue Jul 28, 2:00 PM PT'.

    Built by hand rather than with strftime's '%-I'/'%-d': those are glibc
    extensions and raise ValueError on Windows, where this is developed.
    """
    hour = (moment.hour - 1) % 12 + 1
    meridiem = "AM" if moment.hour < 12 else "PM"
    stamp = f"{moment:%a %b} {moment.day}, {hour}:{moment:%M} {meridiem}"
    zone = moment.strftime("%Z")
    return f"{stamp} {zone}".strip()


def format_day(day: date) -> str:
    return f"{day:%a %b} {day.day}"


def _expandable_quote(lines: list[str], style: str) -> str:
    """Fold detail into a collapsed block the reader can tap to open.

    MarkdownV2 spells an expandable blockquote as '**>' on the first line, '>'
    on every line after it, and '||' closing the last one (Bot API 7.0+). Older
    clients degrade to a plain quote rather than showing the markup.

    Every line must carry its '>' -- a bare blank line ends the quote early and
    strands the '||', which Telegram rejects as a 400.
    """
    body = [line for line in lines if line.strip()]
    if not body:
        return ""
    if style == "plain":
        return "\n".join(body)
    if style == "markdown":  # Discord renders '>' as a quote too
        return "\n".join(f"> {line}" for line in body)
    return "**" + "\n".join(f">{line}" for line in body) + "||"


def render_subject(alert: Alert) -> str:
    count = len(alert.events)
    noun = "event" if count == 1 else "events"
    subject = (
        f"Stock News Agent: {count} {noun} — "
        f"{alert.run_timestamp_local.strftime('%Y-%m-%d %H:%M %Z')}"
    )
    return subject[:MAX_SUBJECT_CHARS]


def render_blocks(alert: Alert, style: str = "markdown") -> list[str]:
    """Ordered, independently-splittable chunks of the Alert.

    One block per event, so ``pack_blocks`` can never split a card across a
    message boundary -- which for MarkdownV2 would strand the '||' that closes
    an expandable quote and get the whole message rejected.
    """
    blocks: list[str] = [_render_header(alert, style)]

    if not alert.events:
        summary = alert.no_events_summary or "No significant events in this run."
        blocks.append(_esc(summary, style))
    else:
        for group, by_ticker in group_events(alert.events).items():
            if group:
                blocks.append(_bold(_esc(pretty_group(group), style), style))
            for events in by_ticker.values():
                blocks.extend(_render_event(event, style) for event in events)

    if alert.earnings_upcoming:
        lines = [_bold(_esc("📅 Earnings this week", style), style)]
        for ticker, day in alert.earnings_upcoming.items():
            lines.append(f"• {_code(ticker, style)} — {_esc(format_day(day), style)}")
        blocks.append("\n".join(lines))

    return blocks


def _render_header(alert: Alert, style: str) -> str:
    """Two lines: who and when, then what was covered.

    The old header printed the same instant twice -- a full ISO-8601 UTC stamp
    next to the local one -- which is machine output, not something a person
    reads on a phone.
    """
    title = _bold(_esc("📊 Stock News Agent", style), style)
    when = _esc(format_local_time(alert.run_timestamp_local), style)
    count = len(alert.events)
    noun = "event" if count == 1 else "events"
    scope = _esc(f"{count} {noun} across {alert.tickers_monitored} tickers", style)
    return f"{title} · {when}\n{_italic(scope, style)}"


def _render_event(event: Event, style: str) -> str:
    """One card: headline, a metadata strip, collapsed detail, and sources."""
    headline = f"{_severity_icon(event.impact_score)} {_bold(_esc(event.headline, style), style)}"

    category = CATEGORY_LABELS.get(event.category, event.category.replace("_", " ").title())
    meta = "  ·  ".join(
        [
            f"{_code(event.ticker, style)}  "
            f"{_direction_arrow(event.price_movement)} {_esc(_compact_movement(event), style)}",
            _esc(f"impact {event.impact_score}/10", style),
            _esc(category, style),
        ]
    )

    detail = _expandable_quote(
        [
            _esc(event.summary, style),
            _esc(event.market_reaction, style) if event.market_reaction else "",
        ],
        style,
    )

    sources = " · ".join(
        _link(publisher_name(url), url, style) for url in event.source_urls[:5]
    )

    return "\n".join(part for part in (headline, meta, detail, sources) if part)


def _format_movement(event: Event) -> str:
    if event.price_movement_pending:
        return "price move pending (market not open yet)"
    if event.price_movement is None:
        return "price move unavailable"
    return f"{event.price_movement:+.2f}% over {event.price_window_hours}h"


def _compact_movement(event: Event) -> str:
    """The card's version of the above.

    ``price_window_hours`` is the same for every event in a run, so spelling out
    'over 2h' on each card is noise on a phone-width line. The long form stays
    on the email/HTML renderings, which have the room for it.
    """
    if event.price_movement_pending:
        return "price pending"
    if event.price_movement is None:
        return "price n/a"
    return f"{event.price_movement:+.2f}%"


def render_plain_text(alert: Alert) -> str:
    return "\n\n".join(render_blocks(alert, style="plain"))


def render_html(alert: Alert) -> str:
    parts = [
        "<h2>Stock News Agent</h2>",
        f"<p>{html_escape(alert.run_timestamp_utc.strftime('%Y-%m-%dT%H:%M:%SZ'))} | "
        f"{html_escape(alert.run_timestamp_local.strftime('%Y-%m-%d %H:%M %Z'))}<br>"
        f"{alert.tickers_monitored} tickers monitored</p>",
    ]
    if not alert.events:
        parts.append(
            f"<p>{html_escape(alert.no_events_summary or 'No significant events in this run.')}</p>"
        )
    for group, by_ticker in group_events(alert.events).items():
        if group:
            parts.append(f"<h3>{html_escape(group)}</h3>")
        for ticker, events in by_ticker.items():
            parts.append(f"<h3>{html_escape(ticker)}</h3>")
            for event in events:
                sources = " ".join(
                    f'<a href="{html_escape(url)}">source {i}</a>'
                    for i, url in enumerate(event.source_urls[:5], 1)
                )
                reaction = (
                    f"<p><em>{html_escape(event.market_reaction)}</em></p>"
                    if event.market_reaction
                    else ""
                )
                parts.append(
                    f"<p><strong>{html_escape(event.headline)}</strong><br>"
                    f"Impact {event.impact_score}/10 | "
                    f"{html_escape(_format_movement(event))}</p>"
                    f"<p>{html_escape(event.summary)}</p>{reaction}<p>{sources}</p>"
                )
    if alert.earnings_upcoming:
        rows = "".join(
            f"<li>{html_escape(t)} — {html_escape(d.isoformat())}</li>"
            for t, d in alert.earnings_upcoming.items()
        )
        parts.append(f"<h3>Upcoming Earnings (next 7 days)</h3><ul>{rows}</ul>")
    return "<html><body>" + "".join(parts) + "</body></html>"


#: Every character MarkdownV2 reserves. If any of these reaches Telegram
#: unescaped the whole message is rejected with a permanent 400, so putting
#: them all in the canary makes --test-channels fail loudly on a regression.
_MDV2_CANARY_TEXT = "escaping canary _*[]()~`>#+-=|{}.! done"


def build_canary_alert(now: Optional[datetime] = None) -> Alert:
    """A fake Alert that goes out through the real ``send_alert`` path.

    ``--test-channels`` used to send plain text, which on Telegram meant it
    never exercised MarkdownV2 at all -- it reported success for a month while
    every real alert was rejected for an unescaped '|'. The point of this
    fixture is to be *harder* to render than a real alert.

    Between them the two events below cover every part of the card that can
    fail to render: all reserved characters (inside an expandable blockquote,
    where an unescaped one strands the closing '||'), a URL with parentheses
    and query separators, a group header, a signed decimal price move, and an
    event with no market_reaction and no price data -- which is the one that
    produces a single-line quote rather than a two-line one.
    """
    now = now or datetime.now(timezone.utc)
    events = [
        Event(
            ticker="TEST",
            group="channel check",
            headline=f"Delivery test — {_MDV2_CANARY_TEXT}",
            # Deliberately near MAX_SUMMARY_WORDS. A short quote body is not a
            # useful test: Telegram only renders the collapsed state, and with
            # it the tap-to-expand affordance, once a quote passes a height
            # threshold -- below that it just shows the quote in full. A canary
            # with a two-line summary therefore proves the entity parsed but
            # shows nothing of the behaviour the layout exists for. This length
            # also approximates the worst-case card size against the 4096 limit.
            summary=(
                "This is an automated channel test from Stock News Agent, not a real alert, "
                "and no action is needed. The body of this card is padded to roughly the "
                "length of a real event summary so that the expandable quote actually "
                "collapses in the Telegram client and shows its tap-to-expand control. If "
                "you are reading this sentence without having tapped anything, the quote "
                "did not collapse and the layout is not behaving as intended. If you had to "
                "tap to reveal it, everything is working. The remainder of this paragraph "
                "exists purely to occupy vertical space in the same way that a genuine "
                "hundred-and-fifty word summary of an earnings report or a regulatory "
                "ruling would occupy it, including the awkward reserved characters that "
                "have broken this renderer before now. Special characters: "
                f"{_MDV2_CANARY_TEXT}"
            ),
            category="other",
            impact_score=9,  # exercises the top severity icon
            # Parentheses and query separators are the two things that break a
            # MarkdownV2 link destination.
            source_urls=["https://example.com/test_(1)?a=b&c=d", "https://www.reuters.com/x"],
            price_movement=-1.25,  # renders a signed decimal behind a '▼'
            price_window_hours=2,
            market_reaction=f"Reaction line: {_MDV2_CANARY_TEXT}",
        ),
        Event(
            ticker="TEST.V",  # a dot inside a code span, which must NOT be escaped
            headline="Second card — no price data, no market reaction",
            summary=f"Single-line quote body. {_MDV2_CANARY_TEXT}",
            category="earnings_release",
            impact_score=6,  # exercises the lowest severity icon
            source_urls=["https://example.com/plain"],
            price_movement=None,
            market_reaction=None,
        ),
    ]
    return Alert(
        run_id="channel-test",
        run_timestamp_utc=now,
        run_timestamp_local=now,
        tickers_monitored=2,
        events=events,
        earnings_upcoming={"TEST": now.date()},
    )


def pack_blocks(blocks: list[str], limit: int, separator: str = "\n\n") -> list[str]:
    """Group blocks into messages of at most ``limit`` chars, splitting only
    within a block when that single block is itself too long."""
    messages: list[str] = []
    current = ""

    for block in blocks:
        for piece in _split_oversized(block, limit):
            if not current:
                current = piece
            elif len(current) + len(separator) + len(piece) <= limit:
                current = f"{current}{separator}{piece}"
            else:
                messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def _split_oversized(block: str, limit: int) -> list[str]:
    """Last-resort splitter for a single block that exceeds the limit.

    This is markup-unaware: splitting a MarkdownV2 card here would strand the
    '||' closing its expandable quote and get the message rejected. It is
    unreachable in practice -- ``MAX_SUMMARY_WORDS`` (150) caps the only
    unbounded field, which puts the largest possible event card near 2k against
    Telegram's 4096 -- so raising that cap means revisiting this function.
    """
    if len(block) <= limit:
        return [block]
    pieces: list[str] = []
    remaining = block
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces


# --------------------------------------------------------------------------
# Channel adapters
# --------------------------------------------------------------------------


class ChannelAdapter(ABC):
    type: str = "unknown"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()

    @abstractmethod
    def missing_config(self) -> list[str]:
        """Names of required env vars that are absent."""

    @abstractmethod
    def send_alert(self, alert: Alert) -> None: ...

    @abstractmethod
    def send_text(self, text: str) -> None: ...

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        try:
            response = self._session.post(url, timeout=TIMEOUT_SECONDS, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TransientDeliveryError(str(exc)) from exc
        _raise_for_status(response)
        return response


def _raise_for_status(response: requests.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = f"HTTP {status}: {response.text[:300]}"
    if status == 429 or status >= 500:
        raise TransientDeliveryError(detail)
    raise PermanentDeliveryError(detail)


class TelegramAdapter(ChannelAdapter):
    type = "telegram"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        super().__init__(session)
        self._token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def missing_config(self) -> list[str]:
        missing = []
        if not self._token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self._chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing

    def send_alert(self, alert: Alert) -> None:
        for message in pack_blocks(render_blocks(alert, style="markdown_v2"), TELEGRAM_LIMIT):
            self._send(message, markdown=True)

    def send_text(self, text: str) -> None:
        self._send(text[:TELEGRAM_LIMIT], markdown=False)

    def _send(self, text: str, markdown: bool) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "MarkdownV2"
        self._post(f"https://api.telegram.org/bot{self._token}/sendMessage", json=payload)


class DiscordAdapter(ChannelAdapter):
    type = "discord"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        super().__init__(session)
        self._webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")

    def missing_config(self) -> list[str]:
        return [] if self._webhook else ["DISCORD_WEBHOOK_URL"]

    def send_alert(self, alert: Alert) -> None:
        blocks = render_blocks(alert, style="markdown")
        plain = "\n\n".join(blocks)
        if len(plain) <= DISCORD_CONTENT_LIMIT:
            self._post(self._webhook, json={"content": plain})
            return

        # Too long for plain content: fall back to embeds (Requirement 7.5).
        descriptions = pack_blocks(blocks, DISCORD_EMBED_FIELD_LIMIT)
        batch: list[dict[str, str]] = []
        batch_chars = 0
        for description in descriptions:
            if batch and (
                batch_chars + len(description) > DISCORD_EMBED_TOTAL_LIMIT or len(batch) >= 10
            ):
                self._post(self._webhook, json={"embeds": batch})
                batch, batch_chars = [], 0
            batch.append({"description": description})
            batch_chars += len(description)
        if batch:
            self._post(self._webhook, json={"embeds": batch})

    def send_text(self, text: str) -> None:
        self._post(self._webhook, json={"content": text[:DISCORD_CONTENT_LIMIT]})


class EmailAdapter(ChannelAdapter):
    type = "email"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        super().__init__(session)
        self._host = os.environ.get("SMTP_HOST", "")
        self._port = int(os.environ.get("SMTP_PORT", "587") or 587)
        self._user = os.environ.get("SMTP_USER", "")
        self._password = os.environ.get("SMTP_PASSWORD", "")
        self._to = os.environ.get("SMTP_TO", "")

    def missing_config(self) -> list[str]:
        return [
            name
            for name, value in (
                ("SMTP_HOST", self._host),
                ("SMTP_USER", self._user),
                ("SMTP_PASSWORD", self._password),
                ("SMTP_TO", self._to),
            )
            if not value
        ]

    def send_alert(self, alert: Alert) -> None:
        self._send(render_subject(alert), render_plain_text(alert), render_html(alert))

    def send_text(self, text: str) -> None:
        self._send("Stock News Agent: channel test", text, f"<html><body><p>{html_escape(text)}</p></body></html>")

    def _send(self, subject: str, plain: str, html: Optional[str]) -> None:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject[:MAX_SUBJECT_CHARS]
        message["From"] = self._user
        message["To"] = self._to
        message.attach(MIMEText(plain, "plain", "utf-8"))
        if html:
            message.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP(self._host, self._port, timeout=TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(self._user, self._password)
                server.sendmail(self._user, [addr.strip() for addr in self._to.split(",")], message.as_string())
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused) as exc:
            raise PermanentDeliveryError(str(exc)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientDeliveryError(str(exc)) from exc


ADAPTERS: dict[str, type[ChannelAdapter]] = {
    TelegramAdapter.type: TelegramAdapter,
    DiscordAdapter.type: DiscordAdapter,
    EmailAdapter.type: EmailAdapter,
}


# --------------------------------------------------------------------------
# Notifier
# --------------------------------------------------------------------------


class Notifier:
    def __init__(
        self,
        logger: Logger,
        session: Optional[requests.Session] = None,
        adapters: Optional[dict[str, ChannelAdapter]] = None,
    ) -> None:
        self._logger = logger
        self._session = session or requests.Session()
        self._override_adapters = adapters or {}

    def dispatch(self, alert: Alert, channels: list[ChannelConfig]) -> list[DeliveryResult]:
        if not channels:
            self._logger.error(
                COMPONENT,
                "No delivery channels are configured; nothing to dispatch",
                error_type="NoChannelsConfigured",
            )
            return []
        return [
            self._deliver(channel.type, lambda adapter: adapter.send_alert(alert))
            for channel in channels
        ]

    def test_channels(self, channels: list[ChannelConfig]) -> list[DeliveryResult]:
        if not channels:
            self._logger.error(
                COMPONENT,
                "No delivery channels are configured; nothing to test",
                error_type="NoChannelsConfigured",
            )
            return []
        alert = build_canary_alert()
        return [
            self._deliver(channel.type, lambda adapter: adapter.send_alert(alert))
            for channel in channels
        ]

    # -- internals ---------------------------------------------------------

    def _build_adapter(self, channel_type: str) -> Optional[ChannelAdapter]:
        if channel_type in self._override_adapters:
            return self._override_adapters[channel_type]
        adapter_cls = ADAPTERS.get(channel_type)
        return adapter_cls(self._session) if adapter_cls else None

    def _deliver(
        self, channel_type: str, action: Callable[[ChannelAdapter], None]
    ) -> DeliveryResult:
        adapter = self._build_adapter(channel_type)
        if adapter is None:
            self._logger.error(
                COMPONENT,
                f"Unsupported delivery channel {channel_type!r}",
                error_type="UnsupportedChannel",
                channel=channel_type,
            )
            return DeliveryResult(channel_type, "failure", "unsupported channel type")

        missing = adapter.missing_config()
        if missing:
            self._logger.error(
                COMPONENT,
                f"Channel {channel_type!r} is missing credentials: {', '.join(missing)}",
                error_type="MissingChannelCredentials",
                channel=channel_type,
                missing_env_vars=missing,
            )
            return DeliveryResult(channel_type, "failure", f"missing env vars: {', '.join(missing)}")

        # Requirement 7.4: retry transient failures exactly once, after 2s.
        for attempt in (1, 2):
            try:
                action(adapter)
                return DeliveryResult(channel_type, "success", None)
            except TransientDeliveryError as exc:
                if attempt == 1:
                    self._logger.warning(
                        COMPONENT,
                        f"Transient failure on {channel_type}; retrying once in "
                        f"{RETRY_BACKOFF_SECONDS}s: {exc}",
                        channel=channel_type,
                        error_type="TransientDeliveryError",
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                self._logger.error(
                    COMPONENT,
                    f"Delivery to {channel_type} failed after one retry: {exc}",
                    error_type="TransientDeliveryError",
                    channel=channel_type,
                )
                return DeliveryResult(channel_type, "failure", str(exc))
            except PermanentDeliveryError as exc:
                self._logger.error(
                    COMPONENT,
                    f"Delivery to {channel_type} failed permanently: {exc}",
                    error_type="PermanentDeliveryError",
                    channel=channel_type,
                )
                return DeliveryResult(channel_type, "failure", str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the rest
                self._logger.error(
                    COMPONENT,
                    f"Delivery to {channel_type} raised {type(exc).__name__}: {exc}",
                    error_type=type(exc).__name__,
                    channel=channel_type,
                )
                return DeliveryResult(channel_type, "failure", str(exc))

        return DeliveryResult(channel_type, "failure", "unreachable")
