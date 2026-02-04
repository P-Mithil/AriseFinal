"""
Shared utilities for faculty conflict prevention and resolution.
Used across phases to check faculty availability before scheduling
and to resolve conflicts after scheduling.
"""

from typing import List, Dict, Optional, Tuple
from datetime import time
from collections import defaultdict

from utils.data_models import TimeBlock, ScheduledSession


def check_faculty_availability_in_period(
    faculty: str,
    day: str,
    start_time: time,
    end_time: time,
    period: str,
    all_sessions: List,
    exclude_session=None,
) -> bool:
    """
    Check if a faculty member is available at a specific time in a specific period.
    
    Args:
        faculty: Faculty member name
        day: Day of the week
        start_time: Session start time
        end_time: Session end time
        period: Period ('PRE' or 'POST')
        all_sessions: List of all scheduled sessions
        exclude_session: Session to exclude from conflict check (for rescheduling)
        
    Returns:
        True if faculty is available, False if there's a conflict
    """
    if not faculty or faculty in ['TBD', 'Various', '-']:
        return True
    
    candidate_block = TimeBlock(day, start_time, end_time)
    
    for session in all_sessions:
        if exclude_session and session == exclude_session:
            continue
        
        # Extract faculty from session
        session_faculty = None
        if isinstance(session, dict):
            session_faculty = session.get('instructor') or session.get('faculty')
        else:
            session_faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        
        if not session_faculty or session_faculty != faculty:
            continue
        
        # Extract period from session
        session_period = None
        if isinstance(session, dict):
            session_period = session.get('period', 'PRE')
        else:
            session_period = getattr(session, 'period', 'PRE')
        
        # Normalize period
        session_period = _normalize_period(session_period)
        candidate_period = _normalize_period(period)
        
        # Only check conflicts within the same period
        if session_period != candidate_period:
            continue
        
        # Extract time block from session
        session_block = None
        if isinstance(session, dict):
            session_block = session.get('time_block')
        else:
            session_block = getattr(session, 'block', None)
        
        if not session_block:
            continue
        
        # Check for time overlap (same day, overlapping times)
        if session_block.day == day and candidate_block.overlaps(session_block):
            return False
    
    return True


def _normalize_period(raw: str) -> str:
    """Normalize period strings to 'PRE' or 'POST'."""
    if not raw:
        return "PRE"
    val = str(raw).strip().upper()
    if val in ("PREMID", "PRE"):
        return "PRE"
    if val in ("POSTMID", "POST"):
        return "POST"
    return val


def get_faculty_sessions_by_period(
    faculty: str,
    all_sessions: List,
    period: Optional[str] = None
) -> Dict[str, List]:
    """
    Get all sessions for a faculty member, optionally filtered by period.
    
    Args:
        faculty: Faculty member name
        all_sessions: List of all scheduled sessions
        period: Optional period filter ('PRE' or 'POST')
        
    Returns:
        Dict mapping period -> list of sessions for that faculty
    """
    faculty_sessions_by_period = defaultdict(list)
    
    for session in all_sessions:
        # Extract faculty
        session_faculty = None
        if isinstance(session, dict):
            session_faculty = session.get('instructor') or session.get('faculty')
        else:
            session_faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        
        if not session_faculty or session_faculty != faculty:
            continue
        
        # Extract period
        session_period = None
        if isinstance(session, dict):
            session_period = session.get('period', 'PRE')
        else:
            session_period = getattr(session, 'period', 'PRE')
        
        session_period = _normalize_period(session_period)
        
        # Filter by period if specified
        if period and session_period != _normalize_period(period):
            continue
        
        faculty_sessions_by_period[session_period].append(session)
    
    return dict(faculty_sessions_by_period)


def get_session_move_priority(session) -> Tuple[int, int, str]:
    """
    Calculate move priority for a session. Lower priority = easier to move.
    
    Priority order (lower = easier to move):
    1. Regular core courses (Phase 5, 7) - priority 1
    2. Elective courses - priority 2
    3. Combined courses (Phase 4) - priority 3
    
    Within same type:
    - Tutorials before Lectures before Practicals
    - Later semester before earlier
    
    Returns:
        Tuple of (type_priority, kind_priority, course_code) for sorting
    """
    # Determine session type
    course_code = None
    if isinstance(session, dict):
        course_code = str(session.get('course_code', ''))
    else:
        course_code = str(getattr(session, 'course_code', ''))
    
    # Type priority: combined/elective baskets are hardest to move
    if course_code.startswith('ELECTIVE_BASKET_'):
        type_priority = 2  # Elective baskets
    elif isinstance(session, dict):
        # Dictionary sessions are typically combined courses
        type_priority = 3  # Combined courses - hardest to move
    else:
        type_priority = 1  # Regular core courses - easiest to move
    
    # Kind priority: T > L > P (tutorials easier to move)
    kind = None
    if isinstance(session, dict):
        kind = session.get('session_type', 'L') or session.get('kind', 'L')
    else:
        kind = getattr(session, 'kind', 'L')
    
    kind_priority_map = {'T': 1, 'L': 2, 'P': 3}
    kind_priority = kind_priority_map.get(kind, 2)
    
    # Semester (extract from section or course)
    semester = 1
    if isinstance(session, dict):
        sections = session.get('sections', [])
        if sections:
            section_str = str(sections[0])
            try:
                if 'Sem' in section_str:
                    semester = int(section_str.split('Sem')[1].split('-')[0])
            except:
                pass
    else:
        section = getattr(session, 'section', '')
        try:
            if 'Sem' in str(section):
                semester = int(str(section).split('Sem')[1].split('-')[0])
        except:
            pass
    
    # Higher semester = easier to move (lower priority)
    semester_priority = 10 - semester  # Invert so later semesters have lower priority
    
    return (type_priority, kind_priority, semester_priority, course_code)


