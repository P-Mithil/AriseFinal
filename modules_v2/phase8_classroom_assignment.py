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
    if period not in room_occupancy:
        return False
    
    if room_number not in room_occupancy[period]:
        return False
    
    if day not in room_occupancy[period][room_number]:
        return False
    
    # Check for overlaps
    for occupied_start, occupied_end, _ in room_occupancy[period][room_number][day]:
        # Check if times overlap
        if not (end_time <= occupied_start or start_time >= occupied_end):
            return True
    
    return False


def calculate_section_enrollment(session: ScheduledSession, sections: List[Section]) -> int:
    """
    Calculate enrollment count for the section in the session.
    
    Returns:
        Enrollment count (typically 30)
    """
    section_label = session.section
    for section in sections:
        if section.label == section_label:
            return section.students
    # Default fallback
    return 30


def find_available_classroom(capacity_needed: int, session_type: str, period: str,
                            day: str, time_block: TimeBlock,
                            room_occupancy: Dict[str, Dict[str, Dict[str, List[Tuple[time, time, str]]]]],
                            classrooms: List[ClassRoom]) -> Optional[str]:
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
    
    Returns:
        Room number if available, None otherwise
    """
    start_time = time_block.start
    end_time = time_block.end
    
    if session_type == "P":
        # Need lab
        labs = [room for room in classrooms 
                if room.room_type.lower() == 'lab' 
                and room.capacity >= 40
                and room.room_number != 'C004']  # Exclude C004
        
        # Sort by capacity (prefer smaller labs first)
        labs.sort(key=lambda r: r.capacity)
        
        for lab in labs:
            if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                return lab.room_number
    else:
        # Need classroom - exclude ALL 240-seater rooms (capacity >= 240), exclude labs
        classrooms_filtered = [room for room in classrooms 
                              if room.room_type.lower() != 'lab' 
                              and 'lab' not in room.room_type.lower()
                              and room.capacity >= capacity_needed
                              and room.capacity < 240]  # Exclude all 240-seaters
        
        # Sort: prefer <120 capacity first, then others (both sorted by capacity ascending)
        classrooms_filtered.sort(key=lambda r: (r.capacity >= 120, r.capacity))
        
        for room in classrooms_filtered:
            if not check_room_conflict(room.room_number, period, day, start_time, end_time, room_occupancy):
                return room.room_number
    
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
            and room.capacity >= 40
            and room.room_number != 'C004']  # Exclude C004
    
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

    # Lab room filter: type contains 'lab', capacity >= 40, not C004, exclude research labs
    def _is_lab_room(room):
        if getattr(room, 'is_research_lab', False):
            return False
        rt = (room.room_type or "").lower()
        return (rt == 'lab' or 'lab' in rt) and room.capacity >= 40 and room.room_number != 'C004'

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

        if assigned_labs:
            labs_str = ", ".join(sorted(assigned_labs))
            for sess in sessions:
                sess['room'] = labs_str
            if len(assigned_labs) < 2 and labs:
                print(f"  [Combined Labs] NOTE: {course_code_base} ({period} {day}): only {len(assigned_labs)} lab(s) assigned")

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
        if session.room == 'C004':
            continue
        
        # Get time block
        if not hasattr(session, 'block') or not session.block:
            continue
        
        # Strip suffix from course_code (-LAB, -TUT) to use base course code for grouping
        course_code = session.course_code.split('-')[0]  # Remove -LAB/-TUT suffix
        section = session.section
        period = session.period or 'PRE'
        
        key = (course_code, section, period)
        sessions_by_course[key].append(session)
    
    # Process each course group
    debug_samples = []  # Collect sample keys for debugging
    for key, course_sessions in sessions_by_course.items():
        course_code, section, period = key
        
        # Initialize assignment
        room_assignments[key] = {
            "classroom": None,
            "labs": []
        }
        
        # Separate sessions by type
        lecture_tutorial_sessions = [s for s in course_sessions if s.kind in ["L", "T"]]
        practical_sessions = [s for s in course_sessions if s.kind == "P"]
        
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
            # Get enrollment from first session
            enrollment = calculate_section_enrollment(lecture_tutorial_sessions[0], sections)
            
            # Get all suitable classrooms - exclude ALL 240-seater rooms (capacity >= 240), exclude labs
            suitable_classrooms = [room for room in classrooms
                                   if room.room_type.lower() != 'lab' 
                                   and 'lab' not in room.room_type.lower()
                                   and room.capacity >= enrollment
                                   and room.capacity < 240]  # Exclude all 240-seaters
            # Sort: prefer <120 capacity first, then others (both sorted by capacity ascending)
            suitable_classrooms.sort(key=lambda r: (r.capacity >= 120, r.capacity))
            
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
            
            # If no single room available for all sessions, assign rooms per session
            if not assigned_classroom:
                # Assign a room for each session individually (distribute across multiple rooms)
                session_rooms = []
                for session in lecture_tutorial_sessions:
                    time_block = session.block
                    day = time_block.day
                    start_time = time_block.start
                    end_time = time_block.end
                    
                    # Try to find an available room for this session (suitable_classrooms already filtered)
                    session_room = None
                    for classroom in suitable_classrooms:
                        if not check_room_conflict(classroom.room_number, period, day, start_time, end_time, room_occupancy):
                            session_room = classroom.room_number
                            mark_room_occupied(session_room, period, day, start_time, end_time, course_code, room_occupancy)
                            break
                    
                    if session_room:
                        session_rooms.append(session_room)
                
                # Use the most common room (or first if all different)
                if session_rooms:
                    from collections import Counter
                    room_counts = Counter(session_rooms)
                    assigned_classroom = room_counts.most_common(1)[0][0]
            
            if assigned_classroom:
                room_assignments[key]["classroom"] = assigned_classroom
                # Also update session.room for all sessions
                for session in lecture_tutorial_sessions:
                    session.room = assigned_classroom
        
        # Assign labs for practicals - ALWAYS assign 2 labs for practical courses
        if practical_sessions:
            # Lab room filter: type contains 'lab', capacity >= 40, not C004, exclude research labs
            def _is_lab_room(room):
                if getattr(room, 'is_research_lab', False):
                    return False
                rt = (room.room_type or "").lower()
                return (rt == 'lab' or 'lab' in rt) and room.capacity >= 40 and room.room_number != 'C004'
            all_labs = [r for r in classrooms if _is_lab_room(r)]
            # Filter by course code: EC -> Hardware; CS/DS/other -> Software
            course_code_base = key[0]
            if course_code_base.upper().startswith("EC"):
                labs = [room for room in all_labs if getattr(room, 'lab_type', None) == 'Hardware']
                if not labs:
                    labs = list(all_labs)
                    if labs:
                        print(f"  [Phase 8] WARNING: No Hardware labs for EC course {course_code_base}; using any lab.")
            else:
                labs = [room for room in all_labs if getattr(room, 'lab_type', None) in (None, 'Software')]
                if not labs:
                    labs = list(all_labs)
                    if labs:
                        print(f"  [Phase 8] WARNING: No Software labs for {course_code_base}; using any lab.")
            # Sort by occupancy count (ascending) to minimize conflicts, then by capacity
            def _lab_occupancy_count(room):
                n = 0
                if period in room_occupancy and room.room_number in room_occupancy[period]:
                    for day in room_occupancy[period][room.room_number]:
                        n += len(room_occupancy[period][room.room_number][day])
                return n
            labs.sort(key=lambda r: (_lab_occupancy_count(r), r.capacity))
            
            # Strategy: Try to find 2 labs that can be used across all practical sessions
            # If not possible, assign 2 labs per session and collect unique labs
            all_assigned_labs = set()
            
            # First, try to find labs available for all sessions (preferred)
            labs_available_for_all = []
            for lab in labs:
                available_for_all_sessions = True
                for session in practical_sessions:
                    time_block = session.block
                    day = time_block.day
                    start_time = time_block.start
                    end_time = time_block.end
                    if check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                        available_for_all_sessions = False
                        break
                
                if available_for_all_sessions:
                    labs_available_for_all.append(lab.room_number)
                    if len(labs_available_for_all) >= 2:
                        break
            
            if len(labs_available_for_all) >= 2:
                # Use the same 2 labs for all sessions
                assigned_labs = labs_available_for_all[:2]
                # Mark labs as occupied for all practical sessions
                for lab in assigned_labs:
                    for session in practical_sessions:
                        time_block = session.block
                        day = time_block.day
                        start_time = time_block.start
                        end_time = time_block.end
                        mark_room_occupied(lab, period, day, start_time, end_time, course_code, room_occupancy)
                all_assigned_labs.update(assigned_labs)
            else:
                # Fallback: Assign 2 labs per session, then collect unique labs
                for session in practical_sessions:
                    time_block = session.block
                    day = time_block.day
                    start_time = time_block.start
                    end_time = time_block.end
                    
                    # Find 2 available labs for this session
                    session_labs = []
                    for lab in labs:
                        if lab.room_number in all_assigned_labs:
                            continue  # Skip if already assigned
                        if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                            session_labs.append(lab.room_number)
                            mark_room_occupied(lab.room_number, period, day, start_time, end_time, course_code, room_occupancy)
                            if len(session_labs) >= 2:
                                break
                    
                    all_assigned_labs.update(session_labs)
                    
                    # If we've found 2 labs total, we can stop (but continue marking for other sessions)
                    if len(all_assigned_labs) >= 2 and len(session_labs) > 0:
                        # For remaining sessions, try to reuse the same labs
                        for remaining_session in practical_sessions[practical_sessions.index(session) + 1:]:
                            remaining_time_block = remaining_session.block
                            remaining_day = remaining_time_block.day
                            remaining_start = remaining_time_block.start
                            remaining_end = remaining_time_block.end
                            
                            # Try to reuse the same labs
                            for lab in list(all_assigned_labs)[:2]:
                                if not check_room_conflict(lab, period, remaining_day, remaining_start, remaining_end, room_occupancy):
                                    mark_room_occupied(lab, period, remaining_day, remaining_start, remaining_end, course_code, room_occupancy)
                        break
            
            # Ensure we have exactly 2 labs (or as many as we found)
            if all_assigned_labs:
                room_assignments[key]["labs"] = sorted(list(all_assigned_labs))[:2]
                assigned_labs_str = ", ".join(room_assignments[key]["labs"])
                for session in practical_sessions:
                    session.room = assigned_labs_str
                # Debug: Update debug_samples with lab info
                for sample in debug_samples:
                    if sample['key'] == key:
                        sample['labs'] = room_assignments[key]["labs"]
                        sample['has_labs'] = True
                        break
            else:
                # Last resort: assign any available labs (even if not ideal)
                final_labs = []
                for session in practical_sessions:
                    time_block = session.block
                    day = time_block.day
                    for lab in labs:
                        if lab.room_number not in final_labs:
                            start_time = time_block.start
                            end_time = time_block.end
                            if not check_room_conflict(lab.room_number, period, day, start_time, end_time, room_occupancy):
                                final_labs.append(lab.room_number)
                                mark_room_occupied(lab.room_number, period, day, start_time, end_time, course_code, room_occupancy)
                                if len(final_labs) >= 2:
                                    break
                    if len(final_labs) >= 2:
                        break
                if final_labs:
                    room_assignments[key]["labs"] = sorted(final_labs[:2])
                    assigned_labs_str = ", ".join(room_assignments[key]["labs"])
                    for session in practical_sessions:
                        session.room = assigned_labs_str
                    # Debug: Update debug_samples with lab info
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
    Returns list of conflict dicts: room, period, day, time, course1, course2, section1, section2.
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
            if p1 != p2:
                continue
            # Same room, same period (both PRE or both POST), same day
            if (session1['room'] == session2['room'] and session1['day'] == session2['day']):
                
                # Check if times overlap
                start1, end1 = session1['start'], session1['end']
                start2, end2 = session2['start'], session2['end']
                
                if not (end1 <= start2 or start1 >= end2):
                    # Normalize course codes (strip suffixes like -LAB, -TUT)
                    course1_base = session1['course'].split('-')[0] if session1['course'] else ''
                    course2_base = session2['course'].split('-')[0] if session2['course'] else ''
                    # Only flag conflict if different courses (same course with different sections is OK)
                    if course1_base != course2_base:
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
    
    return room_assignments

