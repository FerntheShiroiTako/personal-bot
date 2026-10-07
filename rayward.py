"""Async client for the Rayward lookup API (Rotector, TASE, RAB, Okappiki, ServerSweep, RCR).

Reference: https://rayward.app/docs (OpenAPI specs at https://rayward.app/docs/<source>.json).
All six sources share one host, one API key and one endpoint layout:

    GET {BASE_URL}/v2/lookup/{source}/roblox/user/{robloxId}
    GET {BASE_URL}/v2/lookup/{source}/discord/user/{discordId}   (not RAB or RCR)
    GET {BASE_URL}/v2/lookup/rotector/roblox/user/{robloxId}/discord   (Rotector only)

Success: {"success": true, "data": {...}}
Failure: {"success": false, "error": "...", "requestId": "...", "type": "...", "code": "..."}
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

BASE_URL = "https://roscoe.rayward.app"
DEFAULT_TIMEOUT = 10.0

Kind = Literal["roblox", "discord"]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    roblox: bool
    discord: bool
    # Card title. Each source's terms require its data to be labelled as coming from it.
    title: str = ""

    @property
    def card_title(self) -> str:
        return self.title or f"{self.name} database"

    @property
    def only_type(self) -> str | None:
        """'Roblox' or 'Discord' for single-type sources, else None."""
        if self.roblox and not self.discord:
            return "Roblox"
        if self.discord and not self.roblox:
            return "Discord"
        return None

    def supports(self, kind: Kind) -> bool:
        return self.roblox if kind == "roblox" else self.discord


PROVIDERS: tuple[Provider, ...] = (
    Provider("rotector", "Rotector", roblox=True, discord=True),
    Provider("tase", "TASE", roblox=True, discord=True),
    Provider("rab", "RAB", roblox=True, discord=False),
    Provider("okappiki", "Okappiki", roblox=True, discord=True),
    Provider("serversweep", "ServerSweep", roblox=True, discord=True),
    # Roblox Criminal Records: staff-filed records on Roblox users. Roblox only.
    Provider("rcr", "RCR", roblox=True, discord=False, title="Roblox Criminal Records (RCR)"),
)
ROTECTOR = PROVIDERS[0]


@dataclass
class LookupResult:
    provider: Provider
    data: dict[str, Any] | None = None
    error: str | None = None
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None


def _describe_error(status: int, body: Any, headers: Any) -> str:
    body = body if isinstance(body, dict) else {}
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    code = body.get("code") or details.get("code") or body.get("type") or "Error"
    message = str(body.get("error") or "").strip()

    if status == 503:
        message = "Source did not answer. This is NOT a clean result."
    elif status == 429:
        retry = headers.get("Retry-After") or body.get("retryAfter")
        message = (message + f" Retry after: {retry}." if retry else message).strip()
    elif status == 403 and details.get("status"):
        message = f"{message} (status: {details['status']})".strip()
    elif status == 401:
        message = message or "Invalid or missing RAYWARD_API_KEY."

    text = f"{code} (HTTP {status})"
    if message:
        text += f": {message}"
    if body.get("requestId"):
        text += f" [request {body['requestId']}]"
    return text


class RaywardClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _get(self, provider: Provider, path: str) -> LookupResult:
        try:
            async with self._session.get(
                BASE_URL + path, headers=self._headers, timeout=self._timeout
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                status, headers = resp.status, resp.headers
        except asyncio.TimeoutError:
            return LookupResult(provider, error="Timed out")
        except aiohttp.ClientError as exc:
            return LookupResult(provider, error=f"Network error: {type(exc).__name__}")

        if (
            status == 200
            and isinstance(body, dict)
            and body.get("success")
            and isinstance(body.get("data"), dict)
        ):
            return LookupResult(provider, data=body["data"], status=status)
        return LookupResult(provider, error=_describe_error(status, body, headers), status=status)

    async def lookup(self, provider: Provider, kind: Kind, subject_id: int | str) -> LookupResult:
        return await self._get(provider, f"/v2/lookup/{provider.id}/{kind}/user/{subject_id}")

    async def lookup_all(self, kind: Kind, subject_id: int | str) -> list[LookupResult]:
        """Query every source that supports `kind`, concurrently. Never raises."""
        providers = [p for p in PROVIDERS if p.supports(kind)]
        results = await asyncio.gather(
            *(self.lookup(p, kind, subject_id) for p in providers),
            return_exceptions=True,
        )
        out: list[LookupResult] = []
        for provider, result in zip(providers, results):
            if isinstance(result, BaseException):
                out.append(LookupResult(provider, error=f"Unexpected error: {type(result).__name__}"))
            else:
                out.append(result)
        return out

    async def rotector_discord_links(self, roblox_id: int) -> LookupResult:
        """Rotector only: Discord accounts linked to a Roblox user, plus alt Roblox accounts."""
        return await self._get(ROTECTOR, f"/v2/lookup/rotector/roblox/user/{roblox_id}/discord")
