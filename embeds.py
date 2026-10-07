"""Turn Rayward lookup results into Discord embeds, within Discord's embed limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Tuple

import discord
from discord.utils import escape_markdown

from rayward import PROVIDERS, Kind, LookupResult

# Discord limits
MAX_EMBEDS = 10
MAX_DESCRIPTION = 4096  # each source is its own embed, so its body gets the full description limit
MAX_EMBED_TOTAL = 6000
EMBED_BUDGET = 5600  # headroom for the footer

FLAG_NAMES = {
    0: "Unflagged",
    1: "Flagged",
    2: "Confirmed",
    3: "Queued",
    4: "Provisional Flag",
    5: "Mixed",
    6: "Past Offender",
    8: "Redacted",
    10: "Awaiting Human Review",
}
ACTIONABLE = {1, 2}  # per Rayward docs, only these mean the account did something

FOOTER = "Via Rayward. Green means no record, not safe."

# Generous caps; _fit_lines still trims to Discord's limit with a "+N more lines" note.
MAX_REASONS = 15
MAX_EVIDENCE_PER_REASON = 40
MAX_LINKED = 15
MAX_SERVER_NAMES = 25


def _esc(value: Any) -> str:
    return escape_markdown(str(value))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit_lines(lines: list[str], limit: int = MAX_DESCRIPTION) -> str:
    """Join lines, dropping whole lines at the end (with a note) if over the limit."""
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        remaining = len(lines) - i
        note = f"… (+{remaining} more lines)"
        cost = len(line) + (1 if out else 0)
        if used + cost > limit - len(note) - 1 and remaining > 1:
            out.append(note)
            break
        if used + cost > limit:
            out.append(_clip(line, max(limit - used - 1, 1)))
            break
        out.append(line)
        used += cost
    return "\n".join(out) or "-"


def _ts(value: Any, style: str = "R") -> str | None:
    try:
        return f"<t:{int(value)}:{style}>" if value else None
    except (TypeError, ValueError):
        return None


def flag_name(flag: Any) -> str:
    try:
        return FLAG_NAMES.get(int(flag), f"Unknown ({flag})")
    except (TypeError, ValueError):
        return f"Unknown ({flag})"


def _evidence_line(item: dict[str, Any]) -> str | None:
    kind = item.get("kind")
    if kind == "text":
        return "> " + _clip(_esc(item.get("text", "")).replace("\n", " "), 180)
    if kind == "outfit":
        return f"Outfit: {_esc(item.get('name', '?'))} [{_esc(item.get('category', '?'))}]"
    if kind == "discordUser":
        return f"Linked Discord: `{item.get('discordId', '?')}`"
    if kind == "discordGuild":
        # Always display safeName; `name` may contain unsafe content per the docs.
        line = f"Server: {_esc(item.get('safeName', '?'))}"
        types = item.get("types") or []
        if types:
            line += f" ({_esc(', '.join(map(str, types)))})"
        extras = []
        if _ts(item.get("joinedAt"), "d"):
            extras.append(f"joined {_ts(item.get('joinedAt'), 'd')}")
        elif _ts(item.get("firstSeen"), "d"):
            extras.append(f"first seen {_ts(item.get('firstSeen'), 'd')}")
        if item.get("staff"):
            extras.append("staff")
        if item.get("booster"):
            extras.append("booster")
        if item.get("verifiedLeft"):
            extras.append("left")
        if isinstance(item.get("messages"), int):
            extras.append(f"{item['messages']} msgs")
        if extras:
            line += " - " + ", ".join(extras)
        return line
    return None  # unknown kinds are skipped, as the docs require


def format_result(result: LookupResult) -> str:
    if not result.ok:
        return f"**Error** - {_esc(result.error)}"

    d = result.data or {}
    flag = d.get("flagType", 0)
    lines: list[str] = []

    if flag == 0:
        lines.append("**Unflagged** - no record in this database (not a safety verdict)")
    else:
        note = "actionable finding" if flag in ACTIONABLE else "process state, not a finding"
        lines.append(f"**{flag_name(flag)}** - {note}")

    if d.get("categoryLabel"):
        lines.append(f"Category: {_esc(d['categoryLabel'])}")

    reasons = d.get("reasons") or []
    for reason in reasons[:MAX_REASONS]:
        detectors = ", ".join(_esc(s.get("label", s.get("id", "?"))) for s in reason.get("sources") or [])
        head = f"- **{_esc(reason.get('title') or reason.get('type', '?'))}**"
        if detectors:
            head += f" ({detectors})"
        lines.append(head)
        evidence = [e for e in (_evidence_line(x) for x in reason.get("evidence") or []) if e]
        for ev in evidence[:MAX_EVIDENCE_PER_REASON]:
            lines.append(f"  {ev}")
        if len(evidence) > MAX_EVIDENCE_PER_REASON:
            lines.append(f"  … +{len(evidence) - MAX_EVIDENCE_PER_REASON} more evidence items")
    if len(reasons) > MAX_REASONS:
        lines.append(f"… +{len(reasons) - MAX_REASONS} more reasons")

    provisional = d.get("provisionalReasons") or []
    if provisional:
        titles = ", ".join(_esc(r.get("title") or r.get("type", "?")) for r in provisional)
        lines.append(f"Pending review: {titles}")

    linked = d.get("linkedRobloxAccounts") or []
    if linked:
        lines.append("Linked flagged Roblox accounts:")
        for acc in linked[:MAX_LINKED]:
            lines.append(
                f"  [{_esc(acc.get('robloxUsername', '?'))}](https://www.roblox.com/users/{acc.get('robloxUserId')}/profile)"
                f" `{acc.get('robloxUserId')}` - {flag_name(acc.get('flagType'))}"
            )
        if len(linked) > MAX_LINKED:
            lines.append(f"  … +{len(linked) - MAX_LINKED} more")

    reviewer = d.get("reviewer")
    if isinstance(reviewer, dict):
        lines.append(f"Reviewed by {_esc(reviewer.get('displayName', '?'))} (@{_esc(reviewer.get('username', '?'))})")
    if _ts(d.get("lastUpdated")):
        lines.append(f"Updated {_ts(d.get('lastUpdated'))}")

    return _fit_lines(lines)


def format_rotector_links(result: LookupResult) -> str:
    if not result.ok:
        return f"**Error** - {_esc(result.error)}"
    d = result.data or {}
    accounts = d.get("discordAccounts") or []
    alts = d.get("altAccounts") or []
    if not accounts and not alts:
        return "No linked Discord accounts on record."

    lines: list[str] = []
    for acc in accounts[:MAX_LINKED]:
        servers = acc.get("servers") or []
        line = f"Discord `{acc.get('id', '?')}`"
        if _ts(acc.get("detectedAt"), "d"):
            line += f" (seen {_ts(acc.get('detectedAt'), 'd')})"
        lines.append(line)
        if servers:
            names = ", ".join(_esc(s.get("safeName", "?")) for s in servers[:MAX_SERVER_NAMES])
            more = f" +{len(servers) - MAX_SERVER_NAMES}" if len(servers) > MAX_SERVER_NAMES else ""
            lines.append(f"  {len(servers)} tracked server(s): {names}{more}")
    if len(accounts) > MAX_LINKED:
        lines.append(f"… +{len(accounts) - MAX_LINKED} more Discord accounts")
    if alts:
        lines.append("Alt Roblox accounts:")
        for alt in alts[:MAX_LINKED]:
            lines.append(
                f"  [{_esc(alt.get('robloxUsername', '?'))}](https://www.roblox.com/users/{alt.get('robloxUserId')}/profile)"
                f" `{alt.get('robloxUserId')}`"
            )
        if len(alts) > MAX_LINKED:
            lines.append(f"  … +{len(alts) - MAX_LINKED} more")
    return _fit_lines(lines)


def format_rcr(result: LookupResult) -> str:
    """Roblox Criminal Records. Same response shape as the others, but its terms say a record is
    an accusation filed by RCR staff rather than a finding, so it is worded that way, and the
    full filings live on RCR's own site (recordUrls)."""
    if not result.ok:
        return format_result(result)
    d = result.data or {}
    flag = d.get("flagType", 0)
    lines: list[str] = []

    if not flag:
        lines.append("**Not on file** - RCR holds no record (not a clearance)")
    else:
        standing = _esc(d.get("statusLabel") or flag_name(flag))
        lines.append(f"**{standing}** - an accusation filed by RCR staff, not a finding of fact")
        if d.get("headerMessage"):
            lines.append(f"*{_esc(d['headerMessage'])}*")

    reasons = d.get("reasons") or []
    if reasons:
        lines.append("Charges:")
    for reason in reasons[:MAX_REASONS]:
        lines.append(f"- **{_esc(reason.get('title') or reason.get('type', '?'))}**")
        evidence = [e for e in (_evidence_line(x) for x in reason.get("evidence") or []) if e]
        for ev in evidence[:MAX_EVIDENCE_PER_REASON]:
            lines.append(f"  {ev}")
        if len(evidence) > MAX_EVIDENCE_PER_REASON:
            lines.append(f"  … +{len(evidence) - MAX_EVIDENCE_PER_REASON} more notes")
    if len(reasons) > MAX_REASONS:
        lines.append(f"… +{len(reasons) - MAX_REASONS} more charges")

    urls = [u for u in d.get("recordUrls") or [] if isinstance(u, str) and u.startswith("https://")]
    if urls:
        links = " ".join(f"[record {i}]({u})" for i, u in enumerate(urls[:5], 1))
        lines.append(f"Full records on RCR: {links}")
    if _ts(d.get("lastUpdated")):
        lines.append(f"Record updated {_ts(d.get('lastUpdated'))}")
    return _fit_lines(lines)


