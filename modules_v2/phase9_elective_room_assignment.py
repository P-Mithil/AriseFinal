"""
Phase 9: Elective Room Assignment

Assigns rooms and periods to elective courses for each semester.
Checks room availability at elective time slots and resolves faculty conflicts.
"""

import os
import sys
from datetime import time
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
import math

# Add the parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_models import Course, ClassRoom, TimeBlock, ScheduledSession
from modules_v2.phase3_elective_baskets_v2 import group_electives_by_semester, ELECTIVE_BASKET_SLOTS


def get_electives_by_semester(courses: List[Course]) -> Dict[int, List[Course]]:
    """Get all elective courses grouped by semester (1, 3, 5)"""
    return group_electives_by_semester(courses)

def get_electives_by_group(courses: List[Course]) -> Dict[str, List[Course]]:
    """
    Group electives by elective_group (e.g., '5.1', '5.2') instead of just semester.
    This matches the basket grouping in Phase 3.
    
    Returns:
        Dict mapping group_key (e.g., '5.1', '5.2') to list of courses
    """
    electives_by_group = {}
    for course in courses:
        if not course.is_elective:
            continue
        # Use elective_group if available, otherwise fallback to semester
        group_key = course.elective_group if course.elective_group else str(course.semester)
        if group_key not in electives_by_group:
            electives_by_group[group_key] = []
        electives_by_group[group_key].append(course)
    return electives_by_group


def get_elective_time_slots(semester: int) -> Optional[Dict[str, TimeBlock]]:
    """Get elective basket time slots for a semester from ELECTIVE_BASKET_SLOTS
    Returns slots from the first group found for this semester (for backward compatibility)
    """
    # Find groups for this semester
    def extract_semester_from_group(gk: str) -> int:
        try:
            if '.' in str(gk):
                return int(str(gk).split('.')[0])
            else:
                return int(gk)
        except (ValueError, AttributeError):
            return -1
    
    matching_groups = [gk for gk in ELECTIVE_BASKET_SLOTS.keys() 
                      if extract_semester_from_group(gk) == semester]
    
    if not matching_groups:
        return None
    
    # Use first group found (for backward compatibility with existing code)
    # In practice, electives are assigned to specific groups, so this is just for time slot lookup
    group_key = matching_groups[0]
    slots = ELECTIVE_BASKET_SLOTS[group_key]
    return {
        'lecture_1': slots.get('lecture_1'),
        'lecture_2': slots.get('lecture_2'),
        'tutorial': slots.get('tutorial')
    }


def _normalize_period(raw: str) -> str:
    """Normalize period strings to 'PRE' or 'POST' (PreMid->PRE, PostMid->POST)."""
    if not raw:
        return "PRE"
    val = str(raw).strip().upper()
    if val in ("PREMID", "PRE"):
        return "PRE"
    if val in ("POSTMID", "POST"):
        return "POST"
    return val


def check_room_availability_at_time(room_number: str, period: str, day: str, 
                                   start: time, end: time,
                                   all_sessions: List, room_assignments: Dict,
                                   elective_room_occupancy: Dict = None) -> bool:
    """
    Check if a room is available at a specific time slot.
    Capacity filtering (including 240-seaters) is handled by the callers; this
    helper purely checks time overlaps against existing sessions/assignments.

    Args:
        room_number: Room to check
        period: 'PRE' or 'POST'
        day: Day of week
        start: Start time
        end: End time
        all_sessions: All scheduled sessions from all phases
        room_assignments: Room assignments from Phase 8
        elective_room_occupancy: Optional dict period -> room -> day -> [(start, end, course_code)]
                                for electives already assigned in this Phase 9 run
    
    Returns:
        True if room is available, False otherwise
    """
    if room_number in ['C004']:
        return False
    period_norm = _normalize_period(period)
    # Check elective occupancy from this run (electives not yet in all_sessions with room)
    # FULL-period electives store under 'FULL'; when searching with PRE/POST we must still see them
    if elective_room_occupancy:
        for check_period in (period_norm, 'FULL'):
            if check_period in elective_room_occupancy and room_number in elective_room_occupancy[check_period]:
                if day in elective_room_occupancy[check_period][room_number]:
                    for occupied_start, occupied_end, _ in elective_room_occupancy[check_period][room_number][day]:
                        if not (end <= occupied_start or start >= occupied_end):
                            return False
    for session in all_sessions:
        session_period = None
        session_day = None
        session_start = None
        session_end = None
        session_room = None
        if isinstance(session, dict):
            session_period = session.get('period', '')
            session_block = session.get('time_block')
            session_room = session.get('room', '')
            if session_block:
                session_day = session_block.day
                session_start = session_block.start
                session_end = session_block.end
        elif hasattr(session, 'period'):
            session_period = session.period
            session_room = getattr(session, 'room', '')
            if hasattr(session, 'block') and session.block:
                session_day = session.block.day
                session_start = session.block.start
                session_end = session.block.end
        if session_room == room_number and _normalize_period(session_period) == period_norm:
            if session_day == day and session_start and session_end:
                # Check time overlap
                if not (end <= session_start or start >= session_end):
                    return False
    
    if room_assignments:
        for key, assignment in room_assignments.items():
            if isinstance(key, tuple) and len(key) >= 3:
                course_code, section, assign_period = key[0], key[1], key[2]
                if _normalize_period(assign_period) == period_norm:
                    assigned_room = assignment.get('classroom', '')
                    if assigned_room == room_number:
                        # Check if this assignment conflicts with our time
                        # Note: room_assignments may not have time info, so we'll be conservative
                        # and assume it might conflict if room is assigned
                        pass  # We'll rely on session checks above
    
    return True


