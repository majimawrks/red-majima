import base64
import io
import time
from typing import Optional

import aiohttp
import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

# ── Craft constants ────────────────────────────────────────────────────────────
# Scraps and steel required per grade. Scraps x3 each tier, steel x2 each tier.
# Choose always requires 2x the steel of Random (same scraps for both).
GRADES = ("Grey", "Green", "Blue", "Purple", "Gold", "Red")

SCRAPS: dict[str, int] = {
    "Grey": 6, "Green": 18, "Blue": 54,
    "Purple": 162, "Gold": 486, "Red": 1458,
}
STEEL_RANDOM: dict[str, int] = {
    "Grey": 1, "Green": 2, "Blue": 4,
    "Purple": 8, "Gold": 16, "Red": 32,
}
STEEL_CHOOSE: dict[str, int] = {g: STEEL_RANDOM[g] * 2 for g in GRADES}

# ── Coin icon (pre-rendered 64x64 PNG) ────────────────────────────────────────
_COIN_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAACaElEQVR4nO2aP07DMBTG"
    "7ShDxh4AmUrhFGywsqNucAImjtGJE5QNsbOGjVO0Uql6gI7ZioxqcIPtPP9NTN9vgTbY"
    "/r4vTuw8QgiCIAiCIAiCIMgJQlMMsnggS5d290/kgkSmTGH07nZau/W4XsYOhoY07W7U"
    "jufX9SpUGDQX07HCoC7GtaanszdQR+uXGxIxDJsgqI35P8ahhhMHwoOAhkCdznoo4xGD"
    "gM4GOthZH8lsKKx6i20+1RiQABbds59SWMCxuAfTRqwAT/2MMYVQkBOnAP9lpLV76LGK"
    "0QlLGTRxuQRiCkxs3rgPAN0IR7oTtNkLUFND69Vg4GcBl41QqfqymVd7/nOzaX+etMY6"
    "hSEIP9ePLbUqiDBW1WJPndu+QOjmHrxugoxVtRxEDnCtQnewkhjLYDZAz7pzTZAdOpZn"
    "w9BhyFpsjHsVRZk00BBh+JoOWhVmmjBi4mvaO4D6rDr6/P6xUxq/upzUpuN96NqL5Vkc"
    "F6y2bZwA6h7DXSFd+o73oWvfpwMSSAk1Lg/maygUXR1djUK/KYhSd0A0Fp2OxbQJWaOs"
    "m3vRhUBVX36+TPaikxyMm5A9nM92sK3watt+32hyN8/hHngIupWjUH3JzYdcaoaGe9E9"
    "2JWmhuK66a4CueC9Cug6GmsgwfYB7DBldJdB30CxAnIxyPn10sIrQs282v+He4HwoCqG"
    "9JbERAj899yCkHXrzIP+O6wqj401DJVGk3mr9wNEEJzukjJUICYdfca9XpFppDBUQlSC"
    "fID2DzUd/DW5phOIwLqqrEEXpIvhpO8JNppgbAlhFEEQBEEQBEHIEV8etV24xe4lzwAA"
    "AABJRU5ErkJggg=="
)
COIN_PNG: bytes = base64.b64decode(_COIN_B64)

# ── API ────────────────────────────────────────────────────────────────────────
_API_PRICES = "https://api2.warera.io/trpc/itemTrading.getPrices"
_CACHE_TTL  = 60  # seconds


