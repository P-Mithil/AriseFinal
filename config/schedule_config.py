from typing import List, Dict, Tuple
from datetime import time

"""
Central scheduling configuration.

Edit WORKING_DAYS to change which days are used throughout the timetable
generation pipeline (all phases, writers, and conflict checkers).
"""

# Default working days for IIIT Dharwad
WORKING_DAYS: List[str] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
]

def is_working_day(day: str) -> bool:
    """Return True if the given day name is in WORKING_DAYS."""
    if day is None:
        return False
    return str(day).strip() in WORKING_DAYS


# College working hours (used for slot generation and verification)
DAY_START_TIME: time = time(9, 0)
DAY_END_TIME: time = time(18, 0)


# Lunch windows by semester (full-semester verification + slot generators)
# Times are (start, end) in 24-hour clock.
LUNCH_WINDOWS: Dict[int, Tuple[time, time]] = {
    1: (time(12, 30), time(13, 30)),  # Sem 1
    3: (time(12, 45), time(13, 45)),  # Sem 3
    5: (time(13, 0), time(14, 0)),    # Sem 5
}


# Large-room / 240-seater configuration
LARGE_ROOM_CAPACITY_THRESHOLD: int = 240

# Default lab room to use when a practical needs a lab but no explicit lab
# is assigned by Phase 4/8 (kept for backward compatibility).
DEFAULT_COMBINED_LAB_ROOM: str = "L105"

