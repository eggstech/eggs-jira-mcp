from __future__ import annotations

from datetime import date, timedelta


class WorkCalendar:
    """Work calendar supporting working days calculation,
    skipping weekends and holidays.
    """

    def __init__(self, holidays: frozenset[date] = frozenset()) -> None:
        self.holidays = holidays

    def is_working_day(self, d: date) -> bool:
        """Check if date d is a working day (Mon-Fri and not a holiday)."""
        return d.weekday() < 5 and d not in self.holidays

    def working_days_between(self, a: date, b: date) -> int:
        """Count working days in the half-open interval (a, b]. Returns 0 if b <= a."""
        if b <= a:
            return 0
        count = 0
        cur = a + timedelta(days=1)
        while cur <= b:
            if self.is_working_day(cur):
                count += 1
            cur += timedelta(days=1)
        return count

    def add_working_days(self, d: date, n: int) -> date:
        """Add n working days to date d. If n is negative, subtracts working days."""
        cur = d
        step = 1 if n > 0 else -1
        remaining = abs(n)
        while remaining > 0:
            cur += timedelta(days=step)
            if self.is_working_day(cur):
                remaining -= 1
        return cur

    def previous_working_day(self, d: date) -> date:
        """Return the closest working day strictly before d."""
        cur = d - timedelta(days=1)
        while not self.is_working_day(cur):
            cur -= timedelta(days=1)
        return cur
