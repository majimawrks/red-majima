import asyncio
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import aiohttp
import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

BASE_URL = "https://api2.warera.io/trpc"
_COUNTRIES_TTL = 1800  # 30 min
_REGIONS_TTL = 1800

_ETHICS_THRESHOLDS = {0: 0, 1: 5, 2: 15}


def _ethics_bonus(value: int) -> int:
    return _ETHICS_THRESHOLDS.get(abs(value), 15)


def _ethics_label(value: int) -> str:
    if value == 0:
        return "Neutral"
    abs_v = abs(value)
    tier = "Fanatic" if abs_v >= 2 else "Normal"
    pct = _ethics_bonus(value)
    return f"{tier} ({pct}%)"


# ── Paginator view ────────────────────────────────────────────────────────────

class ReportPaginatorView(discord.ui.View):
    def __init__(
        self,
        pages: list[discord.Embed],
        author_id: int,
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Hanya yang menjalankan perintah yang bisa navigasi.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ):
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class AllianceMCU(commands.Cog):
    """Warera alliance cost/benefit analysis report."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=361829475, force_registration=True)
        self.config.register_global(api_key=None)
        self.config.register_guild(
            country_id=None,
            country_tags={},
            bonus_config={"alliance_bonus_pct": 10, "extra_bonus_pct": 0},
            cooldown_seconds=0,
        )

        self._countries_cache: Optional[list] = None
        self._countries_cache_time: float = 0.0
        self._regions_cache: Optional[dict] = None
        self._regions_cache_time: float = 0.0
        self._cooldowns: dict[tuple[int, str], float] = {}

    async def cog_unload(self):
        pass

    async def red_delete_data_for_user(self, *, requester, user_id):
        pass

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _api_call(self, endpoint: str, params: dict | None = None) -> Any:
        input_json = urllib.parse.quote(json.dumps(params or {}))
        url = f"{BASE_URL}/{endpoint}?input={input_json}"
        headers = {
            "Origin": "https://app.warera.io",
            "Referer": "https://app.warera.io/",
        }
        api_key = await self.config.api_key()
        if api_key:
            headers["X-API-Key"] = api_key
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = (await resp.json())["result"]["data"]
        return data

    async def _fetch_country(self, country_id: str) -> dict:
        return await self._api_call("country.getCountryById", {"countryId": country_id})

    async def _fetch_all_countries(self, force: bool = False) -> list:
        now = time.monotonic()
        if (
            not force
            and self._countries_cache
            and (now - self._countries_cache_time) < _COUNTRIES_TTL
        ):
            return self._countries_cache
        data = await self._api_call("country.getAllCountries")
        self._countries_cache = data
        self._countries_cache_time = now
        return data

    async def _fetch_all_regions(self, force: bool = False) -> dict:
        now = time.monotonic()
        if (
            not force
            and self._regions_cache
            and (now - self._regions_cache_time) < _REGIONS_TTL
        ):
            return self._regions_cache
        data = await self._api_call("region.getRegionsObject")
        self._regions_cache = data
        self._regions_cache_time = now
        return data

    async def _fetch_party(self, party_id: str) -> dict:
        return await self._api_call("party.getById", {"partyId": party_id})

    async def _fetch_battles(self, country_id: str, limit: int = 50) -> list:
        data = await self._api_call(
            "battle.getBattles", {"countryId": country_id, "limit": limit},
        )
        return data.get("items", []) if isinstance(data, dict) else data

    async def _fetch_battle_country_ranking(self, battle_id: str) -> list:
        data = await self._api_call(
            "battleRanking.getRanking",
            {"battleId": battle_id, "type": "country", "side": "merged", "dataType": "damage"},
        )
        return data.get("rankings", [])

    async def _compute_ally_battle_damage(
        self, country_id: str, ally_ids: set[str],
    ) -> dict[str, int]:
        """Sum per-ally damage from battles involving our country in the last 2 days.

        Fetches battles filtered by our country, then gets country-level
        damage rankings for each.  Any ally appearing in those rankings
        was fighting alongside us.
        """
        battles = await self._fetch_battles(country_id)

        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        recent_bids: list[str] = []
        for b in battles:
            created = b.get("createdAt", "")
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if dt >= cutoff:
                recent_bids.append(b["_id"])

        if not recent_bids:
            return {}

        # Fetch country rankings for all recent battles in parallel
        tasks = [self._fetch_battle_country_ranking(bid) for bid in recent_bids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ally_damage: dict[str, int] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            for entry in result:
                # type="country" rankings: try every plausible key for the country ID
                cid = (
                    entry.get("country")
                    or entry.get("user")
                    or entry.get("entityId")
                    or ""
                )
                if cid in ally_ids:
                    ally_damage[cid] = ally_damage.get(cid, 0) + int(
                        entry.get("value", 0)
                    )

        return ally_damage

    async def _resolve_country(self, text: str) -> tuple[str, str] | None:
        countries = await self._fetch_all_countries()
        text_lower = text.lower().strip()
        for c in countries:
            if c["_id"] == text:
                return c["_id"], c["name"]
        for c in countries:
            if c.get("code", "").lower() == text_lower:
                return c["_id"], c["name"]
        for c in countries:
            if text_lower in c["name"].lower():
                return c["_id"], c["name"]
        return None

    # ── Cooldown ──────────────────────────────────────────────────────────────

    async def _check_cooldown(self, ctx: commands.Context, cmd: str) -> Optional[float]:
        if ctx.guild is None:
            return None
        cd = await self.config.guild(ctx.guild).cooldown_seconds()
        if not cd:
            return None
        key = (ctx.channel.id, cmd)
        remaining = cd - (time.monotonic() - self._cooldowns.get(key, 0.0))
        return remaining if remaining > 0 else None

    def _mark_used(self, ctx: commands.Context, cmd: str) -> None:
        if ctx.guild is not None:
            self._cooldowns[(ctx.channel.id, cmd)] = time.monotonic()

    # ── Report helpers ────────────────────────────────────────────────────────

    def _get_country_regions(self, regions: dict, country_id: str) -> list[dict]:
        return [r for r in regions.values() if r.get("country") == country_id]

    def _max_bunker(self, country_regions: list[dict]) -> int:
        levels = [
            r.get("activeUpgradeLevels", {}).get("bunker", 0)
            for r in country_regions
        ]
        return max(levels) if levels else 0

    def _max_military_base(self, country_regions: list[dict]) -> int:
        levels = []
        for r in country_regions:
            base = (
                r.get("upgradesV2", {})
                .get("upgrades", {})
                .get("base", {})
            )
            if base.get("status") == "active":
                levels.append(base.get("level", 0))
        return max(levels) if levels else 0

    def _compute_alliance_cost(
        self,
        weekly_damage: float,
        alliance_bonus_pct: int,
        party_bonus_pct: int,
        extra_bonus_pct: int,
    ) -> tuple[float, float]:
        effective_rate = 0.3 * (1 + (alliance_bonus_pct + party_bonus_pct + extra_bonus_pct) / 100)
        weekly_cost = weekly_damage / 1000 * effective_rate
        return effective_rate, weekly_cost

    def _party_combat_bonuses(self, ethics: dict) -> dict:
        militarism = ethics.get("militarism", 0)
        isolationism = ethics.get("isolationism", 0)

        result = {
            "militarist_pct": 0,
            "militarist_label": "Neutral",
            "pacifist_pct": 0,
            "pacifist_label": "Neutral",
            "isolationist_pct": 0,
            "isolationist_label": "Neutral",
            "diplomatic_pct": 0,
            "diplomatic_label": "Neutral",
            "total_attack_pct": 0,
        }

        if militarism > 0:
            result["militarist_pct"] = _ethics_bonus(militarism)
            result["militarist_label"] = _ethics_label(militarism)
        elif militarism < 0:
            result["pacifist_pct"] = _ethics_bonus(militarism)
            result["pacifist_label"] = _ethics_label(militarism)

        if isolationism > 0:
            result["isolationist_pct"] = _ethics_bonus(isolationism)
            result["isolationist_label"] = _ethics_label(isolationism)
        elif isolationism < 0:
            result["diplomatic_pct"] = _ethics_bonus(isolationism)
            result["diplomatic_label"] = _ethics_label(isolationism)

        result["total_attack_pct"] = result["militarist_pct"] + result["diplomatic_pct"]
        return result

    def _build_ally_embed(
        self,
        ally: dict,
        party: dict | None,
        country_regions: list[dict],
        tags: dict,
        bonus_config: dict,
        countries_lookup: dict,
        colour: discord.Colour,
        page: int,
        total: int,
        cache_age: int,
        ally_dmg_for_us: int = 0,
        our_country_name: str = "",
    ) -> discord.Embed:
        ally_name = ally.get("name", "???")
        ally_code = ally.get("code", "??").upper()
        rankings = ally.get("rankings", {})

        weekly_dmg = rankings.get("weeklyCountryDamages", {}).get("value", 0)
        total_dmg = rankings.get("countryDamages", {}).get("value", 0)
        dmg_per_citizen = rankings.get("weeklyCountryDamagesPerCitizen", {}).get("value", 0)
        dmg_tier = rankings.get("weeklyCountryDamages", {}).get("tier", "?")
        active_pop = rankings.get("countryActivePopulation", {}).get("value", 0)
        wealth = rankings.get("countryWealth", {}).get("value", 0)

        embed = discord.Embed(
            title=f"\U0001f91d Alliance Report — {ally_name} ({ally_code})",
            colour=colour,
        )

        # Damage stats
        dmg_lines = [
            f"Dmg for **{our_country_name}** (2d): **{ally_dmg_for_us:,}**",
            f"Weekly (all battles): {weekly_dmg:,}",
            f"Total: {total_dmg:,}",
            f"Per Citizen (weekly): {dmg_per_citizen:,.0f}",
            f"Tier: {dmg_tier}",
        ]
        embed.add_field(
            name="\U0001f4ca Damage Stats (Weekly)",
            value="\n".join(dmg_lines),
            inline=True,
        )

        # Country info
        active_pop_val = active_pop
        wealth_val = wealth
        original_regions = sum(
            1 for r in country_regions
            if r.get("initialCountry") == ally["_id"]
        )
        current_regions = len(country_regions)
        party_name = party.get("name", "?") if party else "?"

        embed.add_field(
            name="\U0001f3db️ Country Info",
            value=(
                f"Ruling Party: **{party_name}**\n"
                f"Active Pop: **{active_pop_val}**\n"
                f"Wealth: **{wealth_val:,.2f}**\n"
                f"Regions: **{current_regions}** (core: {original_regions})"
            ),
            inline=True,
        )

        # Party ethics (combat bonuses)
        ethics = party.get("ethics", {}) if party else {}
        bonuses = self._party_combat_bonuses(ethics)
        ethics_lines = []
        if bonuses["militarist_pct"]:
            ethics_lines.append(f"Militarist: {bonuses['militarist_label']} ⚔️ ATK")
        if bonuses["pacifist_pct"]:
            ethics_lines.append(f"Pacifist: {bonuses['pacifist_label']} \U0001f6e1️ DEF")
        if bonuses["isolationist_pct"]:
            ethics_lines.append(f"Isolationist: {bonuses['isolationist_label']} \U0001f3af Sworn Enemy")
        if bonuses["diplomatic_pct"]:
            ethics_lines.append(f"Diplomatic: {bonuses['diplomatic_label']} \U0001f91d Ally")
        if not ethics_lines:
            ethics_lines.append("No combat bonuses")

        embed.add_field(
            name="⚖️ Party Ethics",
            value="\n".join(ethics_lines),
            inline=False,
        )

        # Alliance cost (based on damage dealt for our country)
        alliance_bonus_pct = bonus_config.get("alliance_bonus_pct", 10)
        extra_bonus_pct = bonus_config.get("extra_bonus_pct", 0)
        party_bonus_pct = bonuses["total_attack_pct"]
        effective_rate, weekly_cost = self._compute_alliance_cost(
            ally_dmg_for_us, alliance_bonus_pct, party_bonus_pct, extra_bonus_pct,
        )

        cost_lines = [
            f"Base Rate: **0.30g** / 1k dmg",
            f"Alliance Bonus: **{alliance_bonus_pct}%**",
        ]
        if party_bonus_pct:
            cost_lines.append(f"Party Ethics: **{party_bonus_pct}%**")
        if extra_bonus_pct:
            cost_lines.append(f"Extra Bonus: **{extra_bonus_pct}%**")
        cost_lines.append(f"Effective Rate: **{effective_rate:.4f}g** / 1k dmg")
        cost_lines.append(f"Est. Weekly Cost: **{weekly_cost:,.2f}g** (2d)")

        embed.add_field(
            name="\U0001f4b0 Alliance Cost Estimate",
            value="\n".join(cost_lines),
            inline=True,
        )

        # Defenses
        max_bunk = self._max_bunker(country_regions)
        max_base = self._max_military_base(country_regions)
        embed.add_field(
            name="\U0001f3f0 Defenses",
            value=(
                f"Max Bunker: **Lv.{max_bunk}**\n"
                f"Max Military Base: **Lv.{max_base}**"
            ),
            inline=True,
        )

        # Tags
        ally_id = ally["_id"]
        tag_data = tags.get(ally_id, {})
        is_merc = tag_data.get("mercenary", False)
        group = tag_data.get("group") or "—"
        notes = tag_data.get("notes") or "—"
        embed.add_field(
            name="\U0001f3f7️ Tags",
            value=(
                f"Type: **{'Mercenary' if is_merc else 'Normal'}**\n"
                f"Group: **{group}**\n"
                f"Notes: {notes}"
            ),
            inline=False,
        )

        # Wars
        wars_with = ally.get("warsWith", [])
        if wars_with:
            enemy_names = []
            for eid in wars_with:
                ec = countries_lookup.get(eid)
                enemy_names.append(ec["name"] if ec else eid[:8])
            embed.add_field(
                name="⚔️ Wars",
                value=", ".join(enemy_names),
                inline=False,
            )

        # Footer
        age_str = f"{cache_age} detik" if cache_age < 60 else f"{cache_age // 60} menit"
        embed.set_footer(text=f"Halaman {page}/{total} · data {age_str} lalu")

        return embed

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(invoke_without_command=True)
    async def amcu(self, ctx: commands.Context):
        """Warera alliance cost/benefit analysis."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @amcu.command(name="report")
    @commands.guild_only()
    @commands.mod()
    async def amcu_report(self, ctx: commands.Context):
        """Generate an alliance report for the guild's country."""
        remaining = await self._check_cooldown(ctx, "report")
        if remaining:
            await ctx.send(
                f"Tunggu **{remaining:.0f} detik** lagi sebelum menggunakan perintah ini.",
                delete_after=remaining,
            )
            return

        country_id = await self.config.guild(ctx.guild).country_id()
        if not country_id:
            await ctx.send(
                "Negara belum di-set. Gunakan `{}amcu setcountry <id_atau_nama>`.".format(
                    ctx.clean_prefix
                )
            )
            return

        async with ctx.typing():
            try:
                country = await self._fetch_country(country_id)
            except Exception as e:
                await ctx.send(f"Gagal mengambil data negara: `{e}`")
                return

            allies_ids = country.get("allies", [])
            if not allies_ids:
                await ctx.send("Negara ini tidak memiliki aliansi aktif.")
                return

            try:
                all_countries = await self._fetch_all_countries()
                all_regions = await self._fetch_all_regions()
            except Exception as e:
                await ctx.send(f"Gagal mengambil data: `{e}`")
                return

            countries_lookup = {c["_id"]: c for c in all_countries}

            # Fetch parties in parallel
            party_ids = []
            for aid in allies_ids:
                ac = countries_lookup.get(aid)
                if ac and ac.get("rulingParty"):
                    party_ids.append((aid, ac["rulingParty"]))

            parties: dict[str, dict | None] = {}
            if party_ids:
                tasks = [self._fetch_party(pid) for _, pid in party_ids]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for (aid, _), result in zip(party_ids, results):
                    parties[aid] = result if not isinstance(result, Exception) else None

            # Fetch per-ally damage from battles for our country (last 7 days)
            try:
                ally_battle_damage = await self._compute_ally_battle_damage(
                    country_id, set(allies_ids),
                )
            except Exception:
                ally_battle_damage = {}

        self._mark_used(ctx, "report")

        country_name = country.get("name", "???")
        bonus_config = await self.config.guild(ctx.guild).bonus_config()
        tags = await self.config.guild(ctx.guild).country_tags()
        colour = await ctx.embed_colour()
        cache_age = int(time.monotonic() - self._countries_cache_time)

        pages = []
        for i, aid in enumerate(allies_ids):
            ally = countries_lookup.get(aid)
            if not ally:
                continue
            party = parties.get(aid)
            c_regions = self._get_country_regions(all_regions, aid)
            embed = self._build_ally_embed(
                ally=ally,
                party=party,
                country_regions=c_regions,
                tags=tags,
                bonus_config=bonus_config,
                countries_lookup=countries_lookup,
                colour=colour,
                page=len(pages) + 1,
                total=0,
                cache_age=cache_age,
                ally_dmg_for_us=ally_battle_damage.get(aid, 0),
                our_country_name=country_name,
            )
            pages.append(embed)

        if not pages:
            await ctx.send("Tidak ada data aliansi yang bisa ditampilkan.")
            return

        # Fix page totals
        for i, embed in enumerate(pages):
            footer_text = embed.footer.text or ""
            embed.set_footer(text=footer_text.replace("/0", f"/{len(pages)}"))

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = ReportPaginatorView(pages, author_id=ctx.author.id)
            await ctx.send(embed=pages[0], view=view)

    @amcu.command(name="setcountry")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def amcu_setcountry(self, ctx: commands.Context, *, country: str):
        """Set the guild's country by name, code, or ID."""
        async with ctx.typing():
            result = await self._resolve_country(country)
        if not result:
            await ctx.send(f"Negara `{country}` tidak ditemukan.")
            return
        cid, cname = result
        await self.config.guild(ctx.guild).country_id.set(cid)
        await ctx.send(f"✅ Negara di-set ke **{cname}** (`{cid}`).")

    @amcu.group(name="tag", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def amcu_tag(self, ctx: commands.Context):
        """Manage country tags (mercenary, group, notes)."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @amcu_tag.command(name="mercenary")
    async def amcu_tag_mercenary(
        self, ctx: commands.Context, country: str, value: bool = True,
    ):
        """Tag a country as mercenary."""
        result = await self._resolve_country(country)
        if not result:
            await ctx.send(f"Negara `{country}` tidak ditemukan.")
            return
        cid, cname = result
        async with self.config.guild(ctx.guild).country_tags() as tags:
            entry = tags.setdefault(cid, {})
            entry["mercenary"] = value
        status = "mercenary" if value else "normal"
        await ctx.send(f"✅ **{cname}** ditandai sebagai **{status}**.")

    @amcu_tag.command(name="group")
    async def amcu_tag_group(
        self, ctx: commands.Context, country: str, *, group_name: str,
    ):
        """Set a country's group/faction."""
        result = await self._resolve_country(country)
        if not result:
            await ctx.send(f"Negara `{country}` tidak ditemukan.")
            return
        cid, cname = result
        async with self.config.guild(ctx.guild).country_tags() as tags:
            entry = tags.setdefault(cid, {})
            entry["group"] = group_name
        await ctx.send(f"✅ **{cname}** group di-set ke **{group_name}**.")

    @amcu_tag.command(name="notes")
    async def amcu_tag_notes(
        self, ctx: commands.Context, country: str, *, text: str,
    ):
        """Add notes to a country."""
        result = await self._resolve_country(country)
        if not result:
            await ctx.send(f"Negara `{country}` tidak ditemukan.")
            return
        cid, cname = result
        async with self.config.guild(ctx.guild).country_tags() as tags:
            entry = tags.setdefault(cid, {})
            entry["notes"] = text
        await ctx.send(f"✅ Notes untuk **{cname}** disimpan.")

    @amcu_tag.command(name="clear")
    async def amcu_tag_clear(self, ctx: commands.Context, *, country: str):
        """Clear all tags for a country."""
        result = await self._resolve_country(country)
        if not result:
            await ctx.send(f"Negara `{country}` tidak ditemukan.")
            return
        cid, cname = result
        async with self.config.guild(ctx.guild).country_tags() as tags:
            tags.pop(cid, None)
        await ctx.send(f"✅ Tags untuk **{cname}** dihapus.")

    @amcu.command(name="tags")
    @commands.guild_only()
    @commands.mod()
    async def amcu_tags(self, ctx: commands.Context):
        """List all tagged countries."""
        tags = await self.config.guild(ctx.guild).country_tags()
        if not tags:
            await ctx.send("Belum ada negara yang di-tag.")
            return

        try:
            all_countries = await self._fetch_all_countries()
        except Exception as e:
            await ctx.send(f"Gagal mengambil data negara: `{e}`")
            return

        lookup = {c["_id"]: c["name"] for c in all_countries}
        lines = []
        for cid, data in tags.items():
            name = lookup.get(cid, cid[:8])
            parts = []
            if data.get("mercenary"):
                parts.append("mercenary")
            if data.get("group"):
                parts.append(f"group: {data['group']}")
            if data.get("notes"):
                parts.append(f"notes: {data['notes']}")
            lines.append(f"**{name}** — {', '.join(parts) if parts else 'no tags'}")

        colour = await ctx.embed_colour()
        embed = discord.Embed(
            title="\U0001f3f7️ Tagged Countries",
            description="\n".join(lines),
            colour=colour,
        )
        await ctx.send(embed=embed)

    @amcu.command(name="bonus")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def amcu_bonus(
        self,
        ctx: commands.Context,
        alliance_pct: Optional[int] = None,
        extra_pct: Optional[int] = None,
    ):
        """View or set bonus config. Usage: `bonus [alliance%] [extra%]`"""
        if alliance_pct is None and extra_pct is None:
            cfg = await self.config.guild(ctx.guild).bonus_config()
            await ctx.send(
                f"Alliance bonus: **{cfg.get('alliance_bonus_pct', 10)}%** · "
                f"Extra bonus: **{cfg.get('extra_bonus_pct', 0)}%**"
            )
            return

        async with self.config.guild(ctx.guild).bonus_config() as cfg:
            if alliance_pct is not None:
                cfg["alliance_bonus_pct"] = alliance_pct
            if extra_pct is not None:
                cfg["extra_bonus_pct"] = extra_pct
            a_pct = cfg["alliance_bonus_pct"]
            e_pct = cfg["extra_bonus_pct"]

        await ctx.send(
            f"✅ Alliance bonus: **{a_pct}%** · Extra bonus: **{e_pct}%**"
        )

    @amcu.command(name="api")
    @commands.is_owner()
    async def amcu_api(self, ctx: commands.Context, api_key: Optional[str] = None):
        """Set or clear the Warera API key (bot owner only).

        Run without arguments to check status.
        Use `[p]amcu api copy` to copy the key from warera_eqcalc if loaded.
        """
        if api_key is None:
            current = await self.config.api_key()
            status = "sudah di-set" if current else "belum di-set"
            eqcalc_hint = ""
            if not current:
                eqcalc_key = await self._get_eqcalc_api_key()
                if eqcalc_key:
                    eqcalc_hint = (
                        f"\n💡 API key ditemukan di **warera_eqcalc**. "
                        f"Gunakan `{ctx.clean_prefix}amcu api copy` untuk menyalin."
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
            await self.config.api_key.set(eqcalc_key)
            self._countries_cache = None
            self._regions_cache = None
            await ctx.send("✅ API key disalin dari **warera_eqcalc**.", delete_after=5)
            return

        await self.config.api_key.set(api_key)
        self._countries_cache = None
        self._regions_cache = None
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
        except Exception:
            return None

    @amcu.command(name="setcd")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def amcu_setcd(self, ctx: commands.Context, seconds: int = 0):
        """Set per-channel cooldown for the report command (seconds, 0 = off)."""
        if seconds < 0:
            await ctx.send("Cooldown tidak boleh negatif.")
            return

        await self.config.guild(ctx.guild).cooldown_seconds.set(seconds)
        if seconds == 0:
            await ctx.send("✅ Cooldown dimatikan.")
        else:
            await ctx.send(f"✅ Cooldown di-set ke **{seconds} detik** per channel.")
