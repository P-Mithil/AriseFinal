"""
Generate 24 sheets for IIIT Dharwad Timetable v2
Creates sheets for all sections, semesters, and periods with dynamic grid format.
"""

import os
import re
import sys
import logging
from datetime import time, datetime, timedelta
from typing import List, Dict

# Add the current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.data_models import DayScheduleGrid, TimeBlock, Section
from utils.timetable_writer_v2 import TimetableWriterV2
from config.schedule_config import WORKING_DAYS, DAY_START_TIME, DAY_END_TIME
from config.structure_config import DEPARTMENTS, SECTIONS_BY_DEPT, STUDENTS_PER_SECTION, get_group_for_section
from utils.faculty_timetable_writer import write_faculty_timetables
from utils.classroom_timetable_writer import write_classroom_timetables
from utils.faculty_conflict_resolver import resolve_all_faculty_conflicts
from modules_v2.phase1_data_validation_v2 import run_phase1
from modules_v2.phase3_elective_baskets_v2 import run_phase3
from modules_v2.phase4_combined_classes_v2_corrected import run_phase4_corrected as run_phase4
from modules_v2.phase5_core_courses import run_phase5
from modules_v2.phase6_faculty_conflicts import run_phase6_faculty_conflicts
from utils.data_models import Course

# Trace config for unsatisfied debugging: list of (section_name, semester, period, course_code).
# Set by trace_unsatisfied.py before running generate_24_sheets.
TRACE_CONFIG: List[tuple] = []


def _trace_enabled(section_name: str, semester: int, period: str) -> List[str]:
    """Return list of course codes we're tracing for this (section, semester, period)."""
    out = []
    period_norm = period.strip().upper() if period else ""
    if period_norm in ("PREMID", "PRE"):
        period_norm = "PRE"
    elif period_norm in ("POSTMID", "POST"):
        period_norm = "POST"
    for sec, sem, prd, code in TRACE_CONFIG:
        if sec != section_name or sem != semester:
            continue
        p = str(prd).strip().upper() if prd else ""
        if p in ("PREMID", "PRE"):
            p = "PRE"
        elif p in ("POSTMID", "POST"):
            p = "POST"
        if p == period_norm:
            out.append(code)
    return out


def _course_matches_trace(display: str, base: str, traced_codes: List[str]) -> bool:
    """Check if this session (display/base) matches any traced course code."""
    if not traced_codes:
        return False
    for tc in traced_codes:
        if isinstance(tc, str) and tc.startswith("ELECTIVE_BASKET_"):
            b = (base or "").replace("-TUT", "").replace("-LAB", "")
            t = tc.replace("-TUT", "").replace("-LAB", "")
            if b == t or (b.startswith("ELECTIVE_BASKET_") and b == t):
                return True
        else:
            base_core = (base or "").split("-")[0] if isinstance(base, str) else ""
            disp_core = (display or "").replace("-TUT", "").replace("-LAB", "").split("-")[0]
            if base_core == tc or disp_core == tc or base == tc or (display or "").split("-")[0] == tc:
                return True
    return False


def create_sample_schedule(day: str, semester: int, section: str) -> DayScheduleGrid:
    """Create a sample schedule for a specific day, semester, and section
    NOTE: This function is for testing only. All actual scheduling is now dynamic.
    Returns empty grid as all scheduling is handled dynamically by the phases.
    """
    grid = DayScheduleGrid(day, semester)
    # All scheduling is now dynamic - no hardcoded sample schedules
    # Add lunch break (only hardcoded exception as per requirements)
    lunch_block = grid.lunch_block
    grid.sessions.append((lunch_block, "LUNCH"))
    return grid

def identify_course_sync_type(course_code: str, semester: int, courses: List[Course], sections: List[Section]) -> str:
    """
    Dynamically identify course synchronization type based on:
    - Course appears in which departments
    - Course credits <= 2
    - Course has 1 faculty
    
    Returns:
    - 'cross_group': Course common in ALL sections (CSE + DSAI/ECE) - CSE PreMid ↔ DSAI/ECE PostMid
    - 'within_group_cse': Course common only in CSE (CSE-A + CSE-B) - CSE-A PreMid ↔ CSE-B PostMid
    - 'within_group_other': Course common only in one department of Group 2 - similar sync
    - 'standard': Default - all sections get same period
    """
    if not courses:
        return 'standard'
    
    # Find all instances of this course
    course_instances = [c for c in courses if c.code == course_code and c.semester == semester]
    if not course_instances:
        return 'standard'
    
    # Check if course meets criteria: <=2 credits, 1 faculty
    first_course = course_instances[0]
    if first_course.credits > 2 or len(first_course.instructors) != 1:
        return 'standard'  # Doesn't meet criteria for special sync
    
    # Get departments that have this course
    departments_with_course = set(c.department for c in course_instances)
    
    # Check which groups have this course
    group1_sections = [s for s in sections if s.semester == semester and s.program == 'CSE']
    group2_sections = [s for s in sections if s.semester == semester and s.program in ['DSAI', 'ECE']]
    
    group1_has_course = 'CSE' in departments_with_course
    group2_has_course = any(dept in departments_with_course for dept in ['DSAI', 'ECE'])
    
    # Determine sync type
    if group1_has_course and group2_has_course:
        # Course appears in both groups (CSE + DSAI/ECE) - CROSS-GROUP synchronization
        # CSE PreMid ↔ DSAI/ECE PostMid (opposite periods)
        return 'cross_group'
    elif group1_has_course and len(group1_sections) > 1:
        # Course appears only in CSE and CSE has multiple sections (CSE-A, CSE-B)
        # WITHIN-GROUP synchronization: CSE-A PreMid ↔ CSE-B PostMid
        return 'within_group_cse'
    elif group2_has_course and len(group2_sections) > 1:
        # Course appears only in Group 2 and Group 2 has multiple sections
        # WITHIN-GROUP synchronization
        return 'within_group_other'
    else:
        # Standard - all sections get same period
        return 'standard'

