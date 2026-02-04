"""
Time Validation Utility
Ensures all time slots are within working hours (9:00-18:00)
"""

from datetime import time
from typing import Tuple, Optional
from utils.data_models import TimeBlock


def validate_time_range(start: time, end: time, min_start: time = time(9, 0), max_end: time = time(18, 0)) -> bool:
    """
    Validate that a time range is within working hours.
    
    Args:
        start: Start time
        end: End time
        min_start: Minimum allowed start time (default: 9:00)
        max_end: Maximum allowed end time (default: 18:00)
    
    Returns:
        True if time range is valid, False otherwise
    """
    if start < min_start:
        return False
    if end > max_end:
        return False
    if start >= end:
        return False
    return True


def ensure_slot_within_hours(slot: TimeBlock, max_end: time = time(18, 0), min_start: time = time(9, 0)) -> bool:
    """
    Ensure a TimeBlock slot is within working hours.
    
    Args:
        slot: TimeBlock to validate
        max_end: Maximum allowed end time (default: 18:00)
        min_start: Minimum allowed start time (default: 9:00)
    
    Returns:
        True if slot is valid, False otherwise
    """
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
    
    # Cap at 18:00
    if end_hour > 18 or (end_hour == 18 and end_minute > 0):
        return time(18, 0)
    
    return time(end_hour, end_minute)


def can_fit_duration(start: time, duration_minutes: int, max_end: time = time(18, 0)) -> bool:
    """
    Check if a duration can fit starting from the given start time without exceeding max_end.
    
    Args:
        start: Start time
        duration_minutes: Duration in minutes
        max_end: Maximum allowed end time (default: 18:00)
    
    Returns:
        True if duration fits, False otherwise
    """
    end_time = calculate_end_time(start, duration_minutes)
    return end_time <= max_end


def get_valid_slot_range(start: time, duration_minutes: int, max_end: time = time(18, 0)) -> Optional[Tuple[time, time]]:
    """
    Get a valid time slot range for given start time and duration, ensuring it doesn't exceed max_end.
    
    Args:
        start: Start time
        duration_minutes: Duration in minutes
        max_end: Maximum allowed end time (default: 18:00)
    
    Returns:
        Tuple of (start, end) if valid, None if would exceed max_end
    """
    if not can_fit_duration(start, duration_minutes, max_end):
        return None
    
    end_time = calculate_end_time(start, duration_minutes)
    return (start, end_time)