def get_faculty_course_count(faculty_name: str, period: str, all_sessions: List, 
                            all_courses: List[Course]) -> int:
    """
    Count total courses (including electives) taught by a faculty in a period.
    Checks all phases (3, 4, 5, 7) for that period.
    
    Args:
        faculty_name: Name of faculty to check
        period: 'PRE' or 'POST'
        all_sessions: All scheduled sessions from all phases
        all_courses: All courses (to look up instructors)
    
    Returns:
        Number of unique courses taught by this faculty in this period
    """
    count = 0
    faculty_courses = set()  # Track unique courses
    
    # Create a mapping of course code to course object
    course_map = {course.code: course for course in all_courses}
    
    # Check all sessions
    for session in all_sessions:
        session_period = None
        session_course = None
        
        if isinstance(session, dict):
            session_period = session.get('period', '')
            session_course = session.get('course_code', '')
        elif hasattr(session, 'period'):
            session_period = session.period
            session_course = getattr(session, 'course_code', '')
        
        # Check if this session is for this period
        if session_period != period:
            continue
        
        if not session_course:
            continue
        
        # Remove suffixes like -TUT, -LAB
        base_course_code = session_course.split('-')[0]
        
        # Check if this course is taught by this faculty
        course = course_map.get(base_course_code)
        if course:
            instructors = getattr(course, 'instructors', [])
            if instructors and faculty_name in instructors:
                # This faculty teaches this course
                if base_course_code not in faculty_courses:
                    faculty_courses.add(base_course_code)
                    count += 1
    
    return count


def get_faculty_elective_count(faculty_name: str, period: str, 
                               elective_assignments: List[Dict]) -> int:
    """Count how many electives a faculty teaches in a period"""
    count = 0
    # Normalize faculty name for comparison
    faculty_name_normalized = faculty_name.strip().upper()
    
    for assignment in elective_assignments:
        if assignment.get('period') == period:
            faculty = assignment.get('faculty', '')
            faculty_normalized = faculty.strip().upper()
            # Check if faculty names match (exact or contains)
            if (faculty_name_normalized == faculty_normalized or 
                faculty_name_normalized in faculty_normalized or 
                faculty_normalized in faculty_name_normalized):
                count += 1
    return count


