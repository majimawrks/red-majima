from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone as tz_utc
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def validate_timezone(tz: str) -> bool:
        if not tz:
            return False
        try:
            ZoneInfo(tz)
            return True
        except (ZoneInfoNotFoundError, KeyError):
            return False

    def _get_tz(tz_name: str):
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return tz_utc.utc

except ImportError:
    import pytz

    def validate_timezone(tz: str) -> bool:
        if not tz:
            return False
        try:
            pytz.timezone(tz)
            return True
        except pytz.UnknownTimeZoneError:
            return False

    def _get_tz(tz_name: str):
        try:
            return pytz.timezone(tz_name)
        except Exception:
            return pytz.utc


# ── Duration parsing ──────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$", re.IGNORECASE)
_MULTIPLIERS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> Optional[timedelta]:
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount <= 0:
        return None
    return timedelta(seconds=amount * _MULTIPLIERS[unit])


# ── Emoji validation (G4) ────────────────────────────────────────────

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:\d+>$")


def validate_emoji(value: str) -> bool:
    if not value:
        return False
    # Custom Discord emoji: <:name:id> or <a:name:id>
    if _CUSTOM_EMOJI_RE.match(value):
        return True
    # Unicode emoji: non-ASCII, reasonably short
    if len(value) <= 10 and not value.isascii():
        return True
    return False


# ── Winner selection ──────────────────────────────────────────────────

def pick_winners(participants: list[int], count: int) -> list[int]:
    if not participants:
        return []
    pool = list(participants)
    random.shuffle(pool)
    return pool[:count]


# ── Time formatting ───────────────────────────────────────────────────

def format_end_time(end_ts: float, tz_name: str) -> str:
    tz = _get_tz(tz_name)
    dt = datetime.fromtimestamp(end_ts, tz=tz)
    return dt.strftime("%H:%M %Z · %d %b %Y")