@dataclass(frozen=True)
class Status:
    label: str
    color: discord.Color


HIT = Status("Flagged", discord.Color.red())
PROCESS = Status("Under review", discord.Color.orange())
CLEAR = Status("No record", discord.Color.green())
ERROR = Status("Error", discord.Color.purple())
INFO = Status("Links found", discord.Color.blue())
NONE_LINKED = Status("None linked", discord.Color.light_grey())

LEGEND = "Card colours: red flagged, orange under review, green no record, purple error"


@dataclass
class Section:
    name: str
    value: str
    status: Status
    label: str | None = None  # defaults to the status label
    short: str | None = None  # name in the header summary line; defaults to name minus " database"


def result_status(result: LookupResult) -> Status:
    if not result.ok:
        return ERROR
    flag = (result.data or {}).get("flagType", 0)
    if flag in ACTIONABLE:
        return HIT
    return PROCESS if flag else CLEAR


def links_status(result: LookupResult) -> Status:
    if not result.ok:
        return ERROR
    d = result.data or {}
    return INFO if d.get("discordAccounts") or d.get("altAccounts") else NONE_LINKED


def overall_color(results: Iterable[LookupResult]) -> discord.Color:
    statuses = {result_status(r) for r in results}
    for status in (HIT, PROCESS, ERROR):
        if status in statuses:
            return status.color
    return CLEAR.color


