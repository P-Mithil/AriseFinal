"""
Phase 8: Classroom Assignment for Core Courses

Assigns appropriate classrooms to core courses (Phase 5) and remaining courses (Phase 7).
Combined courses already have C004 assigned and are skipped.
Tracks room assignments to prevent conflicts within the same period.
"""

import os
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from datetime import time, datetime

from utils.data_models import (
    Course, Section, ClassRoom, ScheduledSession, TimeBlock
)
from utils.room_priority_policy import (
    ordered_classroom_candidates,
    should_prefer_top_large_rooms,
    top_large_classrooms,
)
from config.schedule_config import COMBINED_RESERVED_ROOM_NUMBER


def _room_tier_by_capacity(capacity: int) -> str:
    """Classify classroom tier from capacity."""
    cap = int(capacity or 0)
    if cap >= 240:
        return "xlarge240"
    if cap in (135, 136):
        return "large"
    return "normal"


def _demand_tier(capacity_needed: int) -> str:
    """
    Classify demand into normal/large/xlarge240.
    Policy:
    - normal:     < 120  (fits 96-cap rooms)
    - large:      120-239 (needs C002/C003, 135/136-cap)
    - xlarge240:  >= 240  (needs C004, reserved for combined)

    NOTE: The threshold for 'large' is 120 (not 135) because many sections
    have 120-210 enrolled students that must fit in C002/C003. Using 135 as
    the threshold was incorrectly classifying 137-239 enrolled as xlarge240,
    blocking C002/C003 assignment and forcing fallback to 96-cap rooms.
    """
    need = int(capacity_needed or 0)
    if need >= 240:
        return "xlarge240"
    if need >= 105:
        return "large"
    return "normal"


def _room_allowed_for_demand(room_capacity: int, capacity_needed: int) -> bool:
    """Strict tier gate so small demand does not consume large/240 rooms."""
    room_tier = _room_tier_by_capacity(room_capacity)
    demand_tier = _demand_tier(capacity_needed)
    if demand_tier == "normal":
        return room_tier == "normal"
    if demand_tier == "large":
        # large demand: C002/C003 (136-cap) are perfect; C004 is last resort
        return room_tier in ("large", "xlarge240")
    # xlarge240: only C004 qualifies — but Phase 8 never assigns C004
    # (reserved for Phase 4 combined). This path shouldn't be reached for
    # non-combined courses; keep it for correctness.
    return room_tier == "xlarge240"

def extract_combined_room_occupancy(combined_sessions: List, classrooms: List[ClassRoom] = None) -> Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]]:
    """
    Extract room occupancy from combined sessions for ALL 240-seater classrooms to avoid conflicts.
    Combined classes use 240-seater rooms (capacity >= 240).
    
    Args:
        combined_sessions: List of combined course sessions
        classrooms: List of ClassRoom objects to identify 240-seater rooms
    
    Returns:
        room_occupancy[period][room_number][day] = [(start_time, end_time, course_code), ...]
    """
    room_occupancy = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    
    # Get list of 240-seater room numbers if classrooms provided (excluding labs)
    large_room_numbers = set()
    if classrooms:
        large_room_numbers = {room.room_number for room in classrooms 
                             if room.room_type.lower() != 'lab' 
                             and 'lab' not in room.room_type.lower()
                             and room.capacity >= 240}
    
    for session in combined_sessions:
        room = None
        period = None
        time_block = None
        course_code = ''
        
        if isinstance(session, dict):
            room = session.get('room', '')
            period = session.get('period', '')
            time_block = session.get('time_block')
            course_code = session.get('course_code', '')
        elif hasattr(session, 'room') and hasattr(session, 'period'):
            room = session.room
            period = session.period
            if hasattr(session, 'block') and session.block:
                time_block = session.block
            elif hasattr(session, 'time_block') and session.time_block:
                time_block = session.time_block
            course_code = getattr(session, 'course_code', '')
        
        # Track occupancy for 240-seater rooms only
        if room and period and time_block:
            # Extract day, start_time, end_time from time_block (TimeBlock object)
            if hasattr(time_block, 'day') and hasattr(time_block, 'start') and hasattr(time_block, 'end'):
                day = time_block.day
                start_time = time_block.start
                end_time = time_block.end
                
                # If we have classroom data, check if it's a 240-seater
                # Otherwise, track all rooms (backward compatibility)
                if classrooms:
                    if room in large_room_numbers:
                        room_occupancy[period][room][day].append((start_time, end_time, course_code))
                else:
                    # Fallback: track all rooms (for backward compatibility)
                    room_occupancy[period][room][day].append((start_time, end_time, course_code))
    
    return room_occupancy


# Keep old function name for backward compatibility
def extract_combined_c004_occupancy(combined_sessions: List) -> Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]]:
    """
    Legacy function - use extract_combined_room_occupancy instead.
    Extracts C004 occupancy only (for backward compatibility).
    """
    return extract_combined_room_occupancy(combined_sessions, classrooms=None)


def check_room_conflict(room_number: str, period: str, day: str, 
                       start_time: time, end_time: time,
                       room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]]) -> bool:
    """
    Check if room is occupied at the given time in the given period.
    
    Returns:
        True if conflict exists, False otherwise
    """
    # Normalize period labels so occupancy keys are consistent
    if not period:
        period = "PRE"
    pv = str(period).strip().upper()
    if pv in ("PREMID", "PRE"):
        period = "PRE"
    elif pv in ("POSTMID", "POST"):
        period = "POST"
    else:
        period = pv

    # Check all periods (PRE, POST, FULL) because sessions from different
    # periods can physically overlap in time due to staggered lunch breaks.
    for check_period, occupancy_data in room_occupancy.items():
        if room_number not in occupancy_data:
            continue
        if day not in occupancy_data[room_number]:
            continue
            
        for occupied_start, occupied_end, _ in occupancy_data[room_number][day]:
            # Check if times overlap
            if not (end_time <= occupied_start or start_time >= occupied_end):
                return True
                
    return False


def calculate_section_enrollment(
    session: ScheduledSession,
    sections: List[Section],
    courses: List[Course] = None,
    num_sections: int = 1
) -> int:
    """
    Capacity needed for this session.

    Use the larger of (a) configured section headcount and (b) `Registered Students`
    from `course_data.xlsx` when present, so large cohorts (e.g. DSAI-A with 132
    registered) are not sized to the default section cap (e.g. 85) and still fit the room.
    """
    section_students: int = 0
    section_label = getattr(session, "section", None)
    if section_label:
        for section in sections or []:
            if section.label == section_label:
                section_students = int(section.students)
                break

    registered = 0
    if courses:
        try:
            base_code = (getattr(session, "course_code", "") or "").split("-")[0]
            prog = ""
            if section_label and isinstance(section_label, str) and "-" in section_label:
                prog = section_label.split("-", 1)[0].strip()
            c = None
            if prog:
                c = next(
                    (
                        x
                        for x in courses
                        if getattr(x, "code", "") == base_code
                        and getattr(x, "department", "") == prog
                    ),
                    None,
                )
            if c is None:
                c = next((x for x in courses if getattr(x, "code", "") == base_code), None)
            if c:
                registered = int(getattr(c, "registered_students", 0) or 0)
        except Exception:
            pass

    if registered > 0:
        if num_sections > 1:
            # Distribute registered students evenly across sections, add a 5% buffer just in case
            split_size = int((registered / num_sections) * 1.05)
            if section_students > 0:
                return max(split_size, section_students)
            return max(split_size, 30)
            
        if section_students > 0:
            return max(registered, section_students)
        return registered

    if section_students > 0:
        return section_students
    return 30


