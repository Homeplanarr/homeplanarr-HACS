"""Small async client for the Homeplanarr API."""

from __future__ import annotations

from typing import Any

import aiohttp

API_PREFIX = "/api/v1"
HA_PREFIX = f"{API_PREFIX}/integrations/home-assistant"


class HomeplanarrError(Exception):
    """Any failure talking to Homeplanarr."""

    # Worth sending the same data again later: the network, a server error, or
    # a key/subscription problem the owner can fix. A 4xx about the data is not.
    retryable = True


class HomeplanarrRequestError(HomeplanarrError):
    """Homeplanarr refused the request itself (4xx)."""

    retryable = False

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class HomeplanarrAuthError(HomeplanarrError):
    """The API key was refused (revoked, expired or not a full-access key)."""


class HomeplanarrBillingError(HomeplanarrError):
    """The household has no active subscription."""


class HomeplanarrApi:
    """Thin wrapper over the endpoints the integration uses."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._key = api_key

    @property
    def app_url(self) -> str:
        return f"{self._url}/app"

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                json=json,
                headers={"X-API-Key": self._key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (401, 403):
                    raise HomeplanarrAuthError(await _detail(resp))
                if resp.status == 402:
                    raise HomeplanarrBillingError(await _detail(resp))
                if resp.status == 429 or resp.status >= 500:
                    raise HomeplanarrError(f"HTTP {resp.status}: {await _detail(resp)}")
                if resp.status >= 400:
                    raise HomeplanarrRequestError(resp.status, await _detail(resp))
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeplanarrError(str(err) or type(err).__name__) from err

    async def sensors(self) -> dict[str, Any]:
        return await self._request("GET", f"{HA_PREFIX}/sensors")

    async def energy(self, reading: dict[str, Any]) -> Any:
        return await self._request("POST", f"{HA_PREFIX}/energy", reading)

    async def energy_bulk(self, readings: list[dict[str, Any]]) -> Any:
        return await self._request("POST", f"{API_PREFIX}/energy/bulk", readings)

    async def metrics(self, readings: list[dict[str, Any]]) -> Any:
        return await self._request("POST", f"{HA_PREFIX}/metric", readings)

    async def device_status(self, devices: list[dict[str, Any]]) -> Any:
        return await self._request("POST", f"{HA_PREFIX}/device-status", devices)

    async def chore_event(self, chore: str, member: str, comment: str | None) -> Any:
        body: dict[str, Any] = {"chore": chore, "member": member}
        if comment:
            body["comment"] = comment
        return await self._request("POST", f"{HA_PREFIX}/chore-event", body)

    async def nfc_scan(self, tag_id: str, member: str | None) -> Any:
        body: dict[str, Any] = {"tag_id": tag_id}
        if member:
            body["member"] = member
        return await self._request("POST", f"{API_PREFIX}/nfc/scan", body)

    # Shopping list. origin "ha" tells the server not to echo the change to the
    # older Home Assistant shopping-list mirror (household ha_url/ha_token).

    async def shopping_list(self) -> list[dict[str, Any]]:
        return await self._request("GET", f"{API_PREFIX}/shopping")

    async def shopping_add(self, name: str, quantity: str | None) -> Any:
        return await self._request(
            "POST",
            f"{API_PREFIX}/shopping",
            {"name": name, "quantity": quantity or None, "origin": "ha"},
        )

    async def shopping_update(self, item_id: int, changes: dict[str, Any]) -> Any:
        return await self._request(
            "PATCH", f"{API_PREFIX}/shopping/{item_id}", {**changes, "origin": "ha"}
        )

    async def shopping_delete(self, item_id: int) -> Any:
        return await self._request("DELETE", f"{API_PREFIX}/shopping/{item_id}")


async def _detail(resp: aiohttp.ClientResponse) -> str:
    try:
        data = await resp.json(content_type=None)
        if isinstance(data, dict):
            return str(data.get("detail") or data.get("error") or data)
    except (ValueError, aiohttp.ContentTypeError):
        pass
    return resp.reason or str(resp.status)
