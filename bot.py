"""Personal, owner-only Discord bot for Roblox and Discord lookups via Rayward.

Run: python bot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from typing import Union

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import embeds
from rayward import ROTECTOR, LookupResult, RaywardClient
from roblox import RobloxClient, RobloxError, RobloxUser

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lookup-bot")


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        sys.exit(f"Missing {name} in .env (see .env.example)")
    return value


DISCORD_TOKEN = _require_env("DISCORD_TOKEN")
RAYWARD_API_KEY = _require_env("RAYWARD_API_KEY")
try:
    OWNER_ID = int(_require_env("OWNER_ID"))
except ValueError:
    sys.exit("OWNER_ID must be a numeric Discord user ID")

ROBLOX_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")
MENTION_RE = re.compile(r"^<@!?(\d{15,20})>$")
MAX_EVERYONE = 10  # group DMs hold at most 10 people
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
        # no privileged intents needed; debug events let us log which members Discord sends
        super().__init__(intents=discord.Intents.default(), enable_debug_events=True)
        self.tree = OwnerOnlyTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.rayward: RaywardClient
        self.roblox: RobloxClient

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "personal-lookup-bot/1.0"})
        self.rayward = RaywardClient(self.http_session, RAYWARD_API_KEY)
        self.roblox = RobloxClient(self.http_session)
        synced = await self.tree.sync()
        log.info("Synced %d global command(s)", len(synced))

    async def on_socket_raw_receive(self, msg: str | bytes) -> None:
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


def conversation_members(interaction: discord.Interaction) -> list[discord.User]:
    """Everyone else in the DM or group DM the command was run in."""
    channel = interaction.channel
    if not isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
        return []
    me = {interaction.user.id, bot.user.id if bot.user else 0}
    return [u for u in channel.recipients if u.id not in me]


async def check_everyone(interaction: discord.Interaction, ephemeral: bool) -> None:
    members = conversation_members(interaction)[:MAX_EVERYONE]
    if not members:
        if isinstance(interaction.channel, (discord.DMChannel, discord.GroupChannel)):
            await send_error(interaction, "Discord didn't tell me who's in this conversation. "
                                          "Right-click someone and use Apps > Check user instead.")
        else:
            await send_error(interaction, "Checking everyone only works in DMs and group DMs. "
                                          "A user-installed app can't see a server's member list.")
        return
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)

    limit = asyncio.Semaphore(3)  # be gentle with Rayward's rate limits

    async def lookup(member: discord.User) -> tuple[discord.User, list[LookupResult]]:
        async with limit:
            return member, await bot.rayward.lookup_all("discord", member.id)

    rows = await asyncio.gather(*(lookup(m) for m in members))
    await interaction.followup.send(embed=embeds.everyone_embed(list(rows)), ephemeral=ephemeral)


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


async def check_roblox(interaction: discord.Interaction, query: str, ephemeral: bool) -> None:
    target, error = await resolve_roblox(query)
    if error or target is None:
        await send_error(interaction, error or "Lookup failed.")
        return

    needs_details = target.created is None
    results, links, avatar, details = await asyncio.gather(
        bot.rayward.lookup_all("roblox", target.id),
        bot.rayward.rotector_discord_links(target.id),
        bot.roblox.get_headshot(target.id),
        bot.roblox.get_user(target.id) if needs_details else asyncio.sleep(0, result=None),
        return_exceptions=True,
    )
    if isinstance(results, BaseException):  # lookup_all never raises, but be defensive
        results = []
    if isinstance(links, BaseException):
        links = LookupResult(ROTECTOR, error=f"Unexpected error: {type(links).__name__}")
    if isinstance(details, RobloxUser):
        target.created, target.is_banned = details.created, details.is_banned
        target.name = target.name or details.name
        target.display_name = target.display_name or details.display_name
    target.avatar_url = avatar if isinstance(avatar, str) else None

    if target.name:
        title = f"{target.display_name or target.name} (@{target.name})"
    else:
        title = f"Roblox user {target.id}"
    sections = embeds.provider_sections(results, "roblox")
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
    desc += ["", embeds.summary_line(sections), f"-# {embeds.LEGEND}"]
    header.description = "\n".join(desc)
    if target.avatar_url:
        header.set_thumbnail(url=target.avatar_url)

    await send_messages(interaction, embeds.build_messages(header, sections), ephemeral)


async def check_discord(
    interaction: discord.Interaction, raw: str, ephemeral: bool, known: discord.User | None = None,
) -> None:
    async def fetch_discord_user() -> discord.User | None:
        try:
            return await bot.fetch_user(int(raw))
        except (discord.NotFound, discord.HTTPException):
            return known

    results, user = await asyncio.gather(
        bot.rayward.lookup_all("discord", raw),
        fetch_discord_user(),
    )

    created = discord.utils.snowflake_time(int(raw))
    if user:
        name = user.global_name or user.name
        title = f"{name} (@{user.name})"
    else:
        title = f"Discord user {raw}"
    sections = embeds.provider_sections(results, "discord")

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
    desc += ["", embeds.summary_line(sections), f"-# {embeds.LEGEND}"]
    header.description = "\n".join(desc)
    if user:
        header.set_thumbnail(url=user.display_avatar.url)

    await send_messages(interaction, embeds.build_messages(header, sections), ephemeral)


@bot.tree.command(name="check", description="Check a Roblox or Discord user across all Rayward sources")
@app_commands.describe(
    user="Roblox username/ID, or Discord ID/@mention. Leave empty in a DM to check who you're talking to",
    platform="Which platform to check (default: auto-detect from what you typed)",
    visibility="Who can see the result (default: only you)",
    everyone="Check everyone in this DM or group DM instead of one person",
)
@app_commands.choices(
    platform=[
        app_commands.Choice(name="Auto-detect", value="auto"),
        app_commands.Choice(name="Roblox", value="roblox"),
        app_commands.Choice(name="Discord", value="discord"),
    ],
    visibility=[
        app_commands.Choice(name="Only me", value="me"),
        app_commands.Choice(name="Everyone in the channel", value="everyone"),
    ],
)
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def check_cmd(
    interaction: discord.Interaction,
    user: app_commands.Range[str, 1, 40] | None = None,
    platform: str = "auto",
    visibility: str = "me",
    everyone: bool = False,
) -> None:
    ephemeral = visibility != "everyone"
    if everyone:
        if platform == "roblox":
            await send_error(interaction, "Checking everyone only works for Discord accounts.")
            return
        await check_everyone(interaction, ephemeral)
        return

    query = (user or "").strip()
    partner: discord.User | None = None

    if not query:
        partner = dm_partner(interaction)
        if partner is None:
            await send_error(interaction, "Tell me who to check, or right-click them and use Apps > Check user.")
            return
        if platform == "roblox":
            await send_error(interaction, "I can only default to a DM partner's Discord account. Type a Roblox username for a Roblox check.")
            return
        query, platform = str(partner.id), "discord"

    mention = MENTION_RE.match(query)
    if platform == "auto":
        platform = "discord" if mention or SNOWFLAKE_RE.match(query) else "roblox"

    if platform == "discord":
        raw = mention.group(1) if mention else query
        if not SNOWFLAKE_RE.match(raw):
            await send_error(interaction, "That is not a valid Discord user ID or mention.")
            return
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        await check_discord(interaction, raw, ephemeral, known=partner)
    else:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        await check_roblox(interaction, query, ephemeral)


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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