def find_available_classroom(capacity_needed: int, session_type: str, period: str,
                            day: str, time_block: TimeBlock,
                            room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]],
                            classrooms: List[ClassRoom],
                            course_code: str = None) -> Optional[str]:
    """
    Find an available classroom or lab for the session.
    
    Args:
        capacity_needed: Required capacity
        session_type: "L", "T", or "P"
        period: "PRE" or "POST"
        day: Day of week
        time_block: TimeBlock for the session
        room_occupancy: Room occupancy tracker
        classrooms: List of available classrooms
        course_code: Course code to filter lab types (EC gets Hardware, others get Software)
    
    Returns:
        Room number if available, None otherwise
    """
    start_time = time_block.start
    end_time = time_block.end
    
    if course_code and "CS163" in course_code:
        print(f"[DEBUG FIND ROOM] CS163 requested capacity_needed: {capacity_needed}")
    
    if session_type == "P":
        # Need a lab; rely purely on actual lab capacities from input,
        # only excluding the reserved C004 room.
        all_labs = [room for room in classrooms 
                if room.room_type.lower() == "lab"
                and room.room_number != COMBINED_RESERVED_ROOM_NUMBER]
        
        labs = all_labs
        if course_code:
            course_code_base = course_code.split('-')[0].upper()
            if course_code_base.startswith("EC"):
                labs = [r for r in all_labs if getattr(r, 'lab_type', None) == 'Hardware']
            else:
                labs = [r for r in all_labs if getattr(r, 'lab_type', None) in (None, 'Software')]
        
        # Prefer smaller/less-used labs first
        labs.sort(key=lambda r: r.capacity)
        
        for lab in labs:
            if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                return lab.room_number
        
        # Do not fall back to any lab type. The user explicitly requested to reschedule 
        # instead of giving a wrong lab type.
        return None
    else:
        # User requested: C004 is ONLY for combined classes. Never assign it here.
        classrooms = [r for r in classrooms if r.room_number != COMBINED_RESERVED_ROOM_NUMBER]
        
        top2 = top_large_classrooms(classrooms, n=2)
        prefer_top = should_prefer_top_large_rooms(capacity_needed, top2)
        classrooms_filtered = ordered_classroom_candidates(
            classrooms, capacity_needed, prefer_top_large=prefer_top, top_rooms=top2
        )

        # Strict pass: honor both required capacity and demand-tier gate.
        for room in classrooms_filtered:
            room_cap = int(getattr(room, "capacity", 0) or 0)
            if room_cap < int(capacity_needed or 0):
                continue
            if not _room_allowed_for_demand(room_cap, int(capacity_needed or 0)):
                continue

            if not check_room_conflict(room.room_number, period, day, start_time, end_time, room_occupancy):
                return room.room_number

        # No under-capacity or tier-violating fallback here.
        # Caller should reschedule if strict fit is not available.
    
    return None


def assign_labs_for_practical(enrollment: int, period: str, day: str, time_block: TimeBlock,
                              room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]],
                              classrooms: List[ClassRoom], num_labs_needed: int = 2) -> List[str]:
    """
    Assign labs for practical session.
    Assigns num_labs_needed labs (default 2) for practical courses.
    Labs have capacity 40-45, so we assign 2 labs to split students.
    
    Args:
        enrollment: Total enrollment for the course
        period: PRE or POST
        day: Day of week
        time_block: TimeBlock for the session
        room_occupancy: Room occupancy tracker
        classrooms: List of available classrooms
        num_labs_needed: Number of labs needed (default 2 for all practicals)
    
    Returns:
        List of lab room numbers (num_labs_needed labs, or as many as available)
    """
    start_time = time_block.start
    end_time = time_block.end
    
    labs = [room for room in classrooms 
            if room.room_type.lower() == 'lab'
            and room.room_number != COMBINED_RESERVED_ROOM_NUMBER]
    
    # Sort by capacity (prefer smaller labs first for better utilization)
    labs.sort(key=lambda r: r.capacity)
    
    available_labs = []
    for lab in labs:
        if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
            available_labs.append(lab.room_number)
            if len(available_labs) >= num_labs_needed:
                # Found required number of labs
                return available_labs[:num_labs_needed]
    
    # Return what we found (might be less than needed, but return all available)
    return available_labs


def mark_room_occupied(room_number: str, period: str, day: str,
                      start_time: time, end_time: time, course_code: str,
                      room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]]):
    """Mark a room as occupied for the given time slot."""
    # Normalize period labels so occupancy keys are consistent
    if not period:
        period = "PRE"
    pv = str(period).strip().upper()
    if pv in ("PREMID", "PRE"):
        period = "PRE"
    elif pv in ("POSTMID", "POST"):
        period = "POST"
    else:
        period = pv

    room_occupancy[period][room_number][day].append((start_time, end_time, course_code))


