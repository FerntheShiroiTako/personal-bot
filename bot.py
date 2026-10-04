"""Personal, owner-only Discord bot for Roblox and Discord lookups via Rayward.

Run: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

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
REFUSAL = "This is a private bot. Only its owner can use it."


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
        super().__init__(intents=discord.Intents.default())  # no privileged intents needed
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

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = LookupBot()


async def send_pages(interaction: discord.Interaction, pages: list[discord.Embed], ephemeral: bool) -> None:
    for page in pages:
        await interaction.followup.send(embed=page, ephemeral=ephemeral)


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


@bot.tree.command(name="roblox", description="Look up a Roblox user across all Rayward sources")
@app_commands.describe(user="Roblox username or user ID", public="Show the result to everyone (default: only you)")
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roblox_cmd(
    interaction: discord.Interaction,
    user: app_commands.Range[str, 1, 32],
    public: bool = False,
) -> None:
    ephemeral = not public
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)

    target, error = await resolve_roblox(user)
    if error or target is None:
        await interaction.followup.send(error or "Lookup failed.", ephemeral=True)
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
    header.description = "\n".join(desc)
    if target.avatar_url:
        header.set_thumbnail(url=target.avatar_url)

    fields = embeds.provider_fields(results, "roblox")
    fields.insert(1, ("Rotector database - linked Discord", embeds.format_rotector_links(links)))
    await send_pages(interaction, embeds.paginate(header, fields), ephemeral)


@bot.tree.command(name="discord", description="Look up a Discord user ID across all Rayward sources")
@app_commands.describe(user_id="Discord user ID (snowflake)", public="Show the result to everyone (default: only you)")
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def discord_cmd(
    interaction: discord.Interaction,
    user_id: app_commands.Range[str, 1, 25],
    public: bool = False,
) -> None:
    ephemeral = not public
    raw = user_id.strip().strip("<@!>")
    if not SNOWFLAKE_RE.match(raw):
        await interaction.response.send_message("That is not a valid Discord user ID.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)

    async def fetch_discord_user() -> discord.User | None:
        try:
            return await bot.fetch_user(int(raw))
        except (discord.NotFound, discord.HTTPException):
            return None

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
    header.description = "\n".join(desc)
    if user:
        header.set_thumbnail(url=user.display_avatar.url)

    await send_pages(interaction, embeds.paginate(header, embeds.provider_fields(results, "discord")), ephemeral)


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
