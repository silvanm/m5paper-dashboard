"""TDD tests for refresh schedule and compact bus departures."""

from datetime import datetime, timezone, timedelta
import pytest

# Zurich timezone for test fixtures
from zoneinfo import ZoneInfo
ZRH = ZoneInfo("Europe/Zurich")


# --- Refresh Schedule Tests ---

from main import get_refresh_schedule


class TestRefreshSchedule:
    """Test get_refresh_schedule(now) -> (mode, sleep_minutes)."""

    # Weekday mornings: high refresh
    def test_weekday_morning_0500(self):
        now = datetime(2026, 4, 6, 5, 0, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    def test_weekday_morning_0830(self):
        now = datetime(2026, 4, 6, 8, 30, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    # Weekday midday: low refresh
    def test_weekday_midday(self):
        now = datetime(2026, 4, 6, 12, 0, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "low"
        assert minutes == 60

    def test_weekday_afternoon(self):
        now = datetime(2026, 4, 6, 15, 0, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "low"
        assert minutes == 60

    # Weekday evening boundary
    def test_weekday_2059(self):
        now = datetime(2026, 4, 6, 20, 59, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "low"
        assert minutes == 60

    # Weekday night: sleep
    def test_weekday_night_2100(self):
        now = datetime(2026, 4, 6, 21, 0, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"

    def test_weekday_night_0300(self):
        now = datetime(2026, 4, 6, 3, 0, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"

    def test_weekday_night_0459(self):
        now = datetime(2026, 4, 6, 4, 59, tzinfo=ZRH)  # Monday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"

    # Weekend (Saturday) morning: high
    def test_weekend_morning(self):
        now = datetime(2026, 4, 11, 7, 0, tzinfo=ZRH)  # Saturday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    # Weekend afternoon: still high (unlike weekday)
    def test_weekend_afternoon(self):
        now = datetime(2026, 4, 11, 14, 0, tzinfo=ZRH)  # Saturday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    def test_weekend_1659(self):
        now = datetime(2026, 4, 11, 16, 59, tzinfo=ZRH)  # Saturday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    # Weekend evening: low
    def test_weekend_evening(self):
        now = datetime(2026, 4, 11, 18, 0, tzinfo=ZRH)  # Saturday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "low"
        assert minutes == 60

    # Weekend night: sleep
    def test_weekend_night(self):
        now = datetime(2026, 4, 11, 22, 0, tzinfo=ZRH)  # Saturday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"

    # Sunday is also weekend
    def test_sunday_afternoon(self):
        now = datetime(2026, 4, 12, 14, 0, tzinfo=ZRH)  # Sunday
        mode, minutes = get_refresh_schedule(now)
        assert mode == "high"
        assert minutes == 15

    # Sleep mode should not return a meaningful sleep_minutes
    # (device handles wake-up at 05:00)
    def test_sleep_mode_returns_minutes_until_0500(self):
        now = datetime(2026, 4, 6, 21, 0, tzinfo=ZRH)  # Monday 21:00
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"
        # Should sleep until 05:00 next day = 8 hours = 480 minutes
        assert minutes == 480

    def test_sleep_mode_0300_returns_minutes_until_0500(self):
        now = datetime(2026, 4, 6, 3, 0, tzinfo=ZRH)  # Monday 03:00
        mode, minutes = get_refresh_schedule(now)
        assert mode == "sleep"
        # Should sleep until 05:00 = 2 hours = 120 minutes
        assert minutes == 120


# --- Compact Bus Departures Tests ---

from main import format_compact_departures, strip_zurich_prefix


class TestStripZurichPrefix:
    def test_strips_zurich_comma(self):
        assert strip_zurich_prefix("Zürich, Hegibachplatz") == "Hegibachplatz"

    def test_strips_zurich_space(self):
        assert strip_zurich_prefix("Zürich Wiedikon, Bahnhof") == "Wiedikon, Bahnhof"

    def test_leaves_other_cities(self):
        assert strip_zurich_prefix("Schlieren") == "Schlieren"

    def test_leaves_partial_match(self):
        assert strip_zurich_prefix("Zürichsee") == "Zürichsee"


class TestFormatCompactDepartures:
    """Test format_compact_departures(departures) -> list[CompactBusDeparture]."""

    def test_groups_by_destination(self):
        deps = [
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:17"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:32"},
            {"line": "31", "dest": "Schlieren", "time": "18:20"},
        ]
        result = format_compact_departures(deps)
        assert len(result) == 2
        hegibachplatz = next(r for r in result if r.dest == "Hegibachplatz")
        assert len(hegibachplatz.times) == 2

    def test_strips_zurich_from_dest(self):
        deps = [
            {"line": "80", "dest": "Zürich, Triemlispital", "time": "18:05"},
        ]
        result = format_compact_departures(deps)
        assert result[0].dest == "Triemlispital"

    def test_time_formatting_same_hour(self):
        deps = [
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:17"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:23"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:56"},
        ]
        result = format_compact_departures(deps)
        entry = result[0]
        assert entry.times == ["18:17", ":23", ":56"]

    def test_time_formatting_hour_change(self):
        deps = [
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:45"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "19:05"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "19:20"},
        ]
        result = format_compact_departures(deps)
        entry = result[0]
        assert entry.times == ["18:45", "19:05", ":20"]

    def test_preserves_line_number(self):
        deps = [
            {"line": "80", "dest": "Zürich, Triemlispital", "time": "18:05"},
        ]
        result = format_compact_departures(deps)
        assert result[0].line == "80"

    def test_filters_hidden_destinations(self):
        deps = [
            {"line": "67", "dest": "Zürich, Dunkelhölzli", "time": "18:10"},
            {"line": "80", "dest": "Zürich, Triemlispital", "time": "18:15"},
        ]
        result = format_compact_departures(deps)
        assert len(result) == 1
        assert result[0].dest == "Triemlispital"

    def test_empty_input(self):
        result = format_compact_departures([])
        assert result == []

    def test_multiple_lines_same_dest(self):
        """Different lines to same dest should be separate entries."""
        deps = [
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:17"},
            {"line": "N1", "dest": "Zürich, Hegibachplatz", "time": "18:33"},
        ]
        result = format_compact_departures(deps)
        assert len(result) == 2