def assign_labs_to_combined_practicals(
    combined_sessions: List[Dict],
    classrooms: List[ClassRoom],
) -> List[Dict]:
    """
    Assign 2 labs to combined course practicals, mirroring Phase 8 logic.
    EC courses get hardware labs; others get software labs. Excludes research labs.
    Modifies combined_sessions in place and returns it.
    """
    if not classrooms:
        return combined_sessions

    # Lab room filter: type contains 'lab', uses real capacities from input, not C004, exclude research labs
    def _is_lab_room(room):
        if getattr(room, 'is_research_lab', False):
            return False
        rt = (room.room_type or "").lower()
        return (rt == 'lab' or 'lab' in rt) and room.room_number != COMBINED_RESERVED_ROOM_NUMBER

    all_labs_base = [r for r in classrooms if _is_lab_room(r)]
    if not all_labs_base:
        return combined_sessions

    # Group combined practicals by (course_code_base, period, day, start, end)
    groups: Dict[Tuple[str, str, str, time, time], List[Dict]] = defaultdict(list)
    for session in combined_sessions:
        if not isinstance(session, dict):
            continue
        if session.get('session_type') != 'P':
            continue
        time_block = session.get('time_block')
        if not time_block or not hasattr(time_block, 'day'):
            continue
        course_code = session.get('course_code', '')
        course_code_base = course_code.split('-')[0]
        period = session.get('period', 'PRE')
        day = time_block.day
        start_time = time_block.start
        end_time = time_block.end
        key = (course_code_base, period, day, start_time, end_time)
        groups[key].append(session)

    room_occupancy = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for (course_code_base, period, day, start_time, end_time), sessions in groups.items():
        # Filter labs by course: EC -> Hardware; else -> Software
        if course_code_base.upper().startswith("EC"):
            labs = [r for r in all_labs_base if getattr(r, 'lab_type', None) == 'Hardware']
            if not labs:
                labs = list(all_labs_base)
                if labs:
                    print(f"  [Combined Labs] WARNING: No Hardware labs for EC course {course_code_base}; using any lab.")
        else:
            labs = [r for r in all_labs_base if getattr(r, 'lab_type', None) in (None, 'Software')]
            if not labs:
                labs = list(all_labs_base)
                if labs:
                    print(f"  [Combined Labs] WARNING: No Software labs for {course_code_base}; using any lab.")

        def _lab_occupancy_count(room):
            n = 0
            if period in room_occupancy and room.room_number in room_occupancy[period]:
                for d in room_occupancy[period][room.room_number]:
                    n += len(room_occupancy[period][room.room_number][d])
            return n
        labs.sort(key=lambda r: (_lab_occupancy_count(r), r.capacity))

        assigned_labs = []
        for lab in labs:
            if check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                continue
            assigned_labs.append(lab.room_number)
            mark_room_occupied(lab.room_number, period, day, start_time, end_time, course_code_base, room_occupancy)
            if len(assigned_labs) >= 2:
                break

        # If we couldn't find 2 free labs, force assignment of conflicting labs
        # so that the room conflict resolver will reschedule the course properly!
        if len(assigned_labs) < 2 and labs:
            def _overlap_count(room_number: str) -> int:
                try:
                    pv = str(period).strip().upper()
                    if pv in ("PREMID", "PRE"): pv = "PRE"
                    elif pv in ("POSTMID", "POST"): pv = "POST"
                    occ_list = room_occupancy.get(pv, {}).get(room_number, {}).get(day, []) or []
                    n = 0
                    for occ_s, occ_e, _cc in occ_list:
                        if not (end_time <= occ_s or start_time >= occ_e):
                            n += 1
                    return n
                except Exception:
                    return 10**9

            ranked = sorted([lab.room_number for lab in labs], key=lambda rn: (_overlap_count(rn), rn))
            for r in ranked:
                if len(assigned_labs) >= 2:
                    break
                if r not in assigned_labs:
                    assigned_labs.append(r)
                    mark_room_occupied(r, period, day, start_time, end_time, course_code_base, room_occupancy)

        if assigned_labs:
            labs_str = ", ".join(sorted(assigned_labs))
            for sess in sessions:
                sess['room'] = labs_str

    return combined_sessions


