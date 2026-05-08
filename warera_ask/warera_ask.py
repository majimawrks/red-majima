import asyncio
import json
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

_SAFE_BUILTINS = {
    "len": len, "sum": sum, "min": min, "max": max,
    "sorted": sorted, "round": round, "abs": abs,
    "int": int, "float": float, "list": list, "dict": dict,
    "str": str, "bool": bool, "enumerate": enumerate,
    "zip": zip, "range": range, "any": any, "all": all,
}


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

        # Special calculate tool
        all_declarations.append(
            types.FunctionDeclaration(
                name="calculate",
                description=(
                    "Evaluate a Python expression on data you already fetched. "
                    "Use for counting, summing, averaging, filtering lists. "
                    "Pass the expression and the data as JSON string."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "expression": types.Schema(
                            type="STRING",
                            description="Python expression, e.g. 'len([x for x in data if x[\"isCapital\"]])'",
                        ),
                        "data": types.Schema(
                            type="STRING",
                            description="JSON-encoded data to operate on",
                        ),
                    },
                    required=["expression", "data"],
                ),
            )
        )

        return [types.Tool(function_declarations=all_declarations)]

    def _build_system_prompt(self) -> str:
        """Return the system prompt for the Gemini agent."""
        return (
            "You are a Warera game data assistant embedded in a Discord bot.\n"
            "You answer questions by calling Warera API endpoints as tools.\n\n"
            "RULES:\n"
            "- Answer in the SAME LANGUAGE the user uses. Indonesian question → Indonesian answer.\n"
            "- Be concise and direct. Give the answer, not a data dump.\n"
            "- Format numbers with thousand separators (e.g. 1,234,567).\n"
            "- Format dates as human-readable (e.g. '3 days ago' or 'May 8, 2026').\n"
            "- Call endpoints in sequence if you need data from multiple sources.\n"
            "- Use the calculate tool for counting, summing, filtering, or math on fetched data.\n"
            "- If the question cannot be answered with available endpoints, say so clearly.\n"
            "- Do NOT make up data. Only use data from API responses.\n"
            "- Maximum 3 sentences in your final answer unless the user asks for detail.\n"
        )

    async def _execute_tool_call(self, name: str, args: dict) -> str:
        """Execute a single tool call and return result as a string."""
        if name == "calculate":
            try:
                data = json.loads(args["data"])
                result = eval(
                    args["expression"],
                    {"__builtins__": _SAFE_BUILTINS},
                    {"data": data},
                )
                return str(result)
            except Exception as e:
                return f"Error: {e}"
        else:
            endpoint = name.replace("__", ".")
            try:
                result = await self._api_call(endpoint, args)
                return json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                return f"Error calling {endpoint}: {e}"

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

        for _ in range(5):
            response = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    tools=tools,
                    system_instruction=system_prompt,
                ),
            )

            candidate = response.candidates[0]
            contents.append(candidate.content)

            # Collect function calls from this response
            fn_calls = [p for p in candidate.content.parts if p.function_call is not None]

            if not fn_calls:
                # No tool calls — extract text answer
                text_parts = [p.text for p in candidate.content.parts if p.text]
                return ("\n".join(text_parts).strip() or "Tidak ada jawaban.", tool_calls_log)

            # Execute all function calls and collect responses
            fn_responses = []
            for part in fn_calls:
                fc = part.function_call
                args = dict(fc.args)
                log_entry = f"{fc.name.replace('__', '.')}({json.dumps(args, ensure_ascii=False)})"
                tool_calls_log.append(log_entry)
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
