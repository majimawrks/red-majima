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

    # ------------------------------------------------------------------
    # Permission gate
    # ------------------------------------------------------------------

    async def cog_check(self, ctx: commands.Context) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True
        if ctx.guild is None:
            return False
        allowed = await self.config.guild(ctx.guild).allowed_users()
        return ctx.author.id in allowed

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    @commands.group(name="wask")
    async def wask(self, ctx: commands.Context):
        """Warera natural language query interface."""

    # ------------------------------------------------------------------
    # Owner-only key management
    # ------------------------------------------------------------------

    @wask.command(name="setgemini")
    @commands.is_owner()
    async def wask_setgemini(self, ctx: commands.Context, api_key: str):
        """Set the Gemini API key (bot owner only)."""
        await self.config.gemini_api_key.set(api_key)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("✅ Gemini API key tersimpan.", delete_after=5)

    @wask.command(name="setapi")
    @commands.is_owner()
    async def wask_setapi(self, ctx: commands.Context, api_key: Optional[str] = None):
        """Set or clear the Warera API key (bot owner only).

        Run without arguments to check status.
        Use `[p]wask setapi copy` to copy the key from warera_eqcalc if loaded.
        """
        if api_key is None:
            current = await self.config.warera_api_key()
            status = "sudah di-set" if current else "belum di-set"
            eqcalc_hint = ""
            if not current:
                eqcalc_key = await self._get_eqcalc_api_key()
                if eqcalc_key:
                    eqcalc_hint = (
                        f"\n💡 API key ditemukan di **warera_eqcalc**. "
                        f"Gunakan `{ctx.clean_prefix}wask setapi copy` untuk menyalin."
                    )
            await ctx.send(f"API key {status}.{eqcalc_hint}")
            return

        if api_key.lower() == "copy":
            eqcalc_key = await self._get_eqcalc_api_key()
            if not eqcalc_key:
                await ctx.send(
                    "❌ Tidak bisa menyalin: **warera_eqcalc** tidak loaded atau API key-nya belum di-set."
                )
                return
            await self.config.warera_api_key.set(eqcalc_key)
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await ctx.send("✅ API key disalin dari **warera_eqcalc**.", delete_after=5)
            return

        await self.config.warera_api_key.set(api_key)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("✅ API key tersimpan.", delete_after=5)

    async def _get_eqcalc_api_key(self) -> Optional[str]:
        eqcalc = self.bot.get_cog("EqCalc")
        if eqcalc is None:
            return None
        try:
            return await eqcalc.config.api_key()
        except AttributeError:
            return None

    # ------------------------------------------------------------------
    # Allowlist management (owner only, guild only)
    # ------------------------------------------------------------------

    @wask.command(name="allow")
    @commands.is_owner()
    @commands.guild_only()
    async def wask_allow(self, ctx: commands.Context, member: discord.Member):
        """Grant a member access to wask commands in this server."""
        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            if member.id not in allowed:
                allowed.append(member.id)
        await ctx.send(f"✅ {member.mention} can now use `wask` commands.", delete_after=10)

    @wask.command(name="deny")
    @commands.is_owner()
    @commands.guild_only()
    async def wask_deny(self, ctx: commands.Context, member: discord.Member):
        """Revoke a member's access to wask commands in this server."""
        async with self.config.guild(ctx.guild).allowed_users() as allowed:
            try:
                allowed.remove(member.id)
            except ValueError:
                pass
        await ctx.send(f"✅ {member.mention} access revoked.", delete_after=10)

    @wask.command(name="allowed")
    @commands.is_owner()
    @commands.guild_only()
    async def wask_allowed(self, ctx: commands.Context):
        """Show members allowed to use wask in this server."""
        allowed = await self.config.guild(ctx.guild).allowed_users()
        if not allowed:
            return await ctx.send("No members in allowlist. Only bot owner can use wask.")
        mentions = [f"<@{uid}>" for uid in allowed]
        await ctx.send(f"Allowed members: {', '.join(mentions)}")