def assign_classrooms_to_core_sessions(phase5_sessions: List[ScheduledSession],
                                       phase7_sessions: List[ScheduledSession],
                                       room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]],
                                       classrooms: List[ClassRoom],
                                       courses: List[Course],
                                       sections: List[Section]) -> Dict[Tuple[str, str, str], Dict[str, any]]:
    """
    Assign classrooms to core course sessions (Phase 5 and Phase 7).
    
    Returns:
        Dictionary mapping (course_code, section, period) to room assignments:
        {
            (course_code, section, period): {
                "classroom": "C002",  # For lectures/tutorials (same room for all sessions)
                "labs": ["L105", "L106"]  # For practicals (1 or 2 labs)
            }
        }
    """
    room_assignments = {}
    
    # Group sessions by (course_code, section, period)
    sessions_by_course = defaultdict(list)
    all_sessions = phase5_sessions + phase7_sessions
    
    for session in all_sessions:
        if not hasattr(session, 'course_code') or not hasattr(session, 'section'):
            continue
        
        # Skip if room is C004 (reserved for combined courses)
        if session.room == COMBINED_RESERVED_ROOM_NUMBER:
            continue
        
        # Get time block
        if not hasattr(session, 'block') or not session.block:
            continue
        
        # Strip suffix from course_code (-LAB, -TUT) to use base course code for grouping
        course_code = session.course_code.split('-')[0]  # Remove -LAB/-TUT suffix
        section = session.section
        period = session.period or 'PRE'
        # Normalize PREMID/POSTMID -> PRE/POST for consistent occupancy checks
        pv = str(period).strip().upper()
        if pv in ("PREMID", "PRE"):
            period = "PRE"
        elif pv in ("POSTMID", "POST"):
            period = "POST"
        else:
            period = pv
        
        key = (course_code, section, period)
        sessions_by_course[key].append(session)
    
    # Calculate how many distinct sections exist for each (course, department)
    # Key: (course_code, dept_prefix) -> set of section labels
    # dept_prefix is extracted from the section label e.g. "CSE-A-Sem2" -> "CSE"
    course_dept_sections_count = defaultdict(set)
    for course_code, section, period in sessions_by_course.keys():
        if section:
            dept_prefix = section.split('-')[0].strip() if '-' in section else ''
            course_dept_sections_count[(course_code, dept_prefix)].add(section)
    
    # Pre-calculate enrollments for each group to ensure larger groups are scheduled first
    group_enrollments = {}
    for key, course_sessions in sessions_by_course.items():
        course_code, section, period = key
        def _temp_effective_kind(sess: ScheduledSession) -> str:
            try:
                cc = str(getattr(sess, "course_code", "") or "").upper()
                if "-LAB" in cc: return "P"
                if "-TUT" in cc: return "T"
            except Exception:
                pass
            return getattr(sess, "kind", "L") or "L"
            
        lt_sess = [s for s in course_sessions if _temp_effective_kind(s) in ["L", "T"]]
        if lt_sess:
            dept_prefix = section.split('-')[0].strip() if '-' in section else ''
            num_secs = len(course_dept_sections_count.get((course_code, dept_prefix), set())) or 1
            cap = calculate_section_enrollment(lt_sess[0], sections, courses, num_sections=num_secs)
            group_enrollments[key] = cap
        else:
            group_enrollments[key] = 0
            
    # Sort keys primarily by enrollment descending, then by course code for stability
    sorted_keys = sorted(sessions_by_course.keys(), key=lambda k: (group_enrollments.get(k, 0), k[0]), reverse=True)
    
    # Process each course group in decreasing order of capacity requirement
    debug_samples = []  # Collect sample keys for debugging
    for key in sorted_keys:
        course_sessions = sessions_by_course[key]
        course_code, section, period = key
        
        # Initialize assignment
        room_assignments[key] = {
            "classroom": None,
            "labs": []
        }
        
        # Separate sessions by type.
        # Be defensive: some sessions may carry suffixes (-LAB/-TUT) even if `kind` isn't perfectly set.
        def _effective_kind(sess: ScheduledSession) -> str:
            try:
                cc = str(getattr(sess, "course_code", "") or "").upper()
                if "-LAB" in cc:
                    return "P"
                if "-TUT" in cc:
                    return "T"
            except Exception:
                pass
            return getattr(sess, "kind", "L") or "L"

        lecture_tutorial_sessions = [s for s in course_sessions if _effective_kind(s) in ["L", "T"]]
        practical_sessions = [s for s in course_sessions if _effective_kind(s) == "P"]
        
        # Debug: Track courses with practicals
        if practical_sessions and len(debug_samples) < 10:
            debug_samples.append({
                'key': key,
                'course_code': course_code,
                'section': section,
                'period': period,
                'practical_count': len(practical_sessions),
                'has_labs': False  # Will be updated if labs are assigned
            })
        
        # Assign classroom for lectures/tutorials
        # Strategy: Try to use same room for all sessions (preferred), but allow different rooms if needed
        if lecture_tutorial_sessions:
            # Capacity needed: divide by number of sections within THIS department only
            dept_prefix = section.split('-')[0].strip() if '-' in section else ''
            num_secs = len(course_dept_sections_count.get((course_code, dept_prefix), set())) or 1
            enrollment = calculate_section_enrollment(lecture_tutorial_sessions[0], sections, courses, num_sections=num_secs)
            if "CS163" in course_code:
                print(f"[DEBUG CS163] Section: {section}, num_secs: {num_secs}, enrollment computed: {enrollment}")
            
            # Capacity-aware classroom policy:
            # - never place oversized sections into undersized rooms
            # - allow large rooms only for truly large strength
            non_lab_classrooms = [
                room for room in classrooms
                if room.room_type.lower() != 'lab'
                and 'lab' not in room.room_type.lower()
            ]
            
            suitable_classrooms = [
                room for room in non_lab_classrooms
                if room.capacity >= enrollment and room.room_number != COMBINED_RESERVED_ROOM_NUMBER
            ]
            
            suitable_classrooms = [
                room for room in suitable_classrooms
                if _room_allowed_for_demand(int(getattr(room, "capacity", 0) or 0), enrollment)
            ]

            demand_tier = _demand_tier(enrollment)
            if demand_tier == "xlarge240":
                # Very high demand: prioritize 240-tier first.
                suitable_classrooms.sort(key=lambda r: (_room_tier_by_capacity(r.capacity) != "xlarge240", r.capacity, r.room_number))
            elif demand_tier == "large":
                # Large demand: prioritize 135/136, keep 240 as fallback.
                suitable_classrooms.sort(key=lambda r: (
                    _room_tier_by_capacity(r.capacity) == "xlarge240",
                    abs(int(r.capacity or 0) - int(enrollment or 0)),
                    r.room_number
                ))
            else:
                # Normal demand: strict normal rooms only + best-fit.
                suitable_classrooms.sort(key=lambda r: (abs(int(r.capacity or 0) - int(enrollment or 0)), r.room_number))
            
            # Try to find a classroom available for ALL sessions (preferred)
            assigned_classroom = None
            for classroom in suitable_classrooms:
                # Check if this classroom is available for all sessions
                available_for_all = True
                for session in lecture_tutorial_sessions:
                    time_block = session.block
                    day = time_block.day
                    start_time = time_block.start
                    end_time = time_block.end
                    
                    if check_room_conflict(classroom.room_number, period, day, start_time, end_time, room_occupancy):
                        available_for_all = False
                        break
                
                if available_for_all:
                    assigned_classroom = classroom.room_number
                    # Mark room as occupied for all sessions
                    for session in lecture_tutorial_sessions:
                        time_block = session.block
                        day = time_block.day
                        start_time = time_block.start
                        end_time = time_block.end
                        mark_room_occupied(assigned_classroom, period, day, start_time, end_time, course_code, room_occupancy)
                    break
            
            # If no single room is available for all sessions, we intentionally leave it unassigned here.
            # The room_conflict_resolver.py -> resolve_unassigned_core_classrooms() will pick this up
            # and attempt to RESCHEDULE the course to a time slot where a single room IS available.
            # If rescheduling fails, the resolver will assign C004 as a last resort.

            if assigned_classroom:
                room_assignments[key]["classroom"] = assigned_classroom
                # Also update session.room for all sessions
                for session in lecture_tutorial_sessions:
                    session.room = assigned_classroom
            else:
                for session in lecture_tutorial_sessions:
                    current_room = getattr(session, "room", None)
                    if current_room:
                        cr_obj = next((r for r in classrooms if r.room_number == current_room), None)
                        if cr_obj and int(cr_obj.capacity or 0) < int(enrollment or 0):
                            session.room = None
        
        # Assign labs for practicals PER SESSION (prevents hidden double-booking)
        if practical_sessions:
            dept_prefix = section.split('-')[0].strip() if '-' in section else ''
            num_secs = len(course_dept_sections_count.get((course_code, dept_prefix), set())) or 1
            enrollment = calculate_section_enrollment(practical_sessions[0], sections, courses, num_sections=num_secs)
            # Lab room filter: type contains 'lab', uses real capacities from input, not C004, exclude research labs
            def _is_lab_room(room, allow_research: bool = False):
                if (not allow_research) and getattr(room, 'is_research_lab', False):
                    return False
                rt = (room.room_type or "").lower()
                return (rt == 'lab' or 'lab' in rt) and room.room_number != COMBINED_RESERVED_ROOM_NUMBER
            all_labs = [r for r in classrooms if _is_lab_room(r, allow_research=False)]
            all_labs_with_research = [r for r in classrooms if _is_lab_room(r, allow_research=True)]
            # Filter by course code: EC -> Hardware; CS/DS/other -> Software
            course_code_base = key[0]
            if course_code_base.upper().startswith("EC"):
                labs = [room for room in all_labs if getattr(room, 'lab_type', None) == 'Hardware']
                if not labs:
                    print(f"  [Phase 8] WARNING: No Hardware labs available for EC course {course_code_base}.")
            else:
                labs = [room for room in all_labs if getattr(room, 'lab_type', None) in (None, 'Software')]
                if not labs:
                    print(f"  [Phase 8] WARNING: No Software labs available for {course_code_base}.")
            # Sort by occupancy count (ascending) to minimize conflicts, then by capacity
            def _lab_occupancy_count(room):
                n = 0
                if period in room_occupancy and room.room_number in room_occupancy[period]:
                    for day in room_occupancy[period][room.room_number]:
                        n += len(room_occupancy[period][room.room_number][day])
                return n
            labs.sort(key=lambda r: (_lab_occupancy_count(r), r.capacity))
            if not labs:
                continue
            
            # Always assign exactly 2 labs per practical session.
            # This splits the section across 2 lab rooms consistently,
            # regardless of actual enrollment or individual lab capacity.
            labs_needed = min(2, len(labs)) if labs else 0

            any_assigned = False
            labs_union = set()

            for session in practical_sessions:
                if not getattr(session, "block", None):
                    continue
                time_block = session.block
                day = time_block.day
                start_time = time_block.start
                end_time = time_block.end

                # Choose an ALL-FREE set of N labs first, then mark occupancy.
                # This avoids partial allocation that can hide collisions and later trigger strict verify.
                needed_n = max(labs_needed, 1)

                free_labs = [
                    lab.room_number
                    for lab in labs
                    if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy)
                ]
                if not free_labs and all_labs_with_research:
                    # last resort: allow research labs only when nothing else is free
                    labs_any = [r for r in all_labs_with_research]
                    labs_any.sort(key=lambda r: (_lab_occupancy_count(r), r.capacity))
                    free_labs = [
                        lab.room_number
                        for lab in labs_any
                        if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy)
                    ]

                session_labs: List[str] = []
                if len(free_labs) >= needed_n:
                    session_labs = free_labs[:needed_n]
                else:
                    # We need exactly needed_n labs. If we don't have enough free ones,
                    # we must assign conflicting ones so that the room conflict resolver 
                    # correctly identifies the collision and reschedules the session!
                    # Do NOT silently assign just 1 lab.
                    session_labs = list(free_labs)
                    
                    def _overlap_count(room_number: str) -> int:
                        try:
                            pv = str(period).strip().upper()
                            if pv in ("PREMID", "PRE"): pv = "PRE"
                            elif pv in ("POSTMID", "POST"): pv = "POST"
                            occ_list = room_occupancy.get(pv, {}).get(room_number, {}).get(day, []) or []
                            n = 0
                            for occ_s, occ_e, _cc in occ_list:
                                if not (end_time <= occ_s or start_time >= occ_e):
                                    n += 1
                            return n
                        except Exception:
                            return 10**9

                    # Sort ALL labs by overlap count
                    ranked = sorted([lab.room_number for lab in labs], key=lambda rn: (_overlap_count(rn), rn))
                    
                    # Fill the rest of session_labs with the least-conflicting rooms that aren't already in session_labs
                    for r in ranked:
                        if len(session_labs) >= needed_n:
                            break
                        if r not in session_labs:
                            session_labs.append(r)

                if session_labs:
                    any_assigned = True
                    session.room = ", ".join(session_labs)
                    for lr in session_labs:
                        mark_room_occupied(lr, period, day, start_time, end_time, course_code, room_occupancy)
                        labs_union.add(lr)

            # Keep mapping as best-effort union; DO NOT force onto sessions.
            if any_assigned:
                room_assignments[key]["labs"] = sorted(labs_union)
                for sample in debug_samples:
                    if sample['key'] == key:
                        sample['labs'] = room_assignments[key]["labs"]
                        sample['has_labs'] = True
                        break
    
    # Debug: Print sample keys
    if debug_samples:
        print("\n[PHASE 8 DEBUG] Practical courses found:")
        for sample in debug_samples:
            if sample.get('has_labs'):
                print(f"  {sample['course_code']} ({sample['section']}, {sample['period']}): {sample['practical_count']} practical sessions -> Labs: {sample.get('labs', 'NONE')}")
            else:
                print(f"  {sample['course_code']} ({sample['section']}, {sample['period']}): {sample['practical_count']} practical sessions -> Labs: NOT ASSIGNED")
        
        # Count assignments with labs
        assignments_with_labs = sum(1 for v in room_assignments.values() if v.get('labs'))
        print(f"\n[PHASE 8 DEBUG] Total room assignments: {len(room_assignments)}")
        print(f"[PHASE 8 DEBUG] Assignments with labs: {assignments_with_labs}")
        
        # Print first 10 keys
        sample_keys = list(room_assignments.keys())[:10]
        print(f"[PHASE 8 DEBUG] First 10 keys: {sample_keys}")
    
    return room_assignments


