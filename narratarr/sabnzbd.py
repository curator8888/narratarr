"""Minimal async client for the SABnzbd API.

Docs: https://sabnzbd.org/wiki/configuration/4.3/api
"""

from __future__ import annotations

import httpx


class SABnzbdError(Exception):
    pass


class SABnzbd:
    def __init__(self, url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not (url and api_key):
            raise SABnzbdError("Add your SABnzbd address and API key in Settings first.")
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30, transport=transport)

    async def __aenter__(self) -> SABnzbd:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, mode: str, files: dict | None = None, **params) -> dict:
        params = {"mode": mode, "apikey": self._key, "output": "json", **params}
        try:
            if files:
                resp = await self._client.post("/api", params=params, files=files)
            else:
                resp = await self._client.get("/api", params=params)
        except httpx.HTTPError:
            raise SABnzbdError("Couldn't reach SABnzbd. Check the address in Settings.")
        if resp.status_code in (401, 403):
            raise SABnzbdError("SABnzbd rejected the API key. Check it in Settings.")
        if resp.status_code >= 400:
            raise SABnzbdError(f"SABnzbd returned an error ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            data = {"status": False, "error": resp.text.strip()[:200]}  # e.g. a plain "API Key Incorrect"
        if not isinstance(data, dict):
            raise SABnzbdError("That address doesn't look like SABnzbd. Check it in Settings.")
        if data.get("status") is False:
            error = data.get("error") or "request failed"
            if "api key" in error.lower():
                raise SABnzbdError("SABnzbd rejected the API key. Check it in Settings.")
            raise SABnzbdError(f"SABnzbd: {error}")
        return data

    async def version(self) -> str:
        # The queue needs a valid key, unlike mode=version, so this also checks the key.
        await self._call("queue", limit=0)
        return (await self._call("version")).get("version", "")

    async def add_nzb(self, nzb: bytes, filename: str, name: str, category: str = "") -> str:
        params = {"nzbname": name}
        if category:
            params["cat"] = category
        data = await self._call("addfile", files={"name": (filename, nzb, "application/x-nzb")}, **params)
        ids = data.get("nzo_ids") or []
        if not ids:
            raise SABnzbdError("SABnzbd didn't accept the NZB.")
        return ids[0]

    async def job(self, nzo_id: str) -> dict | None:
        """Where a job is: {"stage": "queue"|"history", ...the SABnzbd slot}, or None if SABnzbd forgot it."""
        queue = (await self._call("queue", nzo_ids=nzo_id)).get("queue", {})
        for slot in queue.get("slots", []):
            if slot.get("nzo_id") == nzo_id:
                return {**slot, "stage": "queue", "kbpersec": queue.get("kbpersec", "0"), "paused": queue.get("paused")}
        history = (await self._call("history", nzo_ids=nzo_id)).get("history", {})
        for slot in history.get("slots", []):
            if slot.get("nzo_id") == nzo_id:
                return {**slot, "stage": "history"}
        return None

    async def forget(self, nzo_id: str) -> None:
        """Clear a finished job from history once its files are in the library."""
        await self._call("history", name="delete", value=nzo_id)

    async def delete(self, nzo_id: str) -> None:
        """Remove a job from the queue (deleting its partial files) and from history."""
        await self._call("queue", name="delete", value=nzo_id, del_files=1)
        await self._call("history", name="delete", value=nzo_id)
