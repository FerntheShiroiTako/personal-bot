"""Turn Rayward lookup results into Discord embeds, within Discord's embed limits."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

import discord
from discord.utils import escape_markdown

from rayward import PROVIDERS, Kind, LookupResult

# Discord limits
MAX_FIELDS = 25
MAX_FIELD_VALUE = 1024
MAX_FIELD_NAME = 256
MAX_EMBED_TOTAL = 6000
EMBED_BUDGET = 5600  # headroom for title/footer on continuation pages

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

COLOR_HIT = discord.Color.red()
COLOR_PROCESS = discord.Color.orange()
COLOR_NONE = discord.Color.light_grey()

FOOTER = "Data labelled per source via Rayward. Unflagged means no record, not safe."

MAX_REASONS = 5
MAX_EVIDENCE_PER_REASON = 4
MAX_LINKED = 5


def _esc(value: Any) -> str:
    return escape_markdown(str(value))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit_lines(lines: list[str], limit: int = MAX_FIELD_VALUE) -> str:
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
            names = ", ".join(_esc(s.get("safeName", "?")) for s in servers[:6])
            more = f" +{len(servers) - 6}" if len(servers) > 6 else ""
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


def overall_color(results: Iterable[LookupResult]) -> discord.Color:
    flags = [r.data.get("flagType", 0) for r in results if r.ok and r.data]
    if any(f in ACTIONABLE for f in flags):
        return COLOR_HIT
    if any(f for f in flags):
        return COLOR_PROCESS
    return COLOR_NONE


def provider_fields(results: list[LookupResult], kind: Kind) -> list[tuple[str, str]]:
    fields = [(f"{r.provider.name} database", format_result(r)) for r in results]
    for p in PROVIDERS:
        if not p.supports(kind):
            fields.append((f"{p.name} database", f"Does not support {kind.capitalize()} lookups."))
    return fields


def paginate(header: discord.Embed, fields: list[tuple[str, str]]) -> list[discord.Embed]:
    """Split fields across embeds so each stays under 25 fields and ~6000 chars.

    Each returned embed should be sent as its own message, since Discord's
    6000-character limit applies to all embeds in one message combined.
    """
    now = discord.utils.utcnow()
    pages: list[discord.Embed] = []
    current = header
    for name, value in fields:
        name = _clip(name, MAX_FIELD_NAME)
        value = _clip(value or "-", MAX_FIELD_VALUE)
        if len(current.fields) >= MAX_FIELDS or len(current) + len(name) + len(value) > EMBED_BUDGET:
            pages.append(current)
            current = discord.Embed(title=_clip(f"{header.title or 'Lookup'} (continued)", 256), color=header.color)
        current.add_field(name=name, value=value, inline=False)
    pages.append(current)

    total = len(pages)
    for i, page in enumerate(pages, 1):
        suffix = f" | Page {i}/{total}" if total > 1 else ""
        page.set_footer(text=FOOTER + suffix)
        page.timestamp = now
    return pages


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Roblox sends e.g. "2006-02-27T21:06:40.3Z"; drop fractional seconds for older Pythons.
    cleaned = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None
