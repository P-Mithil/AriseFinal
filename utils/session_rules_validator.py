"""
Session Rules Validator
Validates one-session-per-day rules for courses
"""

from typing import Dict, Set, List, Tuple
from collections import defaultdict
from utils.data_models import TimeBlock, ScheduledSession

class SessionRulesValidator:
    """Validates session scheduling rules"""
    
    @staticmethod
    def can_schedule_session_type(course_code: str, day: str, session_type: str,
                                  used_days_by_course: Dict[str, Dict[str, Set[str]]]) -> bool:
        """
        Check if a session type can be scheduled on a given day for a course.
        
        Rules:
        - Cannot have 2 lectures on same day for same course
        - Cannot have 2 tutorials on same day for same course
        - Cannot have lecture + tutorial on same day for same course
        - CAN have lecture + practical on same day
        - CAN have tutorial + practical on same day
        
        Args:
            course_code: Course code
            day: Day of week
            session_type: "L" (lecture), "T" (tutorial), "P" (practical)
            used_days_by_course: Dict mapping course_code -> day -> set of session types
        
        Returns:
            True if session can be scheduled, False otherwise
        """
        if course_code not in used_days_by_course:
            return True
        
        if day not in used_days_by_course[course_code]:
            return True
        
        used_types = used_days_by_course[course_code][day]
        
        # Rule: Cannot have 2 lectures on same day
        if session_type == "L" and "L" in used_types:
            return False
        
        # Rule: Cannot have 2 tutorials on same day
        if session_type == "T" and "T" in used_types:
            return False
        
        # Rule: Cannot have lecture + tutorial on same day
        if session_type == "L" and "T" in used_types:
            return False
        if session_type == "T" and "L" in used_types:
            return False
        
        # Rule: CAN have lecture + practical on same day
        # Rule: CAN have tutorial + practical on same day
        # (These are allowed, so no check needed)
        
        return True
    
    @staticmethod
    def mark_day_used(course_code: str, day: str, session_type: str,
                      used_days_by_course: Dict[str, Dict[str, Set[str]]]):
        """Mark a day as used for a specific session type"""
        if course_code not in used_days_by_course:
            used_days_by_course[course_code] = {}
        
        if day not in used_days_by_course[course_code]:
            used_days_by_course[course_code][day] = set()
        
        used_days_by_course[course_code][day].add(session_type)
    
    @staticmethod
    def validate_session_list(sessions: List[ScheduledSession]) -> List[Tuple[ScheduledSession, str]]:
        """
        Validate a list of sessions against one-session-per-day rules.
        
        IMPORTANT: Validates per (course_code, section, period) combination,
        not just course_code, since same course can be taught to different sections.
        
        Returns:
            List of (session, error_message) tuples for violations
        """
        violations = []
        # Track by (course_code, section, period) to handle same course for different sections/periods
        used_days_by_course_section = defaultdict(lambda: defaultdict(set))
        
        # Group sessions by (course_code, section, period)
        sessions_by_key = defaultdict(list)
        for session in sessions:
            if session.kind in ["L", "T", "P"]:  # Only validate lectures, tutorials, practicals
                section = getattr(session, 'section', 'UNKNOWN')
                period = getattr(session, 'period', 'UNKNOWN')
                key = (session.course_code, section, period)
                sessions_by_key[key].append(session)
        
        # Check each (course, section, period) combination
        for (course_code, section, period), course_sessions in sessions_by_key.items():
            key = (course_code, section, period)
            for session in course_sessions:
                if not SessionRulesValidator.can_schedule_session_type(
                    course_code, session.block.day, session.kind, used_days_by_course_section[key]
                ):
                    # Find what's already scheduled on this day for this course/section/period
                    existing_types = used_days_by_course_section[key][session.block.day]
                    if session.kind == "L" and "L" in existing_types:
                        violations.append((session, f"Course {course_code} already has a lecture on {session.block.day}"))
                    elif session.kind == "T" and "T" in existing_types:
                        violations.append((session, f"Course {course_code} already has a tutorial on {session.block.day}"))
                    elif session.kind == "L" and "T" in existing_types:
                        violations.append((session, f"Course {course_code} cannot have lecture and tutorial on same day ({session.block.day})"))
                    elif session.kind == "T" and "L" in existing_types:
                        violations.append((session, f"Course {course_code} cannot have tutorial and lecture on same day ({session.block.day})"))
                else:
                    # Mark as used if valid
                    SessionRulesValidator.mark_day_used(
                        course_code, session.block.day, session.kind, used_days_by_course_section[key]
                    )
        
        return violations
    
    @staticmethod
    def get_used_days_tracker() -> Dict[str, Dict[str, Set[str]]]:
        """Get a new tracker for used days by course"""
        return defaultdict(lambda: defaultdict(set))

def validate_one_session_per_day(sessions: List[ScheduledSession]) -> Tuple[bool, List[str]]:
    """
    Convenience function to validate sessions.
    
    Returns:
        (is_valid, list_of_error_messages)
    """
    violations = SessionRulesValidator.validate_session_list(sessions)
    
    if not violations:
        return True, []
    
    error_messages = [f"{session.course_code} ({session.section}, {session.block.day}): {msg}" 
                     for session, msg in violations]
    return False, error_messages

