from datetime import date

from mcp_atlassian.eggs.calendar import WorkCalendar


def test_working_days_skip_weekend():
    cal = WorkCalendar(frozenset())
    assert cal.working_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1   # Fri -> Mon
    assert cal.working_days_between(date(2026, 11, 13), date(2026, 11, 15)) == 0   # Fri -> Sun


def test_holiday_counts_zero():
    cal = WorkCalendar(frozenset({date(2026, 11, 16)}))
    assert cal.working_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 0
    assert cal.previous_working_day(date(2026, 11, 17)) == date(2026, 11, 13)


def test_is_working_day():
    cal = WorkCalendar(frozenset({date(2026, 11, 16)}))
    assert cal.is_working_day(date(2026, 11, 13)) is True   # Friday
    assert cal.is_working_day(date(2026, 11, 14)) is False  # Saturday
    assert cal.is_working_day(date(2026, 11, 15)) is False  # Sunday
    assert cal.is_working_day(date(2026, 11, 16)) is False  # Monday holiday
    assert cal.is_working_day(date(2026, 11, 17)) is True   # Tuesday


def test_add_working_days():
    cal = WorkCalendar(frozenset({date(2026, 11, 16)}))
    # Fri 11-13 + 1 working day (skips weekend 14, 15 and holiday Mon 16) -> Tue 11-17
    assert cal.add_working_days(date(2026, 11, 13), 1) == date(2026, 11, 17)
    assert cal.add_working_days(date(2026, 11, 13), 2) == date(2026, 11, 18)
    assert cal.add_working_days(date(2026, 11, 17), -1) == date(2026, 11, 13)


def test_working_days_between_reversed():
    cal = WorkCalendar(frozenset())
    assert cal.working_days_between(date(2026, 11, 16), date(2026, 11, 13)) == 0
    assert cal.working_days_between(date(2026, 11, 13), date(2026, 11, 13)) == 0
