"""Personal, owner-only Discord bot for Roblox and Discord lookups via Rayward.

Run: python bot.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine, Union

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import embeds
from rayward import ROTECTOR, Kind, LookupResult, RaywardClient
from roblox import RobloxClient, RobloxError, RobloxUser

# Exit code for configuration errors (sysexits EX_CONFIG). The systemd unit lists it in
# RestartPreventExitStatus, so a missing or bad token doesn't cause a restart loop.
EXIT_CONFIG = 78

load_dotenv()


def _setup_logging() -> None:
    # journald adds its own timestamps, so leave them out when running under systemd.
    if os.getenv("JOURNAL_STREAM"):
        fmt = "%(levelname)s %(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=fmt)


_setup_logging()
log = logging.getLogger("lookup-bot")


def _config_error(message: str) -> None:
    log.critical(message)
    sys.exit(EXIT_CONFIG)


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        _config_error(f"Missing {name} in .env (see .env.example)")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


DISCORD_TOKEN = _require_env("DISCORD_TOKEN")
RAYWARD_API_KEY = _require_env("RAYWARD_API_KEY")
try:
    OWNER_ID = int(_require_env("OWNER_ID"))
except ValueError:
    _config_error("OWNER_ID must be a numeric Discord user ID")

# Logs raw interaction channel payloads (includes user data). Off unless explicitly enabled.
DEBUG_INTERACTIONS = _env_flag("DEBUG_INTERACTIONS")
# auto: sync only when the command definitions changed since the last sync.
# always / never: force it either way.
SYNC_COMMANDS = (os.getenv("SYNC_COMMANDS") or "auto").strip().lower()
# How many linked accounts a one-sided check follows automatically. 0 turns it off.
# Every followed account costs Rayward daily quota.
try:
    AUTO_LINK_MAX = max(0, int((os.getenv("AUTO_LINK_MAX") or "3").strip()))
except ValueError:
    _config_error("AUTO_LINK_MAX must be a whole number (0 turns auto-follow off)")
# systemd sets STATE_DIRECTORY from StateDirectory=; local runs keep state next to the code.
STATE_DIR = Path(os.getenv("STATE_DIRECTORY") or Path(__file__).resolve().parent / ".state")

ROBLOX_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")
MENTION_RE = re.compile(r"^<@!?(\d{15,20})>$")
REFUSAL = "You arent fern, if you got this code off github then change the .env."


class OwnerOnlyTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Runs before every application command.
        if interaction.user.id == OWNER_ID:
            return True
        await interaction.response.send_message(REFUSAL, ephemeral=True)
        log.warning("Refused %s (%s) for /%s", interaction.user, interaction.user.id,
                    interaction.command.name if interaction.command else "?")
        return False


class LookupBot(discord.Client):
    def __init__(self) -> None:
        # No privileged intents needed. Debug events are only for DEBUG_INTERACTIONS.
        super().__init__(intents=discord.Intents.default(), enable_debug_events=DEBUG_INTERACTIONS)
        self.tree = OwnerOnlyTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.rayward: RaywardClient
        self.roblox: RobloxClient

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "personal-lookup-bot/1.0"})
        self.rayward = RaywardClient(self.http_session, RAYWARD_API_KEY)
        self.roblox = RobloxClient(self.http_session)
        await self.sync_commands_if_needed()

    def _commands_fingerprint(self) -> str:
        payload = {
            "application_id": self.application_id,
            "commands": sorted(
                (cmd.to_dict(self.tree) for cmd in self.tree.get_commands()),
                key=lambda c: (c.get("type", 1), c["name"]),
            ),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def sync_commands_if_needed(self) -> None:
        """Sync global commands, skipping it when nothing changed so restarts don't hit rate limits."""
        if SYNC_COMMANDS == "never":
            log.info("Command sync disabled (SYNC_COMMANDS=never)")
            return
        fingerprint = self._commands_fingerprint()
        stamp = STATE_DIR / "commands.sha256"
        if SYNC_COMMANDS != "always":
            try:
                if stamp.read_text(encoding="utf-8").strip() == fingerprint:
                    log.info("Commands unchanged since last sync, skipping (SYNC_COMMANDS=always to force)")
                    return
            except OSError:
                pass  # no stamp yet: sync

        synced = await self.tree.sync()
        log.info("Synced %d global command(s)", len(synced))
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            stamp.write_text(fingerprint, encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save command sync state to %s: %s", stamp, exc)

    async def on_socket_raw_receive(self, msg: str | bytes) -> None:
        # Only dispatched when enable_debug_events is on, i.e. DEBUG_INTERACTIONS=1.
        if b"INTERACTION_CREATE" not in (msg if isinstance(msg, bytes) else msg.encode()):
            return
        try:
            channel = json.loads(msg)["d"].get("channel") or {}
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        recipients = [u.get("id") for u in channel.get("recipients") or []]
        log.info("Interaction channel: type=%s id=%s recipients=%s",
                 channel.get("type"), channel.get("id"), recipients if "recipients" in channel else "not sent")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = LookupBot()


async def send_messages(interaction: discord.Interaction, messages: list[list[discord.Embed]], ephemeral: bool) -> None:
    for group in messages:
        await interaction.followup.send(embeds=group, ephemeral=ephemeral)


async def send_error(interaction: discord.Interaction, message: str) -> None:
    """Errors are always private, even if the deferred reply was public."""
    if interaction.response.is_done():
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def dm_partner(interaction: discord.Interaction) -> discord.User | None:
    """The other person in a 1-on-1 DM, if the command was run in one."""
    channel = interaction.channel
    if not isinstance(channel, discord.DMChannel):
        return None
    me = {interaction.user.id, bot.user.id if bot.user else 0}
    others = [u for u in channel.recipients if u.id not in me]
    return others[0] if len(others) == 1 else None


def normalize_roblox(value: str) -> str | None:
    """Format-check a Roblox username or ID without any network calls. None if invalid."""
    q = value.strip().lstrip("@")
    if (q.isdigit() and len(q) <= 20) or ROBLOX_USERNAME_RE.match(q):
        return q
    return None


def normalize_discord(value: str) -> str | None:
    """A Discord user ID from an ID or <@mention>. None if invalid."""
    q = value.strip()
    mention = MENTION_RE.match(q)
    if mention:
        return mention.group(1)
    return q if SNOWFLAKE_RE.match(q) else None


async def resolve_roblox(query: str) -> tuple[RobloxUser | None, str | None]:
    """Returns (user, error). The user may have only an id if Roblox was unreachable."""
    q = query.strip().lstrip("@")

    if q.isdigit():
        try:
            user = await bot.roblox.get_user(int(q))
        except RobloxError as exc:
            log.warning("Roblox user lookup failed: %s", exc)
            return RobloxUser(id=int(q)), None  # still query Rayward by id
        if user:
            return user, None
        if not ROBLOX_USERNAME_RE.match(q):
            return None, f"No Roblox user with ID `{q}`."
        # numeric usernames exist; fall through and try it as a username

    if not ROBLOX_USERNAME_RE.match(q):
        return None, "That is not a valid Roblox username or user ID."
    try:
        user = await bot.roblox.resolve_username(q)
    except RobloxError as exc:
        log.warning("Roblox username resolve failed: %s", exc)
        return None, "Could not reach the Roblox API to resolve that username. Try the user ID instead."
    if not user:
        return None, f"No Roblox user found with ID or username `{discord.utils.escape_markdown(q)}`."
    return user, None


def _lookup_list(value: Any) -> list[LookupResult]:
    return value if isinstance(value, list) else []  # lookup_all never raises, but be defensive


async def fetch_roblox(target: RobloxUser) -> tuple[list[LookupResult], LookupResult]:
    """All Roblox lookups for one account, concurrently. Fills in target's profile and avatar.
    Never raises."""
    results, links, avatar, details = await asyncio.gather(
        bot.rayward.lookup_all("roblox", target.id),
        bot.rayward.rotector_discord_links(target.id),
        bot.roblox.get_headshot(target.id),
        bot.roblox.get_user(target.id) if target.created is None else asyncio.sleep(0, result=None),
        return_exceptions=True,
    )
    if isinstance(links, BaseException):
        links = LookupResult(ROTECTOR, error=f"Unexpected error: {type(links).__name__}")
    if isinstance(details, RobloxUser):
        target.created, target.is_banned = details.created, details.is_banned
        target.name = target.name or details.name
        target.display_name = target.display_name or details.display_name
    target.avatar_url = avatar if isinstance(avatar, str) else None
    return _lookup_list(results), links


async def fetch_discord(raw: str, known: discord.User | None) -> tuple[list[LookupResult], discord.User | None]:
    """All Discord lookups for one account plus its profile, concurrently. Never raises."""
    async def fetch_discord_user() -> discord.User | None:
        try:
            return await bot.fetch_user(int(raw))
        except discord.HTTPException:
            return known

    results, user = await asyncio.gather(
        bot.rayward.lookup_all("discord", raw), fetch_discord_user(), return_exceptions=True,
    )
    return _lookup_list(results), user if isinstance(user, discord.User) else known


@dataclass
class LinkedAccount:
    id: str
    sources: list[str] = field(default_factory=list)
    name: str | None = None


def linked_discord_accounts(links: LookupResult) -> list[LinkedAccount]:
    """Discord accounts Rotector links to a Roblox user. Masked IDs can't be looked up, so
    only full snowflakes count."""
    found: dict[str, LinkedAccount] = {}
    if links.ok:
        for acc in (links.data or {}).get("discordAccounts") or []:
            raw = str(acc.get("id") or "")
            if SNOWFLAKE_RE.match(raw) and raw not in found:
                found[raw] = LinkedAccount(raw, [links.provider.name])
    return list(found.values())


def linked_roblox_accounts(discord_results: list[LookupResult]) -> list[LinkedAccount]:
    """Roblox accounts any source links to a Discord user, deduped across sources."""
    found: dict[str, LinkedAccount] = {}
    for r in discord_results:
        if not r.ok:
            continue
        for acc in (r.data or {}).get("linkedRobloxAccounts") or []:
            raw = str(acc.get("robloxUserId") or "")
            if not (raw.isdigit() and len(raw) <= 20 and int(raw) > 0):
                continue
            entry = found.setdefault(raw, LinkedAccount(raw, name=acc.get("robloxUsername") or None))
            if r.provider.name not in entry.sources:
                entry.sources.append(r.provider.name)
    return list(found.values())


def _id_list(accounts: list[LinkedAccount], limit: int = 10) -> str:
    shown = ", ".join(f"`{a.id}`" for a in accounts[:limit])
    return shown + (f" and {len(accounts) - limit} more" if len(accounts) > limit else "")


def accounts_linked(
    roblox_id: int, discord_id: str, links: LookupResult, discord_results: list[LookupResult],
) -> list[str]:
    """Names of the sources that say this Roblox and Discord account are linked."""
    sources: list[str] = []
    if links.ok:
        ids = {str(acc.get("id")) for acc in (links.data or {}).get("discordAccounts") or []}
        if discord_id in ids:
            sources.append(f"{links.provider.name} linked-Discord data")
    for r in discord_results:
        if not r.ok:
            continue
        linked = (r.data or {}).get("linkedRobloxAccounts") or []
        if any(str(acc.get("robloxUserId")) == str(roblox_id) for acc in linked):
            sources.append(r.provider.name)
    return sources


def build_roblox_block(
    target: RobloxUser, results: list[LookupResult], links: LookupResult, notes: list[str],
) -> tuple[discord.Embed, list[embeds.Section]]:
    if target.name:
        title = f"{target.display_name or target.name} (@{target.name})"
    else:
        title = f"Roblox user {target.id}"
    sections = embeds.provider_sections(results)
    sections.insert(1, embeds.Section(
        "Rotector - linked Discord", embeds.format_rotector_links(links), embeds.links_status(links),
    ))

    header = discord.Embed(
        title=discord.utils.escape_markdown(title),
        url=target.profile_url,
        color=embeds.overall_color(results),
    )
    desc = [f"**Roblox ID:** `{target.id}`"]
    created = embeds.parse_iso(target.created)
    if created:
        desc.append(f"**Created:** {discord.utils.format_dt(created, 'D')} ({discord.utils.format_dt(created, 'R')})")
    if target.is_banned:
        desc.append("**Banned on Roblox:** yes")
    if not target.name:
        desc.append("*Roblox profile unavailable; showing Rayward data by ID only.*")
    desc += notes
    desc += ["", embeds.summary_line(sections), f"-# {embeds.LEGEND}"]
    header.description = "\n".join(desc)
    if target.avatar_url:
        header.set_thumbnail(url=target.avatar_url)
    return header, sections


def build_discord_block(
    raw: str, results: list[LookupResult], user: discord.User | None, notes: list[str],
) -> tuple[discord.Embed, list[embeds.Section]]:
    created = discord.utils.snowflake_time(int(raw))
    if user:
        title = f"{user.global_name or user.name} (@{user.name})"
    else:
        title = f"Discord user {raw}"
    sections = embeds.provider_sections(results)

    header = discord.Embed(title=discord.utils.escape_markdown(title), color=embeds.overall_color(results))
    desc = [
        f"**Discord ID:** `{raw}`",
        f"**Created:** {discord.utils.format_dt(created, 'D')} ({discord.utils.format_dt(created, 'R')})",
        f"**Mention:** <@{raw}>",
    ]
    if user and user.bot:
        desc.append("**Bot account:** yes")
    if not user:
        desc.append("*Discord profile could not be fetched.*")
    desc += notes
    desc += ["", embeds.summary_line(sections), f"-# {embeds.LEGEND}"]
    header.description = "\n".join(desc)
    if user:
        header.set_thumbnail(url=user.display_avatar.url)
    return header, sections


async def run_checks(
    interaction: discord.Interaction,
    ephemeral: bool,
    roblox_query: str | None = None,
    discord_id: str | None = None,
    known: discord.User | None = None,
) -> None:
    """Run a Roblox check, a Discord check, or both concurrently, and send the results.

    When only one side is given, accounts that Rayward links to it on the other side are
    checked too (up to AUTO_LINK_MAX), so the user gets both sides in one go.

    Expects the interaction to be deferred already. Inputs must be format-checked already.
    """
    target: RobloxUser | None = None
    if roblox_query is not None:
        target, error = await resolve_roblox(roblox_query)
        if error or target is None:
            await send_error(interaction, error or "Lookup failed.")
            return

    jobs: list[Coroutine[Any, Any, Any]] = []
    if target is not None:
        jobs.append(fetch_roblox(target))
    if discord_id is not None:
        jobs.append(fetch_discord(discord_id, known))
    out = await asyncio.gather(*jobs)

    r_results: list[LookupResult] = []
    links = LookupResult(ROTECTOR, error="Not requested")
    if target is not None:
        r_results, links = out[0]
    d_results: list[LookupResult] = []
    d_user: discord.User | None = None
    if discord_id is not None:
        d_results, d_user = out[-1]

    notes: list[str] = []
    if target is not None and discord_id is not None:
        sources = accounts_linked(target.id, discord_id, links, d_results)
        if sources:
            notes = [f"**Linked:** Rayward links this Roblox and Discord account ({', '.join(sources)})."]

    # One side given: follow what Rayward links to it on the other side. Never chained.
    follow_kind: Kind | None = None
    candidates: list[LinkedAccount] = []
    origin = ""
    if AUTO_LINK_MAX > 0 and target is not None and discord_id is None:
        follow_kind, candidates = "discord", linked_discord_accounts(links)
        origin = f"Roblox user {target.name or target.id}"
    elif AUTO_LINK_MAX > 0 and discord_id is not None and target is None:
        follow_kind, candidates = "roblox", linked_roblox_accounts(d_results)
        origin = f"Discord user {d_user.name if d_user else discord_id}"
    followed, extra = candidates[:AUTO_LINK_MAX], candidates[AUTO_LINK_MAX:]

    async def follow(acc: LinkedAccount) -> embeds.Block:
        note = f"**Auto-checked:** linked to {discord.utils.escape_markdown(origin)} via {', '.join(acc.sources)}."
        if follow_kind == "roblox":
            user = RobloxUser(id=int(acc.id), name=acc.name)
            res, lk = await fetch_roblox(user)
            return build_roblox_block(user, res, lk, [note])
        res, u = await fetch_discord(acc.id, None)
        return build_discord_block(acc.id, res, u, [note])

    follow_out = await asyncio.gather(*(follow(a) for a in followed), return_exceptions=True)
    follow_blocks: list[embeds.Block] = []
    failed: list[LinkedAccount] = []
    for acc, result in zip(followed, follow_out):
        if isinstance(result, BaseException):
            log.warning("Auto-check of linked account %s failed", acc.id, exc_info=result)
            failed.append(acc)
        else:
            follow_blocks.append(result)

    # Sources that suit none of the checked account types (e.g. Roblox-only ones on a
    # Discord-only check) get no card, just one line in the first header.
    kinds: set[Kind] = set()
    if target is not None or (follow_kind == "roblox" and follow_blocks):
        kinds.add("roblox")
    if discord_id is not None or (follow_kind == "discord" and follow_blocks):
        kinds.add("discord")
    skipped = embeds.not_checked_line(kinds)

    first_notes = list(notes)
    if extra:
        first_notes.append(f"Also linked, not checked (limit {AUTO_LINK_MAX}): {_id_list(extra)}")
    if failed:
        first_notes.append(f"Could not check linked: {_id_list(failed)}")
    if skipped:
        first_notes.append(f"-# {skipped}")

    blocks: list[embeds.Block] = []
    if target is not None:
        blocks.append(build_roblox_block(target, r_results, links, first_notes))
    if discord_id is not None:
        blocks.append(build_discord_block(discord_id, d_results, d_user, notes if blocks else first_notes))
    blocks += follow_blocks

    await send_messages(interaction, embeds.build_messages(*blocks), ephemeral)


async def check_discord(
    interaction: discord.Interaction, raw: str, ephemeral: bool, known: discord.User | None = None,
) -> None:
    await run_checks(interaction, ephemeral, discord_id=raw, known=known)


@bot.tree.command(name="check", description="Check a Roblox and/or Discord user across all Rayward sources")
@app_commands.describe(
    roblox="Roblox username or user ID",
    discord_="Discord user ID or @mention. Leave both empty in a DM to check who you're talking to",
    visibility="Who can see the result (default: only you)",
)
@app_commands.rename(discord_="discord")
@app_commands.choices(
    visibility=[
        app_commands.Choice(name="Only me", value="me"),
        app_commands.Choice(name="Everyone in the channel", value="everyone"),
    ],
)
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def check_cmd(
    interaction: discord.Interaction,
    roblox: app_commands.Range[str, 1, 40] | None = None,
    discord_: app_commands.Range[str, 1, 40] | None = None,  # renamed: don't shadow the module
    visibility: str = "me",
) -> None:
    ephemeral = visibility != "everyone"
    roblox_in = (roblox or "").strip()
    discord_in = (discord_ or "").strip()

    if not roblox_in and not discord_in:
        partner = dm_partner(interaction)
        if partner is None:
            await send_error(interaction, "Tell me who to check, or right-click them and use Apps > Check user.")
            return
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        await check_discord(interaction, str(partner.id), ephemeral, known=partner)
        return

    # Validate everything before deferring; if anything is invalid, run nothing.
    roblox_query = normalize_roblox(roblox_in) if roblox_in else None
    discord_id = normalize_discord(discord_in) if discord_in else None
    problems = []
    if roblox_in and roblox_query is None:
        problems.append("`roblox` is not a valid Roblox username or user ID.")
    if discord_in and discord_id is None:
        problems.append("`discord` is not a valid Discord user ID or mention.")
    if problems:
        await send_error(interaction, "\n".join(problems))
        return

    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    await run_checks(interaction, ephemeral, roblox_query=roblox_query, discord_id=discord_id)


@bot.tree.context_menu(name="Check user")
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def check_user_menu(interaction: discord.Interaction, user: Union[discord.Member, discord.User]) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await check_discord(interaction, str(user.id), True, known=user)


@bot.tree.context_menu(name="Check author")
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def check_author_menu(interaction: discord.Interaction, message: discord.Message) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await check_discord(interaction, str(message.author.id), True, known=message.author)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Command error", exc_info=error)
    message = "Something went wrong while running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def main() -> None:
    async with bot:  # closes the bot (and our aiohttp session) on the way out
        if sys.platform != "win32":
            # systemctl stop sends SIGTERM; shut down cleanly instead of being killed mid-request.
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda s=sig: _request_shutdown(s))
        await bot.start(DISCORD_TOKEN)


def _request_shutdown(sig: signal.Signals) -> None:
    log.info("Received %s, shutting down", sig.name)
    asyncio.get_running_loop().create_task(bot.close())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C on Windows, where asyncio has no signal handlers
        log.info("Interrupted, shut down")
    except discord.LoginFailure:
        _config_error("Discord rejected DISCORD_TOKEN (login failed). Check the token in .env.")
    except discord.PrivilegedIntentsRequired:
        _config_error("Discord refused the requested intents.")
