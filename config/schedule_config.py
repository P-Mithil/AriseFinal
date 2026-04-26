import os
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
# Single source of truth — keep in sync with registrar policy (8:00 supports early slots).
DAY_START_TIME: time = time(9, 0)
DAY_END_TIME: time = time(18, 30)


# Lunch windows by semester (full-semester verification + slot generators)
# Times are (start, end) in 24-hour clock.
# Staggered lunch by year-level: even semesters mirror the preceding odd semester.
# Extend this dict if your course_data uses additional semester numbers.
LUNCH_WINDOWS: Dict[int, Tuple[time, time]] = {
    1: (time(12, 30), time(13, 30)),  # Sem 1
    2: (time(12, 30), time(13, 30)),  # Sem 2 (same slot band as Sem 1)
    3: (time(12, 45), time(13, 45)),  # Sem 3
    4: (time(12, 45), time(13, 45)),  # Sem 4
    5: (time(13, 0), time(14, 0)),    # Sem 5
    6: (time(13, 0), time(14, 0)),   # Sem 6
    7: (time(13, 0), time(14, 0)),   # Sem 7+ (typical upper-year band)
    8: (time(13, 0), time(14, 0)),
}


# Large-room / 240-seater configuration
LARGE_ROOM_CAPACITY_THRESHOLD: int = 240

# Default fallback section capacity used when the real enrollment cannot be
# determined from session/section data (smallest single-section cohort size).
DEFAULT_SECTION_CAPACITY: int = 60

# Default lab room to use when a practical needs a lab but no explicit lab
# is assigned by Phase 4/8 (kept for backward compatibility).
DEFAULT_COMBINED_LAB_ROOM: str = "L105"


# Faculty verification (post-generate / deep verify):
# Default False: same instructor + same normalized period (PRE/POST) + overlapping
# wall-clock time = clash — even if one class is Sem1 and another Sem3 (one person
# cannot teach two slots at once in the same half-semester).
# Set True only if you intentionally want to ignore overlaps when section labels
# have no shared SemN (e.g. legacy CSV quirks); not recommended for real timetables.
FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER: bool = False

# Phase 4 combined lectures are only for low-credit core courses (typically <= 2 credits).
# If actual course credits are STRICTLY GREATER than this value, the same instructor cannot
# teach that same course at overlapping times for two different section labels (e.g. CSE-A vs CSE-B).
FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD: int = 2


# Generation: require zero violations from run_verification_on_sessions before saving outputs.
# If True, generate_24_sheets raises GenerationViolationError when verify fails (API/CLI handle it).
REQUIRE_ZERO_VERIFICATION_VIOLATIONS: bool = True

# After section-overlap / room moves, re-run faculty resolution this many outer passes.
# Each pass uses a different RNG seed and shuffles session/conflict order.
GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES: int = 20

# Base seed for repair shuffles (change to explore different search paths).
GENERATION_REPAIR_SHUFFLE_SEED: int = 314159

# After strict verify fails on the built grid, reshuffle+repair pipeline and rebuild sheets
# this many times (fresh workbook each attempt). Only used when loading from full pipeline, not CSV log.
GENERATION_STRICT_MACRO_MAX_ATTEMPTS: int = 20

# Hard runtime guard (seconds) for a single generation run.
# Runs exceeding this threshold should fail fast to keep UX bounded.
_max_runtime_raw = (os.getenv("ARISE_MAX_RUNTIME_SECONDS", "300") or "300").strip()
try:
    GENERATION_MAX_RUNTIME_SECONDS: int = max(60, int(_max_runtime_raw))
except Exception:
    GENERATION_MAX_RUNTIME_SECONDS = 300

# Runtime mode for adaptive repair/search budgets.
# Allowed: "strict", "balanced", "fast"
_runtime_mode_raw = (os.getenv("ARISE_RUNTIME_MODE", "balanced") or "balanced").strip().lower()
if _runtime_mode_raw not in {"strict", "balanced", "fast"}:
    _runtime_mode_raw = "balanced"
GENERATION_RUNTIME_MODE: str = _runtime_mode_raw

# Multiplier applied on top of data-driven budgets.
# - strict: wider search, highest chance of recovery in one macro iteration
# - balanced: default
# - fast: tighter search, lower latency with more reliance on macro retries
GENERATION_RUNTIME_SCALE: float = {
    "strict": 1.20,
    "balanced": 1.00,
    "fast": 0.70,
}[GENERATION_RUNTIME_MODE]