def find_alternative_slot_for_faculty(
    session,
    all_sessions: List,
    occupied_slots: Dict[str, List],
    classrooms: List,
    period: str,
    max_attempts: int = 50
) -> Optional[TimeBlock]:
    """
    Find an alternative time slot for a session that avoids faculty conflicts.
    
    Args:
        session: Session to reschedule (dict or ScheduledSession)
        all_sessions: All current sessions
        occupied_slots: Dict of occupied slots by section_period
        classrooms: List of available classrooms
        period: Period to search within ('PRE' or 'POST')
        max_attempts: Maximum number of slot candidates to try
        
    Returns:
        TimeBlock for new slot, or None if no suitable slot found
    """
    from modules_v2.phase5_core_courses import (
        get_available_time_slots,
        get_lunch_blocks,
        find_alternative_slot
    )
    
    # Extract session details
    if isinstance(session, dict):
        course_code = session.get('course_code', '')
        section = session.get('sections', [None])[0] if session.get('sections') else None
        faculty = session.get('instructor') or session.get('faculty')
        semester = None
        course_obj = session.get('course_obj')
        if course_obj:
            semester = getattr(course_obj, 'semester', 1)
        if not semester and section:
            try:
                if 'Sem' in str(section):
                    semester = int(str(section).split('Sem')[1].split('-')[0])
            except:
                semester = 1
    else:
        course_code = getattr(session, 'course_code', '')
        section = getattr(session, 'section', '')
        faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        try:
            if 'Sem' in str(section):
                semester = int(str(section).split('Sem')[1].split('-')[0])
            else:
                semester = 1
        except:
            semester = 1
    
    if not semester:
        semester = 1
    
    # Get available slots for this section/period
    section_key = f"{section}_{period}" if section else f"UNKNOWN_{period}"
    available_slots = get_available_time_slots(semester, occupied_slots, course_code, section, period)
    
    # Get lunch blocks
    lunch_blocks_dict = get_lunch_blocks()
    lunch_base = lunch_blocks_dict.get(semester)
    lunch_blocks = []
    if lunch_base:
        for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
            lunch_blocks.append(TimeBlock(day, lunch_base.start, lunch_base.end))
    
    # Try each available slot from get_available_time_slots first
    for slot in available_slots[:max_attempts]:
        # Check if faculty is available at this time in this period
        if faculty and not check_faculty_availability_in_period(
            faculty, slot.day, slot.start, slot.end, period, all_sessions, exclude_session=session
        ):
            continue
        
        # Check section overlap
        has_section_conflict = False
        for existing_data in occupied_slots.get(section_key, []):
            if isinstance(existing_data, tuple):
                existing_slot, _ = existing_data
            else:
                existing_slot = existing_data
            if slot.overlaps(existing_slot):
                has_section_conflict = True
                break
        
        if has_section_conflict:
            continue
        
        # Check lunch conflict
        lunch_conflict = False
        for lunch_block in lunch_blocks:
            if slot.day == lunch_block.day and slot.overlaps(lunch_block):
                lunch_conflict = True
                break
        
        if lunch_conflict:
            continue
        
        # Slot is available!
        return slot
    
    # AGGRESSIVE FALLBACK: If no slot found via get_available_time_slots, generate full dynamic grid
    # and check ALL possible slots within 09:00-18:00 (excluding lunch)
    from modules_v2.phase5_core_courses import generate_dynamic_time_slots
    from datetime import datetime, timedelta
    
    # Get session duration from original session
    session_block = None
    if isinstance(session, dict):
        session_block = session.get('time_block')
    else:
        session_block = getattr(session, 'block', None)
    
    if session_block:
        duration_minutes = (
            (session_block.end.hour * 60 + session_block.end.minute) -
            (session_block.start.hour * 60 + session_block.start.minute)
        )
    else:
        duration_minutes = 90  # Default to 1.5 hours
    
    # Generate all possible time slots for this semester
    all_possible_slots = generate_dynamic_time_slots(semester, start_hour=9, end_hour=18)
    
    # Filter to slots that match the session duration (within tolerance)
    matching_duration_slots = []
    for candidate_slot in all_possible_slots:
        candidate_duration = (
            (candidate_slot.end.hour * 60 + candidate_slot.end.minute) -
            (candidate_slot.start.hour * 60 + candidate_slot.start.minute)
        )
        # Accept slots within 15 minutes of target duration
        if abs(candidate_duration - duration_minutes) <= 15:
            matching_duration_slots.append(candidate_slot)
    
    # Try all matching duration slots (aggressive search)
    for slot in matching_duration_slots[:max_attempts * 2]:  # Try more slots
        # Check if faculty is available at this time in this period
        if faculty and not check_faculty_availability_in_period(
            faculty, slot.day, slot.start, slot.end, period, all_sessions, exclude_session=session
        ):
            continue
        
        # Check section overlap
        has_section_conflict = False
        for existing_data in occupied_slots.get(section_key, []):
            if isinstance(existing_data, tuple):
                existing_slot, _ = existing_data
            else:
                existing_slot = existing_data
            if slot.overlaps(existing_slot):
                has_section_conflict = True
                break
        
        if has_section_conflict:
            continue
        
        # Check lunch conflict
        lunch_conflict = False
        for lunch_block in lunch_blocks:
            if slot.day == lunch_block.day and slot.overlaps(lunch_block):
                lunch_conflict = True
                break
        
        if lunch_conflict:
            continue
        
        # Slot is available! Return it even if it wasn't in the original available_slots
        return slot
    
    return None
