"""
Time Validation Utility
Ensures all time slots are within working hours from config.schedule_config.
"""

from datetime import time
from typing import Tuple, Optional
from utils.data_models import TimeBlock


def _default_day_bounds() -> Tuple[time, time]:
    from config.schedule_config import DAY_START_TIME, DAY_END_TIME
    return DAY_START_TIME, DAY_END_TIME


def time_to_minutes(t: time) -> int:
    """Minutes from midnight for comparisons."""
    return t.hour * 60 + t.minute


def slot_end_within_day(start: time, duration_minutes: int, max_end: Optional[time] = None) -> bool:
    """
    True if start + duration does not exceed max_end (default DAY_END_TIME).
    Use for 2h labs etc. instead of hardcoded 'last start hour'.
    """
    if max_end is None:
        _, max_end = _default_day_bounds()
    return time_to_minutes(start) + duration_minutes <= time_to_minutes(max_end)


def validate_time_range(
    start: time,
    end: time,
    min_start: Optional[time] = None,
    max_end: Optional[time] = None,
) -> bool:
    """
    Validate that a time range is within working hours.
    
    Args:
        start: Start time
        end: End time
        min_start: Minimum allowed start (default: DAY_START_TIME from config)
        max_end: Maximum allowed end (default: DAY_END_TIME from config)
    
    Returns:
        True if time range is valid, False otherwise
    """
    if min_start is None or max_end is None:
        ds, de = _default_day_bounds()
        if min_start is None:
            min_start = ds
        if max_end is None:
            max_end = de
    if start < min_start:
        return False
    if end > max_end:
        return False
    if start >= end:
        return False
    return True


def ensure_slot_within_hours(
    slot: TimeBlock,
    max_end: Optional[time] = None,
    min_start: Optional[time] = None,
) -> bool:
    """Ensure a TimeBlock is within working hours (defaults from schedule_config)."""
    return validate_time_range(slot.start, slot.end, min_start, max_end)


def calculate_end_time(start: time, duration_minutes: int) -> time:
    """
    Calculate end time from start time and duration, ensuring it doesn't exceed 18:00.
    
    Args:
        start: Start time
        duration_minutes: Duration in minutes
    
    Returns:
        End time (capped at 18:00)
    """
    from datetime import timedelta
    start_datetime = timedelta(hours=start.hour, minutes=start.minute)
    duration = timedelta(minutes=duration_minutes)
    end_datetime = start_datetime + duration
    
    # Convert back to time
    total_minutes = int(end_datetime.total_seconds() / 60)
    end_hour = total_minutes // 60
    end_minute = total_minutes % 60
    
    _, max_end = _default_day_bounds()
    max_h, max_m = max_end.hour, max_end.minute
    if end_hour > max_h or (end_hour == max_h and end_minute > max_m):
        return max_end
    
    return time(end_hour, end_minute)


def can_fit_duration(start: time, duration_minutes: int, max_end: Optional[time] = None) -> bool:
    """
    Check if a duration can fit starting from the given start time without exceeding max_end.
    
    Args:
        start: Start time
        duration_minutes: Duration in minutes
        max_end: Maximum allowed end time (default: DAY_END_TIME)
    
    Returns:
        True if duration fits, False otherwise
    """
    if max_end is None:
        _, max_end = _default_day_bounds()
    end_time = calculate_end_time(start, duration_minutes)
    return end_time <= max_end


def get_valid_slot_range(start: time, duration_minutes: int, max_end: Optional[time] = None) -> Optional[Tuple[time, time]]:
    """
    Get a valid time slot range for given start time and duration, ensuring it doesn't exceed max_end.
    
    Args:
        start: Start time
        duration_minutes: Duration in minutes
        max_end: Maximum allowed end time (default: DAY_END_TIME)
    
    Returns:
        Tuple of (start, end) if valid, None if would exceed max_end
    """
    if max_end is None:
        _, max_end = _default_day_bounds()
    if not can_fit_duration(start, duration_minutes, max_end):
        return None
    
    end_time = calculate_end_time(start, duration_minutes)
    return (start, end_time)