def _card_label(r: LookupResult) -> str | None:
    """Word shown after the card title. None falls back to the status label."""
    d = r.data or {}
    if not r.ok or not d.get("flagType"):
        return None
    if r.provider.id == "rcr" and d.get("statusLabel"):
        return str(d["statusLabel"])  # RCR's own standing: Ban, Flag or Watch
    return flag_name(d.get("flagType"))


def provider_sections(results: list[LookupResult]) -> list[Section]:
    """One section per lookup that ran. Sources that can't do this lookup type get no card."""
    sections = []
    for r in results:
        body = format_rcr(r) if r.provider.id == "rcr" and r.ok else format_result(r)
        sections.append(Section(r.provider.card_title, body, result_status(r), _card_label(r), short=r.provider.name))
    return sections


def not_checked_line(kinds: Iterable[Kind]) -> str | None:
    """e.g. "Not checked: RAB, RCR (Roblox only)" for sources none of the given inputs suit."""
    kinds = set(kinds)
    skipped: dict[str, list[str]] = {}
    for p in PROVIDERS:
        if not any(p.supports(k) for k in kinds):
            skipped.setdefault(p.only_type or "other lookups", []).append(p.name)
    if not skipped:
        return None
    parts = [f"{', '.join(names)} ({only} only)" for only, names in skipped.items()]
    return "Not checked: " + "; ".join(parts)


def summary_line(sections: list[Section]) -> str:
    """Plain-text status per source, e.g. "Rotector: Confirmed | TASE: No record"."""
    return " | ".join(
        f"{s.short or s.name.removesuffix(' database')}: {s.label or s.status.label}" for s in sections
    )


Block = Tuple[discord.Embed, List[Section]]


def build_messages(*blocks: Block) -> list[list[discord.Embed]]:
    """One colour-coded embed per section, grouped into messages within Discord's limits.

    Each block is (header, sections). Every block starts a new message, so with both a
    Roblox and a Discord check the Discord results never share a message with the Roblox
    ones. Each inner list is one message: at most 10 embeds and ~6000 characters combined.
    """
    now = discord.utils.utcnow()
    messages: list[list[discord.Embed]] = []
    for header, sections in blocks:
        cards = [header]
        for s in sections:
            title = _clip(f"{s.name} - {s.label or s.status.label}", 256)
            cards.append(discord.Embed(title=title, description=_clip(s.value or "-", MAX_DESCRIPTION), color=s.status.color))

        messages.append([])
        used = 0
        for embed in cards:
            size = len(embed)
            if messages[-1] and (len(messages[-1]) >= MAX_EMBEDS or used + size > EMBED_BUDGET):
                messages.append([])
                used = 0
            messages[-1].append(embed)
            used += size

    total = len(messages)
    for i, group in enumerate(messages, 1):
        suffix = f" | Page {i}/{total}" if total > 1 else ""
        group[-1].set_footer(text=FOOTER + suffix)
        group[-1].timestamp = now
    return messages


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Roblox sends e.g. "2006-02-27T21:06:40.3Z"; drop fractional seconds for older Pythons.
    cleaned = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None
