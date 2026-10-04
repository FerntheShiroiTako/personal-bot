"""Minimal public Roblox web API helpers: username -> ID, profile info, avatar headshot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp

USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
USER_URL = "https://users.roblox.com/v1/users/{id}"
HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"

TIMEOUT = aiohttp.ClientTimeout(total=8)


class RobloxError(Exception):
    """Roblox API could not be reached or returned an unexpected response."""


@dataclass
class RobloxUser:
    id: int
    name: str | None = None
    display_name: str | None = None
    created: str | None = None
    is_banned: bool | None = None
    avatar_url: str | None = None

    @property
    def profile_url(self) -> str:
        return f"https://www.roblox.com/users/{self.id}/profile"


class RobloxClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def resolve_username(self, username: str) -> RobloxUser | None:
        payload = {"usernames": [username], "excludeBannedUsers": False}
        try:
            async with self._session.post(USERNAMES_URL, json=payload, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise RobloxError(f"username lookup returned HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise RobloxError(f"username lookup failed: {type(exc).__name__}") from exc

        matches = body.get("data") or []
        if not matches:
            return None
        m = matches[0]
        return RobloxUser(id=int(m["id"]), name=m.get("name"), display_name=m.get("displayName"))

    async def get_user(self, user_id: int) -> RobloxUser | None:
        """Returns None if the user does not exist."""
        try:
            async with self._session.get(USER_URL.format(id=user_id), timeout=TIMEOUT) as resp:
                if resp.status in (400, 404):
                    return None
                if resp.status != 200:
                    raise RobloxError(f"user lookup returned HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise RobloxError(f"user lookup failed: {type(exc).__name__}") from exc

        return RobloxUser(
            id=int(body["id"]),
            name=body.get("name"),
            display_name=body.get("displayName"),
            created=body.get("created"),
            is_banned=body.get("isBanned"),
        )

    async def get_headshot(self, user_id: int) -> str | None:
        """Avatar headshot URL, or None on any failure (purely cosmetic)."""
        params = {"userIds": str(user_id), "size": "420x420", "format": "Png", "isCircular": "false"}
        try:
            async with self._session.get(HEADSHOT_URL, params=params, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json(content_type=None)
            item = (body.get("data") or [{}])[0]
            return item.get("imageUrl") if item.get("state") == "Completed" else None
        except Exception:
            return None