def _times_overlap(start1: time, end1: time, start2: time, end2: time) -> bool:
    """Return True if the two time ranges overlap."""
    return not (end1 <= start2 or end2 <= start1)


def detect_lab_conflicts(
    phase5_sessions: List[ScheduledSession],
    phase7_sessions: List[ScheduledSession],
    room_assignments: Dict[Tuple[str, str, str], Dict],
) -> List[Dict]:
    """
    Detect lab conflicts: same lab, same period/day, overlapping time, different courses.
    Builds occupancy from room_assignments and practical sessions, then finds overlaps.
    Returns list of conflict dicts: room, period, day, time, start, end, course1, course2, section1, section2.
    """
    # Build (lab_room, period, day, start, end, course_code, section) for each lab slot
    entries = []
    all_sessions = list(phase5_sessions) + list(phase7_sessions)
    for key, assignment in room_assignments.items():
        labs_list = assignment.get("labs") or []
        if not labs_list:
            continue
        course_code, section, period = key
        for session in all_sessions:
            if not (getattr(session, "course_code", "").split("-")[0] == course_code
                    and getattr(session, "section", "") == section
                    and getattr(session, "period", "") == period
                    and getattr(session, "kind", "") == "P"):
                continue
            if not hasattr(session, "block") or not session.block:
                continue
            day = session.block.day
            start_time = session.block.start
            end_time = session.block.end
            for lab_room in labs_list:
                entries.append({
                    "room": lab_room,
                    "period": period,
                    "day": day,
                    "start": start_time,
                    "end": end_time,
                    "course": course_code,
                    "section": section,
                })
    conflicts = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            if a["room"] != b["room"] or a["period"] != b["period"] or a["day"] != b["day"]:
                continue
            if not _times_overlap(a["start"], a["end"], b["start"], b["end"]):
                continue
            if (a["course"], a["section"]) == (b["course"], b["section"]):
                continue
            conflicts.append({
                "room": a["room"],
                "period": a["period"],
                "day": a["day"],
                "time": f"{a['start'].strftime('%H:%M')}-{a['end'].strftime('%H:%M')}",
                "start": a["start"],
                "end": a["end"],
                "course1": a["course"],
                "course2": b["course"],
                "section1": a["section"],
                "section2": b["section"],
            })
    return conflicts


