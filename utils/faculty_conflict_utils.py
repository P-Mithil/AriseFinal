"""
Shared utilities for faculty conflict prevention and resolution.
Used across phases to check faculty availability before scheduling
and to resolve conflicts after scheduling.
"""

from typing import List, Dict, Optional, Tuple, Set
from datetime import time
from collections import defaultdict

from utils.data_models import TimeBlock, ScheduledSession
from config.schedule_config import WORKING_DAYS
from utils.time_validator import validate_time_range


def _parse_time_hhmmish(val) -> Optional[time]:
    """
    Parse common time representations into datetime.time.
    Accepts:
    - datetime.time
    - 'HH:MM' / 'HH:MM:SS'
    """
    if val is None:
        return None
    if isinstance(val, time):
        return val
    s = str(val).strip()
    if not s:
        return None
    # Accept "09:00" or "09:00:00"
    try:
        parts = s.split(":")
        if len(parts) >= 2:
            hh = int(parts[0])
            mm = int(parts[1])
            return time(hh, mm)
    except Exception:
        return None
    return None


def _coerce_session_timeblock_dict(sess: dict) -> Optional[TimeBlock]:
    """
    Best-effort extraction of a TimeBlock from dict sessions used across pipeline/UI exports.
    Handles either structured fields or a string like 'Monday 09:15-10:45'.
    """
    if not isinstance(sess, dict):
        return None

    tb = sess.get("time_block")
    if isinstance(tb, TimeBlock):
        return tb

    day = (sess.get("day") or sess.get("Day") or "").strip()
    st = _parse_time_hhmmish(sess.get("start_time") or sess.get("Start Time"))
    et = _parse_time_hhmmish(sess.get("end_time") or sess.get("End Time"))
    if day and st and et:
        return TimeBlock(day, st, et)

    # Parse "Monday 09:15-10:45" (and tolerate seconds)
    s = str(tb or "").strip()
    if not s:
        return None
    try:
        # Split first token as day; rest contains time range.
        parts = s.split(None, 1)
        if len(parts) != 2:
            return None
        day2 = parts[0].strip()
        rng = parts[1].strip()
        if "-" not in rng:
            return None
        a, b = [x.strip() for x in rng.split("-", 1)]
        st2 = _parse_time_hhmmish(a)
        et2 = _parse_time_hhmmish(b)
        if day2 and st2 and et2:
            return TimeBlock(day2, st2, et2)
    except Exception:
        return None
    return None


def faculty_name_tokens(faculty_raw: Optional[str]) -> List[str]:
    """
    Split comma-separated instructor lists into normalized lowercase tokens.
    Team-taught rows (e.g. 'Sunil P V, Sunil C K') must participate in overlap checks per person.
    """
    if not faculty_raw or not str(faculty_raw).strip():
        return []
    out: List[str] = []
    for part in str(faculty_raw).split(","):
        # Normalize aggressively:
        # - collapse internal whitespace
        # - lowercase for stable identity across casing differences
        p = " ".join(part.strip().split())
        if not p or p.upper() in ("TBD", "VARIOUS", "-", "MULTIPLE"):
            continue
        out.append(p.lower())
    return out


def faculty_token_set(faculty_raw: Optional[str]) -> Set[str]:
    return set(faculty_name_tokens(faculty_raw))


def try_reassign_away_from_busy_instructor(
    session,
    busy_faculty_lower: str,
    all_sessions: List,
    period: str,
) -> bool:
    """
    For team-taught sessions (comma-separated faculty), if busy_faculty_lower is double-booked,
    try assigning the session to a co-instructor who is free at the same wall-clock slot.
    """
    busy_faculty_lower = " ".join(str(busy_faculty_lower or "").strip().split()).lower()
    if not busy_faculty_lower:
        return False

    if isinstance(session, dict):
        raw = (session.get("instructor") or session.get("faculty") or "").strip()
        block = session.get("time_block")
    else:
        raw = (
            getattr(session, "faculty", None) or getattr(session, "instructor", None) or ""
        ).strip()
        block = getattr(session, "block", None)

    if not raw or not block:
        return False

    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if len(parts) < 2:
        return False

    alts = [p for p in parts if p.strip().lower() != busy_faculty_lower]
    period_n = _normalize_period(period)
    for alt in alts:
        if not check_faculty_availability_in_period(
            alt,
            block.day,
            block.start,
            block.end,
            period_n,
            all_sessions,
            exclude_session=session,
        ):
            continue
        if isinstance(session, dict):
            session["instructor"] = alt
            session["faculty"] = alt
        else:
            session.faculty = alt
        return True
    return False


