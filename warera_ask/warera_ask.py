import asyncio
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from google import genai
from google.genai import types
from redbot.core import commands, Config
from redbot.core.bot import Red

BASE_URL = "https://api2.warera.io/trpc"
_SCHEMA_PATH = Path(__file__).parent / "schema.json"

# Maximum tool calls across the entire agent loop (prevents cost blowup
# from a single query making N parallel calls × 5 iterations).
MAX_TOOL_CALLS = 12

# Word-boundary --debug matcher: matches "--debug" only as a whole token.
_DEBUG_RE = re.compile(r"(?:^|\s)--debug(?:\s|$)")

log = logging.getLogger("red.warera_ask")


class WareraAsk(commands.Cog):
    """Ask Warera game questions in natural language."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=715293840, force_registration=True)
        self.config.register_global(gemini_api_key=None, warera_api_key=None)
        self.config.register_guild(allowed_users=[])

        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._api_sem = asyncio.Semaphore(8)
        self._schema: list[dict] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester, user_id):
        pass

    async def _get_session(self) -> aiohttp.ClientSession:
        # Lock prevents two concurrent first-callers from creating two sessions
        # and leaking one of them (no __aexit__ on the orphaned session).
        async with self._session_lock:
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

    @commands.group(name="wask", invoke_without_command=True)
    async def wask(self, ctx: commands.Context):
        """Warera natural language query interface."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

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
        Use `[p]wask setapi copy` to copy the key from warera_eqcalc or
        warera_alliance_mcu if loaded.
        """
        if api_key is None:
            current = await self.config.warera_api_key()
            status = "sudah di-set" if current else "belum di-set"
            sibling_hint = ""
            if not current:
                sibling_key, sibling_name = await self._get_sibling_api_key()
                if sibling_key:
                    sibling_hint = (
                        f"\n💡 API key ditemukan di **{sibling_name}**. "
                        f"Gunakan `{ctx.clean_prefix}wask setapi copy` untuk menyalin."
                    )
            await ctx.send(f"API key {status}.{sibling_hint}")
            return

        if api_key.lower() == "copy":
            sibling_key, sibling_name = await self._get_sibling_api_key()
            if not sibling_key:
                await ctx.send(
                    "❌ Tidak bisa menyalin: **warera_eqcalc** atau **warera_alliance_mcu** "
                    "tidak loaded, atau API key-nya belum di-set."
                )
                return
            await self.config.warera_api_key.set(sibling_key)
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await ctx.send(f"✅ API key disalin dari **{sibling_name}**.", delete_after=5)
            return

        await self.config.warera_api_key.set(api_key)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("✅ API key tersimpan.", delete_after=5)

    # Cogs to probe for an existing Warera API key (in priority order).
    # First match wins. Each entry is (cog_name, display_name).
    _SIBLING_KEY_SOURCES = (
        ("EqCalc", "warera_eqcalc"),
        ("AllianceMCU", "warera_alliance_mcu"),
    )

    async def _get_sibling_api_key(self) -> tuple[Optional[str], Optional[str]]:
        """Return (api_key, source_display_name) from a sibling cog, or (None, None)."""
        for cog_name, display_name in self._SIBLING_KEY_SOURCES:
            cog = self.bot.get_cog(cog_name)
            if cog is None:
                continue
            try:
                key = await cog.config.api_key()
            except AttributeError:
                continue
            if key:
                return key, display_name
        return None, None

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

    @wask.command(name="info")
    async def wask_info(self, ctx: commands.Context):
        """Show warera_ask status and configuration."""
        gemini_key = await self.config.gemini_api_key()
        warera_key = await self.config.warera_api_key()

        lines = [
            f"**Gemini API key:** {'✅ set' if gemini_key else '❌ not set'}",
            f"**Warera API key:** {'✅ set' if warera_key else '⚠️ not set (optional)'}",
            f"**Endpoints loaded:** {len(self._schema)}",
        ]
        if ctx.guild:
            allowed = await self.config.guild(ctx.guild).allowed_users()
            lines.append(f"**Allowed members (this server):** {len(allowed)}")

        embed = discord.Embed(
            title="WareraAsk",
            description="\n".join(lines),
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Gemini agent engine
    # ------------------------------------------------------------------

    def _build_gemini_tools(self) -> list:
        """Convert self._schema into Gemini types.Tool objects."""
        _TYPE_MAP = {
            "string": "STRING",
            "array": "ARRAY",
            "boolean": "BOOLEAN",
            "number": "NUMBER",
            "integer": "NUMBER",
        }

        all_declarations = []

        for endpoint in self._schema:
            fn_name = endpoint["endpoint"].replace(".", "__")
            description = endpoint["description"]
            params_dict = endpoint.get("params", {})

            if not params_dict:
                decl = types.FunctionDeclaration(
                    name=fn_name,
                    description=description,
                    parameters=None,
                )
            else:
                properties = {}
                required = []
                for param_name, param_info in params_dict.items():
                    raw_type = param_info.get("type", "string")
                    gemini_type = _TYPE_MAP.get(raw_type, "STRING")
                    properties[param_name] = types.Schema(
                        type=gemini_type,
                        description=param_info.get("description", ""),
                    )
                    if param_info.get("required", False):
                        required.append(param_name)

                decl = types.FunctionDeclaration(
                    name=fn_name,
                    description=description,
                    parameters=types.Schema(
                        type="OBJECT",
                        properties=properties,
                        required=required if required else None,
                    ),
                )

            all_declarations.append(decl)

        return [types.Tool(function_declarations=all_declarations)]

    def _build_system_prompt(self) -> str:
        """Return the system prompt for the Gemini agent."""
        return (
            "You are a Warera game data assistant embedded in a Discord bot.\n"
            "You answer questions by calling Warera API endpoints as tools.\n\n"
            "GAME TERMINOLOGY (UI term → API field):\n"
            "- 'companies' / 'organizations' / 'orgs' → field `orgs` on country objects\n"
            "- 'alliance members' / 'allies' → field `allies` on country objects\n"
            "- 'wars' / 'at war with' → field `warsWith` on country objects\n"
            "- 'MU' / 'military unit' / 'mercenary unit' → use mu.getById\n"
            "- 'party' / 'political party' → use party.getById\n"
            "- 'region' / 'province' / 'state' → use region.getById or region.getRegionsObject\n"
            "- 'core region' / 'initial region' → field `initialCountry` on region objects "
            "  (a region is a 'core' region of country X if initialCountry == X)\n"
            "- 'capital' → field `isCapital` on region objects\n"
            "- 'damages' → combat damage stats on rankings/users\n"
            "- 'item prices' / 'market' → use itemTrading.getPrices\n\n"
            "WORKFLOW HINTS:\n"
            "- To find a country/user/MU by name, use search.searchAnything first to get its ID.\n"
            "- For 'how many X in country Y', usually 1) search for Y → get countryId, "
            "  2) call country.getCountryById, 3) count len() of the relevant array field "
            "  (e.g. orgs, allies). Most questions need only 2-3 endpoint calls.\n"
            "- For region-level questions (regions of a country, core regions), "
            "  use region.getRegionsObject and filter by `country` or `initialCountry`.\n\n"
            "RULES:\n"
            "- Answer in the SAME LANGUAGE the user uses. Indonesian question → Indonesian answer.\n"
            "- Be concise and direct. Give the answer, not a data dump.\n"
            "- Format numbers with thousand separators (e.g. 1,234,567).\n"
            "- Format dates as human-readable (e.g. '3 days ago' or 'May 8, 2026').\n"
            "- Call endpoints in sequence when you need data from multiple sources.\n"
            "- For counting/summing/filtering, do the math yourself on fetched JSON. Be precise.\n"
            "- TRY HARDER before giving up: check the terminology glossary above and search.searchAnything "
            "  before saying you can't answer. Most questions ARE answerable with 2-3 calls.\n"
            "- Only say 'cannot answer' if you genuinely tried calling endpoints and the data isn't there.\n"
            "- Do NOT make up data. Only use data from API responses.\n"
            "- Maximum 3 sentences in your final answer unless the user asks for detail.\n"
        )

    async def _execute_tool_call(self, name: str, args: dict) -> str:
        """Execute a single tool call and return result as a string."""
        endpoint = name.replace("__", ".")
        try:
            result = await self._api_call(endpoint, args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            log.warning("API call failed: %s(%r) -> %s", endpoint, args, e)
            return f"Error calling {endpoint}: {type(e).__name__}"

    async def _ask_gemini(self, question: str) -> tuple[str, list[str]]:
        """Run the Gemini agent loop and return (answer, tool_calls_log)."""
        gemini_key = await self.config.gemini_api_key()
        if not gemini_key:
            raise ValueError("Gemini API key not set.")

        client = genai.Client(api_key=gemini_key)
        tools = self._build_gemini_tools()
        system_prompt = self._build_system_prompt()
        tool_calls_log: list[str] = []

        contents = [types.Content(role="user", parts=[types.Part(text=question)])]
        total_calls = 0

        for _ in range(5):
            response = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    tools=tools,
                    system_instruction=system_prompt,
                ),
            )

            # Gemini may return zero candidates when safety/recitation filters block.
            if not response.candidates:
                return ("Maaf, jawaban diblokir oleh filter Gemini.", tool_calls_log)

            candidate = response.candidates[0]
            if candidate.content is None:
                return ("Maaf, Gemini tidak mengembalikan jawaban.", tool_calls_log)

            contents.append(candidate.content)

            # Collect function calls from this response
            parts = candidate.content.parts or []
            fn_calls = [p for p in parts if p.function_call is not None]

            if not fn_calls:
                # No tool calls — extract text answer
                text_parts = [p.text for p in parts if p.text]
                return ("\n".join(text_parts).strip() or "Tidak ada jawaban.", tool_calls_log)

            # Hard cap on total tool calls across the whole loop to bound cost.
            if total_calls + len(fn_calls) > MAX_TOOL_CALLS:
                return (
                    f"Query terlalu kompleks (batas {MAX_TOOL_CALLS} tool call tercapai).",
                    tool_calls_log,
                )

            # Execute all function calls and collect responses
            fn_responses = []
            for part in fn_calls:
                fc = part.function_call
                args = dict(fc.args)
                log_entry = f"{fc.name.replace('__', '.')}({json.dumps(args, ensure_ascii=False)})"
                tool_calls_log.append(log_entry)
                total_calls += 1
                result_str = await self._execute_tool_call(fc.name, args)
                fn_responses.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name,
                            response={"result": result_str},
                        )
                    )
                )

            contents.append(types.Content(role="tool", parts=fn_responses))

        return ("Maaf, tidak dapat menjawab pertanyaan dalam batas iterasi.", tool_calls_log)

    # ------------------------------------------------------------------
    # Main query command
    # ------------------------------------------------------------------

    @commands.cooldown(1, 10, commands.BucketType.user)
    @wask.command(name="ask")
    async def wask_ask(self, ctx: commands.Context, *, question: str):
        """Ask a question about Warera in natural language.

        Add --debug anywhere in your question to see which API calls were made.
        Example: [p]wask ask How many countries? --debug
        """
        # Use word-boundary regex so "--debug" embedded inside a word
        # (e.g. "--debuglevel") doesn't trigger and doesn't get stripped.
        debug = bool(_DEBUG_RE.search(question))
        question = _DEBUG_RE.sub(" ", question).strip()

        if not question:
            await ctx.send_help(ctx.command)
            return

        gemini_key = await self.config.gemini_api_key()
        if not gemini_key:
            embed = discord.Embed(
                description="Gemini API key belum di-set. Gunakan `[p]wask setgemini <key>`.",
                colour=discord.Colour.red(),
            )
            return await ctx.send(embed=embed)

        async with ctx.typing():
            try:
                answer, tool_calls = await self._ask_gemini(question)
                if len(answer) > 4000:
                    answer = answer[:4000] + "… (truncated)"
            except Exception:
                # Don't leak exception details to Discord — the message can
                # contain partial keys, internal paths, or full request URLs.
                log.exception("wask ask failed for question: %s", question[:200])
                embed = discord.Embed(
                    description=(
                        "Terjadi error saat memproses pertanyaan. "
                        "Periksa log bot untuk detail."
                    ),
                    colour=discord.Colour.red(),
                )
                return await ctx.send(embed=embed)

        embed = discord.Embed(
            description=answer,
            colour=await ctx.embed_colour(),
        )
        embed.set_footer(text=f"Q: {question[:100]}")

        if debug and tool_calls:
            debug_lines = "\n".join(f"`{tc}`" for tc in tool_calls)
            embed.add_field(name="🔧 API calls made", value=debug_lines[:1024], inline=False)

        await ctx.send(embed=embed)