def find_suitable_room(capacity_needed: int, period: str, time_slots: Dict[str, TimeBlock],
                      all_sessions: List, room_assignments: Dict,
                      classrooms: List[ClassRoom],
                      already_assigned_rooms: set = None,
                      elective_room_occupancy: Dict = None) -> Optional[str]:
    """
    Find a suitable room that's available at all elective time slots.
    Rooms already assigned to other electives in the same period are excluded.
    
    Args:
        already_assigned_rooms: Set of room numbers already assigned to electives in this period
        elective_room_occupancy: Optional dict period -> room -> day -> [(start, end, course_code)] for this run
    """
    if already_assigned_rooms is None:
        already_assigned_rooms = set()
    
    # Filter rooms: exclude labs, capacity >= capacity_needed, not already assigned.
    # 240‑seater rooms (capacity >= 240) are now allowed here so electives can
    # legitimately use large halls when their registered_students demand it.
    suitable_rooms = [room for room in classrooms 
                     if room.room_type.lower() != 'lab' 
                     and 'lab' not in room.room_type.lower()
                     and room.room_number not in already_assigned_rooms
                     and room.capacity >= capacity_needed]
    
    # Sort: prefer <120 capacity first, then others (both sorted by capacity ascending)
    suitable_rooms.sort(key=lambda r: (r.capacity >= 120, r.capacity))
    
    # Check each room for availability at all time slots
    for room in suitable_rooms:
        room_available = True
        
        # Check all three time slots
        for slot_name, time_block in time_slots.items():
            if not time_block:
                continue
            
            if not check_room_availability_at_time(
                room.room_number, period, time_block.day,
                time_block.start, time_block.end,
                all_sessions, room_assignments,
                elective_room_occupancy=elective_room_occupancy
            ):
                room_available = False
                break
        
        if room_available:
            return room.room_number
    
    return None