def map_corrected_schedule_to_sessions(schedule: Dict, sections: List[Section], periods: List[str], courses: List = None, classrooms: List = None) -> List[dict]:
    """
    Convert corrected Phase 4 schedule format to session dicts.

    NOTE: Phase 4 behavior (grouping + period choices) is fully encoded in the Phase 4
    schedule slots. The mapping must trust those slots 1:1 to avoid introducing
    cross-group remapping bugs.
    """
    return map_corrected_schedule_to_sessions_v2(
        schedule=schedule,
        sections=sections,
        periods=periods,
        courses=courses,
        classrooms=classrooms,
    )

    # Legacy implementation below (kept for reference only; unreachable).
    from modules_v2.phase4_combined_classes_v2_corrected import calculate_slots_from_ltpsc
    sessions = []
    
    # Get lab rooms for practicals - CRITICAL: Must find lab rooms, never use classrooms
    lab_rooms = []
    if classrooms:
        lab_rooms = [r for r in classrooms if hasattr(r, 'room_type') and r.room_type.lower() == 'lab']
    
    # If no lab rooms found, try alternative attribute names
    if not lab_rooms and classrooms:
        lab_rooms = [r for r in classrooms if hasattr(r, 'room_type') and 'lab' in str(r.room_type).lower()]
    
    # If still no lab rooms, try to find by room number pattern (L105, L106, etc.)
    if not lab_rooms and classrooms:
        for r in classrooms:
            room_num = r.room_number if hasattr(r, 'room_number') else str(r)
            if str(room_num).startswith('L') and room_num[1:].isdigit():
                lab_rooms.append(r)
    
    # Helper function to get lab room for practicals
    def get_lab_room_for_practical():
        """Get a lab room for practicals, never return a classroom"""
        if lab_rooms:
            return lab_rooms[0].room_number if hasattr(lab_rooms[0], 'room_number') else str(lab_rooms[0])
        # Fallback: return a default lab room name if no labs found
        return 'L105'  # Default lab room
    
    for semester, sem_data in schedule.items():
        # Separate sections by group for synchronization
        group1_sections = [s for s in sections if s.semester == semester and s.program == 'CSE']  # CSE-A, CSE-B
        group2_sections = [s for s in sections if s.semester == semester and s.program in ['DSAI', 'ECE']]  # DSAI-A, ECE-A
        
        # Get PreMid and PostMid courses and slots
        premid_courses = sem_data.get('premid', {}).get('courses', [])
        postmid_courses = sem_data.get('postmid', {}).get('courses', [])
        premid_course_objects = sem_data.get('premid', {}).get('course_objects', [])
        postmid_course_objects = sem_data.get('postmid', {}).get('course_objects', [])
        premid_slots = sem_data.get('premid', {}).get('slots', [])
        postmid_slots = sem_data.get('postmid', {}).get('slots', [])
        default_room = sem_data.get('premid', {}).get('room', 'C004')
        
        # Helper function to format course code with suffixes based on duration
        def format_course_code(course_code, slot_index_in_course, start, end, slots_info):
            duration = (end.hour - start.hour) * 60 + (end.minute - start.minute)
            # Determine type based on slot index and LTPSC info
            if slot_index_in_course < slots_info['lectures']:
                return course_code  # Lecture
            elif slot_index_in_course < slots_info['lectures'] + slots_info['tutorials']:
                return f"{course_code}-TUT"  # Tutorial
            else:
                return f"{course_code}-LAB"  # Practical
        
        # Legacy dynamic synchronization (no longer used for Phase 4 mapping).
        # Kept only for backward compatibility; current pipeline uses
        # map_corrected_schedule_to_sessions_v2 instead.
        # PreMid courses: Apply dynamic synchronization based on course type
        for i, course_code in enumerate(premid_courses):
            course_obj = premid_course_objects[i]
            slots_info = calculate_slots_from_ltpsc(course_obj.ltpsc)
            
            # Dynamically identify synchronization type (legacy)
            sync_type = identify_course_sync_type(course_code, semester, courses or [], sections)
            
            # Filter slots by course code
            course_slots = []
            for slot in premid_slots:
                if len(slot) >= 6 and slot[0] == course_code:
                    course_slots.append(slot)
                elif len(slot) == 5:
                    pass  # Skip old format
            
            # Create unique time slots with room information
            unique_slots = []
            seen = set()
            for slot_info in course_slots:
                if len(slot_info) == 7:  # (course_code, day, start, end, session_type, section, room)
                    course_code_check, day, start, end, session_type, section_ref, assigned_room = slot_info
                elif len(slot_info) == 6:
                    if slot_info[0] == course_code:
                        course_code_check, day, start, end, session_type, section_ref = slot_info
                        assigned_room = default_room  # Fallback to default
                    else:
                        day, start, end, session_type, section_ref, assigned_room = slot_info
                elif len(slot_info) == 5:
                    day, start, end, session_type, section_ref = slot_info
                    assigned_room = default_room  # Fallback to default
                elif len(slot_info) == 4:
                    day, start, end, session_type = slot_info
                    assigned_room = default_room  # Fallback to default
                else:
                    day, start, end = slot_info
                    session_type = 'L'
                    assigned_room = default_room  # Fallback to default
                key = (day, start, end, session_type)
                if key not in seen:
                    seen.add(key)
                    unique_slots.append((day, start, end, session_type, assigned_room))
            
            # Apply synchronization logic based on course type
            for slot_data in unique_slots:
                if len(slot_data) == 5:
                    day, start, end, session_type, assigned_room = slot_data
                else:
                    day, start, end, session_type = slot_data[:4]
                    assigned_room = default_room
                if session_type == 'L':
                    display_code = course_code
                elif session_type == 'T':
                    display_code = f"{course_code}-TUT"
                elif session_type == 'P':
                    display_code = f"{course_code}-LAB"
                else:
                    display_code = course_code
                
                # Assign room - CRITICAL: Practicals MUST get lab rooms, never classrooms
                # Use room from slot if available, otherwise use default
                if session_type == 'P':
                    assigned_room = get_lab_room_for_practical()
                # else: use assigned_room from slot (already extracted above)
                
                instructor = course_obj.instructors[0] if course_obj.instructors else 'TBD'
                
                # Apply synchronization based on type
                if sync_type == 'cross_group':
                    # Cross-group: Group 1 PreMid ↔ Group 2 PostMid
                    # Group 1 gets PreMid, Group 2 gets PostMid
                    group1_matching = [s for s in group1_sections if s.semester == course_obj.semester]
                    group2_matching = [s for s in group2_sections if s.semester == course_obj.semester]
                    
                    # Group 1 (CSE) in PreMid
                    if group1_matching:
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in group1_matching],
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # DEBUG: Log PreMid assignment
                        print(f"DEBUG PreMid Cross-Group: {course_code} assigned to CSE sections {[s.label for s in group1_matching]} in PRE period at {day} {start}-{end}")
                    
                    # Group 2 (DSAI/ECE) in PostMid (same time slots, opposite period)
                    if group2_matching:
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in group2_matching],
                            'period': 'POST',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # DEBUG: Log PostMid assignment with synchronization confirmation
                        print(f"DEBUG PreMid Cross-Group: {course_code} assigned to DSAI/ECE sections {[s.label for s in group2_matching]} in POST period at {day} {start}-{end} (SYNCHRONIZED - same time slots as CSE PreMid)")
                
                elif sync_type == 'within_group_cse':
                    # Within-group CSE: CSE-A PreMid ↔ CSE-B PostMid
                    # CRITICAL: Always assign CSE-A to PreMid and CSE-B to PostMid (regardless of which period this course was assigned to in Phase 4)
                    cse_sections = sorted([s for s in group1_sections if s.semester == course_obj.semester], 
                                         key=lambda s: s.name)
                    if len(cse_sections) >= 2:
                        # CSE-A gets PreMid, CSE-B gets PostMid (always, regardless of Phase 4 assignment)
                        sessions.append({
                            'course_code': display_code,
                            'sections': [cse_sections[0].label],  # CSE-A
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        sessions.append({
                            'course_code': display_code,
                            'sections': [cse_sections[1].label],  # CSE-B
                            'period': 'POST',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # CSE-A in PreMid, CSE-B in PostMid (synchronized)
                    else:
                        # Fallback: all CSE sections get PreMid
                        matching_sections = [s for s in group1_sections if s.semester == course_obj.semester]
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in matching_sections],
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                
                else:
                    # Standard: All groups get same period (PreMid)
                    matching_sections = [s for s in group1_sections + group2_sections if s.semester == course_obj.semester]
                    sessions.append({
                        'course_code': display_code,
                        'sections': [s.label for s in matching_sections],
                        'period': 'PRE',
                        'day': day,
                        'time_block': TimeBlock(day, start, end),
                        'room': assigned_room,
                        'instructor': instructor,
                        'course_obj': course_obj,
                        'session_type': session_type
                    })
        
        # PostMid courses: Apply dynamic synchronization based on course type (legacy)
        for i, course_code in enumerate(postmid_courses):
            course_obj = postmid_course_objects[i]
            slots_info = calculate_slots_from_ltpsc(course_obj.ltpsc)
            
            # Dynamically identify synchronization type (legacy)
            sync_type = identify_course_sync_type(course_code, semester, courses or [], sections)
            
            # Filter slots by course code
            course_slots = []
            for slot in postmid_slots:
                if len(slot) >= 6 and slot[0] == course_code:
                    course_slots.append(slot)
                elif len(slot) == 5:
                    pass  # Skip old format
            
            # Create unique time slots with room information
            unique_slots = []
            seen = set()
            for slot_info in course_slots:
                if len(slot_info) == 7:  # (course_code, day, start, end, session_type, section, room)
                    course_code_check, day, start, end, session_type, section_ref, assigned_room = slot_info
                elif len(slot_info) == 6:
                    if slot_info[0] == course_code:
                        course_code_check, day, start, end, session_type, section_ref = slot_info
                        assigned_room = default_room  # Fallback to default
                    else:
                        day, start, end, session_type, section_ref, assigned_room = slot_info
                elif len(slot_info) == 5:
                    day, start, end, session_type, section_ref = slot_info
                    assigned_room = default_room  # Fallback to default
                elif len(slot_info) >= 4:
                    day, start, end, session_type = slot_info
                    assigned_room = default_room  # Fallback to default
                else:
                    day, start, end = slot_info
                    session_type = 'L'
                    assigned_room = default_room  # Fallback to default
                key = (day, start, end, session_type)
                if key not in seen:
                    seen.add(key)
                    unique_slots.append((day, start, end, session_type, assigned_room))
            
            # Apply synchronization logic based on course type
            session_count = {'L': 0, 'T': 0, 'P': 0}
            for slot_data in unique_slots:
                if len(slot_data) == 5:
                    day, start, end, session_type, assigned_room = slot_data
                else:
                    day, start, end, session_type = slot_data[:4]
                    assigned_room = default_room
                if session_type == 'L':
                    display_code = course_code
                elif session_type == 'T':
                    display_code = f"{course_code}-TUT"
                elif session_type == 'P':
                    display_code = f"{course_code}-LAB"
                else:
                    display_code = course_code
                
                # Assign room - CRITICAL: Practicals MUST get lab rooms, never classrooms
                # Use room from slot if available, otherwise use default
                if session_type == 'P':
                    assigned_room = get_lab_room_for_practical()
                # else: use assigned_room from slot (already extracted above)
                
                instructor = course_obj.instructors[0] if course_obj.instructors else 'TBD'
                
                # Apply synchronization based on type
                if sync_type == 'cross_group':
                    # Cross-group: Group 1 PostMid ↔ Group 2 PreMid (opposite of PreMid courses)
                    # For PostMid courses: CSE (Group 1) gets PostMid, DSAI/ECE (Group 2) gets PreMid
                    # This is the OPPOSITE of PreMid courses where CSE gets PreMid and DSAI/ECE gets PostMid
                    group1_matching = [s for s in group1_sections if s.semester == course_obj.semester]
                    group2_matching = [s for s in group2_sections if s.semester == course_obj.semester]
                    
                    # Group 1 (CSE) in PostMid
                    if group1_matching:
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in group1_matching],
                            'period': 'POST',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # DEBUG: Log PostMid assignment
                        print(f"DEBUG PostMid Cross-Group: {course_code} assigned to CSE sections {[s.label for s in group1_matching]} in POST period at {day} {start}-{end}")
                    
                    # Group 2 (DSAI/ECE) in PreMid (same time slots, opposite period)
                    if group2_matching:
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in group2_matching],
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # DEBUG: Log PreMid assignment with synchronization confirmation
                        print(f"DEBUG PostMid Cross-Group: {course_code} assigned to DSAI/ECE sections {[s.label for s in group2_matching]} in PRE period at {day} {start}-{end} (SYNCHRONIZED - same time slots as CSE PostMid)")
                
                elif sync_type == 'within_group_cse':
                    # Within-group CSE: CSE-A PreMid ↔ CSE-B PostMid
                    # CRITICAL: Always assign CSE-A to PreMid and CSE-B to PostMid (regardless of which period this course was assigned to in Phase 4)
                    # Even if this course was assigned to PostMid in Phase 4, we still want CSE-A in PreMid and CSE-B in PostMid
                    cse_sections = sorted([s for s in group1_sections if s.semester == course_obj.semester], 
                                         key=lambda s: s.name)
                    if len(cse_sections) >= 2:
                        # CSE-A gets PreMid, CSE-B gets PostMid (always, regardless of Phase 4 assignment)
                        sessions.append({
                            'course_code': display_code,
                            'sections': [cse_sections[0].label],  # CSE-A
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        sessions.append({
                            'course_code': display_code,
                            'sections': [cse_sections[1].label],  # CSE-B
                            'period': 'POST',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                        # CSE-A in PreMid, CSE-B in PostMid (synchronized - same time slots as PostMid course)
                    else:
                        # Fallback: all CSE sections get PreMid
                        matching_sections = [s for s in group1_sections if s.semester == course_obj.semester]
                        sessions.append({
                            'course_code': display_code,
                            'sections': [s.label for s in matching_sections],
                            'period': 'PRE',
                            'day': day,
                            'time_block': TimeBlock(day, start, end),
                            'room': assigned_room,
                            'instructor': instructor,
                            'course_obj': course_obj,
                            'session_type': session_type
                        })
                
                else:
                    # Standard: All groups get same period (PostMid)
                    matching_sections = [s for s in group1_sections + group2_sections if s.semester == course_obj.semester]
                    sessions.append({
                        'course_code': display_code,
                        'sections': [s.label for s in matching_sections],
                        'period': 'POST',
                        'day': day,
                        'time_block': TimeBlock(day, start, end),
                        'room': assigned_room,
                        'instructor': instructor,
                        'course_obj': course_obj,
                        'session_type': session_type
                    })
                
                # Track session creation for all courses
                session_count[session_type] = session_count.get(session_type, 0) + 1
            
            # Log session creation summary for debugging
            if len(unique_slots) > 0:
                print(f"DEBUG {course_code}: Created {len(unique_slots)} unique sessions - L:{session_count.get('L', 0)}, T:{session_count.get('T', 0)}, P:{session_count.get('P', 0)}")
                print(f"DEBUG {course_code}: Expected - L:{slots_info['lectures']}, T:{slots_info['tutorials']}, P:{slots_info['practicals']}")
    
    return sessions


def map_corrected_schedule_to_sessions_v2(
    schedule: Dict,
    sections: List[Section],
    periods: List[str],
    courses: List = None,
    classrooms: List = None,
) -> List[dict]:
    """
    New mapping for Phase 4 combined classes.

    This helper TRUSTS the Phase 4 schedule completely:
    - It does not swap PreMid/PostMid between groups.
    - It does not infer grouping from departments.
    - It simply flattens Phase 4 slots into session dicts, 1:1.
    """
    sessions: List[dict] = []

    # Prepare lab rooms (for practicals that do not already specify a lab)
    lab_rooms = []
    if classrooms:
        lab_rooms = [
            r
            for r in classrooms
            if hasattr(r, "room_type") and "lab" in str(r.room_type).lower()
        ]

    def get_lab_room_for_practical(default_room: str) -> str:
        if lab_rooms:
            room = lab_rooms[0]
            return getattr(room, "room_number", str(room))
        return default_room or "L105"

    for semester, sem_data in schedule.items():
        premid = sem_data.get("premid", {}) or {}
        postmid = sem_data.get("postmid", {}) or {}

        premid_course_objects = premid.get("course_objects", []) or []
        postmid_course_objects = postmid.get("course_objects", []) or []
        premid_slots = premid.get("slots", []) or []
        postmid_slots = postmid.get("slots", []) or []

        default_room_premid = premid.get("room", "C004")
        default_room_postmid = postmid.get("room", "C004")

        # Index course objects by (code, semester)
        course_map: Dict[tuple, object] = {}
        for obj in list(premid_course_objects) + list(postmid_course_objects):
            code = getattr(obj, "code", None)
            if code:
                course_map[(code, semester)] = obj

        def course_obj_for(code: str):
            return course_map.get((code, semester))

        def add_slot_list(slot_list, period_flag: str, default_room: str) -> None:
            """
            Map a list of Phase 4 slots into sessions.
            Primary expected format:
              (course_code, day, start, end, session_type, section, room)
            Older variants are handled defensively.
            """
            for slot in slot_list:
                if not slot:
                    continue

                course_code = None
                day = None
                start = None
                end = None
                session_type = "L"
                section_ref = None
                room = default_room

                if len(slot) >= 7 and isinstance(slot[0], str):
                    course_code, day, start, end, session_type, section_ref, room = slot[:7]
                elif len(slot) == 6:
                    if isinstance(slot[0], str):
                        # (course_code, day, start, end, session_type, section)
                        course_code, day, start, end, session_type, section_ref = slot
                        room = default_room
                    else:
                        # (day, start, end, session_type, section, room)
                        day, start, end, session_type, section_ref, room = slot
                elif len(slot) == 5:
                    # (day, start, end, session_type, section)
                    day, start, end, session_type, section_ref = slot
                    room = default_room
                elif len(slot) == 4:
                    # (day, start, end, session_type)
                    day, start, end, session_type = slot
                    room = default_room
                elif len(slot) >= 3:
                    # (day, start, end)
                    day, start, end = slot[:3]
                    session_type = "L"
                    room = default_room

                if course_code is None:
                    continue  # cannot safely map

                # Resolve section label
                if hasattr(section_ref, "label"):
                    section_label = section_ref.label
                else:
                    section_label = str(section_ref) if section_ref is not None else ""

                # Determine display code
                if session_type == "T":
                    display_code = f"{course_code}-TUT"
                elif session_type == "P":
                    display_code = f"{course_code}-LAB"
                else:
                    display_code = course_code

                # Ensure practicals use a lab room
                if session_type == "P":
                    room = room or get_lab_room_for_practical(default_room)

                course_obj = course_obj_for(course_code)
                instructor = (
                    course_obj.instructors[0]
                    if course_obj and getattr(course_obj, "instructors", None)
                    else "TBD"
                )

                time_block = TimeBlock(day, start, end)

                sessions.append(
                    {
                        "course_code": display_code,
                        "sections": [section_label] if section_label else [],
                        "period": period_flag,
                        "day": day,
                        "time_block": time_block,
                        "room": room,
                        "instructor": instructor,
                        "course_obj": course_obj,
                        "session_type": session_type,
                    }
                )

        add_slot_list(premid_slots, "PRE", default_room_premid)
        add_slot_list(postmid_slots, "POST", default_room_postmid)

    return sessions

# ============================================================================
# RESCHEDULING HELPER FUNCTIONS
# ============================================================================

def validate_one_day_one_session_rule(candidate_slot: TimeBlock, course_display: str, base_course: str,
                                       session_type: str, day_sessions: List[tuple], 
                                       base_course_code_func) -> bool:
    """
    Validate if a rescheduled slot complies with one-day-one-session rule.
    
    Rules:
    - Cannot have 2 lectures on same day for same course
    - Cannot have 2 tutorials on same day for same course
    - Cannot have lecture + tutorial on same day for same course
    - CAN have lecture + practical on same day
    - CAN have tutorial + practical on same day
    
    Args:
        candidate_slot: TimeBlock for the candidate slot
        course_display: Course display code (e.g., "CS307", "CS307-TUT")
        base_course: Base course code (e.g., "CS307")
        session_type: Session type ('L', 'T', or 'P')
        day_sessions: List of sessions already on this day
        base_course_code_func: Function to extract base course code
    
    Returns:
        True if rule is satisfied, False if violated
    """
    # Check all sessions on this day for the same course
    for existing in day_sessions:
        existing_base = base_course_code_func(existing[1])
        if existing_base == base_course:
            # Same course already has a session on this day
            existing_type = 'P' if '-LAB' in existing[1] else ('T' if '-TUT' in existing[1] else 'L')
            
            # Rule: Cannot have 2 lectures on same day
            if session_type == 'L' and existing_type == 'L':
                return False
            
            # Rule: Cannot have 2 tutorials on same day
            if session_type == 'T' and existing_type == 'T':
                return False
            
            # Rule: Cannot have lecture + tutorial on same day
            if (session_type == 'L' and existing_type == 'T') or (session_type == 'T' and existing_type == 'L'):
                return False
            
            # Lecture + Practical and Tutorial + Practical are allowed, so continue checking
    
    # No rule violation found
    return True

def get_available_slots_for_rescheduling(day: str, semester: int, existing_sessions: List[tuple], 
                                         session_duration_minutes: int, session_type: str) -> List[TimeBlock]:
    """
    Generate available time slots for rescheduling a session.
    
    Args:
        day: Day of the week (Monday-Friday)
        semester: Semester number (1, 3, or 5)
        existing_sessions: List of (TimeBlock, course_display, priority, base_course) tuples already scheduled
        session_duration_minutes: Duration of the session in minutes (90 for lectures, 60 for tutorials, 120 for practicals)
        session_type: 'L' for lecture, 'T' for tutorial, 'P' for practical
    
    Returns:
        List of available TimeBlocks that don't conflict with existing sessions
    """
    available_slots = []
    
    # Generate time slots for the day (9:00-18:00, 15-minute intervals)
    start_hour, end_hour = 9, 18
    current_time = time(start_hour, 0)
    end_time = time(end_hour, 0)
    
    # Get lunch block for this semester
    lunch_blocks = {
        1: TimeBlock(day, time(12, 30), time(13, 30)),
        3: TimeBlock(day, time(12, 45), time(13, 45)),
        5: TimeBlock(day, time(13, 0), time(14, 0))
    }
    lunch_block = lunch_blocks.get(semester, TimeBlock(day, time(12, 30), time(13, 30)))
    
    # Get elective basket slots for this semester (find all groups for this semester)
    try:
        from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
        elective_slots = []
        # Find all groups that belong to this semester
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
        
        for group_key in matching_groups:
            slots = ELECTIVE_BASKET_SLOTS[group_key]
            for slot_type in ['lecture_1', 'lecture_2', 'tutorial']:
                if slot_type in slots:
                    elective_slot = slots[slot_type]
                    if elective_slot.day == day:
                        elective_slots.append(elective_slot)
    except:
        elective_slots = []
    
    # Generate candidate slots
    current_dt = datetime.combine(datetime.min, current_time)
    end_dt = datetime.combine(datetime.min, end_time)
    
    while current_dt < end_dt:
        # Calculate end time for this slot
        slot_end_dt = current_dt + timedelta(minutes=session_duration_minutes)
        if slot_end_dt > end_dt:
            break
        
        slot_start = current_dt.time()
        slot_end = slot_end_dt.time()
        candidate_block = TimeBlock(day, slot_start, slot_end)
        
        # Check lunch conflict
        if candidate_block.overlaps(lunch_block):
            current_dt += timedelta(minutes=15)
            continue
        
        # Check elective conflict
        elective_conflict = False
        for elective_block in elective_slots:
            if candidate_block.overlaps(elective_block):
                elective_conflict = True
                break
        if elective_conflict:
            current_dt += timedelta(minutes=15)
            continue
        
        # Check overlap with existing sessions
        overlaps_existing = False
        for existing in existing_sessions:
            existing_block = existing[0]  # TimeBlock is first element
            if candidate_block.overlaps(existing_block):
                overlaps_existing = True
                break
        
        if not overlaps_existing:
            available_slots.append(candidate_block)
        
        current_dt += timedelta(minutes=15)
    
    return available_slots

def try_reschedule_session(candidate_session: tuple, existing_sessions: List[tuple],
                           all_candidates: List[tuple], semester: int, section_name: str,
                           period: str, current_day: str, base_course_code_func,
                           final_sessions_all_days: List[tuple] = None,
                           course_requirements: Dict = None, is_required: bool = False,
                           future_days_only: bool = False) -> tuple:
    """
    Try to reschedule a session to an alternative time slot.
    Tries all days and available slots; when is_required, also tries reverse day order,
    reverse slot order, and a couple of shuffled day orders.
    When future_days_only=True (e.g. dedup deferral), only tries days after current_day
    in processing order (Mon->Fri) so deferred sessions can be injected into later days.
    """
    import random
    block, course_display, priority, base_course = candidate_session

    if '-LAB' in course_display:
        session_type = 'P'
        duration_minutes = 120
    elif '-TUT' in course_display:
        session_type = 'T'
        duration_minutes = 60
    else:
        session_type = 'L'
        duration_minutes = 90

    base_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    try:
        current_idx = base_days.index(current_day)
    except ValueError:
        current_idx = -1
    def allowed_days(days_list: List[str]) -> List[str]:
        if not future_days_only:
            return days_list
        return [d for d in days_list if (base_days.index(d) if d in base_days else -1) > current_idx]

    strategies = [(base_days, False)]
    if future_days_only and is_required:
        future_ordered = [d for d in base_days if base_days.index(d) > current_idx]
        if future_ordered:
            strategies.insert(0, (future_ordered, False))
    if is_required:
        strategies.extend([
            (list(reversed(base_days)), False),
            (base_days, True),
            (list(reversed(base_days)), True),
        ])
        for _ in range(2):
            s = base_days[:]
            random.shuffle(s)
            strategies.append((s, False))

    def try_with_order(days_order: List[str], reverse_slots: bool):
        for day_to_try in allowed_days(days_order):
            day_sessions = []
            if day_to_try == current_day:
                day_sessions.extend(existing_sessions)
            day_sessions.extend([c for c in all_candidates if c[0].day == day_to_try])
            if final_sessions_all_days:
                day_sessions.extend([s for s in final_sessions_all_days if s[0].day == day_to_try])
            seen = set()
            unique_day_sessions = []
            for sess in day_sessions:
                key = (sess[0].day, sess[0].start, sess[0].end, sess[1])
                if key not in seen:
                    seen.add(key)
                    unique_day_sessions.append(sess)
            day_sessions = unique_day_sessions
            available_slots = get_available_slots_for_rescheduling(
                day_to_try, semester, day_sessions, duration_minutes, session_type
            )
            if reverse_slots:
                available_slots = list(reversed(available_slots))
            for candidate_slot in available_slots:
                if validate_one_day_one_session_rule(
                    candidate_slot, course_display, base_course, session_type,
                    day_sessions, base_course_code_func
                ):
                    return (candidate_slot, course_display, priority, base_course)
        return None

    for days_order, reverse_slots in strategies:
        res = try_with_order(days_order, reverse_slots)
        if res is not None:
            return res

    # Simple fallback when future_days_only + is_required: try each future day in order, first valid slot
    if future_days_only and is_required:
        future_ordered = [d for d in base_days if (base_days.index(d) if d in base_days else -1) > current_idx]
        for day_to_try in future_ordered:
            day_sessions = []
            day_sessions.extend([c for c in all_candidates if c[0].day == day_to_try])
            if final_sessions_all_days:
                day_sessions.extend([s for s in final_sessions_all_days if s[0].day == day_to_try])
            seen = set()
            unique = []
            for s in day_sessions:
                k = (s[0].day, s[0].start, s[0].end, s[1])
                if k not in seen:
                    seen.add(k)
                    unique.append(s)
            day_sessions = unique
            slots = get_available_slots_for_rescheduling(
                day_to_try, semester, day_sessions, duration_minutes, session_type
            )
            for cand_slot in slots:
                if validate_one_day_one_session_rule(
                    cand_slot, course_display, base_course, session_type,
                    day_sessions, base_course_code_func
                ):
                    return (cand_slot, course_display, priority, base_course)
        logging.warning(
            f"Dedup reschedule: failed to place required {course_display} on any future day "
            f"(tried {future_ordered}); no valid slot or one-day-one-session violated."
        )
    return None

# Conflict logging - global list to track rescheduling conflicts
rescheduling_conflicts = []

def log_rescheduling_conflict(course_display: str, original_block: TimeBlock, 
                              reason: str, attempted_slots: List[TimeBlock], 
                              final_action: str):
    """Log a rescheduling conflict for later analysis"""
    rescheduling_conflicts.append({
        'course': course_display,
        'original_day': original_block.day,
        'original_time': f"{original_block.start.strftime('%H:%M')}-{original_block.end.strftime('%H:%M')}",
        'reason': reason,
        'attempted_slots': len(attempted_slots),
        'final_action': final_action
    })

def get_course_requirements(courses: List[Course], semester: int, section_name: str) -> Dict[str, Dict[str, int]]:
    """Get LTPSC requirements for all courses in this semester/section (department-based only)."""
    from modules_v2.phase5_core_courses import calculate_slots_needed

    requirements = {}
    section_dept = section_name.split('-')[0] if '-' in section_name else section_name

    for course in courses:
        if (hasattr(course, 'semester') and course.semester == semester and
            hasattr(course, 'department') and course.department == section_dept and
            not getattr(course, 'is_elective', False)):
            ltpsc = getattr(course, 'ltpsc', '')
            if ltpsc:
                slots_needed = calculate_slots_needed(ltpsc)
                requirements[course.code] = {
                    'lectures': slots_needed.get('lectures', 0),
                    'tutorials': slots_needed.get('tutorials', 0),
                    'practicals': slots_needed.get('practicals', 0)
                }
    return requirements


def get_course_requirements_for_sheet(
    courses: List[Course],
    semester: int,
    section_name: str,
    period: str,
    combined_sessions: List,
    phase5_sessions: List = None,
    phase7_sessions: List = None,
) -> Dict[str, Dict[str, int]]:
    """Merge dept-based requirements with all courses on this sheet (cross-listed / Phase 4).
    Ensures Phase 4 combined courses (e.g. MA161 for DSAI-A, ECE-A) are protected in overlap resolution.
    """
    from modules_v2.phase5_core_courses import calculate_slots_needed

    period_code = normalize_period(period)
    expected_section = f"{section_name}-Sem{semester}"
    courses_on_sheet = set()

    for s in combined_sessions:
        if not isinstance(s, dict):
            continue
        secs = s.get('sections', [])
        prd = normalize_period(s.get('period') or '')
        if match_section(expected_section, secs) and prd == period_code:
            code = (s.get('course_code') or '').replace('-TUT', '').replace('-LAB', '').strip()
            if code:
                courses_on_sheet.add((code.split('-')[0], semester))

    for sess_list in [phase5_sessions or [], phase7_sessions or []]:
        for s in sess_list:
            sec = getattr(s, 'section', None) or ''
            prd = normalize_period(getattr(s, 'period', '') or '')
            if not match_section(expected_section, sec) or prd != period_code:
                continue
            code = getattr(s, 'course_code', None) or ''
            if code:
                courses_on_sheet.add((code.split('-')[0], semester))

    sheet_reqs = {}
    for code, sem in courses_on_sheet:
        if sem != semester:
            continue
        c = next((x for x in (courses or []) if getattr(x, 'code', None) == code and getattr(x, 'semester', None) == sem), None)
        if not c or getattr(c, 'is_elective', False):
            continue
        ltpsc = getattr(c, 'ltpsc', '')
        if ltpsc:
            slots = calculate_slots_needed(ltpsc)
            sheet_reqs[code] = {
                'lectures': slots.get('lectures', 0),
                'tutorials': slots.get('tutorials', 0),
                'practicals': slots.get('practicals', 0)
            }

    base = get_course_requirements(courses or [], semester, section_name)
    for k, v in sheet_reqs.items():
        base[k] = v
    return base


def get_elective_basket_requirements(
    section_name: str, semester: int, period: str, elective_sessions: List
) -> Dict[str, Dict[str, int]]:
    """Return { ELECTIVE_BASKET_X.Y: { lectures: 2, tutorials: 1, practicals: 0 } } for baskets on this sheet."""
    out = {}
    period_code = normalize_period(period)
    expected_section = f"{section_name}-Sem{semester}"
    seen = set()
    for s in elective_sessions or []:
        sec = getattr(s, "section", "") or ""
        pr = normalize_period(getattr(s, "period", "") or "")
        if not match_section(expected_section, sec) or pr != period_code:
            continue
        code = (getattr(s, "course_code", "") or "").strip()
        if not code.startswith("ELECTIVE_BASKET_") or code in seen:
            continue
        seen.add(code)
        out[code] = {"lectures": 2, "tutorials": 1, "practicals": 0}
    return out


def check_course_requirements_met(course_code: str, session_type: str, 
                                 sessions_added: List[tuple], 
                                 course_requirements: Dict[str, Dict[str, int]],
                                 base_course_code_func,
                                 session_to_check: tuple = None) -> bool:
    """Check if removing this session would make the course unsatisfied
    Returns True if requirements would still be met after removal, False if removal would break requirements
    
    Args:
        course_code: Course code (e.g., "CS307", "CS307-TUT")
        session_type: Session type ('L', 'T', or 'P')
        sessions_added: List of sessions already added (to count from)
        course_requirements: Dict mapping course codes to their LTPSC requirements
        base_course_code_func: Function to extract base course code
        session_to_check: Optional tuple (TimeBlock, course_display, priority, base_course) being checked
                         If provided, ensures we don't double-count if it's already in sessions_added
    """
    if not course_requirements:
        return True  # No requirements defined, assume OK to remove
    
    base_code = base_course_code_func(course_code)
    if base_code not in course_requirements:
        return True  # Course not in requirements, assume OK
    
    reqs = course_requirements[base_code]
    
    # Count current sessions by type for this course
    current_counts = {'L': 0, 'T': 0, 'P': 0}
    session_to_check_included = False
    
    for sess in sessions_added:
        sess_base = base_course_code_func(sess[1])
        if sess_base == base_code:
            # Check if this is the session we're checking (to avoid double-counting)
            if session_to_check and sess == session_to_check:
                session_to_check_included = True
            
            if '-LAB' in sess[1]:
                current_counts['P'] += 1
            elif '-TUT' in sess[1]:
                current_counts['T'] += 1
            else:
                current_counts['L'] += 1
    
    # If session_to_check is not in sessions_added, we need to account for it
    # (This happens when checking a candidate before it's added)
    if session_to_check and not session_to_check_included:
        sess_base_check = base_course_code_func(session_to_check[1])
        if sess_base_check == base_code:
            if '-LAB' in session_to_check[1]:
                current_counts['P'] += 1
            elif '-TUT' in session_to_check[1]:
                current_counts['T'] += 1
            else:
                current_counts['L'] += 1
    
    # Determine what type this session is and simulate removal
    if '-LAB' in course_code or session_type == 'P':
        # This is a practical - check if we have enough after removal
        return (current_counts['P'] - 1) >= reqs.get('practicals', 0)
    elif '-TUT' in course_code or session_type == 'T':
        # This is a tutorial - check if we have enough after removal
        return (current_counts['T'] - 1) >= reqs.get('tutorials', 0)
    else:
        # This is a lecture - check if we have enough after removal
        return (current_counts['L'] - 1) >= reqs.get('lectures', 0)

def normalize_section_string(section: str) -> str:
    """
    Normalize section string for robust matching.
    Handles case insensitivity, whitespace, and X-SemY vs X-Sem Y formats.
    """
    if not section:
        return ""
    s = str(section).strip().upper()
    s = re.sub(r"\s*SEM\s*", "-SEM", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

def match_section(expected_section: str, session_sections) -> bool:
    """
    Robust section matching that handles list, string, and various formats.
    
    Args:
        expected_section: Expected section format (e.g., "CSE-A-Sem1")
        session_sections: Section(s) from session (can be list, string, or other)
    
    Returns:
        True if sections match, False otherwise
    """
    expected_normalized = normalize_section_string(expected_section)
    
    if isinstance(session_sections, list):
        # Check if expected_section is in the list
        for sec in session_sections:
            sec_normalized = normalize_section_string(sec)
            if expected_normalized == sec_normalized:
                return True
        return False
    elif isinstance(session_sections, str):
        session_normalized = normalize_section_string(session_sections)
        return expected_normalized == session_normalized
    else:
        # Try to convert to string and match
        session_str = normalize_section_string(str(session_sections))
        return expected_normalized == session_str

def normalize_period(period: str) -> str:
    """
    Normalize period string to standard format (PRE or POST).
    
    Args:
        period: Period string (e.g., "PreMid", "PRE", "PostMid", "POST")
    
    Returns:
        Normalized period ("PRE" or "POST")
    """
    if not period:
        return ""
    period_upper = str(period).strip().upper()
    if period_upper in ["PREMID", "PRE"]:
        return "PRE"
    elif period_upper in ["POSTMID", "POST"]:
        return "POST"
    return period_upper

def create_integrated_schedule(
    day: str,
    semester: int,
    section_name: str,
    period: str,
    elective_sessions: List,
    combined_sessions: List,
    phase5_sessions: List = None,
    phase7_sessions: List = None,
    courses: List = None,
    final_sessions_all_days: List[tuple] = None,
    extra_candidates: List[tuple] = None,
) -> tuple:
    """Create an integrated schedule with electives, combined classes, core courses, and Phase 7 courses.
    Returns (DayScheduleGrid, deferred) where deferred is a list of (block, course, prio, base) rescheduled to another day.
    """
    grid = DayScheduleGrid(day, semester)
    deferred: List[tuple] = []

    period_code = normalize_period(period)
    expected_section = f"{section_name}-Sem{semester}"
    traced = _trace_enabled(section_name, semester, period)

    def trace_log(msg: str) -> None:
        if traced:
            logging.warning("[TRACE] " + msg)

    # Get course requirements for this semester/section (incl. cross-listed / Phase 4)
    course_requirements = {}
    if courses:
        course_requirements = get_course_requirements_for_sheet(
            courses, semester, section_name, period,
            combined_sessions, phase5_sessions, phase7_sessions
        )
    basket_reqs = get_elective_basket_requirements(section_name, semester, period, elective_sessions)
    for k, v in basket_reqs.items():
        course_requirements[k] = v
    
    # Collect candidate sessions with priorities to enforce 1-day-1-session and resolve overlaps
    # Priority: Elective (4) > Combined (3) > Phase5 (2) > Phase7 (1)
    # CRITICAL: Electives have highest priority to prevent other courses from overwriting them
    # NOTE: Lab sessions (P) should not be filtered out by other courses if they don't overlap in time
    candidates: List[tuple] = []  # (TimeBlock, course_display, priority, base_course)
    for ext in (extra_candidates or []):
        candidates.append(ext)
    
    def base_course_code(code: str) -> str:
        if not isinstance(code, str):
            return str(code)
        # For elective baskets, remove suffix but keep group (e.g., "5.1")
        if code.startswith('ELECTIVE_BASKET_'):
            # Remove -TUT or -LAB suffix if present, but keep group number
            base = code.replace('-LAB', '').replace('-TUT', '')
            return base  # Returns "ELECTIVE_BASKET_5.1" (preserves group)
        # For other courses, keep session type suffix to distinguish different session types
        if '-LAB' in code or '-TUT' in code:
            # Keep the suffix to distinguish lectures from labs/tutorials
            return code
        return code.split('-')[0]  # Get base code before any dash
    
    # Add elective basket sessions for this section/semester/period
    # CRITICAL: Electives must have highest priority to prevent overwriting
    # CRITICAL: Preserve original course_code (e.g., "ELECTIVE_BASKET_5.1") instead of generic "ELECTIVE"
    import logging
    elective_count = 0
    for session in elective_sessions:
        session_period = normalize_period(getattr(session, 'period', '')) if hasattr(session, 'period') else ''
        # Use robust section matching instead of raw equality to handle format/case variations
        section_match = False
        if hasattr(session, 'section'):
            section_match = match_section(expected_section, getattr(session, 'section', ''))
        period_match = (session_period == period_code)
        day_match = bool(hasattr(session, 'block') and session.block and
                        str(getattr(session.block, 'day', '')).strip().upper() == str(day).strip().upper())
        if section_match and period_match and day_match:
            # Preserve original course_code (e.g., "ELECTIVE_BASKET_5.1")
            original_code = getattr(session, 'course_code', 'ELECTIVE')
            session_kind = getattr(session, 'kind', 'L')
            
            # Add suffix based on kind for display, but keep base code for matching
            if session_kind == 'T':
                display_code = f"{original_code}-TUT"
            elif session_kind == 'P':
                display_code = f"{original_code}-LAB"
            else:
                display_code = original_code  # Keep original "ELECTIVE_BASKET_5.1"
            
            candidates.append((session.block, display_code, 4, original_code))  # Priority 4 (highest)
            elective_count += 1
        else:
            original_code = getattr(session, 'course_code', 'ELECTIVE')
            if _course_matches_trace(original_code, original_code, traced):
                trace_log(f"COLLECT elective [{day}]: SKIP {original_code} section={section_match} period={period_match} "
                          f"day={day_match} (period={session_period!r} vs {period_code!r})")
    if elective_count > 0:
        logging.debug(f"Added {elective_count} elective sessions to candidates for {expected_section} {day} {period_code}")
    
    # Add combined class sessions for this section/semester/period
    for session in combined_sessions:
        # Check if this section is in the session's section list
        session_sections = session.get('sections', [])
        session_period = session.get('period')
        session_day = session.get('day')
        session_block = session.get('time_block')
        session_course = session.get('course_code', 'COMBINED')
        session_type = session.get('session_type', 'L')
        
        # Format course code with proper suffix based on session type
        if session_type == 'T':
            display_course = session_course if '-TUT' in session_course else f"{session_course}-TUT"
        elif session_type == 'P':
            display_course = session_course if '-LAB' in session_course else f"{session_course}-LAB"
        else:
            display_course = session_course.replace('-TUT', '').replace('-LAB', '')
        
        # Check section match using robust normalization
        # CRITICAL: Must match EXACT section including semester (e.g., CSE-A-Sem1 != CSE-A-Sem3)
        section_match = match_section(expected_section, session_sections)
        
        # Normalize period for comparison
        session_period_normalized = normalize_period(session_period) if session_period else ""
        
        # CRITICAL: Check if session_block is a TimeBlock object, not just truthy
        has_valid_block = False
        if session_block:
            # Check if it's a TimeBlock or has start/end attributes
            if hasattr(session_block, 'start') and hasattr(session_block, 'end'):
                has_valid_block = True
            elif isinstance(session_block, TimeBlock):
                has_valid_block = True
        
        # Match period and day
        period_match = (session_period_normalized == period_code)
        day_match = (str(session_day).strip().upper() == str(day).strip().upper())
        
        if section_match and period_match and day_match and has_valid_block:
            candidates.append((session_block, display_course, 3, base_course_code(display_course)))
            if _course_matches_trace(display_course, session_course.split("-")[0], traced):
                trace_log(f"COLLECT combined [{day}]: ADDED {display_course} {session_block.start}-{session_block.end} "
                         f"(section={session_sections}, period={session_period_normalized}, day={session_day})")
        elif section_match and has_valid_block:
            if _course_matches_trace(display_course, session_course.split("-")[0], traced):
                trace_log(f"COLLECT combined [{day}]: SKIP period/day mismatch {display_course} "
                         f"section={session_sections} period={session_period_normalized} vs {period_code} day={session_day} vs {day}")
            base = (session_course or "").replace("-TUT", "").replace("-LAB", "").split("-")[0]
            if course_requirements and base in course_requirements:
                logging.debug(f"Combined skip (period/day): {display_course} period={session_period_normalized} vs {period_code} day={session_day} vs {day}")
            logging.debug(f"Section match but period/day mismatch: {session_course} - "
                        f"section={session_sections}, expected={expected_section}, "
                        f"period={session_period_normalized} vs {period_code}, "
                        f"day={session_day} vs {day}")
        elif not section_match and has_valid_block:
            if _course_matches_trace(display_course, session_course.split("-")[0], traced):
                trace_log(f"COLLECT combined [{day}]: SKIP section mismatch {display_course} "
                         f"session_sections={session_sections} expected={expected_section}")
            base = (session_course or "").replace("-TUT", "").replace("-LAB", "").split("-")[0]
            if course_requirements and base in course_requirements:
                logging.debug(f"Combined skip (section): {display_course} session_sections={session_sections} expected={expected_section}")
            logging.debug(f"Section mismatch: {session_course} - "
                        f"session_sections={session_sections}, expected={expected_section}")

    # Add Phase 5 core course sessions for this section/semester/period
    if phase5_sessions:
        for session in phase5_sessions:
            # Get session attributes with defaults
            session_section = getattr(session, 'section', '') if hasattr(session, 'section') else ''
            session_period = getattr(session, 'period', '') if hasattr(session, 'period') else ''
            session_block = getattr(session, 'block', None) if hasattr(session, 'block') else None
            session_day = session_block.day if (session_block and hasattr(session_block, 'day')) else ''
            
            # Use robust matching functions
            section_match = match_section(expected_section, session_section) if session_section else False
            period_match = (normalize_period(session_period) == period_code) if session_period else False
            day_match = (str(session_day).strip().upper() == str(day).strip().upper()) if session_day else False
            
            # Debug logging for CS307
            is_cs307 = hasattr(session, 'course_code') and session.course_code == 'CS307'
            if is_cs307:
                import logging
                logging.debug(f"CS307 matching: section={section_match} (session='{session_section}', expected='{expected_section}'), "
                            f"period={period_match} (session='{session_period}', expected='{period_code}'), "
                            f"day={day_match} (session='{session_day}', expected='{day}')")
            
            if section_match and period_match and day_match and session_block:
                # Format course code with suffixes
                course_display = session.course_code
                if session.kind == "T":
                    course_display += "-TUT"
                elif session.kind == "P":
                    course_display += "-LAB"
                
                if is_cs307:
                    import logging
                    logging.debug(f"CS307: Added to candidates: {course_display} on {day} {session_block.start}-{session_block.end}")
                
                candidates.append((session_block, course_display, 2, base_course_code(course_display)))
            else:
                course_display = (session.course_code or "")
                if getattr(session, "kind", "L") == "T":
                    course_display += "-TUT"
                elif getattr(session, "kind", "L") == "P":
                    course_display += "-LAB"
                base_ph5 = base_course_code(course_display)
                if _course_matches_trace(course_display, base_ph5, traced):
                    trace_log(f"COLLECT phase5 [{day}]: SKIP {course_display} section={section_match} period={period_match} "
                              f"day={day_match} (session_sec={session_section!r} period={session_period!r} day={session_day!r})")
                elif is_cs307:
                    import logging
                    logging.warning(f"CS307 NOT added: section_match={section_match}, period_match={period_match}, "
                              f"day_match={day_match}, has_block={session_block is not None}")
    
    # Add Phase 7 remaining <=2 credit course sessions for this section/semester/period
    if phase7_sessions:
        for session in phase7_sessions:
            # Get session attributes with defaults
            session_section = getattr(session, 'section', '') if hasattr(session, 'section') else ''
            session_period = getattr(session, 'period', '') if hasattr(session, 'period') else ''
            session_block = getattr(session, 'block', None) if hasattr(session, 'block') else None
            session_day = session_block.day if (session_block and hasattr(session_block, 'day')) else ''
            
            # Use robust matching functions
            section_match = match_section(expected_section, session_section) if session_section else False
            period_match = (normalize_period(session_period) == period_code) if session_period else False
            day_match = (str(session_day).strip().upper() == str(day).strip().upper()) if session_day else False
            
            if section_match and period_match and day_match and session_block:
                # Format course code with suffixes
                course_display = session.course_code
                if session.kind == "T":
                    course_display += "-TUT"
                elif session.kind == "P":
                    course_display += "-LAB"
                
                candidates.append((session_block, course_display, 1, base_course_code(course_display)))
            else:
                course_display = (session.course_code or "")
                if getattr(session, "kind", "L") == "T":
                    course_display += "-TUT"
                elif getattr(session, "kind", "L") == "P":
                    course_display += "-LAB"
                base_ph7 = base_course_code(course_display)
                if _course_matches_trace(course_display, base_ph7, traced):
                    trace_log(f"COLLECT phase7 [{day}]: SKIP {course_display} section={section_match} period={period_match} "
                              f"day={day_match} (session_sec={session_section!r} period={session_period!r} day={session_day!r})")

    for tc in traced:
        cnt = sum(1 for _b, d, _p, b in candidates if _course_matches_trace(d, b, [tc]))
        if cnt:
            trace_log(f"CANDIDATES [{day}]: {tc} -> {cnt} session(s)")

    # Helper function to extract session type from course display code
    def get_session_type(course_display: str) -> str:
        """Extract session type from course display code"""
        if '-LAB' in course_display:
            return 'P'  # Practical
        elif '-TUT' in course_display:
            return 'T'  # Tutorial
        else:
            return 'L'  # Lecture
    
    # Deduplication with session type rules (STRICT):
    # - No 2 lectures same day for a course
    # - No 2 tutorials same day for a course
    # - No 1 lecture + 1 tutorial same day for a course
    # - 1 lecture + 1 practical on same day: ALLOWED
    # - 1 tut + 1 practical on same day: ALLOWED
    # CRITICAL: Use a grouping base (strip -TUT/-LAB) so L and T are grouped for same-day rule.
    # Track sessions by (base_group, day) -> {session_type: (block, course, prio, base)}
    def base_for_dedup_key(disp: str, b: str) -> str:
        if (b or "").startswith("ELECTIVE_BASKET_"):
            return (b or "").replace("-TUT", "").replace("-LAB", "").strip()
        return (b or disp or "").replace("-TUT", "").replace("-LAB", "").split("-")[0]

    sessions_by_course_day: Dict[tuple, Dict[str, tuple]] = {}
    import logging
    filtered_by_dedup = []  # Track sessions filtered by deduplication

    for block, course, prio, base in candidates:
        session_type = get_session_type(course)
        key = (base_for_dedup_key(course, base), block.day)
        
        if key not in sessions_by_course_day:
            sessions_by_course_day[key] = {}
        
        existing_types = set(sessions_by_course_day[key].keys())
        
        # Check if we can add this session based on rules
        can_add = False
        if not existing_types:
            # No existing session for this course/day - allow
            can_add = True
        elif session_type == 'P' and ('L' in existing_types or 'T' in existing_types):
            # Practical can coexist with lecture or tutorial
            can_add = True
        elif (session_type == 'L' or session_type == 'T') and 'P' in existing_types:
            # Lecture or tutorial can coexist with practical
            can_add = True
        else:
            # Same type (2L or 2T) or 1L+1T same day: STRICT - not allowed. Keep higher priority only.
            if existing_types:
                existing = sessions_by_course_day[key][list(existing_types)[0]]
                if prio > existing[2] or (prio == existing[2] and block.start < existing[0].start):
                    # Replace existing with higher priority
                    existing_req = course_requirements and existing[3] in course_requirements
                    if course_requirements and base in course_requirements:
                        reqs = course_requirements[base]
                        existing_session_type_char = 'P' if 'LAB' in existing[1] else ('T' if '-TUT' in existing[1] else 'L')
                        if existing_session_type_char == 'L' and reqs.get('lectures', 0) > 1:
                            logging.warning(f"Dedup: Replaced required {existing[1]} with {course} on {block.day}; "
                                          f"course needs {reqs.get('lectures', 0)} lectures, may be under-satisfied.")
                        elif existing_session_type_char == 'T' and reqs.get('tutorials', 0) > 0:
                            logging.warning(f"Dedup: Replaced required {existing[1]} with {course} on {block.day}; "
                                          f"course needs {reqs.get('tutorials', 0)} tutorial(s), may be under-satisfied.")
                    filtered_by_dedup.append((existing[1], existing[0], f"Replaced by higher priority {course}"))
                    if existing_req:
                        resched = try_reschedule_session(
                            existing, [], candidates, semester, section_name, period_code, day, base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements, is_required=True,
                            future_days_only=True
                        )
                        if resched and resched[0].day != day:
                            deferred.append(resched)
                            if _course_matches_trace(existing[1], existing[3], traced):
                                trace_log(f"DEDUP [{day}]: RESCHEDULED {existing[1]} -> {resched[0].day} {resched[0].start}-{resched[0].end}")
                    sessions_by_course_day[key] = {session_type: (block, course, prio, base)}
                    can_add = True
                    if _course_matches_trace(course, base, traced) or _course_matches_trace(existing[1], existing[3], traced):
                        trace_log(f"DEDUP [{day}]: REPLACED {existing[1]} by {course} (higher priority)")
                else:
                    # Lower priority - filter (strict: no 2L, no 2T, no L+T same day)
                    cand_req = course_requirements and base in course_requirements
                    if course_requirements and base in course_requirements:
                        reqs = course_requirements[base]
                        if session_type == 'L' and reqs.get('lectures', 0) > 1:
                            logging.warning(f"Dedup: Filtering {course} on {block.day} (lower priority than {existing[1]}, but course needs {reqs.get('lectures', 0)} lectures)")
                        elif session_type == 'T' and reqs.get('tutorials', 0) > 0:
                            logging.warning(f"Dedup: Filtering {course} on {block.day} (lower priority than {existing[1]}, but course needs {reqs.get('tutorials', 0)} tutorials)")
                    filtered_by_dedup.append((course, block, f"Lower priority than {existing[1]}"))
                    if cand_req:
                        cand_tup = (block, course, prio, base)
                        resched = try_reschedule_session(
                            cand_tup, [], candidates, semester, section_name, period_code, day, base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements, is_required=True,
                            future_days_only=True
                        )
                        if resched and resched[0].day != day:
                            deferred.append(resched)
                            if _course_matches_trace(course, base, traced):
                                trace_log(f"DEDUP [{day}]: RESCHEDULED {course} -> {resched[0].day} {resched[0].start}-{resched[0].end}")
                    if _course_matches_trace(course, base, traced):
                        trace_log(f"DEDUP [{day}]: FILTERED {course} (lower priority than {existing[1]})")
            else:
                can_add = True
        
        if can_add and (session_type not in sessions_by_course_day[key] or 
                        prio > sessions_by_course_day[key][session_type][2] or
                        (prio == sessions_by_course_day[key][session_type][2] and 
                         block.start < sessions_by_course_day[key][session_type][0].start)):
            sessions_by_course_day[key][session_type] = (block, course, prio, base)
    
    # Log deduplication summary
    if filtered_by_dedup:
        logging.debug(f"Dedup filtered {len(filtered_by_dedup)} sessions on {day} for {expected_section}")
    
    # Flatten to list
    deduped = []
    for key, session_dict in sessions_by_course_day.items():
        deduped.extend(session_dict.values())
    
    # Sort by start time then by priority desc
    deduped.sort(key=lambda x: (x[0].start, -x[2]))
    
    # Resolve overlaps: try rescheduling before removing sessions
    # CRITICAL: Different session types (L, T, P) of same course should NOT overlap with each other
    # But they can be on the same day if they don't overlap in time
    # CRITICAL: Multiple sessions of same course (e.g., 2 lectures) are allowed if non-overlapping
    final_sessions: List[tuple] = []
    
    # Get period code for rescheduling (use normalized version)
    # Note: period_code was already set at the start of function using normalize_period
    # This is just for clarity - period_code is already normalized
    expected_section = f"{section_name}-Sem{semester}"
    
    for cand in deduped:
        overlaps = False
        cand_is_lab = 'LAB' in cand[1]
        rescheduled = False
        force_add_despite_overlap = False

        for existing in list(final_sessions):
            if cand[0].overlaps(existing[0]):
                # Check if they're different session types of the same course (e.g., CS161 vs CS161-LAB)
                cand_base = base_course_code(cand[1])
                existing_base = base_course_code(existing[1])
                
                # Get session types for requirement checking
                cand_session_type = 'P' if 'LAB' in cand[1] else ('T' if '-TUT' in cand[1] else 'L')
                existing_session_type = 'P' if 'LAB' in existing[1] else ('T' if '-TUT' in existing[1] else 'L')
                
                # Check if removing candidate would break its course requirements
                cand_would_break = False
                if course_requirements and cand_base in course_requirements:
                    # Check if removing candidate would break requirements
                    # Note: cand might not be in final_sessions yet, so pass it as session_to_check
                    cand_would_break = not check_course_requirements_met(
                        cand[1], cand_session_type, final_sessions, course_requirements, base_course_code,
                        session_to_check=cand
                    )
                
                # Check if removing existing would break its course requirements
                existing_would_break = False
                if course_requirements and existing_base in course_requirements:
                    # existing is in final_sessions, so we exclude it from the list and pass it as session_to_check
                    existing_would_break = not check_course_requirements_met(
                        existing[1], existing_session_type, 
                        [s for s in final_sessions if s != existing], 
                        course_requirements, base_course_code,
                        session_to_check=existing
                    )
                
                # If same base course but different session types, allow both if they don't overlap in time
                # (This should already be handled by the overlap check, but be explicit)
                if cand_base == existing_base and cand[1] != existing[1]:
                    # Different session types of same course (e.g., CS161 vs CS161-LAB)
                    # Check if they actually overlap in time
                    if not cand[0].overlaps(existing[0]):
                        # They don't overlap in time, so both can exist
                        continue
                    # They do overlap in time - this shouldn't happen for same course different types
                    # But if it does, keep both by adjusting the lab time or keeping the one that fits better
                    # For now, prefer keeping both by not marking as overlap if they're different types
                    # Actually, if they overlap, we need to choose one - prefer the one with higher priority
                    # But labs and lectures shouldn't overlap - this is a scheduling error
                    # For now, keep the lab if it's a lab (practicals are important)
                    if 'LAB' in cand[1] or 'LAB' in existing[1]:
                        # If one is a lab, keep both by adjusting - but for now, just continue
                        # This means we're allowing overlapping sessions of different types
                        # This is not ideal but necessary for now
                        continue
                    # Neither is a lab - check course requirements and try rescheduling
                    # Check if removing would break requirements (same course, so both need to be kept if required)
                    cand_would_break_same = False
                    existing_would_break_same = False
                    if course_requirements and cand_base in course_requirements:
                        # For same course, we need both sessions - check if we have enough of each type
                        reqs = course_requirements[cand_base]
                        # Count current sessions of this course
                        current_counts = {'L': 0, 'T': 0, 'P': 0}
                        for sess in final_sessions:
                            sess_base = base_course_code(sess[1])
                            if sess_base == cand_base:
                                if '-LAB' in sess[1]:
                                    current_counts['P'] += 1
                                elif '-TUT' in sess[1]:
                                    current_counts['T'] += 1
                                else:
                                    current_counts['L'] += 1
                        
                        # Check if we need this candidate session type
                        if cand_session_type == 'L' and current_counts['L'] < reqs.get('lectures', 0):
                            cand_would_break_same = True
                        elif cand_session_type == 'T' and current_counts['T'] < reqs.get('tutorials', 0):
                            cand_would_break_same = True
                        elif cand_session_type == 'P' and current_counts['P'] < reqs.get('practicals', 0):
                            cand_would_break_same = True
                        
                        # Check if we need existing session type
                        if existing_session_type == 'L' and current_counts['L'] < reqs.get('lectures', 0):
                            existing_would_break_same = True
                        elif existing_session_type == 'T' and current_counts['T'] < reqs.get('tutorials', 0):
                            existing_would_break_same = True
                        elif existing_session_type == 'P' and current_counts['P'] < reqs.get('practicals', 0):
                            existing_would_break_same = True
                    
                    # If both are required, try to reschedule one to a different time
                    if cand_would_break_same and existing_would_break_same:
                        # Both are required - try rescheduling candidate first
                        rescheduled_cand = try_reschedule_session(
                            cand, final_sessions, deduped, semester, section_name, 
                            period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_cand:
                            cand = rescheduled_cand
                            rescheduled = True
                            continue
                        # If candidate rescheduling failed, try existing
                        rescheduled_existing = try_reschedule_session(
                            existing, [s for s in final_sessions if s != existing], deduped, 
                            semester, section_name, period_code, day, base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_existing:
                            final_sessions.remove(existing)
                            final_sessions.append(rescheduled_existing)
                            continue
                        # Both failed - keep both and log (do not drop required)
                        import logging
                        logging.warning(f"Keep both: {cand[1]} and {existing[1]} overlap, both required, "
                                      f"rescheduling failed. Adding both; overlap logged.")
                        trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                        force_add_despite_overlap = True
                        break
                    # Prefer higher priority, but try rescheduling first
                    if cand[2] <= existing[2]:
                        # Candidate has lower priority - try to reschedule it
                        rescheduled_cand = try_reschedule_session(
                            cand, final_sessions, deduped, semester, section_name, 
                            period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_cand:
                            # Rescheduling succeeded - update candidate and continue
                            cand = rescheduled_cand
                            rescheduled = True
                            continue  # Skip overlap check, will be checked again with new time
                        else:
                            # Rescheduling failed - if candidate is required, keep it
                            if cand_would_break_same:
                                # Candidate is required - try rescheduling existing
                                rescheduled_existing = try_reschedule_session(
                                    existing, [s for s in final_sessions if s != existing], deduped, 
                                    semester, section_name, period_code, day, base_course_code,
                                    final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_existing:
                                    final_sessions.remove(existing)
                                    final_sessions.append(rescheduled_existing)
                                    continue
                                # Both failed - keep candidate (required)
                                log_rescheduling_conflict(
                                    existing[1], existing[0],
                                    f"Required candidate overlaps, keeping required candidate",
                                    [], "REMOVED"
                                )
                                final_sessions.remove(existing)
                                continue
                            # Candidate not required - log and mark as overlap
                            if cand_would_break_same:
                                import logging
                                logging.warning(f"Keep both: {cand[1]} and {existing[1]} same-course overlap, "
                                              f"cand required, rescheduling failed. Adding both; overlap logged.")
                                trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                force_add_despite_overlap = True
                                break
                            log_rescheduling_conflict(
                                cand[1], cand[0],
                                f"Same course different types overlap, lower priority (cand={cand[2]}, existing={existing[2]})",
                                [], "REMOVED"
                            )
                            overlaps = True
                            break
                    else:
                        # Candidate has higher priority - try to reschedule existing
                        rescheduled_existing = try_reschedule_session(
                            existing, [s for s in final_sessions if s != existing], deduped, 
                            semester, section_name, period_code, day, base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_existing:
                            # Rescheduling succeeded - replace existing with rescheduled version
                            final_sessions.remove(existing)
                            final_sessions.append(rescheduled_existing)
                            continue  # Continue with candidate
                        else:
                            # Rescheduling failed - if existing is required, keep it
                            if existing_would_break_same:
                                # Existing is required - try rescheduling candidate
                                rescheduled_cand = try_reschedule_session(
                                    cand, final_sessions, deduped, semester, section_name, 
                                    period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_cand:
                                    cand = rescheduled_cand
                                    rescheduled = True
                                    continue
                                # Both failed - keep existing (required)
                                if cand_would_break:
                                    import logging
                                    logging.warning(f"Keep both: {cand[1]} and {existing[1]} overlap, "
                                                  f"both required, keeping existing. Adding both; overlap logged.")
                                    trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                    force_add_despite_overlap = True
                                    break
                                log_rescheduling_conflict(
                                    cand[1], cand[0],
                                    f"Required existing overlaps, keeping required existing",
                                    [], "REMOVED"
                                )
                                overlaps = True
                                break
                            # Existing not required - replace existing with candidate
                            log_rescheduling_conflict(
                                existing[1], existing[0],
                                f"Same course different types overlap, lower priority (existing={existing[2]}, cand={cand[2]})",
                                [], "REMOVED"
                            )
                            final_sessions.remove(existing)
                            break
                else:
                    # Different courses or same course+type - check course requirements before removing
                    # (cand_would_break and existing_would_break already defined above)
                    #
                    # CRITICAL: Phase 4 combined classes must NEVER lose their synchronized slots to
                    # lower-priority Phase 5/7 core sessions for the same section/day/period.
                    # Combined sessions come from the user-driven Phase 4 logic and encode grouping.
                    # If a combined session and a non-combined session overlap in time, we must:
                    #   - Try to reschedule the non-combined session first.
                    #   - If rescheduling fails, drop the non-combined one and keep the combined.
                    cand_is_combined = (cand[2] == 3)
                    existing_is_combined = (existing[2] == 3)
                    if cand_is_combined != existing_is_combined:
                        # Exactly one of these is a Phase 4 combined session
                        combined_session = cand if cand_is_combined else existing
                        other_session = existing if cand_is_combined else cand
                        other_is_existing = not cand_is_combined  # True if existing is the non-combined one

                        # Optional tracing to understand MA161/DS161 behaviour for specific sections
                        if _course_matches_trace(combined_session[1], base_course_code(combined_session[1]), traced) or \
                           _course_matches_trace(other_session[1], base_course_code(other_session[1]), traced):
                            trace_log(
                                f"OVERLAP [{day}]: COMBINED_vs_CORE combined={combined_session[1]} other={other_session[1]} (combined_wins)"
                            )

                        # Try to reschedule the non-combined session first
                        rescheduled_other = try_reschedule_session(
                            other_session,
                            [s for s in final_sessions if s != existing] if other_is_existing else final_sessions,
                            deduped,
                            semester,
                            section_name,
                            period_code,
                            day,
                            base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True,
                        )

                        if rescheduled_other:
                            # Successfully rescheduled the non-combined session
                            if other_is_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_other)
                            else:
                                # Candidate was rescheduled; update cand and continue checks
                                cand = rescheduled_other
                                rescheduled = True
                            # Do not mark overlap; keep both with new times
                            continue

                        # Rescheduling failed. Keep the combined session and drop the non-combined one.
                        if other_is_existing:
                            log_rescheduling_conflict(
                                existing[1],
                                existing[0],
                                "Dropped lower-priority core session overlapping Phase 4 combined slot",
                                [],
                                "REMOVED",
                            )
                            final_sessions.remove(existing)
                            # We can now keep cand (combined) without marking overlap
                            continue
                        else:
                            # Other is the candidate (core) - drop candidate and keep existing combined
                            log_rescheduling_conflict(
                                cand[1],
                                cand[0],
                                "Dropped lower-priority core candidate overlapping Phase 4 combined slot",
                                [],
                                "REMOVED",
                            )
                            overlaps = True
                            break

                    # If removing candidate would break requirements, try harder to keep it
                    if cand_would_break and not existing_would_break:
                        # Candidate is required, existing is not - try to reschedule existing
                        rescheduled_existing = try_reschedule_session(
                            existing, [s for s in final_sessions if s != existing], deduped,
                            semester, section_name, period_code, day, base_course_code,
                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_existing:
                            final_sessions.remove(existing)
                            final_sessions.append(rescheduled_existing)
                            continue
                        # If rescheduling failed, we still need to keep candidate (it's required)
                        # Try rescheduling candidate to a different time
                        rescheduled_cand = try_reschedule_session(
                            cand, final_sessions, deduped, semester, section_name,
                            period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_cand:
                            cand = rescheduled_cand
                            rescheduled = True
                            continue
                        # Both rescheduling attempts failed - keep candidate (required) and remove existing
                        log_rescheduling_conflict(
                            existing[1], existing[0],
                            f"Required course session overlaps, keeping required candidate",
                            [], "REMOVED"
                        )
                        final_sessions.remove(existing)
                        continue
                    
                    # If removing existing would break requirements, try harder to keep it
                    if existing_would_break and not cand_would_break:
                        # Existing is required, candidate is not - try to reschedule candidate
                        rescheduled_cand = try_reschedule_session(
                            cand, final_sessions, deduped, semester, section_name,
                            period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                        )
                        if rescheduled_cand:
                            cand = rescheduled_cand
                            rescheduled = True
                            continue
                        # Existing required, cand not; reschedule failed. Keep existing, drop candidate. Never drop required.
                        log_rescheduling_conflict(
                            cand[1], cand[0],
                            f"Required existing overlaps; keeping required existing, dropping candidate",
                            [], "REMOVED"
                        )
                        overlaps = True
                        break
                    # If both would break or neither would break, use priority-based logic
                    # CRITICAL: Labs should have higher priority than non-lab sessions (except electives)
                    # If a lab overlaps with a non-lab session, keep the lab
                    # CRITICAL: Protect required sessions - only remove if not required
                    if 'LAB' in cand[1]:
                        # Candidate is a lab - prioritize it over non-lab sessions (except electives)
                        if 'LAB' not in existing[1] and 'ELECTIVE' not in existing[1]:
                            # Existing is not a lab and not an elective - try to reschedule existing first
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue
                            else:
                                # Rescheduling failed - check if existing is required before removing
                                if existing_would_break:
                                    # Existing is required - try rescheduling candidate instead
                                    rescheduled_cand = try_reschedule_session(
                                        cand, final_sessions, deduped, semester, section_name,
                                        period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                    )
                                    if rescheduled_cand:
                                        cand = rescheduled_cand
                                        rescheduled = True
                                        continue
                                    # Both failed - keep both, do not drop
                                    import logging
                                    logging.warning(f"Keep both: {existing[1]} and {cand[1]} overlap, "
                                                  f"both required, rescheduling failed. Adding both; overlap logged.")
                                    trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                    force_add_despite_overlap = True
                                    break
                                # Existing is not required - remove existing, keep the lab
                                log_rescheduling_conflict(
                                    existing[1], existing[0],
                                    f"Lab overlaps non-lab session, rescheduling failed",
                                    [], "REMOVED"
                                )
                                final_sessions.remove(existing)
                                # Don't mark as overlap, continue to add the lab
                                continue
                        elif 'LAB' in existing[1]:
                            # Both are labs - prefer higher priority, but if same priority, keep both if they don't overlap in time
                            if not cand[0].overlaps(existing[0]):
                                # They don't overlap in time, keep both
                                continue
                            elif cand[2] <= existing[2]:
                                # Candidate has lower priority - try rescheduling
                                rescheduled_cand = try_reschedule_session(
                                    cand, final_sessions, deduped, semester, section_name,
                                    period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_cand:
                                    cand = rescheduled_cand
                                    rescheduled = True
                                    continue
                                else:
                                    # Rescheduling failed - check if candidate is required
                                    if cand_would_break:
                                        # Candidate is required - try rescheduling existing instead
                                        rescheduled_existing = try_reschedule_session(
                                            existing, [s for s in final_sessions if s != existing], deduped,
                                            semester, section_name, period_code, day, base_course_code,
                                            final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                        )
                                        if rescheduled_existing:
                                            final_sessions.remove(existing)
                                            final_sessions.append(rescheduled_existing)
                                            continue
                                        # Both failed - keep both if both required
                                        if existing_would_break:
                                            import logging
                                            logging.warning(f"Keep both: {cand[1]} and {existing[1]} lab-lab overlap, "
                                                          f"both required, rescheduling failed. Adding both; overlap logged.")
                                            trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                            force_add_despite_overlap = True
                                            break
                                    # Cand required, existing not - keep cand, remove existing
                                    log_rescheduling_conflict(
                                        existing[1], existing[0],
                                        f"Lab-lab overlap, keeping required candidate",
                                        [], "REMOVED"
                                    )
                                    final_sessions.remove(existing)
                                    continue
                            else:
                                # Candidate has higher priority - try rescheduling existing
                                rescheduled_existing = try_reschedule_session(
                                    existing, [s for s in final_sessions if s != existing], deduped,
                                    semester, section_name, period_code, day, base_course_code,
                                    final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_existing:
                                    final_sessions.remove(existing)
                                    final_sessions.append(rescheduled_existing)
                                    continue
                                else:
                                    # Rescheduling failed - check if existing is required
                                    if existing_would_break:
                                        # Existing is required - try rescheduling candidate instead
                                        rescheduled_cand = try_reschedule_session(
                                            cand, final_sessions, deduped, semester, section_name,
                                            period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                        )
                                        if rescheduled_cand:
                                            cand = rescheduled_cand
                                            rescheduled = True
                                            continue
                                        # Both failed - keep both if both required
                                        if cand_would_break:
                                            import logging
                                            logging.warning(f"Keep both: {cand[1]} and {existing[1]} lab-lab overlap, "
                                                          f"both required, rescheduling failed. Adding both; overlap logged.")
                                            trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                            force_add_despite_overlap = True
                                            break
                                    # Existing not required - remove it
                                    log_rescheduling_conflict(
                                        existing[1], existing[0],
                                        f"Lab-lab overlap, lower priority (existing={existing[2]}, cand={cand[2]})",
                                        [], "REMOVED"
                                    )
                                    final_sessions.remove(existing)
                                    break
                        elif 'ELECTIVE' in existing[1]:
                            # Existing is elective, cand is lab - electives have priority 4, labs have 2
                            # But check if candidate is required first
                            if cand_would_break:
                                # Candidate is required - try harder to reschedule existing
                                rescheduled_existing = try_reschedule_session(
                                    existing, [s for s in final_sessions if s != existing], deduped,
                                    semester, section_name, period_code, day, base_course_code,
                                    final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_existing:
                                    final_sessions.remove(existing)
                                    final_sessions.append(rescheduled_existing)
                                    continue
                                # If rescheduling existing failed, try candidate
                                rescheduled_cand = try_reschedule_session(
                                    cand, final_sessions, deduped, semester, section_name,
                                    period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_cand:
                                    cand = rescheduled_cand
                                    rescheduled = True
                                    continue
                                # Both failed - keep candidate (required)
                                log_rescheduling_conflict(
                                    existing[1], existing[0],
                                    f"Required lab overlaps elective, keeping required lab",
                                    [], "REMOVED"
                                )
                                final_sessions.remove(existing)
                                continue
                            else:
                                # Candidate not required - keep elective (higher priority) - try rescheduling candidate
                                rescheduled_cand = try_reschedule_session(
                                    cand, final_sessions, deduped, semester, section_name,
                                    period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                )
                                if rescheduled_cand:
                                    cand = rescheduled_cand
                                    rescheduled = True
                                    continue
                                else:
                                    if cand_would_break:
                                        import logging
                                        logging.warning(f"Keep both: lab {cand[1]} and elective {existing[1]} overlap, "
                                                      f"lab required, rescheduling failed. Adding both; overlap logged.")
                                        trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                        force_add_despite_overlap = True
                                        break
                                    log_rescheduling_conflict(
                                        cand[1], cand[0],
                                        f"Lab overlaps elective, rescheduling failed",
                                        [], "REMOVED"
                                    )
                                    overlaps = True
                                    break
                    elif 'LAB' in existing[1]:
                        # Existing is a lab, cand is not - check if candidate is required
                        if cand_would_break:
                            # Candidate is required - try harder to reschedule existing
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue
                            # If rescheduling existing failed, try candidate
                            rescheduled_cand = try_reschedule_session(
                                cand, final_sessions, deduped, semester, section_name,
                                period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_cand:
                                cand = rescheduled_cand
                                rescheduled = True
                                continue
                            # Both failed - keep candidate (required) and remove existing
                            log_rescheduling_conflict(
                                existing[1], existing[0],
                                f"Required candidate overlaps lab, keeping required candidate",
                                [], "REMOVED"
                            )
                            final_sessions.remove(existing)
                            continue
                        else:
                            # Candidate not required - keep the existing lab, try rescheduling candidate
                            rescheduled_cand = try_reschedule_session(
                                cand, final_sessions, deduped, semester, section_name,
                                period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_cand:
                                cand = rescheduled_cand
                                rescheduled = True
                                continue
                            else:
                                log_rescheduling_conflict(
                                    cand[1], cand[0],
                                    f"Non-lab overlaps lab, rescheduling failed",
                                    [], "REMOVED"
                                )
                                overlaps = True
                                break
                    elif 'LAB' in cand[1] and cand_base == existing_base:
                        # Lab of same course overlapping with lecture - keep the lab, try rescheduling lecture
                        if 'LAB' not in existing[1]:
                            # Existing is a lecture, cand is a lab - try rescheduling existing
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue
                            else:
                                # Rescheduling failed - check if existing lecture is required
                                if existing_would_break:
                                    # Existing lecture is required - try rescheduling lab instead
                                    rescheduled_cand = try_reschedule_session(
                                        cand, final_sessions, deduped, semester, section_name,
                                        period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                                    )
                                    if rescheduled_cand:
                                        cand = rescheduled_cand
                                        rescheduled = True
                                        continue
                                    # Both failed - keep both if both required
                                    if cand_would_break:
                                        import logging
                                        logging.warning(f"Keep both: {existing[1]} and {cand[1]} lecture-lab overlap, "
                                                      f"both required, rescheduling failed. Adding both; overlap logged.")
                                        trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                        force_add_despite_overlap = True
                                        break
                                # Existing not required - remove it
                                log_rescheduling_conflict(
                                    existing[1], existing[0],
                                    f"Lecture overlaps lab of same course, rescheduling failed",
                                    [], "REMOVED"
                                )
                                final_sessions.remove(existing)
                                # Don't mark as overlap, continue to add the lab
                                continue
                    
                    # Standard priority-based resolution - try rescheduling before removing
                    # But first check course requirements
                    if cand[2] <= existing[2]:
                        # Candidate has lower priority - check if it's required
                        if cand_would_break:
                            # Candidate is required - try harder to reschedule existing instead
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue
                            # If rescheduling existing failed, try rescheduling candidate
                            rescheduled_cand = try_reschedule_session(
                                cand, final_sessions, deduped, semester, section_name,
                                period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_cand:
                                cand = rescheduled_cand
                                rescheduled = True
                                continue
                            # Both failed - check if existing is also required
                            if existing_would_break:
                                # Both are required - keep both, do not drop
                                import logging
                                logging.warning(f"Keep both: {cand[1]} and {existing[1]} overlap, "
                                              f"both required, rescheduling failed. Adding both; overlap logged.")
                                trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                force_add_despite_overlap = True
                                break
                            # Only candidate is required - remove existing
                            log_rescheduling_conflict(
                                existing[1], existing[0],
                                f"Required candidate overlaps, keeping required candidate",
                                [], "REMOVED"
                            )
                            final_sessions.remove(existing)
                            continue
                        else:
                            # Candidate is not required - try to reschedule it
                            rescheduled_cand = try_reschedule_session(
                                cand, final_sessions, deduped, semester, section_name,
                                period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_cand:
                                # Rescheduling succeeded - update candidate and continue
                                cand = rescheduled_cand
                                rescheduled = True
                                continue  # Will check overlap again with new time
                            else:
                                # Rescheduling failed - check if existing is required
                                if existing_would_break:
                                    if cand_would_break:
                                        import logging
                                        logging.warning(f"Keep both: {cand[1]} and {existing[1]} overlap, "
                                                      f"both required. Adding both; overlap logged.")
                                        trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                        force_add_despite_overlap = True
                                        break
                                    log_rescheduling_conflict(
                                        cand[1], cand[0],
                                        f"Required existing overlaps, keeping required existing",
                                        [], "REMOVED"
                                    )
                                    overlaps = True
                                    break
                                else:
                                    # Neither is required - log and mark as overlap
                                    log_rescheduling_conflict(
                                        cand[1], cand[0],
                                        f"Standard overlap, lower priority (cand={cand[2]}, existing={existing[2]})",
                                        [], "REMOVED"
                                    )
                                    overlaps = True
                                    break
                    else:
                        # Candidate has higher priority - check if existing is required
                        if existing_would_break:
                            # Existing is required - try harder to reschedule candidate
                            rescheduled_cand = try_reschedule_session(
                                cand, final_sessions, deduped, semester, section_name,
                                period_code, day, base_course_code, final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_cand:
                                cand = rescheduled_cand
                                rescheduled = True
                                continue
                            # If rescheduling candidate failed, try rescheduling existing
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue
                            # Both failed - check if candidate is also required
                            if cand_would_break:
                                # Both are required - keep both, do not drop
                                import logging
                                logging.warning(f"Keep both: {cand[1]} and {existing[1]} overlap, "
                                              f"both required, rescheduling failed. Adding both; overlap logged.")
                                trace_log(f"OVERLAP [{day}]: KEEP_BOTH {cand[1]} and {existing[1]}")
                                force_add_despite_overlap = True
                                break
                            # Only existing is required - skip candidate
                            log_rescheduling_conflict(
                                cand[1], cand[0],
                                f"Required existing overlaps, keeping required existing",
                                [], "REMOVED"
                            )
                            overlaps = True
                            break
                        else:
                            # Existing is not required - try to reschedule existing
                            rescheduled_existing = try_reschedule_session(
                                existing, [s for s in final_sessions if s != existing], deduped,
                                semester, section_name, period_code, day, base_course_code,
                                final_sessions_all_days=(final_sessions_all_days or []),
                            course_requirements=course_requirements,
                            is_required=True
                            )
                            if rescheduled_existing:
                                # Rescheduling succeeded - replace existing with rescheduled version
                                final_sessions.remove(existing)
                                final_sessions.append(rescheduled_existing)
                                continue  # Continue with candidate
                            else:
                                # Rescheduling failed - replace existing with candidate
                                log_rescheduling_conflict(
                                    existing[1], existing[0],
                                    f"Standard overlap, lower priority (existing={existing[2]}, cand={cand[2]})",
                                    [], "REMOVED"
                                )
                                final_sessions.remove(existing)
                                break
        
        # If candidate was rescheduled, we need to check overlaps again with the new time
        if rescheduled:
            # Re-check overlaps with rescheduled time
            overlaps = False
            for existing in final_sessions:
                if cand[0].overlaps(existing[0]):
                    overlaps = True
                    break

        if overlaps and not force_add_despite_overlap:
            # Never drop required: last-line-of-defence check before skipping append
            cand_base = base_course_code(cand[1])
            cand_stype = 'P' if 'LAB' in cand[1] else ('T' if '-TUT' in cand[1] else 'L')
            cand_req = False
            if course_requirements and cand_base in course_requirements:
                cand_req = not check_course_requirements_met(
                    cand[1], cand_stype, final_sessions, course_requirements, base_course_code,
                    session_to_check=cand
                )
            if cand_req:
                logging.warning(f"Overlap: would have dropped required {cand[1]}; forcing add despite overlap.")
                force_add_despite_overlap = True
            else:
                for tc in traced:
                    if _course_matches_trace(cand[1], cand[3], [tc]):
                        trace_log(f"OVERLAP [{day}]: DROP candidate {cand[1]} (overlaps=True, not kept)")
                        break

        if force_add_despite_overlap or not overlaps:
            final_sessions.append(cand)

    for tc in traced:
        cnt = sum(1 for _b, d, _p, b in final_sessions if _course_matches_trace(d, b, [tc]))
        trace_log(f"FINAL [{day}]: {tc} -> {cnt} session(s)")

    # VALIDATION: After overlap resolution, verify all courses still meet requirements
    if course_requirements:
        validation_issues = []
        for base_code, reqs in course_requirements.items():
            # Count sessions for this course
            course_counts = {'L': 0, 'T': 0, 'P': 0}
            for sess in final_sessions:
                sess_base = base_course_code(sess[1])
                if sess_base == base_code:
                    if '-LAB' in sess[1]:
                        course_counts['P'] += 1
                    elif '-TUT' in sess[1]:
                        course_counts['T'] += 1
                    else:
                        course_counts['L'] += 1
            
            # Check if requirements are met
            if course_counts['L'] < reqs.get('lectures', 0):
                validation_issues.append(
                    f"{base_code}: Missing {reqs.get('lectures', 0) - course_counts['L']} lecture(s) "
                    f"(have {course_counts['L']}, need {reqs.get('lectures', 0)})"
                )
            if course_counts['T'] < reqs.get('tutorials', 0):
                validation_issues.append(
                    f"{base_code}: Missing {reqs.get('tutorials', 0) - course_counts['T']} tutorial(s) "
                    f"(have {course_counts['T']}, need {reqs.get('tutorials', 0)})"
                )
            if course_counts['P'] < reqs.get('practicals', 0):
                validation_issues.append(
                    f"{base_code}: Missing {reqs.get('practicals', 0) - course_counts['P']} practical(s) "
                    f"(have {course_counts['P']}, need {reqs.get('practicals', 0)})"
                )
        
        # Log validation issues if any, and also log satisfied courses
        import logging
        logger = logging.getLogger(__name__)
        if validation_issues:
            for issue in validation_issues:
                logger.warning(f"VALIDATION WARNING [{day} {period} {expected_section}]: {issue}")
        else:
            # Log summary of satisfied courses
            satisfied_courses = []
            for base_code, reqs in course_requirements.items():
                course_counts = {'L': 0, 'T': 0, 'P': 0}
                for sess in final_sessions:
                    sess_base = base_course_code(sess[1])
                    if sess_base == base_code:
                        if '-LAB' in sess[1]:
                            course_counts['P'] += 1
                        elif '-TUT' in sess[1]:
                            course_counts['T'] += 1
                        else:
                            course_counts['L'] += 1
                if (course_counts['L'] >= reqs.get('lectures', 0) and
                    course_counts['T'] >= reqs.get('tutorials', 0) and
                    course_counts['P'] >= reqs.get('practicals', 0)):
                    satisfied_courses.append(base_code)
            if satisfied_courses:
                logger.debug(f"VALIDATION OK [{day} {period} {expected_section}]: {len(satisfied_courses)} courses satisfied")
        
        # Log candidate vs final session counts
        logger.debug(f"Session counts [{day} {period} {expected_section}]: "
                    f"{len(candidates)} candidates -> {len(deduped)} after dedup -> {len(final_sessions)} final")
    
    # Add lunch block
    lunch_block = grid.lunch_block
    final_sessions.append((lunch_block, "LUNCH", -1, "LUNCH"))
    
    # Sort again by start
    final_sessions.sort(key=lambda x: x[0].start)
    
    # Smart break insertion - Fixed logic
    def should_place_break(current_block: TimeBlock, next_item: tuple) -> bool:
        """Determine if a break should be placed after current block"""
        if next_item is None:
            # Last session of the day - add break
            return True
        
        next_block, next_course = next_item[0], next_item[1]
        
        # Don't add break before lunch
        if next_course == "LUNCH":
            return False
        
        # Don't add break if next item is already a break
        if isinstance(next_course, str) and "Break" in next_course:
            return False
        
        # Calculate break end time
        break_start = current_block.end
        break_end = (datetime.combine(datetime.min, break_start) + timedelta(minutes=15)).time()
        break_block = TimeBlock(day, break_start, break_end)
        
        # Check if break would overlap with next session
        if break_block.overlaps(next_block):
            # If break would overlap, check if next session starts exactly when break would end
            # In that case, we still want the break (no overlap, just back-to-back)
            if break_end == next_block.start:
                return True
            # Otherwise, there's an overlap - don't add break
            return False
        
        # No overlap - safe to add break
        return True
    
    sessions_with_breaks: List[tuple] = []
    lab_count_in_breaks = 0
    for idx, item in enumerate(final_sessions):
        block, course = item[0], item[1]
        sessions_with_breaks.append((block, course))
        
        # Track labs being added
        if 'LAB' in course:
            lab_count_in_breaks += 1
            print(f"DEBUG LAB: Adding lab {course} to sessions_with_breaks at {block.day} {block.start}-{block.end}")
        
        # Add break after session (but not after lunch, breaks, or electives)
        if isinstance(course, str) and course not in ["LUNCH", "ELECTIVE"] and "Break" not in course:
            next_item = final_sessions[idx + 1] if idx + 1 < len(final_sessions) else None
            if should_place_break(block, next_item):
                break_start = block.end
                break_end = (datetime.combine(datetime.min, break_start) + timedelta(minutes=15)).time()
                
                # Double-check break doesn't overlap with next session
                break_block = TimeBlock(day, break_start, break_end)
                if next_item:
                    next_block = next_item[0]
                    # Only add if no overlap or ends exactly when next starts
                    if not break_block.overlaps(next_block) or break_end == next_block.start:
                        sessions_with_breaks.append((break_block, "Break(15min)"))
                else:
                    # No next item - safe to add
                        sessions_with_breaks.append((break_block, "Break(15min)"))
    
    grid.sessions = sessions_with_breaks
    print(f"DEBUG: Total sessions added to grid: {len(sessions_with_breaks)}, Labs in grid: {lab_count_in_breaks}")
    
    return (grid, deferred)

def generate_24_sheets():
    """Generate all 24 sheets for the timetable"""
    global rescheduling_conflicts
    rescheduling_conflicts = []  # Reset conflicts for each generation
    
    print("Generating 24 sheets for IIIT Dharwad Timetable v2")
    print("=" * 60)
    
    # Step 1: Run Phase 1 - Data validation and section creation
    print("Step 1: Running Phase 1 - Data validation...")
    courses, classrooms, statistics = run_phase1()
    
    # Extract unique semesters from course data
    unique_semesters = sorted(set(course.semester for course in courses 
                                 if course.department in ['CSE', 'DSAI', 'ECE']))
    print(f"Detected semesters from data: {unique_semesters}")
    
    # Create sections based on the data & config
    sections = []
    for dept in DEPARTMENTS:
        for sem in unique_semesters:
            for sec_label in SECTIONS_BY_DEPT.get(dept, []):
                group = get_group_for_section(dept, sec_label)
                sections.append(Section(dept, group, sec_label, sem, STUDENTS_PER_SECTION))
    
    # Step 2: Run Phase 3 - Elective basket scheduling
    print("\nStep 2: Running Phase 3 - Elective basket scheduling...")
    elective_baskets, elective_sessions = run_phase3(courses, sections)
    
    # Step 3: Run Phase 4 - Combined class scheduling
    print("\nStep 3: Running Phase 4 - Combined class scheduling...")
    print("DEBUG: About to call run_phase4")
    print("DEBUG: courses type:", type(courses), "length:", len(courses))
    print("DEBUG: sections type:", type(sections), "length:", len(sections))
    print("DEBUG: run_phase4 function:", run_phase4)
    try:
        phase4_result = run_phase4(courses, sections, classrooms)
        print("DEBUG: run_phase4 completed")
    except Exception as e:
        print(f"DEBUG: Error in run_phase4: {e}")
        import traceback
        traceback.print_exc()
        raise
    schedule = phase4_result['schedule']
    periods = ["PreMid", "PostMid"]
    combined_sessions = map_corrected_schedule_to_sessions(schedule, sections, periods, courses, classrooms)

    # Assign 2 labs to combined course practicals (EC->hardware, exclude research)
    from modules_v2.phase8_classroom_assignment import assign_labs_to_combined_practicals
    combined_sessions = assign_labs_to_combined_practicals(combined_sessions, classrooms)
    
    # Step 4: Run Phase 5 - Core courses scheduling
    print("\nStep 4: Running Phase 5 - Core courses scheduling...")
    phase5_sessions = run_phase5(courses, sections, classrooms, elective_sessions, combined_sessions)
    
    # Step 4.5: Run Phase 7 - Remaining <=2 credit courses scheduling
    print("\n" + "="*80)
    print("Step 4.5: Running Phase 7 - Remaining <=2 credit courses scheduling...")
    print("="*80)
    phase7_sessions = []
    try:
        from modules_v2.phase7_remaining_courses import run_phase7, add_session_to_occupied_slots
        import time
        
        # Build occupied_slots from all previous phases
        occupied_slots = {}
        for session in elective_sessions + phase5_sessions:
            add_session_to_occupied_slots(session, occupied_slots)
        
        # Add combined sessions
        for session in combined_sessions:
            add_session_to_occupied_slots(session, occupied_slots)
        
        # Run Phase 7 with 60 second timeout
        phase7_start = time.time()
        phase7_sessions = run_phase7(courses, sections, classrooms, occupied_slots, {}, combined_sessions, timeout_seconds=60)
        phase7_elapsed = time.time() - phase7_start
        
        if phase7_elapsed > 55:
            print(f"⚠️  Phase 7 took {phase7_elapsed:.1f}s (slow, but completed)")
        else:
            print(f"[OK] Phase 7 completed in {phase7_elapsed:.1f}s: {len(phase7_sessions)} sessions scheduled")
    except Exception as e:
        print(f"WARNING: Phase 7 failed. Continuing without Phase 7. Reason: {e}")
        import traceback
        traceback.print_exc()
        phase7_sessions = []
    
    # Step 5: Run Phase 8 - Classroom assignment for core courses
    print("\n" + "="*80)
    print("Step 5: Running Phase 8 - Classroom assignment for core courses...")
    print("="*80)
    room_assignments = {}
    try:
        from modules_v2.phase8_classroom_assignment import run_phase8
        room_assignments = run_phase8(
            phase5_sessions, phase7_sessions, combined_sessions,
            courses, sections, classrooms, elective_sessions
        )
    except Exception as e:
        print(f"WARNING: Phase 8 failed. Continuing without room assignments. Reason: {e}")
        import traceback
        traceback.print_exc()
        room_assignments = {}
    
    # Step 5.5: Run Phase 9 - Elective Room Assignment
    print("\n" + "="*80)
    print("Step 5.5: Running Phase 9 - Elective Room Assignment...")
    print("="*80)
    elective_assignments = {}
    try:
        from modules_v2.phase9_elective_room_assignment import run_phase9
        all_sessions_for_phase9 = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions
        elective_assignments = run_phase9(
            courses, all_sessions_for_phase9, room_assignments, classrooms,
            all_courses=courses
        )
    except Exception as e:
        print(f"WARNING: Phase 9 failed. Continuing without elective assignments. Reason: {e}")
        import traceback
        traceback.print_exc()
        elective_assignments = {}
    
    # Step 5.55: Room conflict resolution (0 conflicts target)
    elective_sessions_with_rooms = []
    try:
        from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
        def _extract_semester_from_group(gk):
            try:
                return int(str(gk).split('.')[0]) if '.' in str(gk) else int(gk)
            except (ValueError, AttributeError):
                return -1
        for semester, assignments in (elective_assignments or {}).items():
            for a in assignments:
                group_key = a.get('group_key', str(semester) + '.1')
                slots = (ELECTIVE_BASKET_SLOTS or {}).get(group_key) or {}
                course = a.get('course')
                course_code = getattr(course, 'code', '') if course else ''
                # Include assignments with no room (Phase 9 may leave None); resolver will assign
                room = a.get('room') if a.get('room') is not None else ''
                period = a.get('period', 'PRE')
                for slot_name in ('lecture_1', 'lecture_2', 'tutorial'):
                    tb = slots.get(slot_name)
                    if not tb:
                        continue
                    elective_sessions_with_rooms.append({
                        'room': room,
                        'period': period,
                        'time_block': tb,
                        'course_code': course_code,
                        'section': 'ELECTIVE_BASKET_' + str(group_key),
                        '_assignment': a,
                    })
        from utils.room_conflict_resolver import resolve_room_conflicts
        from modules_v2.phase8_classroom_assignment import detect_room_conflicts
        resolved, remaining = resolve_room_conflicts(
            phase5_sessions, phase7_sessions, combined_sessions,
            elective_sessions_with_rooms, classrooms, max_passes=15
        )
        for s in elective_sessions_with_rooms:
            if isinstance(s, dict) and '_assignment' in s and 'room' in s:
                s['_assignment']['room'] = s['room']
        room_conflicts_after = detect_room_conflicts(
            phase5_sessions, phase7_sessions, combined_sessions,
            elective_sessions_with_rooms, classrooms
        )
        if room_conflicts_after:
            print(f"\nWARNING: {len(room_conflicts_after)} classroom conflict(s) remain after resolution:")
            for idx, c in enumerate(room_conflicts_after, 1):
                print(f"  {idx}. Room {c.get('room')} on {c.get('day')} ({c.get('period')}) at {c.get('time')}")
                print(f"     - {c.get('course1')} ({c.get('section1')})")
                print(f"     - {c.get('course2')} ({c.get('section2')})")
        else:
            print("\n[OK] 0 classroom conflicts after resolution")
    except Exception as e:
        print(f"WARNING: Room conflict resolution failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Step 5.6: Run Phase 10 - Course Color Assignment
    print("\n" + "="*80)
    print("Step 5.6: Running Phase 10 - Course Color Assignment...")
    print("="*80)
    course_colors = {}
    try:
        from modules_v2.phase10_course_colors import run_phase10
        course_colors = run_phase10(courses)
    except Exception as e:
        print(f"WARNING: Phase 10 failed. Continuing without course colors. Reason: {e}")
        import traceback
        traceback.print_exc()
        course_colors = {}
    
    # Step 6: Run Phase 6 - Faculty conflict detection and resolution
    print("\nStep 6: Running Phase 6 - Faculty conflict detection and resolution...")
    all_sessions = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions
    
    # First detect conflicts
    faculty_conflicts, conflict_report = run_phase6_faculty_conflicts(all_sessions)
    
    from modules_v2.phase5_core_courses import detect_and_resolve_section_overlaps
    from collections import defaultdict

    # If conflicts exist, resolve them using the central resolver
    if faculty_conflicts and len(faculty_conflicts) > 0:
        print(f"\n=== RESOLVING FACULTY CONFLICTS (Central Resolver) ===")
        
        # Build occupied_slots for the resolver
        occupied_slots = defaultdict(list)
        for session in all_sessions:
            if isinstance(session, dict):
                sections = session.get('sections', [])
                period = session.get('period', 'PRE')
                block = session.get('time_block')
                course_code = session.get('course_code', '')
                if block and sections:
                    for section in sections:
                        section_key = f"{section}_{period}"
                        occupied_slots[section_key].append((block, course_code))
            elif hasattr(session, 'section') and hasattr(session, 'block'):
                section_key = f"{session.section}_{getattr(session, 'period', 'PRE')}"
                occupied_slots[section_key].append((session.block, session.course_code))

        # Use central resolver (handles all session types, respects move priorities)
        all_sessions, remaining_conflicts = resolve_all_faculty_conflicts(
            all_sessions, classrooms, occupied_slots, max_passes=3
        )

        # Update faculty_conflicts with remaining conflicts
        faculty_conflicts = remaining_conflicts
        if faculty_conflicts and len(faculty_conflicts) > 0:
            print(f"WARNING: {len(faculty_conflicts)} conflicts remain after resolution")
            print("  These may require manual review or indicate scheduling constraints")
        else:
            print("[OK] All faculty conflicts resolved successfully")

    # Always resolve overlaps within the same section+period (regardless of faculty-conflict presence)
    # Include BOTH object sessions and combined (dict) sessions so resolver sees full occupancy
    occupied_slots = defaultdict(list)
    for session in all_sessions:
        if isinstance(session, dict):
            sections = session.get('sections', [])
            period = session.get('period', 'PRE')
            block = session.get('time_block')
            course_code = session.get('course_code', '')
            if block and sections:
                for section in sections:
                    section_key = f"{section}_{period}"
                    occupied_slots[section_key].append((block, course_code))
        elif hasattr(session, 'section') and hasattr(session, 'block'):
            section_key = f"{session.section}_{getattr(session, 'period', 'PRE')}"
            occupied_slots[section_key].append((session.block, session.course_code))
    all_sessions = detect_and_resolve_section_overlaps(all_sessions, occupied_slots, classrooms)
    
    # Step 5.5: Validate one-session-per-day rules
    print("\nStep 5.5: Validating one-session-per-day rules...")
    from utils.session_rules_validator import validate_one_session_per_day, SessionRulesValidator
    from utils.data_models import ScheduledSession
    
    # Convert combined_sessions to ScheduledSession objects for validation
    validation_sessions = []
    for session in all_sessions:
        if isinstance(session, dict):
            # Convert dict to ScheduledSession
            session_obj = ScheduledSession(
                course_code=session.get('course_code', '').split('-')[0],
                section=session.get('sections', [None])[0] if session.get('sections') else None,
                kind=session.get('session_type', 'L'),
                block=session.get('time_block'),
                room=session.get('room'),
                period=session.get('period', 'PRE'),
                faculty=session.get('instructor', 'TBD')
            )
            validation_sessions.append(session_obj)
        else:
            validation_sessions.append(session)
    
    is_valid, error_messages = validate_one_session_per_day(validation_sessions)
    if is_valid:
        print("[OK] One-session-per-day rule: PASSED")
    else:
        print(f"⚠️  One-session-per-day rule: {len(error_messages)} violations found")
        for msg in error_messages[:10]:  # Show first 10 violations
            print(f"  - {msg}")
        if len(error_messages) > 10:
            print(f"  ... and {len(error_messages) - 10} more violations")
    
    # Step 6: Verify no overlaps between electives and other courses
    print("\nStep 6: Verifying no overlaps between electives and other courses...")
    
    # Get elective time slots
    from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
    
    elective_conflicts = []
    # Helper to extract semester from group key
    def extract_semester_from_group(gk: str) -> int:
        try:
            if '.' in str(gk):
                return int(str(gk).split('.')[0])
            else:
                return int(gk)
        except (ValueError, AttributeError):
            return -1
    
    for semester in unique_semesters:
        # Find all groups for this semester
        matching_groups = [gk for gk in ELECTIVE_BASKET_SLOTS.keys() 
                          if extract_semester_from_group(gk) == semester]
        
        if not matching_groups:
            continue
        
        # Check conflicts for all groups in this semester
        for group_key in matching_groups:
            elective_slots = ELECTIVE_BASKET_SLOTS[group_key]
            elective_blocks = [
                elective_slots.get('lecture_1'),
                elective_slots.get('lecture_2'),
                elective_slots.get('tutorial')
            ]
            # Filter out None values
            elective_blocks = [b for b in elective_blocks if b is not None]
        
        # Check all other sessions for conflicts with electives
        all_other_sessions = combined_sessions + phase5_sessions + phase7_sessions
        
        for elective_block in elective_blocks:
            for session in all_other_sessions:
                session_block = None
                session_semester = None
                
                if isinstance(session, dict):
                    session_block = session.get('time_block')
                    course_obj = session.get('course_obj')
                    if course_obj:
                        session_semester = getattr(course_obj, 'semester', None)
                elif hasattr(session, 'block'):
                    session_block = session.block
                    # Try to get semester from section
                    if hasattr(session, 'section'):
                        section_label = session.section
                        if f"-Sem{semester}" in section_label:
                            session_semester = semester
                
                if session_block and session_semester == semester:
                    if elective_block.day == session_block.day and elective_block.overlaps(session_block):
                        course_code = session.get('course_code', '') if isinstance(session, dict) else getattr(session, 'course_code', 'Unknown')
                        elective_conflicts.append({
                            'semester': semester,
                            'elective_time': f"{elective_block.day} {elective_block.start}-{elective_block.end}",
                            'conflicting_course': course_code,
                            'conflicting_time': f"{session_block.day} {session_block.start}-{session_block.end}"
                        })
    
    if elective_conflicts:
        print(f"  ERROR: Found {len(elective_conflicts)} conflicts with elective time slots:")
        for conflict in elective_conflicts[:10]:  # Show first 10
            print(f"    Sem {conflict['semester']}: {conflict['conflicting_course']} conflicts with elective at {conflict['elective_time']}")
        if len(elective_conflicts) > 10:
            print(f"    ... and {len(elective_conflicts) - 10} more conflicts")
    else:
        print("  [OK] No conflicts found between electives and other courses")

    # Step 6.25: Verify no overlaps within the SAME section+period (the Excel grid should never show 2 courses at same time)
    print("\nStep 6.25: Verifying no overlaps within each section/period...")
    per_section_conflicts = []
    from collections import defaultdict

    # Use the same normalized session list we already built for one-session-per-day validation
    by_section_period = defaultdict(list)
    for s in validation_sessions:
        if not getattr(s, "section", None) or not getattr(s, "block", None):
            continue
        sec = str(s.section)
        per = str(getattr(s, "period", "PRE"))
        by_section_period[(sec, per)].append(s)

    for (sec, per), sess_list in by_section_period.items():
        # group by day
        by_day = defaultdict(list)
        for s in sess_list:
            by_day[s.block.day].append(s)
        for day, day_sessions in by_day.items():
            # sort by start time
            day_sessions.sort(key=lambda x: (x.block.start.hour, x.block.start.minute, x.block.end.hour, x.block.end.minute))
            # check overlaps
            for i in range(len(day_sessions)):
                for j in range(i + 1, len(day_sessions)):
                    a = day_sessions[i]
                    b = day_sessions[j]
                    if a.block.overlaps(b.block):
                        per_section_conflicts.append(
                            f"{sec} {per} {day}: {a.course_code} {a.block.start}-{a.block.end} overlaps {b.course_code} {b.block.start}-{b.block.end}"
                        )

    if per_section_conflicts:
        print(f"  ERROR: Found {len(per_section_conflicts)} per-section time overlaps:")
        for msg in per_section_conflicts[:10]:
            print(f"    - {msg}")
        if len(per_section_conflicts) > 10:
            print(f"    ... and {len(per_section_conflicts) - 10} more")
    else:
        print("  [OK] No per-section overlaps detected")
    
    # The new time slot allocation should prevent conflicts:
    # - Semester 1 combined courses: Tuesday/Thursday 14:00-16:45 (afternoon)
    # - Semester 3 combined courses: Monday/Wednesday/Friday mornings (09:00-10:30)
    # - Electives: Sem1 (Mon/Wed/Fri 09:00), Sem3 (Mon/Wed/Fri 11:00), Sem5 (Mon/Wed/Fri 14:00)
    
    print("\nTime slot design summary:")
    print("  - Semester 1 combined courses: Tuesday/Thursday afternoons (14:00-16:45)")
    print("  - Semester 3 combined courses: Monday/Wednesday/Friday mornings (09:00-10:30)")
    print("  - Electives: Sem1 (09:00), Sem3 (11:00), Sem5 (14:00) - no overlaps")
    
    # Step 6.5: Final validation - Check all time slots are within 9:00-18:00
    print("\nStep 6.5: Final validation - Checking all time slots are within 9:00-18:00...")
    from utils.time_validator import validate_time_range
    from datetime import time
    
    all_sessions_for_validation = combined_sessions + phase5_sessions + phase7_sessions
    time_violations = []
    for session in all_sessions_for_validation:
        if isinstance(session, dict):
            time_block = session.get('time_block')
            if time_block:
                if not validate_time_range(time_block.start, time_block.end):
                    course_code = session.get('course_code', 'Unknown')
                    time_violations.append(f"{course_code}: {time_block.start}-{time_block.end}")
        elif hasattr(session, 'block'):
            if not validate_time_range(session.block.start, session.block.end):
                time_violations.append(f"{session.course_code}: {session.block.start}-{session.block.end}")
    
    if time_violations:
        print(f"WARNING: Found {len(time_violations)} time violations (outside 9:00-18:00):")
        for violation in time_violations[:10]:  # Show first 10
            print(f"  {violation}")
        if len(time_violations) > 10:
            print(f"  ... and {len(time_violations) - 10} more violations")
    else:
        print("[OK] All time slots are within 9:00-18:00")
    
    # Step 6.75: Final classroom conflict resolution after all rescheduling
    print("\nStep 6.75: Final classroom conflict check after all rescheduling...")
    try:
        from modules_v2.phase8_classroom_assignment import detect_room_conflicts
        from utils.room_conflict_resolver import resolve_room_conflicts

        # Elective room sessions list may have been built earlier; fetch if present
        elective_sessions_for_rooms = locals().get("elective_sessions_with_rooms", None)

        final_room_conflicts = detect_room_conflicts(
            phase5_sessions,
            phase7_sessions,
            combined_sessions,
            elective_sessions_for_rooms,
            classrooms,
        )
        if final_room_conflicts:
            print(
                f"  Found {len(final_room_conflicts)} room conflict(s) after "
                "faculty/section moves; resolving again..."
            )
            _, remaining_final = resolve_room_conflicts(
                phase5_sessions,
                phase7_sessions,
                combined_sessions,
                elective_sessions_for_rooms,
                classrooms,
                max_passes=5,
            )
            room_conflicts_after = detect_room_conflicts(
                phase5_sessions,
                phase7_sessions,
                combined_sessions,
                elective_sessions_for_rooms,
                classrooms,
            )
            if room_conflicts_after:
                print(
                    f"  WARNING: {len(room_conflicts_after)} room conflict(s) "
                    "remain after final pass."
                )
            else:
                print("  [OK] 0 classroom conflicts after final pass.")
        else:
            print("  [OK] No classroom conflicts after rescheduling.")
    except Exception as e:
        print(f"WARNING: Final classroom conflict check failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Step 7: Create timetable with all sessions
    print("\nStep 7: Creating 24 sheets with all sessions...")
    
    # Define sections, semesters, and periods
    section_names = []
    for dept in DEPARTMENTS:
        for sec_label in SECTIONS_BY_DEPT.get(dept, []):
            section_names.append(f"{dept}-{sec_label}")
    semesters = unique_semesters  # Use dynamically extracted semesters
    periods = ["PreMid", "PostMid"]
    
    # Create writer
    writer = TimetableWriterV2(course_colors=course_colors)
    
    # Generate sheets for each combination
    sheet_count = 0
    for section_name in section_names:
        for semester in semesters:
            for period in periods:
                sheet_count += 1
                sheet_name = f"{section_name} Sem{semester} {period}"
                print(f"Creating sheet {sheet_count}/24: {sheet_name}")
                
                # Create sheet
                sheet = writer.workbook.create_sheet(title=sheet_name)
                
                # Set column widths
                sheet.column_dimensions['A'].width = 15
                for col in range(2, 20):
                    sheet.column_dimensions[writer.workbook.worksheets[0].cell(row=1, column=col).column_letter].width = 12
                
                # Add title
                title_cell = sheet['A1']
                title_cell.value = f"Timetable - {section_name} Semester {semester} {period}"
                title_cell.font = writer.header_font
                title_cell.fill = writer.colors['header']
                
                # Add days with integrated schedules
                days = list(WORKING_DAYS)
                current_row = 3
                accumulated_final_sessions = []  # Cross-day finalized sessions for rescheduling
                grid_sessions_dict = {}  # {day: [(TimeBlock, course_display), ...]}
                deferred_by_day = {}  # day -> [(block, course, prio, base), ...] from dedup reschedule

                for day in days:
                    extra = deferred_by_day.get(day, [])
                    schedule_grid, deferred = create_integrated_schedule(
                        day, semester, section_name, period,
                        elective_sessions, combined_sessions, phase5_sessions, phase7_sessions,
                        courses=courses,
                        final_sessions_all_days=accumulated_final_sessions,
                        extra_candidates=extra,
                    )
                    for t in deferred:
                        target_day = t[0].day
                        if target_day not in deferred_by_day:
                            deferred_by_day[target_day] = []
                        deferred_by_day[target_day].append(t)
                    grid_sessions_dict[day] = schedule_grid.sessions
                    for s in schedule_grid.sessions:
                        accumulated_final_sessions.append(s)  # (block, course) each
                    current_row = writer.write_day_schedule(sheet, day, schedule_grid, current_row)
                    current_row += 1
                print("[OK] Done")
                
                # Add verification table after all days - pass grid_sessions to count only displayed sessions
                print(f"    Adding verification table...", end=" ", flush=True)
                current_row += 2  # Add spacing
                current_row = writer.write_verification_table(
                    sheet, current_row, courses, 
                    combined_sessions + elective_sessions,  # Still pass for reference, but grid_sessions takes precedence
                    semester, section_name, period, phase5_sessions,
                    phase7_sessions, combined_sessions, faculty_conflicts,
                    room_assignments,  # Pass room assignments from Phase 8
                    grid_sessions=grid_sessions_dict  # Pass actual displayed sessions from grid
                )
                print("[OK] Done")
                
                # Add elective assignment table below verification table
                print(f"    Adding elective assignment table...", end=" ", flush=True)
                current_row += 2  # Add spacing
                current_row = writer.write_elective_assignment_table(
                    sheet, current_row, semester, courses, elective_assignments.get(semester, [])
                )
                print("[OK] Done")
    
    # Create dedicated C004 (240-seater) timetable sheets for quick visual inspection
    print("Creating C004 room timetable sheets (PreMid/PostMid)...")
    for period_name, period_flag in [("PreMid", "PRE"), ("PostMid", "POST")]:
        sheet_title = f"C004 {period_name}"
        sheet = writer.workbook.create_sheet(title=sheet_title)

        # Set column widths
        sheet.column_dimensions["A"].width = 18
        for col in range(2, 20):
            col_letter = writer.workbook.worksheets[0].cell(row=1, column=col).column_letter
            sheet.column_dimensions[col_letter].width = 14

        # Title
        title_cell = sheet["A1"]
        title_cell.value = f"C004 240-Seater Timetable {period_name}"
        title_cell.font = writer.header_font
        title_cell.fill = writer.colors["header"]

        days = list(WORKING_DAYS)
        current_row = 3

        # Build a day grid from combined_sessions for this room/period
        for day in days:
            grid = DayScheduleGrid(day, 1)  # semester value is only used for lunch; not critical here

            for sess in combined_sessions:
                if not isinstance(sess, dict):
                    continue
                if sess.get("room") != "C004":
                    continue
                if sess.get("period") != period_flag:
                    continue
                tb = sess.get("time_block")
                if not tb or getattr(tb, "day", None) != day:
                    continue

                display_code = sess.get("course_code", "")
                sections = sess.get("sections", [])
                short_secs = sorted({str(s).split("-Sem")[0] for s in sections})
                if short_secs:
                    course_label = f"{display_code} ({', '.join(short_secs)})"
                else:
                    course_label = display_code

                grid.add_session(tb, course_label)

            current_row = writer.write_day_schedule(sheet, day, grid, current_row)
            current_row += 1

    # Create summary sheet
    print("Creating summary sheet...")
    writer.create_summary_sheet(courses)
    
    # Save the main timetable workbook
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"DATA/OUTPUT/IIITDWD_24_Sheets_v2_{timestamp}.xlsx"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    writer.save_timetable(output_path)
    
    print(f"\nGenerated 24 sheets successfully!")
    print(f"Output file: {output_path}")
    print(f"Total sheets: {len(writer.workbook.worksheets)}")
    
    # Print time slot logging summary
    from utils.time_slot_logger import get_logger
    logger = get_logger()
    logger.print_summary()
    
    # Export time slot log to CSV
    log_output_path = f"DATA/OUTPUT/time_slot_log_{timestamp}.csv"
    logger.export_to_csv(log_output_path)

    # ------------------------------------------------------------------
    # Faculty-level outputs: per-faculty timetables + conflict summary
    # ------------------------------------------------------------------
    try:
        faculty_tt_path = f"DATA/OUTPUT/faculty_timetables_{timestamp}.xlsx"
        print(f"\nCreating per-faculty timetables at: {faculty_tt_path}")

        # Include synthetic elective sessions with faculty names so electives
        # appear in per-faculty timetables as well.
        try:
            from modules_v2.phase3_elective_baskets_v2 import build_faculty_elective_sessions
            faculty_elective_sessions = build_faculty_elective_sessions(courses)
        except Exception:
            faculty_elective_sessions = []

        faculty_all_sessions = list(all_sessions) + list(faculty_elective_sessions or [])

        ft_written = write_faculty_timetables(faculty_all_sessions, faculty_conflicts, faculty_tt_path)
        if ft_written:
            print(f"[OK] Faculty timetables written to: {ft_written}")
        else:
            print("[OK] No faculty timetables written (no faculty sessions found).")
    except Exception as e:
        print(f"WARNING: Failed to create faculty timetables. Reason: {e}")
        import traceback
        traceback.print_exc()

    # Classroom-wise outputs: per-room timetables + clash summary
    try:
        classroom_tt_path = os.path.join(
            os.path.dirname(output_path),
            f"classroom_timetables_{timestamp}.xlsx",
        )
        print(f"\nCreating per-classroom timetables at: {classroom_tt_path}")

        # Include elective room assignments as real sessions for per-room view.
        room_all_sessions = list(all_sessions)
        try:
            room_all_sessions += list(elective_sessions_with_rooms or [])
        except NameError:
            # Fallback if elective_sessions_with_rooms is not defined
            pass

        ct_written = write_classroom_timetables(room_all_sessions, classroom_tt_path)
        if ct_written:
            print(f"[OK] Classroom timetables written to: {ct_written}")
        else:
            print("[OK] No classroom timetables written (no room sessions found).")
    except Exception as e:
        print(f"WARNING: Failed to create classroom timetables. Reason: {e}")
        import traceback
        traceback.print_exc()

    # Note: Faculty conflict summary is included in the faculty timetables workbook (SUMMARY sheet),
    # and room clash information is included in the classroom timetables workbook (SUMMARY sheet).

    return output_path, timestamp

def main():
    """Main function to generate 24 sheets"""
    print("IIIT Dharwad Timetable Generator v2 - 24 Sheets Generation")
    print("=" * 70)
    print("Generating timetable with dynamic grid format for:")
    print("- 4 Sections: CSE-A, CSE-B, DSAI-A, ECE-A")
    print("- 3 Semesters: 1, 3, 5")
    print("- 2 Periods: PreMid, PostMid")
    print("- Total: 24 sheets")
    print()
    
    try:
        output_file, _ = generate_24_sheets()
        
        print("\n" + "=" * 70)
        # Save rescheduling conflict log if any conflicts occurred
        from generate_24_sheets import rescheduling_conflicts
        if rescheduling_conflicts:
            import csv
            from datetime import datetime
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DATA", "OUTPUT")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"rescheduling_conflicts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            
            with open(log_file, 'w', newline='', encoding='utf-8') as f:
                if rescheduling_conflicts:
                    writer = csv.DictWriter(f, fieldnames=rescheduling_conflicts[0].keys())
                    writer.writeheader()
                    writer.writerows(rescheduling_conflicts)
            
            print(f"\n⚠️  Rescheduling conflicts logged: {len(rescheduling_conflicts)} conflicts")
            print(f"   Conflict log: {log_file}")
        
        print("SUCCESS: 24 sheets generated successfully!")
        print(f"Output file: {output_file}")
        print("\nFeatures included:")
        print("- Dynamic time slots (only scheduled sessions shown)")
        print("- Staggered lunch breaks per semester")
        print("- Color-coded cells (Lunch=Gray, Courses=Colored)")
        print("- Professional formatting with merged cells")
        print("- 15-minute breaks after each class")
        
    except Exception as e:
        print(f"ERROR: Failed to generate 24 sheets: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
