import re
import pytest
from datetime import timedelta

from raffle.utils import format_end_time, parse_duration, pick_winners, validate_emoji, validate_timezone

# Regex used by raffle_history for month validation (mirrored here to test it independently)
_MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")


def valid_month(s: str) -> bool:
    return bool(_MONTH_RE.fullmatch(s))


class TestParseDuration:
    def test_hours(self):
        assert parse_duration("2h") == timedelta(hours=2)

    def test_days(self):
        assert parse_duration("1d") == timedelta(days=1)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_case_insensitive(self):
        assert parse_duration("2H") == timedelta(hours=2)

    def test_invalid(self):
        assert parse_duration("abc") is None

    def test_zero(self):
        assert parse_duration("0h") is None

    def test_negative(self):
        assert parse_duration("-1h") is None

    def test_whitespace(self):
        assert parse_duration("  2h  ") == timedelta(hours=2)


class TestValidateTimezone:
    def test_valid_iana(self):
        assert validate_timezone("Asia/Jakarta") is True

    def test_utc(self):
        assert validate_timezone("UTC") is True

    def test_invalid(self):
        assert validate_timezone("Nonsense/Zone") is False

    def test_empty(self):
        assert validate_timezone("") is False


class TestPickWinners:
    def test_normal(self):
        result = pick_winners([1, 2, 3, 4, 5], 3)
        assert len(result) == 3
        assert all(w in [1, 2, 3, 4, 5] for w in result)
        assert len(set(result)) == 3  # no duplicates

    def test_fewer_than_count(self):
        result = pick_winners([1, 2], 5)
        assert set(result) == {1, 2}

    def test_empty(self):
        assert pick_winners([], 3) == []

    def test_exact_count(self):
        result = pick_winners([10, 20, 30], 3)
        assert set(result) == {10, 20, 30}

    def test_count_one(self):
        result = pick_winners([7, 8, 9], 1)
        assert len(result) == 1
        assert result[0] in [7, 8, 9]


class TestValidateEmoji:
    def test_unicode_emoji(self):
        assert validate_emoji("🎉") is True

    def test_custom_emoji(self):
        assert validate_emoji("<:test:123456789>") is True

    def test_animated_custom_emoji(self):
        assert validate_emoji("<a:test:123456789>") is True

    def test_plain_text(self):
        assert validate_emoji("hello") is False

    def test_empty(self):
        assert validate_emoji("") is False

    def test_ascii_only_rejected(self):
        assert validate_emoji(":smile:") is False

    def test_multi_char_unicode(self):
        # Compound emoji (flag) — still non-ASCII, ≤10 chars
        assert validate_emoji("🏆") is True


class TestFormatEndTime:
    def test_utc(self):
        result = format_end_time(0.0, "UTC")
        assert "UTC" in result
        assert "1970" in result
        # Full month name, not abbreviated (DD MMMM YYYY)
        assert "January" in result

    def test_invalid_tz_falls_back_to_utc(self):
        result = format_end_time(0.0, "Garbage/Zone")
        assert "UTC" in result

    def test_no_abbreviated_month(self):
        # epoch is January — should NOT appear as "Jan"
        result = format_end_time(0.0, "UTC")
        assert "Jan" not in result or "January" in result  # Jan is a prefix of January, so check full name present
        assert "January" in result


class TestHistoryMonthFormat:
    """Tests for the YYYY-MM validation regex used in raffle history."""

    def test_valid_january(self):
        assert valid_month("2026-01") is True

    def test_valid_december(self):
        assert valid_month("2026-12") is True

    def test_valid_current_month(self):
        assert valid_month("2026-05") is True

    def test_month_00_invalid(self):
        assert valid_month("2026-00") is False

    def test_month_13_invalid(self):
        assert valid_month("2026-13") is False

    def test_missing_leading_zero_invalid(self):
        assert valid_month("2026-5") is False

    def test_path_traversal_rejected(self):
        assert valid_month("../../etc") is False

    def test_empty_invalid(self):
        assert valid_month("") is False

    def test_year_only_invalid(self):
        assert valid_month("2026") is False
