"""
Phase 6: Faculty Conflict Detection
Ensures no faculty member is double-booked (assigned to multiple classes at the same time).
"""

from typing import List, Dict, Tuple, Set, Any
from dataclasses import dataclass
from datetime import time
from collections.abc import Mapping

from utils.data_models import ScheduledSession
from utils.period_utils import normalize_period
from utils.faculty_conflict_utils import faculty_name_tokens

@dataclass
class FacultyConflict:
    faculty_name: str
    time_slot: str
    day: str
    conflicting_sessions: List[str]
    conflict_type: str  # "DOUBLE_BOOKING" or "OVERLAP"


def _is_session_mapping(session: Any) -> bool:
    """True for dict / UserDict / other Mappings; False for ScheduledSession."""
    if session is None:
        return False
    if isinstance(session, ScheduledSession):
        return False
    if isinstance(session, dict):
        return True
    return isinstance(session, Mapping)


def _session_course_section_key(session: Any) -> str:
    """Stable key for conflict dedup: ScheduledSession or combined-class dict / mapping."""
    if _is_session_mapping(session):
        cc = str(
            session.get("course_code")
            or session.get("Course Code")
            or ""
        ).strip()
        secs = session.get("sections") or session.get("Section")
        if isinstance(secs, str) and secs:
            secs = [x.strip() for x in secs.split(",") if x.strip()]
        if isinstance(secs, (list, tuple)) and secs:
            sec = str(secs[0]).strip()
        else:
            sec = str(secs).strip() if secs else ""
        return f"{cc}_{sec}"
    return f"{getattr(session, 'course_code', '')}_{getattr(session, 'section', '')}"


def detect_faculty_conflicts(all_sessions: List) -> List[FacultyConflict]:
    """
    Detect faculty conflicts using the same overlap semantics as post-generate verification:
    same instructor, same normalized period (PRE/POST), overlapping TimeBlocks on the same day,
    different course/section keys. Partial overlaps (e.g. 08:00-09:30 vs 09:00-10:30) are included.
    """
    from collections import defaultdict

    # (faculty_lower, period_norm, day) -> list of (session, block, course_section_key)
    by_bucket: Dict[Tuple[str, str, str], List[Tuple[Any, Any, str]]] = defaultdict(list)

    for session in all_sessions:
        if _is_session_mapping(session):
            st = (
                session.get("session_type")
                or session.get("Session Type")
                or session.get("kind")
                or "L"
            )
            if str(st).strip().upper() == "P":
                continue
            faculty = (
                session.get("instructor")
                or session.get("faculty")
                or session.get("Faculty")
                or ""
            )
            if isinstance(faculty, str):
                faculty = faculty.strip()
            if not faculty or str(faculty).upper() in ["TBD", "VARIOUS", ""]:
                continue
            tb = session.get("time_block") or session.get("block")
            if not tb:
                continue
            raw_period = session.get("period") or session.get("Period")
            period_norm = normalize_period(raw_period)
            for fac_lower in faculty_name_tokens(str(faculty)):
                by_bucket[(fac_lower, period_norm, str(tb.day))].append(
                    (session, tb, _session_course_section_key(session))
                )
            continue

        faculty = getattr(session, "faculty", "") or getattr(session, "instructor", "")
        if str(getattr(session, "kind", "")).strip().upper() == "P":
            continue
        if isinstance(faculty, str):
            faculty = faculty.strip()
        if not faculty or faculty in ["TBD", "Various", "-"]:
            continue
        tb = getattr(session, "block", None)
        if not tb:
            continue
        period_norm = normalize_period(getattr(session, "period", None))
        for fac_lower in faculty_name_tokens(str(faculty)):
            by_bucket[(fac_lower, period_norm, str(tb.day))].append(
                (session, tb, _session_course_section_key(session))
            )

    conflicts: List[FacultyConflict] = []
    seen_pair: Set[Tuple[str, str, frozenset]] = set()

    for (faculty_lower, period_norm, day), items in by_bucket.items():
        n = len(items)
        for i in range(n):
            _sess_i, block_i, key_i = items[i]
            for j in range(i + 1, n):
                _sess_j, block_j, key_j = items[j]
                if _sess_i is _sess_j:
                    continue
                if key_i == key_j:
                    continue
                if not block_i.overlaps(block_j):
                    continue
                dedup = (faculty_lower, day, frozenset((key_i, key_j)))
                if dedup in seen_pair:
                    continue
                seen_pair.add(dedup)

                ov_start = max(block_i.start, block_j.start)
                ov_end = min(block_i.end, block_j.end)
                time_slot = (
                    f"{day} {ov_start.strftime('%H:%M:%S')}-{ov_end.strftime('%H:%M:%S')} "
                    f"({period_norm})"
                )
                conflicts.append(
                    FacultyConflict(
                        faculty_name=faculty_lower,
                        time_slot=time_slot,
                        day=day,
                        conflicting_sessions=sorted([key_i, key_j]),
                        conflict_type="OVERLAP",
                    )
                )

    return conflicts