def detect_room_conflicts(phase5_sessions: List[ScheduledSession],
                         phase7_sessions: List[ScheduledSession],
                         combined_sessions: List,
                         elective_sessions: List = None,
                         classrooms: List[ClassRoom] = None) -> List[Dict]:
    """
    Detect classroom conflicts (same room, same time, different courses).
    Only checks classrooms, not labs.
    
    Args:
        phase5_sessions: Core course sessions from Phase 5
        phase7_sessions: Remaining course sessions from Phase 7
        combined_sessions: Combined course sessions
        elective_sessions: Elective course sessions (optional)
        classrooms: List of ClassRoom objects to identify classrooms vs labs
    
    Returns:
        List of conflict dictionaries:
        {
            'room': room_number,
            'period': period,
            'day': day,
            'time': f"{start_time}-{end_time}",
            'course1': course_code1,
            'course2': course_code2,
            'section1': section1,
            'section2': section2
        }
    """
    conflicts = []
    
    # Get set of classroom room numbers (exclude labs)
    classroom_numbers = set()
    if classrooms:
        classroom_numbers = {room.room_number for room in classrooms 
                           if room.room_type.lower() != 'lab' 
                           and 'lab' not in room.room_type.lower()}
    
    # Collect all sessions with room assignments
    all_sessions = []
    
    # Add Phase 5 sessions
    for session in phase5_sessions:
        if hasattr(session, 'room') and session.room:
            # Check if it's a classroom (not a lab)
            if not classrooms or session.room in classroom_numbers:
                all_sessions.append({
                    'room': session.room,
                    'period': getattr(session, 'period', 'PRE'),
                    'day': session.block.day if hasattr(session, 'block') and session.block else None,
                    'start': session.block.start if hasattr(session, 'block') and session.block else None,
                    'end': session.block.end if hasattr(session, 'block') and session.block else None,
                    'course': getattr(session, 'course_code', ''),
                    'section': getattr(session, 'section', '')
                })
    
    # Add Phase 7 sessions
    for session in phase7_sessions:
        if hasattr(session, 'room') and session.room:
            if not classrooms or session.room in classroom_numbers:
                all_sessions.append({
                    'room': session.room,
                    'period': getattr(session, 'period', 'PRE'),
                    'day': session.block.day if hasattr(session, 'block') and session.block else None,
                    'start': session.block.start if hasattr(session, 'block') and session.block else None,
                    'end': session.block.end if hasattr(session, 'block') and session.block else None,
                    'course': getattr(session, 'course_code', ''),
                    'section': getattr(session, 'section', '')
                })
    
    # Add combined sessions
    for session in combined_sessions:
        room = None
        period = None
        day = None
        start = None
        end = None
        course = ''
        section = ''
        
        if isinstance(session, dict):
            room = session.get('room', '')
            period = session.get('period', '')
            time_block = session.get('time_block')
            course = session.get('course_code', '')
            sections = session.get('sections', [])
            section = ', '.join(sections) if sections else ''
            
            if time_block and hasattr(time_block, 'day'):
                day = time_block.day
                start = time_block.start
                end = time_block.end
        elif hasattr(session, 'room') and session.room:
            room = session.room
            period = getattr(session, 'period', 'PRE')
            if hasattr(session, 'block') and session.block:
                day = session.block.day
                start = session.block.start
                end = session.block.end
            course = getattr(session, 'course_code', '')
            section = getattr(session, 'section', '')
        
        if room and day and start and end:
            if not classrooms or room in classroom_numbers:
                all_sessions.append({
                    'room': room,
                    'period': period or 'PRE',
                    'day': day,
                    'start': start,
                    'end': end,
                    'course': course,
                    'section': section
                })
    
    # Add elective sessions if provided (object or dict with room + time_block)
    if elective_sessions:
        for session in elective_sessions:
            room = None
            period = None
            day = None
            start = None
            end = None
            course = ''
            section = ''
            if isinstance(session, dict):
                room = session.get('room', '')
                period = session.get('period', 'PRE')
                tb = session.get('time_block')
                course = session.get('course_code', '')
                section = session.get('section', '')
                if tb and hasattr(tb, 'day'):
                    day = tb.day
                    start = tb.start
                    end = tb.end
            elif hasattr(session, 'room') and session.room:
                room = session.room
                period = getattr(session, 'period', 'PRE')
                course = getattr(session, 'course_code', '')
                section = getattr(session, 'section', '')
                if hasattr(session, 'block') and session.block:
                    day = session.block.day
                    start = session.block.start
                    end = session.block.end
            if room and day and start and end:
                if not classrooms or room in classroom_numbers:
                    all_sessions.append({
                        'room': room,
                        'period': period or 'PRE',
                        'day': day,
                        'start': start,
                        'end': end,
                        'course': course,
                        'section': section
                    })
    
    def _norm_period(p):
        if not p:
            return "PRE"
        v = str(p).strip().upper()
        if v in ("PREMID", "PRE"):
            return "PRE"
        if v in ("POSTMID", "POST"):
            return "POST"
        return v

    # Check for conflicts: same room, same period (Pre/Post separate - pre vs post can be ignored), same day, overlapping times
    for i, session1 in enumerate(all_sessions):
        if not all([session1['room'], session1['period'], session1['day'], session1['start'], session1['end']]):
            continue
        p1 = _norm_period(session1['period'])
        for j, session2 in enumerate(all_sessions[i+1:], start=i+1):
            if not all([session2['room'], session2['period'], session2['day'], session2['start'], session2['end']]):
                continue
            p2 = _norm_period(session2['period'])
            # Same room, same day (period tags don't grant immunity if times actually overlap)
            if (session1['room'] == session2['room'] and session1['day'] == session2['day']):
                
                # Check if times overlap
                start1, end1 = session1['start'], session1['end']
                start2, end2 = session2['start'], session2['end']
                
                if not (end1 <= start2 or start1 >= end2):
                    # Any two different sessions (even of the same course) sharing the
                    # same room, day and period at overlapping times are a real
                    # classroom clash, because a physical room cannot host two
                    # sections simultaneously. Do not exempt same-course overlaps.
                    conflicts.append({
                        'room': session1['room'],
                        'period': session1['period'],
                        'day': session1['day'],
                        'time': f"{start1.strftime('%H:%M')}-{end1.strftime('%H:%M')}",
                        'course1': session1['course'],
                        'course2': session2['course'],
                        'section1': session1['section'],
                        'section2': session2['section']
                    })
    
    return conflicts