def assign_electives_to_rooms_and_periods(elective_courses: List[Course], semester: int,
                                         all_sessions: List, room_assignments: Dict,
                                         classrooms: List[ClassRoom],
                                         all_courses: List[Course] = None) -> List[Dict]:
    """
    Main assignment logic for electives.
    
    Returns list of assignments: [{'course': Course, 'room': str, 'period': str, 'faculty': str}, ...]
    """
    if not elective_courses:
        return []
    
    # Get elective time slots
    time_slots = get_elective_time_slots(semester)
    if not time_slots:
        return []
    
    # Helper: determine capacity needed for a single elective from its registered_students.
    # Falls back to a modest default if data is missing or zero, so we never request a
    # gigantic room based on arbitrary constants like 80.
    def _capacity_for_course(course: Course) -> int:
        raw = getattr(course, "registered_students", None)
        try:
            value = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            value = 0
        # Use real registered_students when available; otherwise fall back to 40
        # as a reasonable small-class default.
        return value if value and value > 0 else 40
    
    # Initialize assignments
    assignments = []
    premid_count = 0
    postmid_count = 0
    
    # Track rooms assigned to electives in each period to avoid conflicts
    premid_assigned_rooms = set()
    postmid_assigned_rooms = set()
    
    # Track elective room occupancy in this run: period -> room -> day -> [(start, end, course_code)]
    # So later electives see earlier assignments when checking availability
    elective_room_occupancy = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    
    def add_elective_to_occupancy(period_norm: str, room_number: str, time_slots: Dict, course_code: str):
        for slot_name, time_block in time_slots.items():
            if not time_block:
                continue
            elective_room_occupancy[period_norm][room_number][time_block.day].append(
                (time_block.start, time_block.end, course_code)
            )
    
    def remove_elective_from_occupancy(period_norm: str, room_number: str, time_slots: Dict, course_code: str):
        for slot_name, time_block in time_slots.items():
            if not time_block:
                continue
            day = time_block.day
            if period_norm in elective_room_occupancy and room_number in elective_room_occupancy[period_norm] and day in elective_room_occupancy[period_norm][room_number]:
                elective_room_occupancy[period_norm][room_number][day] = [
                    (s, e, c) for s, e, c in elective_room_occupancy[period_norm][room_number][day] if c != course_code
                ]
    
    # First pass: Try to assign all electives
    for course in elective_courses:
        instructors = getattr(course, 'instructors', [])
        faculty = ', '.join(instructors) if instructors else 'TBD'
        
        # For credits > 2, set period to "FULL" (full semester)
        if course.credits > 2:
            period = 'FULL'
            capacity_needed = _capacity_for_course(course)
            # For FULL semester, try to find room in either period (use PRE as default for room search)
            room = find_suitable_room(capacity_needed, 'PRE', time_slots, all_sessions, room_assignments, classrooms, premid_assigned_rooms, elective_room_occupancy)
            if not room:
                room = find_suitable_room(capacity_needed, 'POST', time_slots, all_sessions, room_assignments, classrooms, postmid_assigned_rooms, elective_room_occupancy)
        else:
            # For credits <= 2, use existing logic (assign to PRE or POST)
            # Determine preferred period based on faculty conflicts and balance
            # Start with balanced preference (alternate or based on current counts)
            preferred_period = 'PRE' if premid_count <= postmid_count else 'POST'
            
            # Check faculty course count AND time conflicts in PreMid/PostMid
            # Prefer period where faculty has fewer conflicts
            if instructors and all_courses:
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
                
                # Extract semester from course
                semester_for_group = getattr(course, 'semester', semester) if hasattr(course, 'semester') else semester
                
                # Find elective basket slots for this semester
                basket_slots_for_semester = {}
                for group_key, slots in ELECTIVE_BASKET_SLOTS.items():
                    try:
                        if '.' in str(group_key):
                            group_sem = int(str(group_key).split('.')[0])
                        else:
                            group_sem = int(group_key)
                        if group_sem == semester_for_group:
                            basket_slots_for_semester = slots
                            break
                    except:
                        pass
                
                for instructor in instructors:
                    premid_course_count = get_faculty_course_count(instructor, 'PRE', all_sessions, all_courses)
                    postmid_course_count = get_faculty_course_count(instructor, 'POST', all_sessions, all_courses)
                    
                    # Check for time conflicts at elective basket slots
                    premid_has_time_conflict = False
                    postmid_has_time_conflict = False
                    
                    if basket_slots_for_semester:
                        # Check lecture slots
                        for slot_name in ['lecture_1', 'lecture_2', 'tutorial']:
                            slot = basket_slots_for_semester.get(slot_name)
                            if slot:
                                # Check PreMid
                                if not check_faculty_availability_in_period(
                                    instructor, slot.day, slot.start, slot.end, 'PRE', all_sessions
                                ):
                                    premid_has_time_conflict = True
                                # Check PostMid
                                if not check_faculty_availability_in_period(
                                    instructor, slot.day, slot.start, slot.end, 'POST', all_sessions
                                ):
                                    postmid_has_time_conflict = True
                    
                    # Prefer period with fewer conflicts
                    if premid_has_time_conflict and not postmid_has_time_conflict:
                        preferred_period = 'POST'
                        break
                    elif postmid_has_time_conflict and not premid_has_time_conflict:
                        preferred_period = 'PRE'
                        break
                    # If both or neither have time conflicts, use course count logic
                    elif premid_course_count >= 3:
                        preferred_period = 'POST'
                        break
                    elif postmid_course_count >= 3:
                        preferred_period = 'PRE'
                        break
                    elif premid_course_count >= 2 and postmid_course_count < premid_course_count:
                        preferred_period = 'POST'
                        break
            
            # Try preferred period first
            period = preferred_period
            capacity_needed = _capacity_for_course(course)
            assigned_rooms_set = premid_assigned_rooms if period == 'PRE' else postmid_assigned_rooms
            room = find_suitable_room(capacity_needed, period, time_slots, all_sessions, room_assignments, classrooms, assigned_rooms_set, elective_room_occupancy)
        
            # If no room in preferred period, try other period
            if not room:
                period = 'POST' if preferred_period == 'PRE' else 'PRE'
                assigned_rooms_set = premid_assigned_rooms if period == 'PRE' else postmid_assigned_rooms
                room = find_suitable_room(capacity_needed, period, time_slots, all_sessions, room_assignments, classrooms, assigned_rooms_set, elective_room_occupancy)
            
            # If still no room, try to balance periods
            if not room:
                # Try to balance between PRE and POST
                if premid_count <= postmid_count:
                    period = 'PRE'
                    assigned_rooms_set = premid_assigned_rooms
                    room = find_suitable_room(capacity_needed, period, time_slots, all_sessions, room_assignments, classrooms, assigned_rooms_set, elective_room_occupancy)
                else:
                    period = 'POST'
                    assigned_rooms_set = postmid_assigned_rooms
                    room = find_suitable_room(capacity_needed, period, time_slots, all_sessions, room_assignments, classrooms, assigned_rooms_set, elective_room_occupancy)
            
            # If still no room, be more aggressive - try any available room (even if slightly over capacity)
            if not room:
                # Try all periods with relaxed capacity requirements
                for try_period in ['PRE', 'POST']:
                    assigned_rooms_set = premid_assigned_rooms if try_period == 'PRE' else postmid_assigned_rooms
                    
                    # Try with relaxed capacity (50% of expected)
                    relaxed_room = find_suitable_room(
                        max(10, int(capacity_needed * 0.5)),  # At least 10, or 50% of needed
                        try_period, time_slots, all_sessions, room_assignments, classrooms, assigned_rooms_set, elective_room_occupancy
                    )
                    
                    if relaxed_room:
                        room = relaxed_room
                        period = try_period
                        break
            
            # If STILL no room, try any classroom (excluding labs and already assigned)
            if not room:
                for try_period in ['PRE', 'POST']:
                    assigned_rooms_set = premid_assigned_rooms if try_period == 'PRE' else postmid_assigned_rooms
                    
                    for classroom in classrooms:
                        if (classroom.room_type.lower() != 'lab' 
                            and 'lab' not in classroom.room_type.lower()
                            and classroom.room_number not in assigned_rooms_set):
                            
                            # Check if available at all time slots
                            room_available = True
                            for slot_name, time_block in time_slots.items():
                                if not time_block:
                                    continue
                                if not check_room_availability_at_time(
                                    classroom.room_number, try_period, time_block.day,
                                    time_block.start, time_block.end,
                                    all_sessions, room_assignments,
                                    elective_room_occupancy=elective_room_occupancy
                                ):
                                    room_available = False
                                    break
                            
                            if room_available:
                                room = classroom.room_number
                                period = try_period
                                break
                    
                    if room:
                        break
            
            # No room found: leave room unset so room conflict resolver can assign (or reschedule) later
            # Do NOT assign a conflicting room on purpose
            if not room:
                period = period if period else ('FULL' if course.credits > 2 else 'PRE')
                print(f"  INFO: No free room for {getattr(course, 'code', 'Unknown')} in {period}; resolver will assign later.")
        
        assignment = {
            'course': course,
            'room': room,  # None when no room found; resolver will assign
            'period': period if period else ('FULL' if course.credits > 2 else 'PRE'),
            'faculty': faculty
        }
        assignments.append(assignment)
        
        if room:
            # Mark room as assigned for this period so each elective course gets its own room
            if period == 'PRE':
                premid_assigned_rooms.add(room)
                premid_count += 1
            elif period == 'POST':
                postmid_assigned_rooms.add(room)
                postmid_count += 1
            elif period == 'FULL':
                premid_assigned_rooms.add(room)
                postmid_assigned_rooms.add(room)
            # Add this elective's slots to occupancy so later electives see it (and get a different room)
            period_norm = _normalize_period(period)
            course_code = getattr(course, 'code', '') or ''
            add_elective_to_occupancy(period_norm, room, time_slots, course_code)
    
    # Second pass: Resolve faculty conflicts (2+ electives in same period)
    # Group assignments by faculty and period to identify conflicts
    faculty_period_map = {}  # {(faculty_normalized, period): [assignments]}
    
    for assignment in assignments:
        # Skip TBD assignments (but we shouldn't have any now)
        if assignment.get('room') == 'TBD' or assignment.get('period') == 'TBD':
            continue
        
        faculty = assignment.get('faculty', '')
        period = assignment.get('period', '')
        
        if not faculty or faculty == 'TBD':
            continue
        
        # Normalize faculty name for consistent grouping
        # Handle multiple instructors (comma-separated)
        faculty_normalized = faculty.strip().upper()
        # If multiple instructors, use the first instructor for conflict checking
        if ',' in faculty_normalized:
            faculty_normalized = faculty_normalized.split(',')[0].strip()
        
        # Normalize spaces (multiple spaces to single space)
        faculty_normalized = ' '.join(faculty_normalized.split())
        
        key = (faculty_normalized, period)
        if key not in faculty_period_map:
            faculty_period_map[key] = []
        faculty_period_map[key].append(assignment)
    
    # For each faculty with 2+ electives in same period, move at least one to other period
    for (faculty_normalized, period), faculty_assignments in faculty_period_map.items():
        if len(faculty_assignments) >= 2:
            # This faculty has 2+ electives in this period - need to move some
            other_period = 'POST' if period == 'PRE' else 'PRE'
            
            # Calculate how many to move (move at least 1, or half if more than 2)
            num_to_move = max(1, len(faculty_assignments) // 2)
            
            print(f"  DEBUG: Faculty '{faculty_normalized}' has {len(faculty_assignments)} electives in {period}, moving {num_to_move} to {other_period}")
            
            moved_count = 0
            for assignment in faculty_assignments:
                if moved_count >= num_to_move:
                    break
                
                course = assignment.get('course')
                if not course:
                    continue
                
                capacity_needed = _capacity_for_course(course)
                
                # Get assigned rooms set for other period
                other_assigned_rooms = premid_assigned_rooms if other_period == 'PRE' else postmid_assigned_rooms
                
                # Try to find room in other period (excluding already assigned rooms)
                other_room = find_suitable_room(capacity_needed, other_period, time_slots, all_sessions, room_assignments, classrooms, other_assigned_rooms, elective_room_occupancy)
                
                if other_room:
                    # Move to other period
                    course_code = getattr(course, 'code', 'Unknown')
                    print(f"    Moved {course_code} from {period} to {other_period} (room: {other_room})")
                    
                    # Remove from old period's assigned rooms and occupancy
                    old_room = assignment.get('room')
                    if old_room:
                        if period == 'PRE':
                            premid_assigned_rooms.discard(old_room)
                        else:
                            postmid_assigned_rooms.discard(old_room)
                        remove_elective_from_occupancy(_normalize_period(period), old_room, time_slots, course_code)
                    
                    assignment['period'] = other_period
                    assignment['room'] = other_room
                    
                    # Add to new period's assigned rooms and occupancy
                    if other_period == 'PRE':
                        premid_assigned_rooms.add(other_room)
                    else:
                        postmid_assigned_rooms.add(other_room)
                    add_elective_to_occupancy(_normalize_period(other_period), other_room, time_slots, course_code)
                    
                    moved_count += 1
                    
                    # Update counts
                    if period == 'PRE':
                        premid_count -= 1
                        postmid_count += 1
                    else:
                        postmid_count -= 1
                        premid_count += 1
                else:
                    # If no room found, try to be more aggressive - check if we can use a larger room
                    # or try to find any available room (even if slightly over capacity)
                    # But still exclude already assigned rooms, 240-seaters, and labs
                    for room in classrooms:
                        if (room.room_type.lower() != 'lab' 
                            and 'lab' not in room.room_type.lower()
                            and room.room_number not in other_assigned_rooms and
                            room.capacity >= capacity_needed * 0.8):  # Allow 80% capacity
                            
                            # Check if available at all time slots
                            room_available = True
                            for slot_name, time_block in time_slots.items():
                                if not time_block:
                                    continue
                                if not check_room_availability_at_time(
                                    room.room_number, other_period, time_block.day,
                                    time_block.start, time_block.end,
                                    all_sessions, room_assignments,
                                    elective_room_occupancy=elective_room_occupancy
                                ):
                                    room_available = False
                                    break
                            
                            if room_available:
                                course_code = getattr(course, 'code', 'Unknown')
                                print(f"    Moved {course_code} from {period} to {other_period} (room: {room.room_number}, aggressive search)")
                                
                                # Remove from old period's assigned rooms and occupancy
                                old_room = assignment.get('room')
                                if old_room:
                                    if period == 'PRE':
                                        premid_assigned_rooms.discard(old_room)
                                    else:
                                        postmid_assigned_rooms.discard(old_room)
                                    remove_elective_from_occupancy(_normalize_period(period), old_room, time_slots, course_code)
                                
                                assignment['period'] = other_period
                                assignment['room'] = room.room_number
                                
                                # Add to new period's assigned rooms and occupancy
                                if other_period == 'PRE':
                                    premid_assigned_rooms.add(room.room_number)
                                else:
                                    postmid_assigned_rooms.add(room.room_number)
                                add_elective_to_occupancy(_normalize_period(other_period), room.room_number, time_slots, course_code)
                                
                                moved_count += 1
                                
                                # Update counts
                                if period == 'PRE':
                                    premid_count -= 1
                                    postmid_count += 1
                                else:
                                    postmid_count -= 1
                                    premid_count += 1
                                break
            
            if moved_count == 0 and len(faculty_assignments) >= 2:
                # Could not move any with room availability - force move with fallback room
                course_codes = [getattr(a.get('course'), 'code', 'Unknown') for a in faculty_assignments if a.get('course')]
                print(f"    WARNING: Could not find available room for '{faculty_normalized}' electives, forcing move with fallback room")
                
                # Force move at least one elective to other period (use fallback room)
                for assignment in faculty_assignments[:num_to_move]:  # Move first num_to_move electives
                    course = assignment.get('course')
                    if not course:
                        continue
                    
                    course_code = getattr(course, 'code', 'Unknown')
                    
                    # Find any available room (even if it means a conflict)
                    # Exclude labs only
                    fallback_room = None
                    for classroom in classrooms:
                        if (classroom.room_type.lower() != 'lab' 
                            and 'lab' not in classroom.room_type.lower()
                            and classroom.room_number not in other_assigned_rooms):
                            fallback_room = classroom.room_number
                            break
                    
                    if not fallback_room:
                        # Last resort: use C002
                        fallback_room = 'C002'
                    
                    # Remove from old period's assigned rooms
                    old_room = assignment.get('room')
                    if old_room:
                        if period == 'PRE':
                            premid_assigned_rooms.discard(old_room)
                        else:
                            postmid_assigned_rooms.discard(old_room)
                    
                    # Move to other period
                    assignment['period'] = other_period
                    assignment['room'] = fallback_room
                    
                    # Add to new period's assigned rooms
                    if other_period == 'PRE':
                        premid_assigned_rooms.add(fallback_room)
                    else:
                        postmid_assigned_rooms.add(fallback_room)
                    
                    print(f"    FORCED: Moved {course_code} from {period} to {other_period} (room: {fallback_room}, forced move)")
                    
                    # Update counts
                    if period == 'PRE':
                        premid_count -= 1
                        postmid_count += 1
                    else:
                        postmid_count -= 1
                        premid_count += 1
                    
                    moved_count += 1
                    if moved_count >= num_to_move:
                        break
    
    return assignments


def run_phase9(courses: List[Course], all_sessions: List, room_assignments: Dict,
              classrooms: List[ClassRoom],
              all_courses: List[Course] = None) -> Dict[int, List[Dict]]:
    """
    Main entry point for Phase 9.
    
    Args:
        courses: All courses
        all_sessions: All scheduled sessions from all phases
        room_assignments: Room assignments from Phase 8
        classrooms: List of available classrooms
        registered_students: Number of registered students (default 80)
    
    Returns:
        Dict mapping semester to list of elective assignments:
        {semester: [{'course': Course, 'room': str, 'period': str, 'faculty': str}, ...], ...}
    """
    print("\n" + "="*80)
    print("Phase 9: Elective Room Assignment")
    print("="*80)
    
    # Get electives by group (5.1, 5.2, etc.) instead of just semester
    electives_by_group = get_electives_by_group(courses)
    
    # Helper to extract semester from group key
    def extract_semester_from_group(gk: str) -> int:
        try:
            if '.' in str(gk):
                return int(str(gk).split('.')[0])
            else:
                return int(gk)
        except (ValueError, AttributeError):
            return -1
    
    # Group by semester for return structure (but process by group internally)
    elective_assignments = {}
    
    # Process each group separately
    for group_key, elective_courses in sorted(electives_by_group.items()):
        semester = extract_semester_from_group(group_key)
        if semester == -1:
            continue
        
        print(f"\nProcessing Group {group_key} (Semester {semester}): {len(elective_courses)} electives")
        
        # Assign rooms and periods for this group
        # Use time slots from the matching basket group
        assignments = assign_electives_to_rooms_and_periods(
            elective_courses, semester, all_sessions, room_assignments,
            classrooms, all_courses or courses
        )
        for a in assignments:
            a['group_key'] = group_key
        if semester not in elective_assignments:
            elective_assignments[semester] = []
        elective_assignments[semester].extend(assignments)
        
        # Print summary
        assigned_count = sum(1 for a in assignments if a.get('room') != 'TBD')
        print(f"  Assigned: {assigned_count}/{len(assignments)} electives")
        premid_count = sum(1 for a in assignments if a.get('period') == 'PRE' and a.get('room') != 'TBD')
        postmid_count = sum(1 for a in assignments if a.get('period') == 'POST' and a.get('room') != 'TBD')
        print(f"  PreMid: {premid_count}, PostMid: {postmid_count}")
        
        # Debug: Show why electives were assigned to each period
        print(f"  Distribution details:")
        for assignment in assignments[:5]:  # Show first 5 as sample
            course = assignment.get('course')
            if course:
                course_code = getattr(course, 'code', '')
                period = assignment.get('period', 'TBD')
                faculty = assignment.get('faculty', 'TBD')
                if period != 'TBD':
                    print(f"    {course_code}: {period} (Faculty: {faculty})")
    
    return elective_assignments