class EqCalc(commands.Cog):
    """Warera equipment sell/craft calculator using live market prices."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=847261935, force_registration=True)
        self.config.register_global(api_key=None)
        self.config.register_guild(cooldown_seconds=0)

        self._price_cache: Optional[dict] = None
        self._cache_time: float = 0.0
        # Maps (guild_id, user_id, command_name) -> last-used monotonic timestamp
        self._cooldowns: dict[tuple[int, int, str], float] = {}

    async def cog_unload(self):
        pass

    async def red_delete_data_for_user(self, *, requester, user_id):
        pass

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _check_cooldown(self, ctx: commands.Context, cmd: str) -> Optional[float]:
        """Return remaining cooldown seconds, or None if the channel is clear."""
        if ctx.guild is None:
            return None
        cd = await self.config.guild(ctx.guild).cooldown_seconds()
        if not cd:
            return None
        key = (ctx.channel.id, cmd)
        last = self._cooldowns.get(key, 0.0)
        remaining = cd - (time.monotonic() - last)
        if remaining > 0:
            return remaining
        return None

    def _mark_used(self, ctx: commands.Context, cmd: str) -> None:
        if ctx.guild is not None:
            self._cooldowns[(ctx.channel.id, cmd)] = time.monotonic()

    async def _fetch_prices(self) -> dict:
        """Return cached or freshly fetched prices from itemTrading.getPrices."""
        now = time.monotonic()
        if self._price_cache and (now - self._cache_time) < _CACHE_TTL:
            return self._price_cache

        api_key = await self.config.api_key()
        headers = {
            "Origin":  "https://app.warera.io",
            "Referer": "https://app.warera.io/",
        }
        if api_key:
            headers["X-API-Key"] = api_key

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                _API_PRICES, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                envelope = await resp.json()
                data = envelope["result"]["data"]

        self._price_cache = data
        self._cache_time  = now
        return data

    def _cache_age_str(self) -> str:
        age = int(time.monotonic() - self._cache_time)
        if age < 60:
            return f"{age}s lalu"
        return f"{age // 60}m lalu"

    @staticmethod
    def _fmt_g(value: float) -> str:
        return f"{value:,.2f} g"

    def _sell_table(self, scrap_price: float) -> str:
        rows = [(g, SCRAPS[g], SCRAPS[g] * scrap_price) for g in GRADES]

        cg = max(len(g) for g in GRADES)
        cs = max(len(str(r[1])) for r in rows)
        cd = max(len(self._fmt_g(r[2])) for r in rows)

        header = f" {'Grade':<{cg}}  {'Scraps':>{cs}}  {'Nilai Dismantle':>{cd}}"
        sep    = "─" * len(header)
        lines  = [header, sep]
        for grade, scraps, dismantle in rows:
            lines.append(
                f" {grade:<{cg}}  {scraps:>{cs}}  {self._fmt_g(dismantle):>{cd}}"
            )
        return "\n".join(lines)

    def _craft_table(self, scrap_price: float, steel_price: float) -> str:
        rows = []
        for g in GRADES:
            r = (SCRAPS[g] * scrap_price) + (STEEL_RANDOM[g] * steel_price)
            c = (SCRAPS[g] * scrap_price) + (STEEL_CHOOSE[g] * steel_price)
            rows.append((g, r, c))

        cg = max(len(g) for g in GRADES)
        cr = max(len(self._fmt_g(r[1])) for r in rows)
        cc = max(len(self._fmt_g(r[2])) for r in rows)

        header = f" {'Grade':<{cg}}  {'Random':>{cr}}  {'Choose':>{cc}}"
        sep    = "─" * len(header)
        lines  = [header, sep]
        for grade, rand_cost, choose_cost in rows:
            lines.append(
                f" {grade:<{cg}}  {self._fmt_g(rand_cost):>{cr}}  "
                f"{self._fmt_g(choose_cost):>{cc}}"
            )
        return "\n".join(lines)

    @staticmethod
    def _coin_file() -> discord.File:
        return discord.File(io.BytesIO(COIN_PNG), filename="coin.png")

    # ── Commands ───────────────────────────────────────────────────────────────

    @commands.group(invoke_without_command=True)
    async def eqcalc(self, ctx: commands.Context):
        """Warera equipment calculator. Use `sell` or `craft` subcommands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @eqcalc.command(name="sell")
    async def eqcalc_sell(self, ctx: commands.Context):
        """Show whether to sell equipment whole or dismantle it first."""
        remaining = await self._check_cooldown(ctx, "sell")
        if remaining:
            await ctx.send(
                f"Tunggu **{remaining:.0f}s** lagi sebelum menggunakan perintah ini.",
                delete_after=remaining,
            )
            return

        async with ctx.typing():
            try:
                prices = await self._fetch_prices()
            except Exception as e:
                await ctx.send(f"Gagal mengambil harga pasar: `{e}`")
                return

        self._mark_used(ctx, "sell")

        scrap_price = prices["scraps"]
        embed = discord.Embed(
            title="⚔️  Equipment — Jual vs Dismantle",
            description=f"```\n{self._sell_table(scrap_price)}\n```",
            colour=await ctx.embed_colour(),
        )
        embed.add_field(
            name="Cara baca",
            value=(
                "**Nilai Dismantle** = hasil kalau equipment di-dismantle lalu jual scrapnya\n"
                "Harga jual di market **> nilai** → jual utuh\n"
                "Harga jual di market **≤ nilai** → dismantle dulu"
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"scraps {self._fmt_g(scrap_price)}  ·  data {self._cache_age_str()}",
            icon_url="attachment://coin.png",
        )
        await ctx.send(embed=embed, file=self._coin_file())

    @eqcalc.command(name="craft")
    async def eqcalc_craft(self, ctx: commands.Context):
        """Show whether to craft equipment or buy it from the market."""
        remaining = await self._check_cooldown(ctx, "craft")
        if remaining:
            await ctx.send(
                f"Tunggu **{remaining:.0f}s** lagi sebelum menggunakan perintah ini.",
                delete_after=remaining,
            )
            return

        async with ctx.typing():
            try:
                prices = await self._fetch_prices()
            except Exception as e:
                await ctx.send(f"Gagal mengambil harga pasar: `{e}`")
                return

        self._mark_used(ctx, "craft")

        scrap_price = prices["scraps"]
        steel_price = prices["steel"]
        embed = discord.Embed(
            title="\U0001f528  Equipment — Craft vs Beli",
            description=f"```\n{self._craft_table(scrap_price, steel_price)}\n```",
            colour=await ctx.embed_colour(),
        )
        embed.add_field(
            name="Cara baca",
            value=(
                "Nilai = biaya kalau kalian beli scraps & steel dari pasar lalu craft sendiri\n"
                "**Random** = roll 6 jenis equipment  ·  "
                "**Choose** = pilih jenis, steel 2× lipat\n\n"
                "Ketemu di market **lebih mahal** dari nilai → craft sendiri lebih hemat\n"
                "Ketemu di market **lebih murah** dari nilai → beli langsung di market"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"scraps {self._fmt_g(scrap_price)}  ·  "
                f"steel {self._fmt_g(steel_price)}  ·  data {self._cache_age_str()}"
            ),
            icon_url="attachment://coin.png",
        )
        await ctx.send(embed=embed, file=self._coin_file())

    @eqcalc.command(name="api")
    @commands.is_owner()
    async def eqcalc_api(self, ctx: commands.Context, api_key: Optional[str] = None):
        """Set or clear the Warera API key (bot owner only).

        Run without an argument to check whether a key is currently set.
        """
        if api_key is None:
            current = await self.config.api_key()
            status = "sudah di-set" if current else "belum di-set"
            await ctx.send(f"API key {status}.")
            return

        await self.config.api_key.set(api_key)
        self._price_cache = None
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("✅ API key tersimpan.", delete_after=5)

    # ── eqcalc set subgroup ────────────────────────────────────────────────────

    @eqcalc.group(name="set")
    @commands.admin_or_permissions(manage_guild=True)
    async def eqcalc_set(self, ctx: commands.Context):
        """Configure eqcalc settings for this server."""
        await ctx.send_help(ctx.command)

    @eqcalc_set.command(name="cd")
    async def eqcalc_set_cd(self, ctx: commands.Context, seconds: int = 0):
        """Set the per-user cooldown for sell/craft commands (in seconds, 0 = off)."""
        if seconds < 0:
            await ctx.send("Cooldown tidak boleh negatif.")
            return

        await self.config.guild(ctx.guild).cooldown_seconds.set(seconds)
        if seconds == 0:
            await ctx.send("✅ Cooldown dimatikan.")
        else:
            await ctx.send(f"✅ Cooldown di-set ke **{seconds}s** per pengguna.")
