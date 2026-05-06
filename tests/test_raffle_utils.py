import pytest
from datetime import timedelta

from raffle.utils import format_end_time, parse_duration, pick_winners, validate_emoji, validate_timezone


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
        assert len(set(result)) == 3

    def test_fewer_than_count(self):
        result = pick_winners([1, 2], 5)
        assert set(result) == {1, 2}

    def test_empty(self):
        assert pick_winners([], 3) == []


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


class TestFormatEndTime:
    def test_utc(self):
        result = format_end_time(0.0, "UTC")
        assert "UTC" in result
        assert "1970" in result

    def test_invalid_tz_falls_back_to_utc(self):
        result = format_end_time(0.0, "Garbage/Zone")
        assert "UTC" in result
