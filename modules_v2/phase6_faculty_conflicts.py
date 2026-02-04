"""
Phase 6: Faculty Conflict Detection
Ensures no faculty member is double-booked (assigned to multiple classes at the same time).
"""

from typing import List, Dict, Tuple, Set
from dataclasses import dataclass
from datetime import time
import pandas as pd

@dataclass
class FacultyConflict:
    faculty_name: str
    time_slot: str
    day: str
    conflicting_sessions: List[str]
    conflict_type: str  # "DOUBLE_BOOKING" or "OVERLAP"

def detect_faculty_conflicts(all_sessions: List) -> List[FacultyConflict]:
    """
    Detect faculty conflicts - IGNORE PreMid/PostMid overlaps, NO DUPLICATES.
    Only reports conflicts where same faculty teaches different courses/sections 
    at the same time within the same period.
    
    Args:
        all_sessions: List of all scheduled sessions from all phases
        
    Returns:
        List of FacultyConflict objects representing detected conflicts
    """
    from collections import defaultdict
    
    # Group sessions by faculty, time, AND period
    faculty_schedule = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    
    for session in all_sessions:
        # Handle both ScheduledSession objects and dictionaries
        if isinstance(session, dict):
            # Combined sessions are dictionaries - they don't have faculty conflicts
            continue
        if session.kind == 'P':  # Skip labs
            continue
        
        faculty = getattr(session, 'faculty', '')
        if not faculty or faculty in ['TBD', 'Various']:
            continue
        
        period = getattr(session, 'period', 'UNKNOWN')
        time_key = f"{session.block.day}_{session.block.start}_{session.block.end}"
        
        # Group by faculty -> time -> period
        faculty_schedule[faculty][time_key][period].append(session)
    
    # Find conflicts - only within same period
    conflicts = []
    for faculty, time_slots in faculty_schedule.items():
        for time_key, periods in time_slots.items():
            # Check each period separately
            for period, sessions in periods.items():
                if len(sessions) > 1:
                    # Check if different courses/sections (use set to eliminate duplicates)
                    unique_sessions = {}
                    for s in sessions:
                        key = f"{s.course_code}_{s.section}"
                        if key not in unique_sessions:
                            unique_sessions[key] = s
                    
                    # Only report if more than one unique course/section
                    if len(unique_sessions) > 1:
                        # Real conflict - same faculty, same time, same period, different courses/sections
                        day, start, end = time_key.split('_')
                        conflicts.append(FacultyConflict(
                            faculty_name=faculty,
                            time_slot=f"{day} {start}-{end} ({period})",
                            day=day,
                            conflicting_sessions=list(unique_sessions.keys()),
                            conflict_type="DOUBLE_BOOKING"
                        ))
    
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
        report += f"  Time: {conflict.day} {conflict.time_slot}\n"
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