def _session_section_label_for_faculty_check(session) -> str:
    if isinstance(session, dict):
        secs = session.get("sections")
        if isinstance(secs, list) and secs:
            return str(secs[0] or "").strip()
        return str(session.get("section") or "").strip()
    return str(getattr(session, "section", "") or "").strip()


def _session_course_code_for_faculty_check(session) -> str:
    if isinstance(session, dict):
        return str(session.get("course_code") or "").strip()
    return str(getattr(session, "course_code", "") or "").strip()


def check_faculty_availability_in_period(
    faculty: str,
    day: str,
    start_time: time,
    end_time: time,
    period: str,
    all_sessions: List,
    exclude_session=None,
    candidate_session_type: Optional[str] = None,
    candidate_course_code: Optional[str] = None,
    candidate_section_label: Optional[str] = None,
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
    if not faculty or str(faculty).strip().upper() in ("TBD", "VARIOUS", "-", "MULTIPLE"):
        return True
    if str(candidate_session_type or "").strip().upper() == "P":
        return True

    candidate_tokens = faculty_token_set(faculty)
    if not candidate_tokens:
        return True

    candidate_block = TimeBlock(day, start_time, end_time)

    for session in all_sessions:
        if exclude_session and session == exclude_session:
            continue

        if isinstance(session, dict):
            st = (
                session.get("session_type")
                or session.get("Session Type")
                or session.get("kind")
                or ""
            )
            if str(st).strip().upper() == "P":
                continue
            session_faculty = session.get("instructor") or session.get("faculty")
        else:
            if str(getattr(session, "kind", "")).strip().upper() == "P":
                continue
            session_faculty = getattr(session, "faculty", None) or getattr(
                session, "instructor", None
            )

        other_tokens = faculty_token_set(
            session_faculty if isinstance(session_faculty, str) else str(session_faculty or "")
        )
        if not candidate_tokens & other_tokens:
            continue

        if isinstance(session, dict):
            session_block = _coerce_session_timeblock_dict(session)
        else:
            session_block = getattr(session, "block", None)
            if not session_block:
                # Some pipeline objects may carry a `time_block` attribute (sometimes as string).
                tb = getattr(session, "time_block", None)
                if isinstance(tb, TimeBlock):
                    session_block = tb
                elif tb:
                    session_block = _coerce_session_timeblock_dict(
                        {
                            "time_block": tb,
                            "day": getattr(getattr(session, "block", None), "day", None) or "",
                        }
                    )

        if not session_block:
            continue

        # Faculty availability is enforced across the whole day, regardless of PRE/POST.
        # Strict verification treats any overlapping wall-clock time as a conflict.

        if session_block.day == day and candidate_block.overlaps(session_block):
            # Same course + different department section (e.g. ECE-A vs DSAI-A): one joint slot.
            if candidate_course_code and candidate_section_label:
                cc = str(candidate_course_code).split("-")[0].strip().upper()
                oc = _session_course_code_for_faculty_check(session).split("-")[0].strip().upper()
                osec = _session_section_label_for_faculty_check(session)
                p0 = str(candidate_section_label).split("-", 1)[0].strip().upper()
                p1 = osec.split("-", 1)[0].strip().upper() if osec else ""
                if cc and oc == cc and p0 and p1 and p0 != p1:
                    continue
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
    # Special handling for combined dict sessions:
    # they occupy MULTIPLE sections simultaneously, so slot search must ensure the slot is
    # free for *all* those sections (not just the first one).
    if isinstance(session, dict):
        try:
            from modules_v2.phase5_core_courses import generate_dynamic_time_slots
            from config.schedule_config import LUNCH_WINDOWS
            from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
        except Exception:
            generate_dynamic_time_slots = None
            LUNCH_WINDOWS = {}
            ELECTIVE_BASKET_SLOTS = {}

    from modules_v2.phase5_core_courses import (
        get_available_time_slots,
        get_lunch_blocks,
        find_alternative_slot
    )
    
    # Extract session details
    if isinstance(session, dict):
        course_code = session.get('course_code', '')
        section_list = session.get('sections') or []
        section = section_list[0] if section_list else None
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
        section_list = [section] if section else []
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

    # For combined dict sessions, prefer a full-grid search that checks ALL sections.
    # This is stricter (and more correct) than using get_available_time_slots() which is per-section.
    if isinstance(session, dict) and section_list and len(section_list) > 1 and generate_dynamic_time_slots:
        # Derive duration from original block (default 90m)
        session_block = session.get('time_block') or session.get('block')
        if session_block:
            duration_minutes = (
                (session_block.end.hour * 60 + session_block.end.minute) -
                (session_block.start.hour * 60 + session_block.start.minute)
            )
        else:
            duration_minutes = 90

        def _elective_conflict(day_val: str, cand: TimeBlock) -> bool:
            try:
                # ELECTIVE_BASKET_SLOTS keys are like "3.1", "7.2" → semester is prefix before '.'
                for gk, slots in (ELECTIVE_BASKET_SLOTS or {}).items():
                    try:
                        gk_s = str(gk)
                        gk_sem = int(gk_s.split(".", 1)[0]) if "." in gk_s else int(gk_s)
                    except Exception:
                        continue
                    if gk_sem != int(semester):
                        continue
                    if not isinstance(slots, dict):
                        continue
                    for k in ("lecture_1", "lecture_2", "tutorial"):
                        eb = slots.get(k)
                        if eb and getattr(eb, "day", None) == day_val and cand.overlaps(eb):
                            return True
            except Exception:
                return False
            return False

        def _lunch_conflict(day_val: str, cand: TimeBlock) -> bool:
            win = (LUNCH_WINDOWS or {}).get(int(semester))
            if not win:
                return False
            lb = TimeBlock(day_val, win[0], win[1])
            return cand.overlaps(lb)

        def _all_sections_free(cand: TimeBlock) -> bool:
            for sec in section_list:
                sk = f"{sec}_{period}"
                for existing_data in occupied_slots.get(sk, []):
                    existing_slot = existing_data[0] if isinstance(existing_data, tuple) else existing_data
                    if existing_slot and cand.overlaps(existing_slot):
                        return False
            return True

        # Candidate slots: full semester grid filtered by duration.
        all_possible = generate_dynamic_time_slots(int(semester)) or []
        tried = 0
        for cand in all_possible:
            if tried >= max_attempts * 6:
                break
            tried += 1
            cand_dur = (
                (cand.end.hour * 60 + cand.end.minute) -
                (cand.start.hour * 60 + cand.start.minute)
            )
            if abs(cand_dur - duration_minutes) > 15:
                continue
            if not validate_time_range(cand.start, cand.end):
                continue
            if _lunch_conflict(str(cand.day), cand):
                continue
            if _elective_conflict(str(cand.day), cand):
                continue
            if faculty and not check_faculty_availability_in_period(
                faculty, cand.day, cand.start, cand.end, period, all_sessions, exclude_session=session
            ):
                continue
            if not _all_sections_free(cand):
                continue
            return cand
    
    # Get available slots for this section/period
    section_key = f"{section}_{period}" if section else f"UNKNOWN_{period}"
    available_slots = get_available_time_slots(semester, occupied_slots, course_code, section, period)
    
    # Get lunch blocks
    lunch_blocks_dict = get_lunch_blocks()
    lunch_base = lunch_blocks_dict.get(semester)
    lunch_blocks = []
    if lunch_base:
        for day in WORKING_DAYS:
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

        if not validate_time_range(slot.start, slot.end):
            continue

        # Slot is available!
        return slot

    # AGGRESSIVE FALLBACK: If no slot found via get_available_time_slots, generate full dynamic grid
    # and check ALL possible slots within the configured working window (excluding lunch)
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
    
    # Generate all possible time slots for this semester (config-driven window)
    all_possible_slots = generate_dynamic_time_slots(semester)
    
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

        if not validate_time_range(slot.start, slot.end):
            continue

        # Slot is available! Return it even if it wasn't in the original available_slots
        return slot

    return None
