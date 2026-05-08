import asyncio
import json
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

BASE_URL = "https://api2.warera.io/trpc"
_SCHEMA_PATH = Path(__file__).parent / "schema.json"


class WareraAsk(commands.Cog):
    """Ask Warera game questions in natural language."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=715293840, force_registration=True)
        self.config.register_global(gemini_api_key=None, warera_api_key=None)
        self.config.register_guild(allowed_users=[])

        self._session: Optional[aiohttp.ClientSession] = None
        self._api_sem = asyncio.Semaphore(8)
        self._schema: list[dict] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester, user_id):
        pass

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def _api_call(self, endpoint: str, params: dict | None = None) -> Any:
        input_json = urllib.parse.quote(json.dumps(params or {}))
        url = f"{BASE_URL}/{endpoint}?input={input_json}"
        headers = {
            "Origin": "https://app.warera.io",
            "Referer": "https://app.warera.io/",
        }
        warera_api_key = await self.config.warera_api_key()
        if warera_api_key:
            headers["X-API-Key"] = warera_api_key
        session = await self._get_session()
        async with self._api_sem:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = (await resp.json())["result"]["data"]
        return data