def check_faculty_availability(faculty: str, day: str, start_time: time, end_time: time, 
                              all_sessions: List, exclude_session=None) -> bool:
    """
    Check if a faculty member is available at a specific time.
    
    Args:
        faculty: Faculty member name
        day: Day of the week
        start_time: Session start time
        end_time: Session end time
        all_sessions: List of all scheduled sessions
        exclude_session: Session to exclude from conflict check (for rescheduling)
        
    Returns:
        True if faculty is available, False if there's a conflict
    """
    if not faculty or faculty == 'TBD' or faculty == '-':
        return True
        
    for session in all_sessions:
        if exclude_session and session == exclude_session:
            continue
            
        session_faculty = getattr(session, 'faculty', '')
        if session_faculty != faculty:
            continue
            
        # Check for time overlap
        if (session.block.day == day and 
            session.block.start < end_time and 
            session.block.end > start_time):
            return False
            
    return True

def get_faculty_schedule(faculty: str, all_sessions: List) -> List:
    """
    Get all sessions for a specific faculty member.
    
    Args:
        faculty: Faculty member name
        all_sessions: List of all scheduled sessions
        
    Returns:
        List of sessions for the faculty member
    """
    faculty_sessions = []
    
    for session in all_sessions:
        session_faculty = getattr(session, 'faculty', '')
        if session_faculty == faculty:
            faculty_sessions.append(session)
            
    return faculty_sessions

def generate_faculty_conflict_report(conflicts: List[FacultyConflict]) -> str:
    """
    Generate a detailed report of faculty conflicts.
    
    Args:
        conflicts: List of FacultyConflict objects
        
    Returns:
        Formatted conflict report string
    """
    if not conflicts:
        return "[OK] NO FACULTY CONFLICTS DETECTED\nAll faculty members are properly scheduled without double-booking."
    
    report = "[FAIL] FACULTY CONFLICTS DETECTED\n"
    report += "=" * 50 + "\n\n"
    
    for i, conflict in enumerate(conflicts, 1):
        report += f"Conflict #{i}: {conflict.faculty_name}\n"
        # time_slot already includes day (required by resolver parser)
        report += f"  Time: {conflict.time_slot}\n"
        report += f"  Type: {conflict.conflict_type}\n"
        report += f"  Conflicting Sessions: {', '.join(conflict.conflicting_sessions)}\n\n"
    
    return report

def run_phase6_faculty_conflicts(all_sessions: List) -> Tuple[List[FacultyConflict], str]:
    """
    Run Phase 6: Faculty conflict detection.
    
    Args:
        all_sessions: List of all scheduled sessions from all phases
        
    Returns:
        Tuple of (conflicts_list, report_string)
    """
    print("=== PHASE 6: FACULTY CONFLICT DETECTION ===")
    
    # Detect conflicts
    conflicts = detect_faculty_conflicts(all_sessions)
    
    # Generate report
    report = generate_faculty_conflict_report(conflicts)
    
    print(f"Faculty conflict detection completed:")
    print(f"  - Total conflicts found: {len(conflicts)}")
    print(f"  - Total sessions analyzed: {len(all_sessions)}")
    
    if conflicts:
        print("\n" + report)
    else:
        print("[OK] All faculty members are properly scheduled without conflicts!")
    
    return conflicts, report