def run_phase8(phase5_sessions: List[ScheduledSession],
               phase7_sessions: List[ScheduledSession],
               combined_sessions: List,
               courses: List[Course],
               sections: List[Section],
               classrooms: List[ClassRoom],
               elective_sessions: List = None) -> Dict[Tuple[str, str, str], Dict[str, any]]:
    """
    Run Phase 8: Assign classrooms to core courses.
    
    Args:
        phase5_sessions: Core course sessions from Phase 5
        phase7_sessions: Remaining course sessions from Phase 7
        combined_sessions: Combined course sessions (for 240-seater room conflict checking)
        courses: List of all courses
        sections: List of all sections
        classrooms: List of all classrooms
        elective_sessions: Elective course sessions (optional, for conflict detection)
    
    Returns:
        Dictionary mapping (course_code, section, period) to room assignments
    """
    print("=== PHASE 8: CLASSROOM ASSIGNMENT FOR CORE COURSES ===")
    print()
    
    # Step 1: Extract occupancy from combined sessions for ALL 240-seater rooms
    print("Step 1: Extracting 240-seater room occupancy from combined courses...")
    room_occupancy = extract_combined_room_occupancy(combined_sessions, classrooms)
    
    # Count all 240-seater room sessions (excluding labs)
    total_large_room_sessions = 0
    large_room_numbers = {room.room_number for room in classrooms 
                          if room.room_type.lower() != 'lab' 
                          and 'lab' not in room.room_type.lower()
                          and room.capacity >= 240} if classrooms else set()
    
    for period in room_occupancy:
        for room_number in room_occupancy[period]:
            if not classrooms or room_number in large_room_numbers:
                for day in room_occupancy[period][room_number]:
                    total_large_room_sessions += len(room_occupancy[period][room_number][day])
    
    print(f"  Found {total_large_room_sessions} 240-seater room sessions from combined courses")
    if large_room_numbers:
        print(f"  Tracking rooms: {sorted(large_room_numbers)}")
    print()
    
    # Step 2: Assign classrooms to core courses
    print("Step 2: Assigning classrooms to core courses (Phase 5 and Phase 7)...")
    room_assignments = assign_classrooms_to_core_sessions(
        phase5_sessions, phase7_sessions, room_occupancy,
        classrooms, courses, sections
    )
    
    # Count assignments
    total_assignments = len(room_assignments)
    classroom_assignments = sum(1 for v in room_assignments.values() if v["classroom"])
    lab_assignments = sum(1 for v in room_assignments.values() if v["labs"])
    multi_lab_assignments = sum(1 for v in room_assignments.values() if len(v["labs"]) >= 2)
    
    print(f"  Total course assignments: {total_assignments}")
    print(f"  Classroom assignments: {classroom_assignments}")
    print(f"  Lab assignments: {lab_assignments}")
    print(f"  Multi-lab assignments (2+ labs): {multi_lab_assignments}")
    print()
    
    # DEBUG: Print sample keys to understand key format
    print("DEBUG: Sample Phase 8 room assignment keys:")
    sample_keys = list(room_assignments.keys())[:10]
    for key in sample_keys:
        course_code, section, period = key
        assignment = room_assignments[key]
        labs_str = ', '.join(assignment['labs']) if assignment['labs'] else 'None'
        classroom_str = assignment['classroom'] or 'None'
        print(f"  Key: {key} -> Classroom: {classroom_str}, Labs: {labs_str}")
    if len(room_assignments) > 10:
        print(f"  ... and {len(room_assignments) - 10} more keys")
    print()

    # Step 2.5: Assign C004 to combined course lectures/tutorials
    # Combined courses (Phase 4) need the 240-seater auditorium since they combine
    # multiple sections. Phase 4 scheduling doesn't assign rooms, so we do it here.
    print("Step 2.5: Assigning C004 to combined course lectures/tutorials...")
    combined_c004_count = 0
    for session in combined_sessions:
        if isinstance(session, dict):
            cc = session.get('course_code', '')
            stype = session.get('session_type', 'L')
            secs = session.get('sections', [])
            period_val = session.get('period', 'PRE')
        elif hasattr(session, 'course_code'):
            cc = getattr(session, 'course_code', '')
            stype = getattr(session, 'kind', getattr(session, 'session_type', 'L'))
            secs = getattr(session, 'sections', [])
            period_val = getattr(session, 'period', 'PRE')
        else:
            continue

        # Only assign C004 for lectures and tutorials (not practicals — labs get separate assignment)
        if stype == 'P':
            continue

        base_code = cc.split('-')[0]
        period_val = (period_val or 'PRE').strip().upper()
        if period_val == 'PREMID': period_val = 'PRE'
        if period_val == 'POSTMID': period_val = 'POST'

        for sec in secs:
            sec_str = str(sec).strip()
            key = (base_code, sec_str, period_val)
            if key not in room_assignments:
                room_assignments[key] = {"classroom": COMBINED_RESERVED_ROOM_NUMBER, "labs": []}
                combined_c004_count += 1
            else:
                # Force C004 even if a smaller room was assigned earlier.
                if room_assignments[key].get("classroom") != COMBINED_RESERVED_ROOM_NUMBER:
                    room_assignments[key]["classroom"] = COMBINED_RESERVED_ROOM_NUMBER
                    combined_c004_count += 1

        # Also assign room to the session object itself
        if isinstance(session, dict):
            if not session.get('room') or session.get('room') in ('', 'None', 'TBD', 'nan'):
                session['room'] = COMBINED_RESERVED_ROOM_NUMBER
        elif hasattr(session, 'room'):
            if not session.room or str(session.room).strip().lower() in ('', 'none', 'tbd', 'nan'):
                session.room = COMBINED_RESERVED_ROOM_NUMBER

    print(f"  Assigned C004 to {combined_c004_count} combined course entries")
    print()

    # NOTE: C004 is strictly reserved for Phase 4 combined sessions only.
    
    # Step 3: Conflict Detection and Reporting
    print("Step 3: Detecting classroom conflicts...")
    conflicts = detect_room_conflicts(
        phase5_sessions, phase7_sessions, combined_sessions, 
        elective_sessions, classrooms
    )
    
    if conflicts:
        print(f"\n=== ROOM CONFLICT DETECTION ===")
        print(f"WARNING: Found {len(conflicts)} classroom conflict(s):")
        for idx, conflict in enumerate(conflicts, 1):
            print(f"  CONFLICT {idx}: Room {conflict['room']} on {conflict['day']} ({conflict['period']}) at {conflict['time']}")
            print(f"    - {conflict['course1']} ({conflict['section1']})")
            print(f"    - {conflict['course2']} ({conflict['section2']})")
        print()
    else:
        print("  [OK] No classroom conflicts detected")
        print()
    
    # Step 3.4: Repair lab double-booking (target: 0)
    print("Step 3.4: Repairing lab double-bookings (target: 0)...")
    try:
        # Build lab list
        def _is_lab_room(room):
            if getattr(room, "is_research_lab", False):
                return False
            rt = (room.room_type or "").lower()
            return (rt == "lab" or "lab" in rt) and room.room_number != COMBINED_RESERVED_ROOM_NUMBER

        lab_rooms_all = [r for r in (classrooms or []) if _is_lab_room(r)]

        def _np(p):
            if not p:
                return "PRE"
            v = str(p).strip().upper()
            if v in ("PREMID", "PRE"):
                return "PRE"
            if v in ("POSTMID", "POST"):
                return "POST"
            return v

        def _is_practical(sess: ScheduledSession) -> bool:
            cc_u = str(getattr(sess, "course_code", "") or "").upper()
            return getattr(sess, "kind", "") == "P" or "-LAB" in cc_u

        # occupancy: period -> lab -> day -> [(start,end, tag)]
        lab_occ = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        practicals = []
        for s in (phase5_sessions or []) + (phase7_sessions or []):
            if not getattr(s, "block", None):
                continue
            if not _is_practical(s):
                continue
            practicals.append(s)
            period_v = _np(getattr(s, "period", "PRE"))
            day = s.block.day
            st = s.block.start
            et = s.block.end
            room_raw = str(getattr(s, "room", "") or "").strip()
            labs = [x.strip() for x in room_raw.split(",") if x.strip()]
            for lr in labs:
                lab_occ[period_v][lr][day].append((st, et, f"{getattr(s,'course_code','')}/{getattr(s,'section','')}"))

        def _conflicts_now() -> List[Tuple[str, str, time, time, ScheduledSession, ScheduledSession]]:
            out = []
            # Pairwise check by (period, lab, day)
            for period_v, by_lab in lab_occ.items():
                for lab, by_day in by_lab.items():
                    for day, slots in by_day.items():
                        for i, (s1, e1, tag1) in enumerate(slots):
                            for (s2, e2, tag2) in slots[i + 1:]:
                                if _times_overlap(s1, e1, s2, e2) and tag1 != tag2:
                                    out.append((period_v, lab, max(s1, s2), min(e1, e2), day, None))  # placeholder
            return out

        # Simple pass-based repair: for each practical session, ensure its labs are conflict-free.
        max_passes = 8
        repaired = 0
        for _pass in range(max_passes):
            # Recompute conflicts by checking sessions directly (more reliable than occ tags)
            conflicts_sess = []
            # build index: (period, lab, day) -> list of sessions using it
            idx = defaultdict(list)
            for s in practicals:
                period_v = _np(getattr(s, "period", "PRE"))
                day = s.block.day
                room_raw = str(getattr(s, "room", "") or "").strip()
                labs = [x.strip() for x in room_raw.split(",") if x.strip()]
                for lr in labs:
                    idx[(period_v, lr, day)].append(s)
            for (period_v, lr, day), ss in idx.items():
                for i, a in enumerate(ss):
                    for b in ss[i + 1:]:
                        if not _times_overlap(a.block.start, a.block.end, b.block.start, b.block.end):
                            continue
                        if (getattr(a, "course_code", ""), getattr(a, "section", "")) == (getattr(b, "course_code", ""), getattr(b, "section", "")):
                            continue
                        conflicts_sess.append((period_v, lr, day, a, b))

            if not conflicts_sess:
                break

            for period_v, _lr, day, a, b in conflicts_sess:
                # Try to move session b to alternative labs
                base_code = (getattr(b, "course_code", "") or "").split("-")[0]
                num_secs = 1
                if hasattr(b, "section") and b.section:
                    # Approximation: if looking to reassign, we can just use 1 or try to derive.
                    # Since it's lab, capacity is constrained by lab size (45) anyway.
                    num_secs = 1
                enrollment = calculate_section_enrollment(b, sections, courses, num_sections=num_secs)

                # Filter lab pool by course type (match Phase 8 logic)
                if base_code.upper().startswith("EC"):
                    lab_pool = [r for r in lab_rooms_all if getattr(r, "lab_type", None) == "Hardware"]
                else:
                    lab_pool = [r for r in lab_rooms_all if getattr(r, "lab_type", None) in (None, "Software")]

                # Determine needed labs count (by capacity sum)
                lab_pool_desc = sorted(lab_pool, key=lambda r: r.capacity, reverse=True)
                if not lab_pool_desc:
                    continue
                needed_n = 0
                remaining = enrollment if enrollment and enrollment > 0 else 0
                for r in lab_pool_desc:
                    if remaining <= 0:
                        break
                    remaining -= r.capacity
                    needed_n += 1
                if needed_n == 0 and lab_pool_desc:
                    needed_n = 1
                needed_n = min(needed_n, len(lab_pool_desc)) if lab_pool_desc else 0
                needed_n = max(needed_n, 1) if lab_pool_desc else 0

                # Remove b's current labs from occupancy
                cur = [x.strip() for x in str(getattr(b, "room", "") or "").split(",") if x.strip()]
                for lr_name in cur:
                    try:
                        lab_occ[period_v][lr_name][day] = [
                            t for t in lab_occ[period_v][lr_name][day]
                            if not (t[0] == b.block.start and t[1] == b.block.end and base_code in str(t[2]))
                        ]
                    except Exception:
                        pass

                # Find replacement labs that are free for b's time
                chosen = []
                for r in sorted(lab_pool_desc, key=lambda x: x.capacity):  # prefer smaller first
                    if not check_room_conflict(r.room_number, period_v, day, b.block.start, b.block.end, room_occupancy):
                        chosen.append(r.room_number)
                        if len(chosen) >= needed_n:
                            break

                if chosen and chosen != cur:
                    b.room = ", ".join(chosen)
                    for lr_name in chosen:
                        mark_room_occupied(lr_name, period_v, day, b.block.start, b.block.end, base_code, room_occupancy)
                        lab_occ[period_v][lr_name][day].append((b.block.start, b.block.end, f"{getattr(b,'course_code','')}/{getattr(b,'section','')}"))
                    repaired += 1

            # next pass

        # Recheck
        idx2 = defaultdict(list)
        for s in practicals:
            period_v = _np(getattr(s, "period", "PRE"))
            day = s.block.day
            labs = [x.strip() for x in str(getattr(s, "room", "") or "").split(",") if x.strip()]
            for lr in labs:
                idx2[(period_v, lr, day)].append(s)
        remaining_conflicts = 0
        for (period_v, lr, day), ss in idx2.items():
            for i, a in enumerate(ss):
                for b in ss[i + 1:]:
                    if _times_overlap(a.block.start, a.block.end, b.block.start, b.block.end):
                        if (getattr(a, "course_code", ""), getattr(a, "section", "")) != (getattr(b, "course_code", ""), getattr(b, "section", "")):
                            remaining_conflicts += 1
        if remaining_conflicts == 0:
            print(f"  [OK] Lab double-booking repaired to 0 (moves applied: {repaired})")
        else:
            print(f"  [WARN] Lab double-booking still present: {remaining_conflicts} pair(s) (moves applied: {repaired})")
    except Exception as _lab_fix_e:
        print(f"  WARNING: lab double-booking repair failed: {_lab_fix_e}")
    print()

    # Step 3.5: Lab conflict detection and logging
    print("Step 3.5: Detecting lab conflicts...")
    lab_conflicts = detect_lab_conflicts(phase5_sessions, phase7_sessions, room_assignments)
    if lab_conflicts:
        print(f"\n=== LAB CONFLICT DETECTION ===")
        print(f"WARNING: Found {len(lab_conflicts)} lab conflict(s):")
        for idx, c in enumerate(lab_conflicts, 1):
            print(f"  LAB CONFLICT {idx}: Lab {c['room']} on {c['day']} ({c['period']}) at {c['time']}")
            print(f"    - {c['course1']} ({c['section1']})")
            print(f"    - {c['course2']} ({c['section2']})")
        print()
        # Log to file
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out_dir = os.path.join(base_dir, "DATA", "OUTPUT")
            os.makedirs(out_dir, exist_ok=True)
            log_path = os.path.join(out_dir, f"lab_conflicts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"LAB CONFLICTS: {len(lab_conflicts)} conflict(s)\n")
                f.write("=" * 60 + "\n")
                for idx, c in enumerate(lab_conflicts, 1):
                    f.write(f"  {idx}. Lab {c['room']} on {c['day']} ({c['period']}) at {c['time']}\n")
                    f.write(f"     - {c['course1']} ({c['section1']})\n")
                    f.write(f"     - {c['course2']} ({c['section2']})\n")
            print(f"  Lab conflicts logged to: {log_path}")
        except Exception as e:
            print(f"  WARNING: Could not write lab conflict log file: {e}")
        print()
    else:
        print("  [OK] No lab conflicts detected")
        print()
    
    # Step 4: Summary
    print("=== PHASE 8 COMPLETED ===")
    print(f"Room assignments completed for {total_assignments} courses")
    if conflicts:
        print(f"WARNING: {len(conflicts)} conflict(s) detected - see details above")
    print()
    
    # Attach room to phase5 and phase7 sessions before returning
    for s in phase5_sessions + phase7_sessions:
        if not hasattr(s, 'course_code') or not hasattr(s, 'section'):
            continue
        # Only assign if not already assigned (e.g. combined courses already got C004)
        if hasattr(s, 'room') and s.room and str(s.room).strip().lower() not in ('', 'none', 'tbd', 'nan'):
            continue
            
        base_code = s.course_code.split('-')[0]
        sec_str = str(s.section).strip()
        period_val = s.period or 'PRE'
        pv = str(period_val).strip().upper()
        if pv in ("PREMID", "PRE"):
            period_val = "PRE"
        elif pv in ("POSTMID", "POST"):
            period_val = "POST"
        else:
            period_val = pv
            
        key = (base_code, sec_str, period_val)
        if key in room_assignments:
            kind = getattr(s, 'kind', getattr(s, 'session_type', 'L')).strip().upper()
            if kind == 'P':
                labs = room_assignments[key].get("labs", [])
                s.room = ", ".join(labs) if labs else ""
            else:
                s.room = room_assignments[key].get("classroom", "")

    return room_assignments

