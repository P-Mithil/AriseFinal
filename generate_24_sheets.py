"""
Generate 24 sheets for IIIT Dharwad Timetable v2
Creates sheets for all sections, semesters, and periods with dynamic grid format.
"""

import os
import re
import sys
import logging
from datetime import time, datetime, timedelta
from typing import List, Dict, Tuple, Optional

# Add the current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.data_models import DayScheduleGrid, TimeBlock, Section
from utils.timetable_writer_v2 import TimetableWriterV2
from config.schedule_config import (
    WORKING_DAYS,
    DAY_START_TIME,
    DAY_END_TIME,
    LUNCH_WINDOWS,
    GENERATION_RUNTIME_MODE,
    GENERATION_RUNTIME_SCALE,
)
from utils.time_validator import validate_time_range
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


def _scaled_budget(base_value: int, minimum: int = 1) -> int:
    """Apply runtime scale to a data-driven budget."""
    try:
        scaled = int(round(float(base_value) * float(GENERATION_RUNTIME_SCALE)))
    except Exception:
        scaled = int(base_value)
    return max(int(minimum), scaled)


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

                # Resolve section label(s) — may be a comma-joined combined-class string
                if hasattr(section_ref, "label"):
                    section_label = section_ref.label
                else:
                    section_label = str(section_ref) if section_ref is not None else ""

                # Split comma-joined section labels (e.g. "CSE-A-Sem1,CSE-B-Sem1")
                section_labels_list = [s.strip() for s in section_label.split(",") if s.strip()]
                if not section_labels_list:
                    section_labels_list = []

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
                        "sections": section_labels_list,
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
    
    # Generate time slots for the day (config-driven window, 15-minute intervals; honor minutes)
    current_dt = datetime.combine(datetime.min, DAY_START_TIME)
    end_dt = datetime.combine(datetime.min, DAY_END_TIME)
    
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
    
    # Generate candidate slots (current_dt / end_dt set above from DAY_START_TIME / DAY_END_TIME)
    while current_dt < end_dt:
        # Calculate end time for this slot
        slot_end_dt = current_dt + timedelta(minutes=session_duration_minutes)
        if slot_end_dt > end_dt:
            break
        
        slot_start = current_dt.time()
        slot_end = slot_end_dt.time()
        candidate_block = TimeBlock(day, slot_start, slot_end)

        if not validate_time_range(slot_start, slot_end):
            current_dt += timedelta(minutes=15)
            continue

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


def rebalance_lt_mix_for_section_courses(sessions: List, courses: List[Course]) -> int:
    """
    Rebalance L/T labels when a section-course has lecture deficit but tutorial surplus.

    This keeps weekly contact count unchanged while matching LTPSC expectations more robustly
    across stochastic runs (e.g., one lecture dropped and one extra tutorial added).

    Rebalancing is done per (section, code, period) to avoid cross-period L/T swaps that
    would leave one period under-scheduled while the other looks correct in aggregate.
    """
    from modules_v2.phase5_core_courses import calculate_slots_needed

    def _base_code(code: str) -> str:
        return (code or "").replace("-TUT", "").replace("-LAB", "").strip()

    def _semester_from_section(section: str) -> int:
        m = re.search(r"Sem(\d+)", section or "")
        return int(m.group(1)) if m else -1

    def _period_bin(sess) -> str:
        p = str(getattr(sess, "period", "") or "").strip().upper()
        if p in ("PRE", "PREMID", "PRE-MID", "PRE MID", "PRE_MID"):
            return "PRE"
        if p in ("POST", "POSTMID", "POST-MID", "POST MID", "POST_MID"):
            return "POST"
        return "PRE"  # default

    # Build fast lookup: (code, semester, dept) -> required counts
    req_lookup: Dict[Tuple[str, int, str], Dict[str, int]] = {}
    for c in courses or []:
        if getattr(c, "is_elective", False):
            continue
        code = getattr(c, "code", None)
        sem = getattr(c, "semester", None)
        dept = getattr(c, "department", None)
        ltpsc = getattr(c, "ltpsc", None)
        if not code or sem is None or not dept or not ltpsc:
            continue
        slots = calculate_slots_needed(ltpsc)
        req_lookup[(code, sem, dept)] = {
            "lectures": slots.get("lectures", 0),
            "tutorials": slots.get("tutorials", 0),
        }

    # Key is (section, code, period) — rebalance independently per period
    buckets: Dict[Tuple[str, str, str], Dict[str, List]] = {}
    for s in sessions or []:
        section = getattr(s, "section", "") or ""
        sem = _semester_from_section(section)
        dept = section.split("-")[0] if "-" in section else ""
        code_raw = getattr(s, "course_code", "") or ""
        code = _base_code(code_raw)
        if not section or not code or sem <= 0 or not dept:
            continue

        req = req_lookup.get((code, sem, dept))
        if not req:
            continue

        kind = (getattr(s, "kind", None) or getattr(s, "session_type", None) or "L").upper()
        if kind not in ("L", "T"):
            continue

        pbin = _period_bin(s)
        key = (section, code, pbin)
        if key not in buckets:
            buckets[key] = {"L": [], "T": [], "req": req}
        buckets[key][kind].append(s)

    changed = 0
    for (_section, _code, _pbin), bucket in buckets.items():
        req_lectures = bucket["req"]["lectures"]
        req_tutorials = bucket["req"]["tutorials"]
        lectures = bucket["L"]
        tutorials = bucket["T"]

        lecture_deficit = max(0, req_lectures - len(lectures))
        tutorial_surplus = max(0, len(tutorials) - req_tutorials)
        to_convert = min(lecture_deficit, tutorial_surplus)
        if to_convert <= 0:
            continue

        # Convert surplus tutorials into lectures to satisfy LT mix.
        for sess in tutorials[:to_convert]:
            ccode = getattr(sess, "course_code", "") or ""
            if ccode.endswith("-TUT"):
                setattr(sess, "course_code", ccode[:-4])
            if hasattr(sess, "kind"):
                setattr(sess, "kind", "L")
            if hasattr(sess, "session_type"):
                setattr(sess, "session_type", "L")
            changed += 1

    return changed


def trim_core_sessions_to_exact_ltpsc(phase5_sessions: List, phase7_sessions: List, courses: List[Course]) -> int:
    """
    Trim extra non-elective sessions so each (section, course, period) matches LTPSC exactly.

    Counts are enforced **per PreMid / PostMid period** (PRE vs POST), not merged across the
    whole semester. Merging PRE+POST into one bucket and trimming to a single ``required``
    count used to delete every POST session when Phase 5 scheduled full loads in both halves
    (e.g. CS163), emptying PostMid grids while semester totals looked "correct".
    """
    from modules_v2.phase5_core_courses import calculate_slots_needed

    def _base_code(code: str) -> str:
        return (code or "").replace("-TUT", "").replace("-LAB", "").split("-")[0].strip()

    def _section_sem_dept(section: str) -> Tuple[int, str]:
        m = re.search(r"Sem(\d+)", section or "")
        sem = int(m.group(1)) if m else -1
        dept = section.split("-")[0].strip() if "-" in section else ""
        return sem, dept

    def _kind_of(sess) -> str:
        return (getattr(sess, "kind", None) or getattr(sess, "session_type", None) or "L").upper()

    def _period_bin(sess) -> Optional[str]:
        p = str(getattr(sess, "period", "") or "").strip().upper()
        if p in ("PRE", "PREMID", "PRE-MID", "PRE MID", "PRE_MID"):
            return "PRE"
        if p in ("POST", "POSTMID", "POST-MID", "POST MID", "POST_MID"):
            return "POST"
        return None

    def _sort_key_within_period(sess):
        block = getattr(sess, "block", None)
        day = getattr(block, "day", "") if block else ""
        start = getattr(block, "start", None)
        start_s = start.strftime("%H:%M") if start else "99:99"
        return (day, start_s)

    req_lookup: Dict[Tuple[str, int, str], Dict[str, int]] = {}
    for c in courses or []:
        if getattr(c, "is_elective", False):
            continue
        code = getattr(c, "code", None)
        sem = getattr(c, "semester", None)
        dept = getattr(c, "department", None)
        ltpsc = getattr(c, "ltpsc", "")
        if not code or sem is None or not dept or not ltpsc:
            continue
        slots = calculate_slots_needed(ltpsc)
        req_lookup[(code, int(sem), dept)] = {
            "L": int(slots.get("lectures", 0)),
            "T": int(slots.get("tutorials", 0)),
            "P": int(slots.get("practicals", 0)),
        }

    buckets: Dict[Tuple[str, str, str], Dict[str, List]] = {}
    for sess in (phase5_sessions or []) + (phase7_sessions or []):
        section = getattr(sess, "section", "") or ""
        if not section:
            continue
        sem, dept = _section_sem_dept(section)
        code = _base_code(getattr(sess, "course_code", "") or "")
        if not code or sem <= 0 or not dept:
            continue
        req = req_lookup.get((code, sem, dept))
        if not req:
            continue
        pbin = _period_bin(sess)
        if pbin is None:
            continue
        kind = _kind_of(sess)
        if kind not in ("L", "T", "P"):
            kind = "L"
        key = (section, code, pbin)
        if key not in buckets:
            buckets[key] = {"req": req, "L": [], "T": [], "P": []}
        buckets[key][kind].append(sess)

    to_remove_ids = set()
    for _key, b in buckets.items():
        for kind in ("L", "T", "P"):
            sessions_of_kind = sorted(b[kind], key=_sort_key_within_period)
            required = b["req"][kind]
            if len(sessions_of_kind) > required:
                for s in sessions_of_kind[required:]:
                    to_remove_ids.add(id(s))

    before = len(phase5_sessions or []) + len(phase7_sessions or [])
    if to_remove_ids:
        phase5_sessions[:] = [s for s in (phase5_sessions or []) if id(s) not in to_remove_ids]
        phase7_sessions[:] = [s for s in (phase7_sessions or []) if id(s) not in to_remove_ids]
    after = len(phase5_sessions or []) + len(phase7_sessions or [])
    return max(0, before - after)


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
        for sec in session_sections:
            if expected_normalized == normalize_section_string(sec):
                return True
        return False
    elif isinstance(session_sections, str):
        # The logged string could be "CSE-A-Sem1, CSE-B-Sem1" or "['CSE-A-Sem1', 'CSE-B-Sem1']"
        # We check if the expected_normalized section appears as a distinct word in the string
        session_str = session_sections.upper()
        # Remove brackets and quotes if it's a string representation of a list
        session_str = session_str.replace('[', '').replace(']', '').replace("'", "").replace('"', '')
        parts = [normalize_section_string(p.strip()) for p in session_str.split(',')]
        return expected_normalized in parts
    else:
        # Try to convert to string and match
        session_str = str(session_sections).upper().replace('[', '').replace(']', '').replace("'", "").replace('"', '')
        parts = [normalize_section_string(p.strip()) for p in session_str.split(',')]
        return expected_normalized in parts

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
    sessions_from_log: List[Dict] = None,
) -> tuple:
    """Create an integrated schedule with electives, combined classes, core courses, and Phase 7 courses.
    Returns (DayScheduleGrid, deferred) where deferred is a list of (block, course, prio, base) rescheduled to another day.
    """
    grid = DayScheduleGrid(day, semester)
    deferred: List[tuple] = []

    period_code = normalize_period(period)
    expected_section = f"{section_name}-Sem{semester}"

    if sessions_from_log is not None:
        # FAST PATH: use pre-existing sessions from log to rebuild exactly
        relevant = []
        for s in sessions_from_log:
            if (match_section(expected_section, s.get("Section", "")) and 
                str(s.get("Day", "")).strip().upper() == str(day).strip().upper() and 
                normalize_period(s.get("Period", "")) == period_code):
                relevant.append(s)
        
        final_sessions = []
        for s in relevant:
            try:
                start_str = s.get("Start Time", "00:00")
                end_str = s.get("End Time", "00:00")
                sh, sm = map(int, start_str.split(':')[:2])
                eh, em = map(int, end_str.split(':')[:2])
                block = TimeBlock(day, time(sh, sm), time(eh, em))
                # Skip if already in final_sessions (some logs might have redundancy if not cleaned)
                if any(b.start == block.start and b.end == block.end for b, c, p, bse, *rest in final_sessions):
                    continue
                
                # Support both API-style (course_code, faculty, room) and Log-style (Course Code, Faculty, Room)
                c_code = s.get("course_code") or s.get("Course Code", "")
                fac = s.get("faculty") or s.get("Faculty", "")
                rm = s.get("room") or s.get("Room", "")
                
                # Extract base_course for verification (satisfaction check)
                if c_code.startswith('ELECTIVE_BASKET_'):
                    base = c_code.replace('-LAB', '').replace('-TUT', '')
                else:
                    base = c_code.replace("-TUT", "").replace("-LAB", "").split("-")[0]
                
                # Include metadata for Faculty and Room
                meta = {"faculty": fac, "room": rm}
                final_sessions.append((block, c_code, 0, base, meta))
            except Exception as e:
                print(f"Error processing session from log: {e}")
                continue
        
        # Add lunch block with empty metadata to prevent unpack errors downstream
        final_sessions.append((grid.lunch_block, "LUNCH", -1, "LUNCH", {"faculty": "", "room": ""}))
        final_sessions.sort(key=lambda x: x[0].start)
        
        return _add_breaks_to_grid(grid, final_sessions, day), []

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
                    # Keep this at debug level; warning-level spam here significantly slows runs.
                    import logging
                    logging.debug(
                        "CS307 NOT added: section_match=%s, period_match=%s, day_match=%s, has_block=%s",
                        section_match, period_match, day_match, (session_block is not None)
                    )
    
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
    
    # Add lunch block before calculating breaks
    final_sessions.append((grid.lunch_block, "LUNCH", -1, "LUNCH"))
    
    # Sort again by start
    final_sessions.sort(key=lambda x: x[0].start)
    
    return _add_breaks_to_grid(grid, final_sessions, day), deferred

def _add_breaks_to_grid(grid: DayScheduleGrid, final_sessions: List[tuple], day: str) -> DayScheduleGrid:
    """Helper to insert smart 15-minute breaks after sessions in the grid"""
    from datetime import datetime, timedelta
    
    def should_place_break(current_block: TimeBlock, next_item: tuple) -> bool:
        if next_item is None: return True
        next_block, next_course = next_item[0], next_item[1]
        if next_course == "LUNCH": return False
        if isinstance(next_course, str) and "Break" in next_course: return False
        
        break_start = current_block.end
        break_end = (datetime.combine(datetime.min, break_start) + timedelta(minutes=15)).time()
        break_block = TimeBlock(day, break_start, break_end)
        
        if break_block.overlaps(next_block):
            if break_end == next_block.start: return True
            return False
        return True

    sessions_with_breaks: List[tuple] = []
    for idx, item in enumerate(final_sessions):
        block, course = item[0], item[1]
        sessions_with_breaks.append(item)  # Preserve the full 5-tuple
        
        if isinstance(course, str) and course not in ["LUNCH", "ELECTIVE"] and "Break" not in course:
            next_item = final_sessions[idx + 1] if idx + 1 < len(final_sessions) else None
            if should_place_break(block, next_item):
                break_start = block.end
                break_end = (datetime.combine(datetime.min, break_start) + timedelta(minutes=15)).time()
                break_block = TimeBlock(day, break_start, break_end)
                
                # Create a uniform 5-tuple for the break slot
                break_tuple = (break_block, "Break(15min)", -1, "Break", {"faculty": "", "room": ""})
                
                if next_item:
                    next_block = next_item[0]
                    if not break_block.overlaps(next_block) or break_end == next_block.start:
                        sessions_with_breaks.append(break_tuple)
                else:
                    sessions_with_breaks.append(break_tuple)
    
    grid.sessions = sessions_with_breaks
    return grid


def _build_faculty_room_lookup_from_pipeline_sessions(all_sessions) -> dict:
    """
    Map (section, course_code, day, start, end, period, session_type) -> (faculty, room)
    from post–Phase-6 pipeline sessions so Excel/CSV use the same instructor as the resolver.
    """
    lookup = {}

    def _store(sec: str, cc: str, day: str, t0: str, t1: str, per: str, stype_k: str, fac: str, room: str):
        if not sec or not day:
            return
        key = (sec, cc, day, t0, t1, per, stype_k)
        if key not in lookup and (fac or room):
            lookup[key] = (fac or "", room or "")

    for s in all_sessions or []:
        if isinstance(s, dict):
            block = s.get("time_block") or s.get("block")
            if not block:
                continue
            fac = str(s.get("instructor") or s.get("faculty") or "").strip()
            room = str(s.get("room") or "").strip()
            cc = str(s.get("course_code") or "").strip()
            per = normalize_period(str(s.get("period") or "PRE"))
            st_raw = str(s.get("session_type") or s.get("kind") or "L").strip().upper()
            if st_raw in ("L", "T", "P"):
                st_map = st_raw
            elif st_raw == "ELECTIVE" or "ELECTIVE_BASKET" in cc.upper():
                st_map = "ELECTIVE"
            else:
                st_map = "L"
            day = str(block.day).strip()
            t0 = block.start.strftime("%H:%M")
            t1 = block.end.strftime("%H:%M")
            secs = s.get("sections") or []
            if not secs:
                continue
            stypes = {st_map}
            if st_map == "L" and "ELECTIVE_BASKET" in cc.upper():
                stypes.add("ELECTIVE")
            for sec in secs:
                sec = str(sec).strip()
                for st_k in stypes:
                    _store(sec, cc, day, t0, t1, per, st_k, fac, room)
                base = cc.replace("-TUT", "").replace("-LAB", "").split("-")[0]
                if base and base != cc:
                    for st_k in stypes:
                        _store(sec, base, day, t0, t1, per, st_k, fac, room)
        else:
            block = getattr(s, "block", None)
            if not block:
                continue
            fac = str(getattr(s, "faculty", None) or "").strip()
            room = str(getattr(s, "room", None) or "").strip()
            cc = str(getattr(s, "course_code", "") or "").strip()
            per = normalize_period(str(getattr(s, "period", None) or "PRE"))
            kind = str(getattr(s, "kind", "L") or "L").strip().upper()
            if kind in ("L", "T", "P"):
                st_map = kind
            elif kind == "ELECTIVE" or "ELECTIVE_BASKET" in cc.upper():
                st_map = "ELECTIVE"
            else:
                st_map = "L"
            day = str(block.day).strip()
            t0 = block.start.strftime("%H:%M")
            t1 = block.end.strftime("%H:%M")
            sec = str(getattr(s, "section", "") or "").strip()
            if not sec:
                continue
            stypes = {st_map}
            if st_map == "L" and "ELECTIVE_BASKET" in cc.upper():
                stypes.add("ELECTIVE")
            for st_k in stypes:
                _store(sec, cc, day, t0, t1, per, st_k, fac, room)
            base = cc.split("-")[0] if cc else ""
            if kind == "T" and base:
                cc_tut = f"{base}-TUT" if "-TUT" not in cc.upper() else cc
                for st_k in ("T", "L"):
                    _store(sec, cc_tut, day, t0, t1, per, st_k, fac, room)
            if kind == "P" and base:
                cc_lab = f"{base}-LAB" if "-LAB" not in cc.upper() else cc
                for st_k in ("P", "L"):
                    _store(sec, cc_lab, day, t0, t1, per, st_k, fac, room)
            if base and base != cc:
                for st_k in stypes:
                    _store(sec, base, day, t0, t1, per, st_k, fac, room)
    return lookup


def _lookup_faculty_room_from_pipeline_map(
    faculty_room_lookup: dict,
    section_key: str,
    course_display: str,
    day: str,
    block,
    period: str,
    stype: str,
) -> tuple:
    """Return (faculty, room) from lookup or ("", "") if missing."""
    if not faculty_room_lookup or not block:
        return "", ""
    lk_period = normalize_period(period)
    day_s = str(day).strip()
    t0 = block.start.strftime("%H:%M")
    t1 = block.end.strftime("%H:%M")
    for st_try in (stype, "L", "T", "P", "ELECTIVE"):
        k = (section_key, course_display, day_s, t0, t1, lk_period, st_try)
        if k in faculty_room_lookup:
            return faculty_room_lookup[k]
    base = course_display.replace("-TUT", "").replace("-LAB", "").split("-")[0]
    if base and base != course_display:
        for st_try in (stype, "L", "T", "P", "ELECTIVE"):
            k = (section_key, base, day_s, t0, t1, lk_period, st_try)
            if k in faculty_room_lookup:
                return faculty_room_lookup[k]
    return "", ""


def generate_24_sheets(sessions_from_log: List[Dict] = None):
    """Generate all 24 sheets for the timetable.
    If sessions_from_log is provided, it use those sessions directly and skips the scheduling phases.
    """
    import random

    _run_started_at = datetime.now()

    # Reproducible scheduling:
    # - If ARISE_GENERATION_SEED is set, honor it.
    # - Otherwise use a stable per-dataset default to avoid run-to-run drift.
    _seed_raw = str(os.environ.get("ARISE_GENERATION_SEED", "") or "").strip()
    if _seed_raw.isdigit():
        random.seed(int(_seed_raw))
    else:
        import time
        _seed = int(time.time() * 1000) % 100000
        random.seed(_seed)
        # Intentionally leave _seed_raw empty so macro-retries can sweep seeds if needed

    from utils.time_slot_logger import reset_logger
    if sessions_from_log is None:
        reset_logger()
    
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
    
    # Step 2-6: Scheduling Phases (Skip if pre-loaded from log)
    elective_sessions = []
    combined_sessions = []
    phase5_sessions = []
    phase7_sessions = []
    faculty_conflicts = []
    course_colors = {}
    all_sessions = []
    elective_sessions_with_rooms = []
    elective_assignments = {}
    room_assignments = {}

    if sessions_from_log is not None:
        print("Bypassing Phase 2-6 scheduling as sessions were provided from log.")
        all_sessions = sessions_from_log

        # Build room_assignments from the log so verification table has correct rooms
        # Also detect combined courses (same course+day+time in multiple sections)
        # and ensure they get C004 if no room was already assigned.
        from collections import defaultdict as _defaultdict
        _combined_course_codes: set = set()
        _slot_sections = _defaultdict(set)  # (course_code_base, day, start) -> set(sections)
        for s in sessions_from_log:
            cc = str(s.get('Course Code', s.get('course_code', ''))).split('-')[0].strip()
            sec = str(s.get('Section', s.get('section', ''))).strip()
            day = str(s.get('Day', s.get('day', ''))).strip()
            start = str(s.get('Start Time', s.get('start_time', ''))).strip()
            _slot_sections[(cc, day, start)].add(sec)
        course_map_all = {getattr(c, 'code', ''): c for c in courses}
        for key, secs in _slot_sections.items():
            if len(secs) > 1:
                cc_obj = course_map_all.get(key[0])
                if cc_obj and getattr(cc_obj, 'is_combined', False):
                    _combined_course_codes.add(key[0])

        for s in sessions_from_log:
            cc = str(s.get('Course Code', s.get('course_code', ''))).strip()
            sec = str(s.get('Section', s.get('section', ''))).strip()
            period = str(s.get('Period', s.get('period', 'PRE'))).strip().upper()
            if period in ('PREMID',): period = 'PRE'
            if period in ('POSTMID',): period = 'POST'
            room = str(s.get('Room', s.get('room', ''))).strip()
            stype = str(s.get('Session Type', s.get('session_type', 'L'))).strip().upper()

            base_code = cc.replace('-TUT', '').replace('-LAB', '').split('-')[0]
            room_key = (base_code, sec, period)

            if room_key not in room_assignments:
                room_assignments[room_key] = {'classroom': '', 'labs': []}

            if room and room.lower() not in ('nan', 'none', 'tbd', ''):
                # Classify as lab or classroom using room_type from classrooms list
                is_lab = any(
                    hasattr(cr, 'room_number') and str(cr.room_number).strip() == room
                    and hasattr(cr, 'room_type') and 'lab' in str(cr.room_type).lower()
                    for cr in classrooms
                )
                if is_lab:
                    if room not in room_assignments[room_key]['labs']:
                        room_assignments[room_key]['labs'].append(room)
                elif not room_assignments[room_key]['classroom']:
                    room_assignments[room_key]['classroom'] = room

            # Phase 4 combined courses: default to C004 if no classroom found
            if not room_assignments[room_key]['classroom'] and base_code in _combined_course_codes:
                room_assignments[room_key]['classroom'] = 'C004'

        
        # Load cached elective assignments to prevent "TBD" in the Excel tables
        import json
        cache_file = "DATA/OUTPUT/elective_assignments_cache.json"
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cache_data = json.load(f)
                # Reconstruct course objects for the assignments
                course_map = {getattr(c, 'code', ''): c for c in courses}
                for sem_str, assignments in cache_data.items():
                    try:
                        sem = int(sem_str)
                    except ValueError:
                        sem = sem_str
                    elective_assignments[sem] = []
                    for a in assignments:
                        course_obj = course_map.get(a.get('course_code', ''))
                        if course_obj:
                            elective_assignments[sem].append({
                                'course': course_obj,
                                'room': a.get('room', ''),
                                'period': a.get('period', ''),
                                'faculty': a.get('faculty', ''),
                                'group_key': a.get('group_key', '')
                            })
                print("Loaded cached elective assignments.")
            except Exception as e:
                print(f"Error loading elective assignments cache: {e}")
    else:
        # Step 2: Run Phase 3 - Elective basket scheduling
        print("\nStep 2: Running Phase 3 - Elective basket scheduling...")
        elective_baskets, elective_sessions = run_phase3(courses, sections)
        
        # Step 3: Run Phase 4 - Combined class scheduling
        print("\nStep 3: Running Phase 4 - Combined class scheduling...")
        phase4_result = run_phase4(courses, sections, classrooms)
        schedule = phase4_result['schedule']
        periods = ["PreMid", "PostMid"]
        combined_sessions = map_corrected_schedule_to_sessions(schedule, sections, periods, courses, classrooms)

        # Assign 2 labs to combined course practicals
        from modules_v2.phase8_classroom_assignment import assign_labs_to_combined_practicals
        combined_sessions = assign_labs_to_combined_practicals(combined_sessions, classrooms)
        
        # Step 4: Run Phase 5 - Core courses scheduling
        print("\nStep 4: Running Phase 5 - Core courses scheduling...")
        phase5_sessions = run_phase5(courses, sections, classrooms, elective_sessions, combined_sessions)
        
        # Step 4.5: Run Phase 7 - Remaining <=2 credit courses scheduling
        phase7_sessions = []
        try:
            from modules_v2.phase7_remaining_courses import run_phase7, add_session_to_occupied_slots
            occupied_slots = {}
            for session in elective_sessions + phase5_sessions:
                add_session_to_occupied_slots(session, occupied_slots)
            for session in combined_sessions:
                add_session_to_occupied_slots(session, occupied_slots)
            # Run Phase 7
            phase7_sessions = run_phase7(
                courses,
                sections,
                classrooms,
                occupied_slots,
                {},
                combined_sessions,
                context_sessions=(elective_sessions + combined_sessions + phase5_sessions),
                timeout_seconds=60,
            )
        except Exception as e:
            print(f"ERROR in Phase 7: {e}")
            phase7_sessions = []

        # Stabilize LT mix for non-elective section courses across stochastic runs.
        lt_relabels = rebalance_lt_mix_for_section_courses((phase5_sessions or []) + (phase7_sessions or []), courses)
        if lt_relabels:
            print(f"Adjusted {lt_relabels} session(s) from T to L to satisfy LTPSC lecture/tutorial mix.")

        # Enforce exact LTPSC counts for non-elective core sessions by trimming surplus.
        trimmed_exact = trim_core_sessions_to_exact_ltpsc(phase5_sessions, phase7_sessions, courses)
        if trimmed_exact:
            print(f"Trimmed {trimmed_exact} surplus core session(s) to enforce exact LTPSC counts.")


    
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
            # Cache elective assignments for fast-path regeneration
            import json
            os.makedirs("DATA/OUTPUT", exist_ok=True)
            cache_data = {}
            for sem, assignments in elective_assignments.items():
                cache_data[sem] = []
                for a in assignments:
                    cache_data[sem].append({
                        'course_code': getattr(a.get('course'), 'code', '') if a.get('course') else '',
                        'room': a.get('room', ''),
                        'period': a.get('period', ''),
                        'faculty': a.get('faculty', ''),
                        'group_key': a.get('group_key', '')
                    })
            with open("DATA/OUTPUT/elective_assignments_cache.json", "w") as f:
                json.dump(cache_data, f)
        except Exception as e:
            print(f"WARNING: Phase 9 failed. Continuing without elective assignments. Reason: {e}")
            import traceback
            traceback.print_exc()
            elective_assignments = {}
        
        # Step 5.55: Room conflict resolution (0 conflicts target)
        elective_sessions_with_rooms = []
        try:
            from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
            def _extract_semester_from_group_i(gk):
                try:
                    return int(str(gk).split('.')[0]) if '.' in str(gk) else int(gk)
                except (ValueError, AttributeError):
                    return -1
            for semester_val, assignments in (elective_assignments or {}).items():
                for a in assignments:
                    group_key = a.get('group_key', str(semester_val) + '.1')
                    slots = (ELECTIVE_BASKET_SLOTS or {}).get(group_key) or {}
                    course = a.get('course')
                    course_code = getattr(course, 'code', '') if course else ''
                    room = a.get('room') if a.get('room') is not None else ''
                    period_val = a.get('period', 'PRE')
                    for slot_name in ('lecture_1', 'lecture_2', 'tutorial'):
                        tb = slots.get(slot_name)
                        if not tb:
                            continue
                        elective_sessions_with_rooms.append({
                            'room': room,
                            'period': period_val,
                            'time_block': tb,
                            'course_code': f"{course_code}-Elective",
                            'section': 'Elective',
                            'course_obj': course,
                            'session_type': 'L' if 'lecture' in slot_name else 'T'
                        })
        except Exception as e:
            print(f"WARNING: Error building elective_sessions_with_rooms: {e}")
            import traceback
            traceback.print_exc()
        try:
            from utils.room_conflict_resolver import resolve_room_conflicts
            from utils.room_conflict_resolver import resolve_unassigned_core_classrooms
            from modules_v2.phase8_classroom_assignment import detect_room_conflicts
            room_slot_attempt_budget = max(
                len(classrooms or []),
                (len(sections or []) + len(courses or [])) // 3
            )
            room_slot_attempt_budget = _scaled_budget(max(18, room_slot_attempt_budget), minimum=12)
            room_pass_budget = _scaled_budget(
                max(3, (len(classrooms or []) + len(sections or [])) // 4),
                minimum=2,
            )
            recovered_unassigned = resolve_unassigned_core_classrooms(
                phase5_sessions,
                phase7_sessions,
                combined_sessions,
                elective_sessions_with_rooms,
                classrooms,
                courses=courses,
                sections=sections,
                max_slot_attempts=room_slot_attempt_budget,
            )
            if recovered_unassigned:
                print(f"  [OK] Recovered unassigned core classrooms: {recovered_unassigned}")
            resolved, remaining = resolve_room_conflicts(
                phase5_sessions, phase7_sessions, combined_sessions,
                elective_sessions_with_rooms, classrooms, courses=courses, sections=sections, max_passes=room_pass_budget
            )
            for s in elective_sessions_with_rooms:
                if isinstance(s, dict) and '_assignment' in s and 'room' in s:
                    s['_assignment']['room'] = s['room']

            # CRITICAL FIX: resolve_room_conflicts mutates session.room in-place but does NOT
            # update room_assignments.  Sync them now so final_ui_sessions gets correct rooms.
            if room_assignments:
                for sess in list(phase5_sessions) + list(phase7_sessions):
                    r = getattr(sess, 'room', None)
                    if not r:
                        continue
                    cc = str(getattr(sess, 'course_code', '') or '').split('-')[0]
                    sec = getattr(sess, 'section', '')
                    per = str(getattr(sess, 'period', 'PRE') or 'PRE').strip().upper()
                    per = 'PRE' if per in ('PRE', 'PREMID') else ('POST' if per in ('POST', 'POSTMID') else per)
                    key = (cc, sec, per)
                    kind = getattr(sess, 'kind', 'L') or 'L'
                    if key in room_assignments:
                        if kind == 'P':
                            # For practicals update the per-session room on the object only
                            pass
                        else:
                            room_assignments[key]['classroom'] = r
                    else:
                        room_assignments[key] = {'classroom': r if kind != 'P' else '', 'labs': [r] if kind == 'P' else []}
                print(f"  [OK] room_assignments refreshed from {len(phase5_sessions)+len(phase7_sessions)} post-resolution sessions")

            room_conflicts_after = detect_room_conflicts(
                phase5_sessions, phase7_sessions, combined_sessions,
                elective_sessions_with_rooms, classrooms
            )
            if room_conflicts_after:
                print(f"\nWARNING: {len(room_conflicts_after)} classroom conflict(s) remain after resolution:")
            else:
                print("\n[OK] 0 classroom conflicts after resolution")
        except Exception as e:
            print(f"WARNING: Room conflict resolution failed: {e}")
    
        # Step 6: Run Phase 6 - Faculty conflict detection and resolution
        print("\nStep 6: Running Phase 6 - Faculty conflict detection and resolution...")
        all_sessions = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions

        def _print_faculty_conflict_diagnostics(fc_list, stage: str, top_faculties: int = 6, sample_size: int = 8):
            fc_list = fc_list or []
            if not fc_list:
                print(f"  [Diag] {stage}: 0 faculty conflicts")
                return

            from collections import Counter
            faculty_counts = Counter(
                (getattr(fc, "faculty_name", "") or "").strip().lower() for fc in fc_list
            )
            top = faculty_counts.most_common(top_faculties)
            top_str = ", ".join([f"{fac or 'unknown'}({c})" for fac, c in top])
            print(f"  [Diag] {stage}: {len(fc_list)} faculty conflicts. Top faculties: {top_str}")

            for fc in fc_list[:sample_size]:
                fname = getattr(fc, "faculty_name", "UNKNOWN")
                tslot = getattr(fc, "time_slot", "")
                cs = getattr(fc, "conflicting_sessions", [])
                cs_str = ""
                if isinstance(cs, (list, tuple)):
                    cs_str = " & ".join([str(x) for x in cs if x is not None])
                print(f"    - {fname}: {cs_str} at {tslot}")
    
        # First detect conflicts
        faculty_conflicts, conflict_report = run_phase6_faculty_conflicts(all_sessions)
        
        from modules_v2.phase5_core_courses import detect_and_resolve_section_overlaps
        from collections import defaultdict
    
        # If conflicts exist, resolve them using the central resolver
        if faculty_conflicts and len(faculty_conflicts) > 0:
            print(f"\n=== RESOLVING FACULTY CONFLICTS (Central Resolver) ===")
        
        # Build occupied_slots for the resolver
        occupied_slots = defaultdict(list)
        for session_val in all_sessions:
            if isinstance(session_val, dict):
                sections_val = session_val.get('sections', [])
                period_val = normalize_period(session_val.get('period', 'PRE') or 'PRE')
                block_val = session_val.get('time_block')
                course_code_val = session_val.get('course_code', '')
                if block_val and sections_val:
                    for section_val_inner in sections_val:
                        section_key_val = f"{section_val_inner}_{period_val}"
                        occupied_slots[section_key_val].append((block_val, course_code_val))
            elif hasattr(session_val, 'section') and hasattr(session_val, 'block'):
                p_obj = normalize_period(getattr(session_val, 'period', 'PRE') or 'PRE')
                section_key_val = f"{session_val.section}_{p_obj}"
                occupied_slots[section_key_val].append((session_val.block, session_val.course_code))

        # Use central resolver (handles all session types, respects move priorities)
        all_sessions, remaining_conflicts = resolve_all_faculty_conflicts(
            all_sessions, classrooms, occupied_slots, max_passes=24
        )

        # Update faculty_conflicts with remaining conflicts
        faculty_conflicts = remaining_conflicts
        if faculty_conflicts and len(faculty_conflicts) > 0:
            print(f"WARNING: {len(faculty_conflicts)} conflicts remain after resolution")
            print("  These may require manual review or indicate scheduling constraints")
        else:
            print("[OK] All faculty conflicts resolved successfully")
        _print_faculty_conflict_diagnostics(faculty_conflicts, "After Step 6")

        # Always resolve overlaps within the same section+period
        occupied_slots_overlap = defaultdict(list)
        for session_overlap in all_sessions:
            if isinstance(session_overlap, dict):
                sections_overlap = session_overlap.get('sections', [])
                period_overlap = normalize_period(session_overlap.get('period', 'PRE') or 'PRE')
                block_overlap = session_overlap.get('time_block')
                course_code_overlap = session_overlap.get('course_code', '')
                if block_overlap and sections_overlap:
                    for section_overlap_inner in sections_overlap:
                        section_key_overlap = f"{section_overlap_inner}_{period_overlap}"
                        occupied_slots_overlap[section_key_overlap].append((block_overlap, course_code_overlap))
            elif hasattr(session_overlap, 'section') and hasattr(session_overlap, 'block'):
                p_ov = normalize_period(getattr(session_overlap, 'period', 'PRE') or 'PRE')
                section_key_overlap = f"{session_overlap.section}_{p_ov}"
                occupied_slots_overlap[section_key_overlap].append((session_overlap.block, session_overlap.course_code))
        all_sessions = detect_and_resolve_section_overlaps(all_sessions, occupied_slots_overlap, classrooms)
    
        # Step 5.5: Validate one-session-per-day rules
        print("\nStep 5.5: Validating one-session-per-day rules...")
        from utils.session_rules_validator import validate_one_session_per_day
        from utils.data_models import ScheduledSession
        
        # Convert combined_sessions to ScheduledSession objects for validation
        validation_sessions = []
        for session_val_check in all_sessions:
            if isinstance(session_val_check, dict):
                # Convert dict to ScheduledSession
                session_obj_check = ScheduledSession(
                    course_code=session_val_check.get('course_code', '').split('-')[0],
                    section=session_val_check.get('sections', [None])[0] if session_val_check.get('sections') else None,
                    kind=session_val_check.get('session_type', 'L'),
                    block=session_val_check.get('time_block'),
                    room=session_val_check.get('room'),
                    period=normalize_period(session_val_check.get('period', 'PRE') or 'PRE'),
                    faculty=session_val_check.get('instructor', 'TBD')
                )
                validation_sessions.append(session_obj_check)
            else:
                validation_sessions.append(session_val_check)
        
        is_valid_check, error_messages_check = validate_one_session_per_day(validation_sessions)
        if is_valid_check:
            print("[OK] One-session-per-day rule: PASSED")
        else:
            print(f"⚠️  One-session-per-day rule: {len(error_messages_check)} violations found")
            for msg_check in error_messages_check[:10]:  # Show first 10 violations
                print(f"  - {msg_check}")
            if len(error_messages_check) > 10:
                print(f"  ... and {len(error_messages_check) - 10} more violations")
    
        # Step 6: Verify no overlaps between electives and other courses
        print("\nStep 6: Verifying no overlaps between electives and other courses...")
        
        # Get elective time slots
        from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
        
        elective_conflicts = []
        # Helper to extract semester from group key
        def extract_semester_from_group_local(gk: str) -> int:
            try:
                if '.' in str(gk):
                    return int(str(gk).split('.')[0])
                else:
                    return int(gk)
            except (ValueError, AttributeError):
                return -1
        
        for sem_val in unique_semesters:
            # Find all groups for this semester
            matching_groups_local = [gk for gk in ELECTIVE_BASKET_SLOTS.keys() 
                                    if extract_semester_from_group_local(gk) == sem_val]
            
            if not matching_groups_local:
                continue
            
            # Check conflicts for all groups in this semester
            elective_blocks_local = []
            for group_key_local in matching_groups_local:
                elective_slots_local = ELECTIVE_BASKET_SLOTS[group_key_local]
                eb_list = [
                    elective_slots_local.get('lecture_1'),
                    elective_slots_local.get('lecture_2'),
                    elective_slots_local.get('tutorial')
                ]
                elective_blocks_local.extend([b for b in eb_list if b is not None])
            
            # Check all other sessions
            all_other_sessions_local = combined_sessions + phase5_sessions + phase7_sessions
            
            for eb_local in elective_blocks_local:
                for session_local in all_other_sessions_local:
                    s_block = None
                    s_semester = None
                    
                    if isinstance(session_local, dict):
                        s_block = session_local.get('time_block')
                        course_obj_local = session_local.get('course_obj')
                        if course_obj_local:
                            s_semester = getattr(course_obj_local, 'semester', None)
                    elif hasattr(session_local, 'block'):
                        s_block = session_local.block
                        if hasattr(session_local, 'section'):
                            if f"-Sem{sem_val}" in session_local.section:
                                s_semester = sem_val
                    
                    if s_block and s_semester == sem_val:
                        if eb_local.day == s_block.day and eb_local.overlaps(s_block):
                            c_code = session_local.get('course_code', '') if isinstance(session_local, dict) else getattr(session_local, 'course_code', 'Unknown')
                            elective_conflicts.append({
                                'semester': sem_val,
                                'elective_time': f"{eb_local.day} {eb_local.start}-{eb_local.end}",
                                'conflicting_course': c_code,
                                'conflicting_time': f"{s_block.day} {s_block.start}-{s_block.end}"
                            })
        
        if elective_conflicts:
            print(f"  ERROR: Found {len(elective_conflicts)} conflicts with elective time slots:")
        else:
            print("  [OK] No conflicts found between electives and other courses")

        # Step 6.25: Verify no overlaps within the SAME section+period
        print("\nStep 6.25: Verifying no overlaps within each section/period...")
        per_section_conflicts = []
        
        # Use simple local by_section_period
        by_sec_per_local = defaultdict(list)
        for s_val in validation_sessions:
            if not getattr(s_val, "section", None) or not getattr(s_val, "block", None):
                continue
            by_sec_per_local[(str(s_val.section), str(getattr(s_val, "period", "PRE")))].append(s_val)

        for (sec_val, per_val), sess_list_val in by_sec_per_local.items():
            by_day_val = defaultdict(list)
            for s_val in sess_list_val:
                by_day_val[s_val.block.day].append(s_val)
            for day_val, day_sess_val in by_day_val.items():
                day_sess_val.sort(key=lambda x: (x.block.start.hour, x.block.start.minute))
                for i_val in range(len(day_sess_val)):
                    for j_val in range(i_val + 1, len(day_sess_val)):
                        if day_sess_val[i_val].block.overlaps(day_sess_val[j_val].block):
                            per_section_conflicts.append(f"{sec_val} {per_val} {day_val}: overlap detected")

        if per_section_conflicts:
            print(f"  ERROR: Found {len(per_section_conflicts)} per-section time overlaps.")
        else:
            print("  [OK] No per-section overlaps detected")
    
        # The new time slot allocation should prevent conflicts:
        # - Semester 1 combined courses: Tuesday/Thursday 14:00-16:45 (afternoon)
        # - Semester 3 combined courses: Monday/Wednesday/Friday mornings (09:00-10:30)
        # - Electives: Phase 3 allocates from elective groups in course data (any semester)
    
        # Step 6.75: Final classroom conflict check
        print("\nStep 6.75: Final classroom conflict check after all rescheduling...")
        try:
            from modules_v2.phase8_classroom_assignment import detect_room_conflicts
            from utils.room_conflict_resolver import resolve_room_conflicts
            
            r_conflicts_final = detect_room_conflicts(
                phase5_sessions, phase7_sessions, combined_sessions,
                locals().get("elective_sessions_with_rooms", None), classrooms
            )
            if r_conflicts_final:
                print(f"  Found {len(r_conflicts_final)} room conflict(s); resolving...")
                _, r_conflicts_final = resolve_room_conflicts(
                    phase5_sessions, phase7_sessions, combined_sessions,
                    locals().get("elective_sessions_with_rooms", None), classrooms, courses=courses, sections=sections,
                    max_passes=1
                )
            if r_conflicts_final:
                print(f"  WARNING: {len(r_conflicts_final)} room conflicts remain.")
            else:
                print("  [OK] 0 classroom conflicts after final pass.")
        except Exception as e_room_final:
            print(f"WARNING: Final classroom conflict check failed: {e_room_final}")
            import traceback
            traceback.print_exc()

        # Step 6.8: Faculty repair after section overlap / room moves (they can recreate double-booking)
        import random
        from config.schedule_config import (
            GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES,
            GENERATION_REPAIR_SHUFFLE_SEED,
        )
        from utils.generation_verify_bridge import rebuild_occupied_slots_from_all_sessions
        from modules_v2.phase6_faculty_conflicts import detect_faculty_conflicts

        print("\nStep 6.8: Post-mover faculty repair (shuffled conflict order)...")
        all_sessions = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions
        for outer in range(GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES):
            fc_now = len(detect_faculty_conflicts(all_sessions))
            if fc_now == 0:
                print(f"  [OK] No faculty conflicts after outer repair pass {outer}.")
                break
            seed = GENERATION_REPAIR_SHUFFLE_SEED + outer * 10007
            random.seed(seed)
            rng = random.Random(seed)
            rng.shuffle(all_sessions)
            occupied_repair = rebuild_occupied_slots_from_all_sessions(all_sessions)
            all_sessions, rem_fc = resolve_all_faculty_conflicts(
                all_sessions,
                classrooms,
                occupied_repair,
                max_passes=12 + outer * 2,
                rng=rng,
            )
            print(
                f"  Outer {outer + 1}/{GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES}: "
                f"resolver left {len(rem_fc)} faculty conflict(s); re-running section-overlap pass."
            )
            occupied_ov = rebuild_occupied_slots_from_all_sessions(all_sessions)
            all_sessions = detect_and_resolve_section_overlaps(
                all_sessions, occupied_ov, classrooms
            )

        fc_final = detect_faculty_conflicts(all_sessions)
        if fc_final:
            print(
                f"  WARNING: {len(fc_final)} faculty conflict(s) remain after Step 6.8 "
                f"(strict verify may fail)."
            )
        _print_faculty_conflict_diagnostics(fc_final, "After Step 6.8")

    # Canonical faculty/room from pipeline (Phase 6) for CSV/UI verify alignment
    faculty_room_lookup = {}
    if sessions_from_log is None:
        try:
            faculty_room_lookup = _build_faculty_room_lookup_from_pipeline_sessions(all_sessions)
        except Exception as ex_lk:
            print(f"WARNING: _build_faculty_room_lookup_from_pipeline_sessions failed: {ex_lk}")

        # Step 6.9: One more resolver pass before Excel (writer + strict verify see this state)
        import random
        from utils.generation_verify_bridge import rebuild_occupied_slots_from_all_sessions

        print("\nStep 6.9: Pre-export faculty resolution (two long passes, no list shuffle)...")
        rem_69 = []
        faculty_pass_budget_69 = _scaled_budget(
            max(
                8,
                (len(all_sessions or []) // 20) + (len(sections or []) // 2)
            ),
            minimum=6,
        )
        for round69 in range(2):
            occ_69 = rebuild_occupied_slots_from_all_sessions(all_sessions)
            rng_69 = random.Random(91011 + round69 * 7919)
            all_sessions, rem_69 = resolve_all_faculty_conflicts(
                all_sessions, classrooms, occ_69, max_passes=faculty_pass_budget_69, rng=rng_69
            )
            print(f"  round {round69 + 1}: {len(rem_69)} faculty conflict(s)")
            if not rem_69:
                print("  [OK] No faculty conflicts before Step 7.")
                break
        if rem_69:
            print(f"  WARNING: {len(rem_69)} faculty conflict(s) remain after Step 6.9")
        _print_faculty_conflict_diagnostics(rem_69, "After Step 6.9")
        try:
            faculty_room_lookup = _build_faculty_room_lookup_from_pipeline_sessions(all_sessions)
        except Exception as ex_lk2:
            print(f"WARNING: faculty_room_lookup rebuild after Step 6.9 failed: {ex_lk2}")

        # Step 6.95: Terminal micro-pass to sync room/section/faculty state before UI row build.
        # This keeps writer output aligned with strict-verify expectations.
        try:
            from modules_v2.phase5_core_courses import detect_and_resolve_section_overlaps
            from utils.room_conflict_resolver import resolve_room_conflicts

            print("\nStep 6.95: Terminal micro-pass (room + section + faculty sync)...")
            course_credit_map = {}
            for c in courses or []:
                cc = str(getattr(c, "code", "") or "").strip().upper()
                if not cc:
                    continue
                try:
                    course_credit_map[cc] = int(getattr(c, "credits", 0) or 0)
                except Exception:
                    course_credit_map[cc] = 0

            phase5_sessions_micro = []
            phase7_sessions_micro = []
            combined_sessions_micro = []
            elective_sessions_micro = []
            for s in all_sessions or []:
                if isinstance(s, dict):
                    cc = str(s.get("course_code", "") or "")
                    base = cc.split("-")[0].strip().upper()
                    if base.startswith("ELECTIVE_BASKET_"):
                        elective_sessions_micro.append(s)
                    else:
                        combined_sessions_micro.append(s)
                    continue

                cc = str(getattr(s, "course_code", "") or "")
                base = cc.split("-")[0].strip().upper()
                if base.startswith("ELECTIVE_BASKET_"):
                    elective_sessions_micro.append(s)
                    continue
                cr = int(course_credit_map.get(base, 0) or 0)
                if cr <= 2:
                    phase7_sessions_micro.append(s)
                else:
                    phase5_sessions_micro.append(s)

            _, rem_room_micro = resolve_room_conflicts(
                phase5_sessions_micro,
                phase7_sessions_micro,
                combined_sessions_micro,
                elective_sessions_micro,
                classrooms,
                courses=courses,
                sections=sections,
                max_passes=_scaled_budget(max(1, len(classrooms or []) // 12), minimum=1),
            )
            occ_micro = rebuild_occupied_slots_from_all_sessions(all_sessions)
            all_sessions = detect_and_resolve_section_overlaps(all_sessions, occ_micro, classrooms, max_passes=2)
            occ_micro = rebuild_occupied_slots_from_all_sessions(all_sessions)
            all_sessions, rem_fac_micro = resolve_all_faculty_conflicts(
                all_sessions,
                classrooms,
                occ_micro,
                max_passes=_scaled_budget(max(6, len(all_sessions or []) // 30), minimum=4),
                rng=random.Random(44051),
            )
            print(
                f"  Micro-pass residuals -> room: {len(rem_room_micro)}, faculty: {len(rem_fac_micro)}"
            )
            faculty_room_lookup = _build_faculty_room_lookup_from_pipeline_sessions(all_sessions)
        except Exception as ex_micro:
            print(f"WARNING: Step 6.95 micro-pass failed: {ex_micro}")

    # Ensure all_sessions is populated if we didn't go through the else block above
    if sessions_from_log is not None:
        all_sessions = sessions_from_log

    # Step 5.6: Run Phase 10 - Course Color Assignment
    print("\n" + "="*80)
    print("Step 5.6: Running Phase 10 - Course Color Assignment...")
    print("="*80)
    try:
        from modules_v2.phase10_course_colors import run_phase10
        course_colors = run_phase10(courses)
    except Exception as e:
        print(f"WARNING: Phase 10 failed. Continuing without course colors. Reason: {e}")
        course_colors = {}

    print("\nStep 7: Creating 24 sheets with all sessions...")
    
    # Define sections, semesters, and periods
    section_names = []
    for dept in DEPARTMENTS:
        for sec_label in SECTIONS_BY_DEPT.get(dept, []):
            section_names.append(f"{dept}-{sec_label}")
    semesters = unique_semesters  # Use dynamically extracted semesters
    periods = ["PreMid", "PostMid"]

    from config.schedule_config import (
        GENERATION_STRICT_MACRO_MAX_ATTEMPTS,
        GENERATION_MAX_RUNTIME_SECONDS,
    )
    from utils.generation_verify_bridge import (
        GenerationViolationError,
        final_ui_rows_to_verify_sessions,
        macro_repair_pipeline_sessions,
        run_strict_verification_on_final_ui,
    )

    def _guard_runtime_or_raise(stage_label: str) -> None:
        elapsed_s = (datetime.now() - _run_started_at).total_seconds()
        if elapsed_s <= float(GENERATION_MAX_RUNTIME_SECONDS):
            return
        raise GenerationViolationError(
            f"Generation exceeded runtime budget ({int(elapsed_s)}s > {GENERATION_MAX_RUNTIME_SECONDS}s) at {stage_label}",
            [
                {
                    "rule": "RUNTIME_TIMEOUT",
                    "message": (
                        f"Generation exceeded runtime budget ({int(elapsed_s)}s > "
                        f"{GENERATION_MAX_RUNTIME_SECONDS}s) at {stage_label}"
                    ),
                }
            ],
            debug_faculty_path=None,
        )

    if sessions_from_log is None:
        _macro_max = _scaled_budget(
            GENERATION_STRICT_MACRO_MAX_ATTEMPTS,
            minimum=2 if GENERATION_RUNTIME_MODE == "fast" else 3,
        )
    else:
        _macro_max = 1

    for _macro_i in range(_macro_max):
        _guard_runtime_or_raise(f"macro_iteration_{_macro_i + 1}")
        if _macro_i > 0:
            if not _seed_raw:
                # Seed sweep across macro retries: deterministic per retry, without requiring a fixed env seed.
                _retry_seed = 1729 + (_macro_i * 9973)
                random.seed(_retry_seed)
                print(f"  [macro-seed] retry {_macro_i + 1}: random.seed({_retry_seed})")
            print(
                f"\nStep 7 — macro retry {_macro_i + 1}/{_macro_max}: "
                "reshuffling pipeline sessions, then rebuilding 24 sheets..."
            )
            macro_repair_pipeline_sessions(all_sessions, classrooms, _macro_i)
            faculty_room_lookup = {}
            if sessions_from_log is None:
                try:
                    faculty_room_lookup = _build_faculty_room_lookup_from_pipeline_sessions(all_sessions)
                except Exception as ex_lk:
                    print(f"WARNING: _build_faculty_room_lookup_from_pipeline_sessions failed: {ex_lk}")
        elif sessions_from_log is None:
            print(
                f"  [runtime-mode] mode={GENERATION_RUNTIME_MODE} "
                f"scale={GENERATION_RUNTIME_SCALE:.2f} macro_budget={_macro_max}"
            )

        # Create writer
        writer = TimetableWriterV2(course_colors=course_colors)

        final_ui_sessions = []

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
                            sessions_from_log=sessions_from_log
                        )
                        for t in deferred:
                            target_day = t[0].day
                            if target_day not in deferred_by_day:
                                deferred_by_day[target_day] = []
                            deferred_by_day[target_day].append(t)
                        grid_sessions_dict[day] = schedule_grid.sessions
                        for s in schedule_grid.sessions:
                            accumulated_final_sessions.append(s)  # (block, course) each
                            
                            # Add to our final_ui_sessions list for re-logging
                            block = s[0]
                            course_display = s[1]
                            
                            meta_fac = ""
                            meta_rm = ""
                            if len(s) > 4 and isinstance(s[4], dict):
                                meta_fac = s[4].get('faculty', '')
                                meta_rm = s[4].get('room', '')
                                
                            if course_display != "LUNCH" and course_display != "Break(15min)":
                                section_key = f"{section_name}-Sem{semester}"
                                stype = 'L'
                                if '-TUT' in course_display: stype = 'T'
                                elif '-LAB' in course_display: stype = 'P'
                                if course_display.startswith("ELECTIVE_BASKET_"): stype = 'ELECTIVE'
                                
                                base_code = course_display.replace('-TUT', '').replace('-LAB', '').split('-')[0]
    
                                # Prefer Phase-6-resolved faculty/room so CSV matches deep_verification
                                if sessions_from_log is None and faculty_room_lookup:
                                    lf, lr = _lookup_faculty_room_from_pipeline_map(
                                        faculty_room_lookup, section_key, course_display, day, block, period, stype
                                    )
                                    # Always prefer latest Phase-6-resolved values when present.
                                    if lf:
                                        meta_fac = lf
                                    if lr:
                                        meta_rm = lr
                                
                                comb_sess = None
                                # Determine room
                                comb_sess = None
                                # Combined (Phase 4) rooms are authoritative; keep C004 strictly for Phase 4.
                                # Match by base course code + session type (L/T/P) since combined dicts may store
                                # course_code without -TUT/-LAB suffix.
                                comb_sess = next(
                                    (
                                        cs
                                        for cs in combined_sessions
                                        if isinstance(cs, dict)
                                        and str(cs.get("course_code", "") or "").split("-")[0].strip() == base_code
                                        and normalize_period(cs.get("period")) == normalize_period(period)
                                        and str(cs.get("session_type", cs.get("kind", "L")) or "L").strip().upper() == stype
                                        and any(str(sec).startswith(section_key) for sec in cs.get("sections", []))
                                    ),
                                    None,
                                )
                                if comb_sess and comb_sess.get("room"):
                                    meta_rm = comb_sess["room"]

                                # For practicals, prefer the session's own room (Phase 8 may assign per-session labs).
                                if sessions_from_log is None and stype == "P" and getattr(block, "start", None) is not None:
                                    try:
                                        _pkey = normalize_period(period)
                                        _match = next(
                                            (
                                                ss for ss in (phase5_sessions + phase7_sessions)
                                                if getattr(ss, "section", "") == section_key
                                                and (getattr(ss, "course_code", "").split("-")[0] == base_code)
                                                and normalize_period(getattr(ss, "period", "PRE")) == _pkey
                                                and getattr(getattr(ss, "block", None), "day", None) == day
                                                and getattr(getattr(ss, "block", None), "start", None) == block.start
                                                and getattr(getattr(ss, "block", None), "end", None) == block.end
                                            ),
                                            None,
                                        )
                                        if _match and getattr(_match, "room", None):
                                            meta_rm = str(_match.room).strip()
                                    except Exception:
                                        pass

                                # For lectures/tutorials, prefer the actual session object's room
                                # (which was updated by resolve_room_conflicts) then fall back to room_assignments.
                                if sessions_from_log is None and stype != "P" and not comb_sess:
                                    pkey = normalize_period(period)
                                    # First: look for a matching ScheduledSession with an up-to-date .room
                                    _direct_match = next(
                                        (
                                            ss for ss in (phase5_sessions + phase7_sessions)
                                            if getattr(ss, 'section', '') == section_key
                                            and str(getattr(ss, 'course_code', '') or '').split('-')[0] == base_code
                                            and normalize_period(getattr(ss, 'period', 'PRE')) == pkey
                                            and getattr(getattr(ss, 'block', None), 'day', None) == day
                                            and getattr(ss, 'kind', 'L') != 'P'
                                        ),
                                        None,
                                    )
                                    if _direct_match and getattr(_direct_match, 'room', None):
                                        meta_rm = _direct_match.room
                                    elif room_assignments:
                                        a = room_assignments.get((base_code, section_key, pkey))
                                        if isinstance(a, dict) and a.get("classroom"):
                                            meta_rm = a.get("classroom")

                                if not meta_rm:
                                    if comb_sess and comb_sess.get("room"):
                                        meta_rm = comb_sess["room"]
                                    elif room_assignments:
                                        # Secondary attempt (should be rare if Phase 8 ran)
                                        pkey = normalize_period(period)
                                        a = room_assignments.get((base_code, section_key, pkey))
                                        if isinstance(a, dict):
                                            if stype == "P":
                                                labs = a.get("labs") or []
                                                labs = [str(x).strip() for x in labs if str(x).strip()]
                                                if labs:
                                                    meta_rm = ", ".join(labs)
                                            else:
                                                meta_rm = a.get("classroom") or ""
                                    
                                # Determine faculty.
                                # Prefer the exact course row for this section's program + semester
                                # (same code can exist across departments with different instructors).
                                sec_prog = ""
                                try:
                                    sec_prog = str(section_key).split("-")[0].strip()
                                except Exception:
                                    sec_prog = ""
                                course_obj = next((
                                    c for c in courses
                                    if getattr(c, 'code', '') == base_code
                                    and getattr(c, 'semester', None) == semester
                                    and (not sec_prog or getattr(c, 'department', '') == sec_prog)
                                ), None)
                                if course_obj is None:
                                    course_obj = next((
                                        c for c in courses
                                        if getattr(c, 'code', '') == base_code
                                        and getattr(c, 'semester', None) == semester
                                    ), None)
                                if course_obj is None:
                                    course_obj = next((c for c in courses if getattr(c, 'code', '') == base_code), None)
                                
                                sec_idx = 0
                                try:
                                    sec_idx = next((i for i, s in enumerate(sections) if s.label == section_key), 0)
                                except:
                                    pass
                                    
                                if not meta_fac:
                                    if comb_sess and comb_sess.get('instructor'):
                                        meta_fac = comb_sess['instructor']
                                    elif sessions_from_log is None:
                                        # Only use sec_idx-based fallback when NOT in log-replay mode
                                        if course_obj and hasattr(course_obj, 'instructors') and course_obj.instructors:
                                            if sec_idx < len(course_obj.instructors):
                                                meta_fac = course_obj.instructors[sec_idx]
                                            else:
                                                meta_fac = course_obj.instructors[0]
                                                
                                # If generating from log (UI edits), preserve original Room and Faculty.
                                # The create_integrated_schedule fast path already set meta_fac/meta_rm from
                                # the original CSV row for this section.  Only do a secondary lookup if the
                                # meta dict didn't carry a value (very rare edge-case).
                                if sessions_from_log is not None and (not meta_fac or not meta_rm):
                                    orig_s = next((os_item for os_item in sessions_from_log if 
                                        match_section(section_key, os_item.get('Section') or os_item.get('section')) and 
                                        (os_item.get('Course Code') == course_display or os_item.get('course_code') == course_display) and 
                                        str(os_item.get('Day') or os_item.get('day')).strip().upper() == str(day).strip().upper() and 
                                        str(os_item.get('Start Time') or os_item.get('start_time')).replace(':', '') == block.start.strftime("%H%M")
                                    ), None)
                                    if orig_s:
                                        if not meta_rm:
                                            meta_rm = orig_s.get('Room') or orig_s.get('room') or meta_rm
                                        if not meta_fac:
                                            # Use faculty value directly — no comma-splitting needed since
                                            # the log stores one faculty per section row.
                                            meta_fac = orig_s.get('Faculty') or orig_s.get('faculty') or meta_fac

                                # Policy: practical sessions do not participate in faculty conflicts.
                                # Keep faculty blank for P rows to align strict verification semantics.
                                if stype == "P":
                                    meta_fac = ""
    
    
                                final_ui_sessions.append({
                                    # Phase tagging is required so strict verification only merges sections
                                    # for Phase 4 combined classes (C004). Phase 5/7 sessions remain per-section.
                                    'phase': (
                                        "Phase 3" if course_display.startswith("ELECTIVE_BASKET_") else
                                        ("Phase 4" if comb_sess else
                                         ("Phase 7" if any(
                                             getattr(ss, "section", "") == section_key
                                             and (getattr(ss, "course_code", "").split("-")[0] == base_code)
                                             and normalize_period(getattr(ss, "period", "PRE")) == normalize_period(period)
                                             and getattr(getattr(ss, "block", None), "day", None) == day
                                             and getattr(getattr(ss, "block", None), "start", None) == block.start
                                             and getattr(getattr(ss, "block", None), "end", None) == block.end
                                             for ss in (phase7_sessions or [])
                                         ) else "Phase 5"))
                                    ),
                                    'course_code': course_display,
                                    'section': section_key,
                                    'day': day,
                                    'start_time': block.start,
                                    'end_time': block.end,
                                    'time_block': block,  # Required by faculty/classroom writers
                                    'period': normalize_period(period),
                                    'session_type': stype,
                                    'faculty': meta_fac,
                                    'room': meta_rm
                                })
    
                        current_row = writer.write_day_schedule(sheet, day, schedule_grid, current_row)
                        current_row += 1
                    print("[OK] Done")
                    
                    # Add verification table after all days - pass grid_sessions to count only displayed sessions
                    print(f"    Adding verification table...", end=" ", flush=True)
                    current_row += 2  # Add spacing
                    
                    # If generated from log, extract ALL sessions (both PreMid/PostMid) 
                    # so full-semester requirements are verified correctly
                    all_section_log_sessions = None
                    if sessions_from_log is not None:
                        expected_sec = f"{section_name}-Sem{semester}"
                        all_section_log_sessions = [
                            s for s in sessions_from_log 
                            if match_section(expected_sec, s.get('Section') or s.get('section', ''))
                        ]
    
                    current_row = writer.write_verification_table(
                        sheet, current_row, courses, 
                        combined_sessions + elective_sessions,  # Still pass for reference, but grid_sessions takes precedence
                        semester, section_name, period, phase5_sessions,
                        phase7_sessions, combined_sessions, faculty_conflicts,
                        room_assignments,  # Pass room assignments from Phase 8
                        grid_sessions=grid_sessions_dict,  # Pass actual displayed sessions from grid
                        all_section_sessions=all_section_log_sessions,
                        classrooms=classrooms
                    )
                    print("[OK] Done")
                    
                    # Add elective assignment table below verification table
                    print(f"    Adding elective assignment table...", end=" ", flush=True)
                    current_row += 2  # Add spacing
                    current_row = writer.write_elective_assignment_table(
                        sheet, current_row, semester, courses, 
                        elective_assignments.get(semester, []),
                        all_section_sessions=all_section_log_sessions
                    )
                    print("[OK] Done")
    
        # Strict gate: zero-tolerance verification before writing workbook/CSV.
        if final_ui_sessions:
            # Repair pass: eliminate room double-bookings + under-capacity rooms
            # (C004 reserved for Phase 4 only)
            try:
                def _t_overlap(a_s, a_e, b_s, b_e):
                    return not (a_e <= b_s or b_e <= a_s)

                def _np(p):
                    return normalize_period(p)

                section_students = {getattr(sec, "label", ""): int(getattr(sec, "students", 0) or 0) for sec in (sections or [])}
                room_caps = {getattr(r, "room_number", ""): int(getattr(r, "capacity", 0) or 0) for r in (classrooms or [])}
                lab_rooms = [
                    r
                    for r in (classrooms or [])
                    if "lab" in str(getattr(r, "room_type", "")).lower()
                    and getattr(r, "room_number", "") != "C004"
                    and not getattr(r, "is_research_lab", False)
                ]
                # Last-resort fallback (only if needed): include research labs so strict verify can reach 0.
                lab_rooms_fallback = [
                    r
                    for r in (classrooms or [])
                    if "lab" in str(getattr(r, "room_type", "")).lower()
                    and getattr(r, "room_number", "") != "C004"
                ]
                class_rooms = [r for r in (classrooms or []) if "lab" not in str(getattr(r, "room_type", "")).lower() and getattr(r, "room_number", "") != "C004"]

                # Prefer smaller rooms first
                lab_rooms.sort(key=lambda r: int(getattr(r, "capacity", 0) or 0))
                lab_rooms_fallback.sort(key=lambda r: int(getattr(r, "capacity", 0) or 0))
                class_rooms.sort(key=lambda r: int(getattr(r, "capacity", 0) or 0))

                _day_order = {d: i for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}

                def _dur_minutes(st_, et_):
                    try:
                        return int((et_.hour * 60 + et_.minute) - (st_.hour * 60 + st_.minute))
                    except Exception:
                        try:
                            ss = str(st_)[:5]
                            ee = str(et_)[:5]
                            sm = int(ss.split(":")[0]) * 60 + int(ss.split(":")[1])
                            em = int(ee.split(":")[0]) * 60 + int(ee.split(":")[1])
                            return em - sm
                        except Exception:
                            return 0

                def _base_code_of(display_code: str) -> str:
                    return str(display_code or "").replace("-TUT", "").replace("-LAB", "").split("-")[0].strip().upper()

                def _is_unassigned_room_value(rv: object) -> bool:
                    s = str(rv or "").strip().lower()
                    return s in ("", "na", "none", "nan", "tbd")

                def _parse_strict_tasks(errors: List[Dict]) -> Dict[str, List[Dict]]:
                    tasks = {
                        "room_conflicts": [],
                        "ltpsc": [],
                        "time_constraints": [],
                        "faculty_conflicts": [],
                        "section_overlaps": [],
                    }
                    for e in errors or []:
                        rule = str(e.get("rule", "") or "").strip().lower()
                        msg = str(e.get("message", "") or "")
                        if "room" in rule and "double-booked" in msg:
                            m = re.search(r"Room\s+(\S+)\s+is double-booked in\s+(\w+):\s+(.+)\s+and\s+(.+)", msg)
                            if m:
                                tasks["room_conflicts"].append({
                                    "room": m.group(1).strip(),
                                    "period": normalize_period(m.group(2).strip()),
                                    "course_a": _base_code_of(m.group(3)),
                                    "course_b": _base_code_of(m.group(4)),
                                })
                        if "ltpsc" in rule and "mismatch" in msg:
                            m = re.search(
                                r"LTPSC mismatch for\s+([A-Z0-9/]+)\s+in\s+([A-Za-z0-9\-]+):\s+expected L/T/P=(\d+)/(\d+)/(\d+),\s+scheduled\s+(\d+)/(\d+)/(\d+)",
                                msg,
                            )
                            if m:
                                tasks["ltpsc"].append({
                                    "course_code": _base_code_of(m.group(1)),
                                    "section": m.group(2).strip(),
                                    "expected": {"L": int(m.group(3)), "T": int(m.group(4)), "P": int(m.group(5))},
                                    "scheduled": {"L": int(m.group(6)), "T": int(m.group(7)), "P": int(m.group(8))},
                                })
                        if "time constraints" in rule and "overlaps lunch break" in msg:
                            m_tc = re.search(
                                r"Session overlaps lunch break:\s+(\w+)\s+([0-9:]+)-([0-9:]+)",
                                msg,
                            )
                            if m_tc:
                                tasks["time_constraints"].append({
                                    "message": msg,
                                    "day": str(m_tc.group(1)).strip(),
                                    "start": str(m_tc.group(2)).strip(),
                                    "end": str(m_tc.group(3)).strip(),
                                })
                            else:
                                tasks["time_constraints"].append({"message": msg})
                        if "section overlap" in rule and "same section has two sessions at same time" in msg:
                            m = re.search(
                                r"Same section has two sessions at same time:\s+([A-Z0-9/\\-]+)\s+and\s+([A-Z0-9/\\-]+)",
                                msg,
                            )
                            if m:
                                tasks["section_overlaps"].append({
                                    "course_a": _base_code_of(m.group(1)),
                                    "course_b": _base_code_of(m.group(2)),
                                })
                        if "faculty" in rule and ("overlap" in msg or "overlapping sessions" in msg):
                            m1 = re.search(
                                r"Faculty\s+(.+?)\s+has overlapping sessions:\s+([A-Z0-9/]+)\s+and\s+([A-Z0-9/]+)\s+on\s+(\w+)\s+([0-9:]+)-([0-9:]+)",
                                msg,
                            )
                            m2 = re.search(
                                r"Faculty\s+(.+?)\s+overlap:\s+([A-Z0-9/]+)\s+\((\w+)\)\s+vs\s+([A-Z0-9/]+)\s+\((\w+)\)\s+on\s+(\w+)\s+([0-9:]+)-([0-9:]+)",
                                msg,
                            )
                            if m1:
                                tasks["faculty_conflicts"].append({
                                    "faculty": str(m1.group(1)).strip(),
                                    "course_a": _base_code_of(m1.group(2)),
                                    "course_b": _base_code_of(m1.group(3)),
                                    "section_a": "",
                                    "section_b": "",
                                    "day": str(m1.group(4)).strip(),
                                    "start": str(m1.group(5)).strip(),
                                    "end": str(m1.group(6)).strip(),
                                    "period": "",
                                })
                            elif m2:
                                tasks["faculty_conflicts"].append({
                                    "faculty": str(m2.group(1)).strip(),
                                    "course_a": _base_code_of(m2.group(2)),
                                    "course_b": _base_code_of(m2.group(4)),
                                    "section_a": "",
                                    "section_b": "",
                                    "day": str(m2.group(6)).strip(),
                                    "start": str(m2.group(7)).strip(),
                                    "end": str(m2.group(8)).strip(),
                                    "period": normalize_period(m2.group(3).strip()),
                                })
                            else:
                                m3 = re.search(
                                    r"Faculty\s+(.+?)\s+cannot teach\s+([A-Z0-9/]+).*parallel sections\s+([A-Za-z0-9\-]+)\s+and\s+([A-Za-z0-9\-]+)\s+at overlapping times on\s+(\w+)\s+([0-9:]+)-([0-9:]+)",
                                    msg,
                                )
                                if m3:
                                    tasks["faculty_conflicts"].append({
                                        "faculty": str(m3.group(1)).strip(),
                                        "course_a": _base_code_of(m3.group(2)),
                                        "course_b": _base_code_of(m3.group(2)),
                                        "section_a": str(m3.group(3)).strip(),
                                        "section_b": str(m3.group(4)).strip(),
                                        "day": str(m3.group(5)).strip(),
                                        "start": str(m3.group(6)).strip(),
                                        "end": str(m3.group(7)).strip(),
                                        "period": "",
                                    })
                    return tasks

                def _parse_clock(v: str):
                    try:
                        bits = [int(x) for x in str(v or "").strip().split(":")]
                        if len(bits) >= 2:
                            return time(bits[0], bits[1])
                    except Exception:
                        return None
                    return None

                def _slot_conflicts_for_section(sec: str, period: str, day: str, st, et) -> bool:
                    for row in final_ui_sessions:
                        if str(row.get("section", "") or "").strip() != sec:
                            continue
                        if normalize_period(row.get("period")) != normalize_period(period):
                            continue
                        if str(row.get("day", "") or "").strip() != day:
                            continue
                        rs, re_ = row.get("start_time"), row.get("end_time")
                        if rs and re_ and _t_overlap(st, et, rs, re_):
                            return True
                    return False

                def _faculty_tokens(nm: str):
                    from utils.faculty_conflict_utils import faculty_name_tokens

                    return set(faculty_name_tokens(str(nm or "").strip()))

                def _faculty_slot_busy(
                    faculty_raw: str, period: str, day: str, st, et, exclude_idx: Optional[int]
                ) -> bool:
                    """True if another non-P row (final UI) uses an overlapping token for this instructor."""
                    toks = _faculty_tokens(faculty_raw)
                    if not toks:
                        return False
                    per = normalize_period(period)
                    dday = str(day or "").strip()
                    for idx, row in enumerate(final_ui_sessions):
                        if exclude_idx is not None and idx == exclude_idx:
                            continue
                        if str(row.get("session_type", "L") or "L").strip().upper() == "P":
                            continue
                        oth = str(row.get("faculty", "") or "").strip()
                        if not oth:
                            continue
                        if not (_faculty_tokens(oth) & toks):
                            continue
                        if normalize_period(row.get("period")) != per:
                            continue
                        if str(row.get("day", "") or "").strip() != dday:
                            continue
                        rs, re_ = row.get("start_time"), row.get("end_time")
                        if rs and re_ and _t_overlap(st, et, rs, re_):
                            return True
                    return False

                def _room_conflicts(room: str, period: str, day: str, st, et, exclude_idx: Optional[int] = None) -> bool:
                    room_tokens = {x.strip() for x in str(room or "").split(",") if x.strip()}
                    for idx, row in enumerate(final_ui_sessions):
                        if exclude_idx is not None and idx == exclude_idx:
                            continue
                        if normalize_period(row.get("period")) != normalize_period(period):
                            continue
                        if str(row.get("day", "") or "").strip() != day:
                            continue
                        rs, re_ = row.get("start_time"), row.get("end_time")
                        if not (rs and re_ and _t_overlap(st, et, rs, re_)):
                            continue
                        other_tokens = {x.strip() for x in str(row.get("room", "") or "").split(",") if x.strip()}
                        if room_tokens & other_tokens:
                            return True
                    return False

                def _room_candidates_for_kind(kind: str):
                    return lab_rooms_fallback if kind == "P" else class_rooms

                def _needed_capacity_for_section(sec: str) -> int:
                    base_sec = str(sec).split("-Sem")[0].strip()
                    return int(section_students.get(base_sec, 0) or section_students.get(sec, 0) or 85)

                def _pick_room_for_slot(
                    kind: str,
                    sec: str,
                    period: str,
                    day: str,
                    st,
                    et,
                    exclude_idx: Optional[int] = None,
                    faculty_raw: Optional[str] = None,
                    allow_faculty_fallback: bool = True,
                    forbidden_rooms: Optional[set] = None,
                ) -> str:
                    need = _needed_capacity_for_section(sec)
                    fac_chk = (
                        str(faculty_raw or "").strip()
                        if str(kind or "L").strip().upper() not in ("P",)
                        else ""
                    )
                    blocked_rooms = {str(x).strip() for x in (forbidden_rooms or set()) if str(x).strip()}

                    def _scan(require_faculty: bool) -> str:
                        for r in _room_candidates_for_kind(kind):
                            rn = getattr(r, "room_number", "")
                            if rn in blocked_rooms:
                                continue
                            cap = room_caps.get(rn, 0)
                            if kind in ("L", "T") and need and cap < need:
                                continue
                            if _room_conflicts(rn, period, day, st, et, exclude_idx=exclude_idx):
                                continue
                            if require_faculty and fac_chk and _faculty_slot_busy(
                                fac_chk, period, day, st, et, exclude_idx
                            ):
                                continue
                            return rn
                        for r in _room_candidates_for_kind(kind):
                            rn = getattr(r, "room_number", "")
                            if rn in blocked_rooms:
                                continue
                            if _room_conflicts(rn, period, day, st, et, exclude_idx=exclude_idx):
                                continue
                            if require_faculty and fac_chk and _faculty_slot_busy(
                                fac_chk, period, day, st, et, exclude_idx
                            ):
                                continue
                            return rn
                        return ""

                    picked = _scan(True)
                    if picked:
                        return picked
                    if fac_chk and allow_faculty_fallback:
                        return _scan(False)
                    return ""

                def _atomic_fill_blank_room(_tasks: Dict[str, List[Dict]]) -> int:
                    for idx, row in enumerate(final_ui_sessions):
                        if not _is_unassigned_room_value(row.get("room", "")):
                            continue
                        kind = str(row.get("session_type", "L") or "L").strip().upper()
                        sec = str(row.get("section", "") or "").strip()
                        per = normalize_period(row.get("period"))
                        day = str(row.get("day", "") or "").strip()
                        st, et = row.get("start_time"), row.get("end_time")
                        if not (sec and day and st and et):
                            continue
                        rn = _pick_room_for_slot(
                            kind,
                            sec,
                            per,
                            day,
                            st,
                            et,
                            exclude_idx=idx,
                            faculty_raw=str(row.get("faculty") or ""),
                        )
                        if rn:
                            row["room"] = rn
                            return 1
                    return 0

                def _atomic_fix_parsed_room_conflict(_tasks: Dict[str, List[Dict]]) -> int:
                    for t in _tasks.get("room_conflicts", []):
                        room = t["room"]
                        per = normalize_period(t["period"])
                        course_a = t["course_a"]
                        course_b = t["course_b"]
                        colliding = []
                        for idx, row in enumerate(final_ui_sessions):
                            if normalize_period(row.get("period")) != per:
                                continue
                            tokens = {x.strip() for x in str(row.get("room", "") or "").split(",") if x.strip()}
                            if room not in tokens:
                                continue
                            base = _base_code_of(row.get("course_code"))
                            if base not in (course_a, course_b):
                                continue
                            colliding.append((idx, row))
                        if len(colliding) < 2:
                            continue
                        idx_move, row_move = sorted(
                            colliding,
                            key=lambda p: (
                                _day_order.get(str(p[1].get("day", "") or ""), 99),
                                str(p[1].get("start_time") or ""),
                                str(p[1].get("course_code") or ""),
                                p[0],
                            ),
                        )[-1]
                        kind = str(row_move.get("session_type", "L") or "L").strip().upper()
                        sec = str(row_move.get("section", "") or "").strip()
                        day = str(row_move.get("day", "") or "").strip()
                        st, et = row_move.get("start_time"), row_move.get("end_time")
                        rn = _pick_room_for_slot(
                            kind,
                            sec,
                            per,
                            day,
                            st,
                            et,
                            exclude_idx=idx_move,
                            faculty_raw=str(row_move.get("faculty") or ""),
                        )
                        if rn and str(row_move.get("room", "") or "").strip() != rn:
                            row_move["room"] = rn
                            return 1
                        if kind == "P":
                            dur = _dur_minutes(st, et)
                            sem = 0
                            for sec_obj in sections or []:
                                if str(getattr(sec_obj, "label", "") or "") == sec:
                                    sem = int(getattr(sec_obj, "semester", 0) or 0)
                                    break
                            lw = LUNCH_WINDOWS.get(sem)
                            for hh in range(9, 18):
                                for mm in (0, 15, 30, 45):
                                    ns = time(hh, mm)
                                    end_m = hh * 60 + mm + dur
                                    eh, em = divmod(end_m, 60)
                                    if eh > 18 or (eh == 18 and em > 0):
                                        continue
                                    ne = time(eh, em)
                                    if lw and _t_overlap(ns, ne, lw[0], lw[1]):
                                        continue
                                    if _slot_conflicts_for_section(sec, per, day, ns, ne):
                                        continue
                                    rn2 = _pick_room_for_slot(
                                        kind,
                                        sec,
                                        per,
                                        day,
                                        ns,
                                        ne,
                                        exclude_idx=idx_move,
                                        faculty_raw=str(row_move.get("faculty") or ""),
                                    )
                                    if not rn2:
                                        continue
                                    row_move["start_time"] = ns
                                    row_move["end_time"] = ne
                                    row_move["time_block"] = TimeBlock(day, ns, ne)
                                    row_move["room"] = rn2
                                    return 1
                    return 0

                def _display_code_for_kind(base: str, kind: str) -> str:
                    if kind == "T":
                        return f"{base}-TUT"
                    if kind == "P":
                        return f"{base}-LAB"
                    return base

                def _atomic_ltpsc_trim_one(tasks: Dict[str, List[Dict]]) -> int:
                    """
                    Remove exactly one overscheduled LTPSC row when strict verify reports
                    scheduled > expected for a concrete course+section+kind.
                    """
                    kind_by_suffix = {"-TUT": "T", "-LAB": "P"}
                    for task in tasks.get("ltpsc", []):
                        base = task["course_code"]
                        sec = task["section"]
                        extra = {
                            k: max(0, int(task["scheduled"][k]) - int(task["expected"][k]))
                            for k in ("L", "T", "P")
                        }
                        if sum(extra.values()) <= 0:
                            continue

                        for kind in ("L", "T", "P"):
                            if extra[kind] <= 0:
                                continue
                            candidates = []
                            for idx, row in enumerate(final_ui_sessions):
                                if str(row.get("section", "") or "").strip() != sec:
                                    continue
                                if _base_code_of(row.get("course_code")) != base:
                                    continue
                                row_kind = str(row.get("session_type", "L") or "L").strip().upper()
                                if row_kind not in ("L", "T", "P"):
                                    cc = str(row.get("course_code", "") or "").strip().upper()
                                    row_kind = "L"
                                    for suf, k2 in kind_by_suffix.items():
                                        if cc.endswith(suf):
                                            row_kind = k2
                                            break
                                if row_kind != kind:
                                    continue
                                candidates.append((idx, row))

                            if not candidates:
                                continue

                            # Deterministic trim: remove latest day/time row first.
                            idx_drop, _ = sorted(
                                candidates,
                                key=lambda p: (
                                    _day_order.get(str(p[1].get("day", "") or ""), 99),
                                    str(p[1].get("start_time") or ""),
                                    normalize_period(p[1].get("period")),
                                    p[0],
                                ),
                            )[-1]
                            final_ui_sessions.pop(idx_drop)
                            return 1
                    return 0

                def _atomic_ltpsc_one(tasks: Dict[str, List[Dict]]) -> int:
                    sec_sem = {}
                    for s0 in sections or []:
                        sec_sem[str(getattr(s0, "label", "") or "")] = int(getattr(s0, "semester", 0) or 0)
                    for task in tasks.get("ltpsc", []):
                        base = task["course_code"]
                        sec = task["section"]
                        miss = {
                            k: max(0, int(task["expected"][k]) - int(task["scheduled"][k]))
                            for k in ("L", "T", "P")
                        }
                        if sum(miss.values()) <= 0:
                            continue

                        rows = [
                            r for r in final_ui_sessions
                            if str(r.get("section", "") or "") == sec
                            and _base_code_of(r.get("course_code")) == base
                        ]
                        per_counts = {"PRE": 0, "POST": 0}
                        for r in rows:
                            per_counts[normalize_period(r.get("period"))] += 1
                        target_period = "PRE" if per_counts["PRE"] <= per_counts["POST"] else "POST"
                        period_order = [target_period, "POST" if target_period == "PRE" else "PRE"]

                        faculty_default = ""
                        for r in rows:
                            f = str(r.get("faculty", "") or "").strip()
                            if f:
                                faculty_default = f
                                break

                        for kind in ("L", "T", "P"):
                            dur = 90 if kind == "L" else (60 if kind == "T" else 120)
                            if miss[kind] <= 0:
                                continue
                            for per0 in period_order:
                                for day in WORKING_DAYS:
                                    for hh in range(9, 18):
                                        for mm in (0, 15, 30, 45):
                                            st = time(hh, mm)
                                            end_m = hh * 60 + mm + dur
                                            eh, em = divmod(end_m, 60)
                                            if eh > 18 or (eh == 18 and em > 0):
                                                continue
                                            et = time(eh, em)
                                            sem = sec_sem.get(sec, 0)
                                            lw = LUNCH_WINDOWS.get(sem)
                                            if lw:
                                                l0, l1 = lw
                                                if _t_overlap(st, et, l0, l1):
                                                    continue
                                            if _slot_conflicts_for_section(sec, per0, day, st, et):
                                                continue
                                            rn = _pick_room_for_slot(
                                                kind,
                                                sec,
                                                per0,
                                                day,
                                                st,
                                                et,
                                                faculty_raw=faculty_default,
                                                allow_faculty_fallback=False,
                                            )
                                            if not rn:
                                                continue
                                            final_ui_sessions.append({
                                                "phase": "Phase 5",
                                                "course_code": _display_code_for_kind(base, kind),
                                                "section": sec,
                                                "day": day,
                                                "start_time": st,
                                                "end_time": et,
                                                "time_block": TimeBlock(day, st, et),
                                                "period": per0,
                                                "session_type": kind,
                                                "faculty": "" if kind == "P" else faculty_default,
                                                "room": rn,
                                            })
                                            return 1
                    return 0

                def _atomic_fix_one_lunch(_tasks: Dict[str, List[Dict]]) -> int:
                    sec_sem = {}
                    for s0 in sections or []:
                        sec_sem[str(getattr(s0, "label", "") or "")] = int(getattr(s0, "semester", 0) or 0)

                    for idx, row in enumerate(final_ui_sessions):
                        sec = str(row.get("section", "") or "")
                        sem = sec_sem.get(sec, 0)
                        lw = LUNCH_WINDOWS.get(sem)
                        if not lw:
                            continue
                        st, et = row.get("start_time"), row.get("end_time")
                        day = str(row.get("day", "") or "")
                        if not (st and et and day):
                            continue
                        l0, l1 = lw
                        if not _t_overlap(st, et, l0, l1):
                            continue
                        kind = str(row.get("session_type", "L") or "L").strip().upper()
                        per = normalize_period(row.get("period"))
                        dur = _dur_minutes(st, et)
                        for hh in range(9, 18):
                            for mm in (0, 15, 30, 45):
                                ns = time(hh, mm)
                                end_m = hh * 60 + mm + dur
                                eh, em = divmod(end_m, 60)
                                if eh > 18 or (eh == 18 and em > 0):
                                    continue
                                ne = time(eh, em)
                                if _t_overlap(ns, ne, l0, l1):
                                    continue
                                if _slot_conflicts_for_section(sec, per, day, ns, ne):
                                    continue
                                rn = _pick_room_for_slot(
                                    kind,
                                    sec,
                                    per,
                                    day,
                                    ns,
                                    ne,
                                    exclude_idx=idx,
                                    faculty_raw=str(row.get("faculty") or ""),
                                    allow_faculty_fallback=True,
                                )
                                if not rn:
                                    continue
                                row["start_time"] = ns
                                row["end_time"] = ne
                                row["time_block"] = TimeBlock(day, ns, ne)
                                row["room"] = rn
                                return 1
                    return 0

                def _atomic_fix_parsed_lunch_conflict(tasks: Dict[str, List[Dict]]) -> int:
                    for t in tasks.get("time_constraints", []):
                        day = str(t.get("day", "") or "").strip()
                        st = _parse_clock(t.get("start"))
                        et = _parse_clock(t.get("end"))
                        if not (day and st and et):
                            continue
                        for idx, row in enumerate(final_ui_sessions):
                            rday = str(row.get("day", "") or "").strip()
                            rst, ret = row.get("start_time"), row.get("end_time")
                            if not (rday and rst and ret):
                                continue
                            if rday != day or not _t_overlap(rst, ret, st, et):
                                continue
                            sec = str(row.get("section", "") or "")
                            kind = str(row.get("session_type", "L") or "L").strip().upper()
                            per = normalize_period(row.get("period"))
                            dur = _dur_minutes(rst, ret)
                            sem = 0
                            for s0 in sections or []:
                                if str(getattr(s0, "label", "") or "") == sec:
                                    sem = int(getattr(s0, "semester", 0) or 0)
                                    break
                            lw = LUNCH_WINDOWS.get(sem)
                            if not lw:
                                continue
                            for hh in range(9, 18):
                                for mm in (0, 15, 30, 45):
                                    ns = time(hh, mm)
                                    end_m = hh * 60 + mm + dur
                                    eh, em = divmod(end_m, 60)
                                    if eh > 18 or (eh == 18 and em > 0):
                                        continue
                                    ne = time(eh, em)
                                    if _t_overlap(ns, ne, lw[0], lw[1]):
                                        continue
                                    if _slot_conflicts_for_section(sec, per, day, ns, ne):
                                        continue
                                    rn = _pick_room_for_slot(
                                        kind,
                                        sec,
                                        per,
                                        day,
                                        ns,
                                        ne,
                                        exclude_idx=idx,
                                        faculty_raw=str(row.get("faculty") or ""),
                                        allow_faculty_fallback=True,
                                    )
                                    if not rn:
                                        continue
                                    row["start_time"] = ns
                                    row["end_time"] = ne
                                    row["time_block"] = TimeBlock(day, ns, ne)
                                    row["room"] = rn
                                    return 1
                    return 0

                def _force_resolve_one_room_conflict() -> int:
                    entries = []
                    for idx, row in enumerate(final_ui_sessions):
                        per = normalize_period(row.get("period"))
                        day = str(row.get("day", "") or "")
                        st, et = row.get("start_time"), row.get("end_time")
                        if not (day and st and et):
                            continue
                        kind = str(row.get("session_type", "L") or "L").strip().upper()
                        sec = str(row.get("section", "") or "").strip()
                        rooms = [x.strip() for x in str(row.get("room", "") or "").split(",") if x.strip()]
                        for rn in rooms:
                            entries.append((idx, per, day, st, et, kind, sec, rn))

                    conflicts = []
                    for i in range(len(entries)):
                        ai, ap, ad, as_, ae, ak, asec, ar = entries[i]
                        for j in range(i + 1, len(entries)):
                            bi, bp, bd, bs, be, bk, bsec, br = entries[j]
                            if ar != br or ap != bp or ad != bd:
                                continue
                            if not _t_overlap(as_, ae, bs, be):
                                continue
                            if ai == bi:
                                continue
                            conflicts.append((ar, ap, ad, min(ai, bi), max(ai, bi)))

                    if not conflicts:
                        return 0

                    conflicts.sort()
                    for _room, per, day, _lo, hi in conflicts:
                        row = final_ui_sessions[hi]
                        st, et = row.get("start_time"), row.get("end_time")
                        key = (_room, per, day, hi, str(st), str(et))
                        if key in _attempted_room_conflict_keys:
                            continue
                        _attempted_room_conflict_keys.add(key)
                        idx_move = hi
                        kind = str(row.get("session_type", "L") or "L").strip().upper()
                        sec = str(row.get("section", "") or "").strip()
                        if not (st and et and sec):
                            continue
                        dur = _dur_minutes(st, et)
                        _sem = 0
                        for s0 in sections or []:
                            if str(getattr(s0, "label", "") or "") == sec:
                                _sem = int(getattr(s0, "semester", 0) or 0)
                                break
                        lw = LUNCH_WINDOWS.get(_sem)

                        day_order = [day] + [d for d in WORKING_DAYS if d != day]
                        for d0 in day_order:
                            for hh in range(9, 18):
                                for mm in (0, 15, 30, 45):
                                    ns = time(hh, mm)
                                    end_m = hh * 60 + mm + dur
                                    eh, em = divmod(end_m, 60)
                                    if eh > 18 or (eh == 18 and em > 0):
                                        continue
                                    ne = time(eh, em)
                                    if lw and _t_overlap(ns, ne, lw[0], lw[1]):
                                        continue
                                    if _slot_conflicts_for_section(sec, per, d0, ns, ne):
                                        continue
                                    rn = _pick_room_for_slot(
                                        kind,
                                        sec,
                                        per,
                                        d0,
                                        ns,
                                        ne,
                                        exclude_idx=idx_move,
                                        faculty_raw=str(row.get("faculty") or ""),
                                        allow_faculty_fallback=False,
                                    forbidden_rooms={_room},
                                    )
                                    if not rn:
                                        continue
                                    row["day"] = d0
                                    row["start_time"] = ns
                                    row["end_time"] = ne
                                    row["time_block"] = TimeBlock(d0, ns, ne)
                                    row["room"] = rn
                                    return 1
                    return 0

                def _faculty_overlap_move_idx() -> Optional[int]:
                    """First overlapping L/T pair (same period+day); move the higher-index row."""
                    n = len(final_ui_sessions)
                    for i in range(n):
                        ri = final_ui_sessions[i]
                        if str(ri.get("session_type", "L") or "L").strip().upper() == "P":
                            continue
                        fi = str(ri.get("faculty", "") or "").strip()
                        toki = _faculty_tokens(fi)
                        if not toki:
                            continue
                        for j in range(i + 1, n):
                            rj = final_ui_sessions[j]
                            if str(rj.get("session_type", "L") or "L").strip().upper() == "P":
                                continue
                            fj = str(rj.get("faculty", "") or "").strip()
                            if not (_faculty_tokens(fj) & toki):
                                continue
                            if normalize_period(ri.get("period")) != normalize_period(rj.get("period")):
                                continue
                            if str(ri.get("day", "") or "").strip() != str(rj.get("day", "") or "").strip():
                                continue
                            st_i, et_i = ri.get("start_time"), ri.get("end_time")
                            st_j, et_j = rj.get("start_time"), rj.get("end_time")
                            if not (st_i and et_i and st_j and et_j):
                                continue
                            if not _t_overlap(st_i, et_i, st_j, et_j):
                                continue
                            return max(i, j)
                    return None

                def _try_relocate_row_faculty(idx_move: int) -> int:
                    """Move one L/T row to a slot free for its section and instructor (cross-section safe)."""
                    row = final_ui_sessions[idx_move]
                    kind = str(row.get("session_type", "L") or "L").strip().upper()
                    if kind == "P":
                        return 0
                    sec = str(row.get("section", "") or "").strip()
                    fac = str(row.get("faculty", "") or "").strip()
                    per = normalize_period(row.get("period"))
                    day = str(row.get("day", "") or "").strip()
                    st, et = row.get("start_time"), row.get("end_time")
                    if not (sec and day and st and et):
                        return 0
                    dur = _dur_minutes(st, et)
                    _sem = 0
                    for s0 in sections or []:
                        if str(getattr(s0, "label", "") or "") == sec:
                            _sem = int(getattr(s0, "semester", 0) or 0)
                            break
                    lw = LUNCH_WINDOWS.get(_sem)
                    day_order = [day] + [d for d in WORKING_DAYS if d != day]
                    for d0 in day_order:
                        for hh in range(9, 18):
                            for mm in (0, 15, 30, 45):
                                ns = time(hh, mm)
                                end_m = hh * 60 + mm + dur
                                eh, em = divmod(end_m, 60)
                                if eh > 18 or (eh == 18 and em > 0):
                                    continue
                                ne = time(eh, em)
                                if lw and _t_overlap(ns, ne, lw[0], lw[1]):
                                    continue
                                if _slot_conflicts_for_section(sec, per, d0, ns, ne):
                                    continue
                                if fac and _faculty_slot_busy(fac, per, d0, ns, ne, idx_move):
                                    continue
                                rn = _pick_room_for_slot(
                                    kind,
                                    sec,
                                    per,
                                    d0,
                                    ns,
                                    ne,
                                    exclude_idx=idx_move,
                                    faculty_raw=fac,
                                    allow_faculty_fallback=False,
                                )
                                if not rn:
                                    continue
                                row["day"] = d0
                                row["start_time"] = ns
                                row["end_time"] = ne
                                row["time_block"] = TimeBlock(d0, ns, ne)
                                row["room"] = rn
                                return 1
                    return 0

                def _atomic_fix_parsed_faculty_conflict(tasks: Dict[str, List[Dict]]) -> int:
                    for t in tasks.get("faculty_conflicts", []):
                        fac = str(t.get("faculty", "") or "").strip()
                        if not fac:
                            continue
                        course_a = str(t.get("course_a", "") or "").strip().upper()
                        course_b = str(t.get("course_b", "") or "").strip().upper()
                        section_a = str(t.get("section_a", "") or "").strip()
                        section_b = str(t.get("section_b", "") or "").strip()
                        day = str(t.get("day", "") or "").strip()
                        per = normalize_period(t.get("period"))
                        st = _parse_clock(t.get("start"))
                        et = _parse_clock(t.get("end"))
                        ftoks = _faculty_tokens(fac)
                        colliding = []
                        for idx, row in enumerate(final_ui_sessions):
                            kind = str(row.get("session_type", "L") or "L").strip().upper()
                            if kind == "P":
                                continue
                            if day and str(row.get("day", "") or "").strip() != day:
                                continue
                            if per and normalize_period(row.get("period")) != per:
                                continue
                            base = _base_code_of(row.get("course_code"))
                            if course_a and course_b and base not in (course_a, course_b):
                                continue
                            if section_a and section_b:
                                sec_now = str(row.get("section", "") or "").strip()
                                if sec_now not in (section_a, section_b):
                                    continue
                            rf = str(row.get("faculty", "") or "").strip()
                            if not rf or not (_faculty_tokens(rf) & ftoks):
                                continue
                            rs, re_ = row.get("start_time"), row.get("end_time")
                            if st and et and rs and re_ and (not _t_overlap(rs, re_, st, et)):
                                continue
                            colliding.append(idx)
                        if len(colliding) < 2:
                            continue
                        for idx_move in sorted(colliding, reverse=True):
                            if _try_relocate_row_faculty(idx_move):
                                return 1
                    return 0

                def _atomic_fix_any_faculty_overlap() -> int:
                    """Fallback: move one row from the first detected faculty overlap pair."""
                    n = len(final_ui_sessions)
                    for i in range(n):
                        ri = final_ui_sessions[i]
                        if str(ri.get("session_type", "L") or "L").strip().upper() == "P":
                            continue
                        fi = str(ri.get("faculty", "") or "").strip()
                        toki = _faculty_tokens(fi)
                        if not toki:
                            continue
                        for j in range(i + 1, n):
                            rj = final_ui_sessions[j]
                            if str(rj.get("session_type", "L") or "L").strip().upper() == "P":
                                continue
                            fj = str(rj.get("faculty", "") or "").strip()
                            if not (_faculty_tokens(fj) & toki):
                                continue
                            if normalize_period(ri.get("period")) != normalize_period(rj.get("period")):
                                continue
                            if str(ri.get("day", "") or "").strip() != str(rj.get("day", "") or "").strip():
                                continue
                            st_i, et_i = ri.get("start_time"), ri.get("end_time")
                            st_j, et_j = rj.get("start_time"), rj.get("end_time")
                            if not (st_i and et_i and st_j and et_j and _t_overlap(st_i, et_i, st_j, et_j)):
                                continue
                            for idx_move in (max(i, j), min(i, j)):
                                if _try_relocate_row_faculty(idx_move):
                                    return 1
                            return 0
                    return 0

                def _count_errors_with(rule_fragment: str, errs: List[Dict]) -> int:
                    key = str(rule_fragment or "").strip().lower()
                    return sum(1 for e in (errs or []) if key in str(e.get("rule", "") or "").strip().lower())

                def _atomic_fix_one_section_overlap(tasks: Dict[str, List[Dict]]) -> int:
                    targets = {(t.get("course_a"), t.get("course_b")) for t in tasks.get("section_overlaps", [])}
                    candidates = []
                    n = len(final_ui_sessions)
                    for i in range(n):
                        a = final_ui_sessions[i]
                        asec = str(a.get("section", "") or "").strip()
                        aday = str(a.get("day", "") or "").strip()
                        aper = normalize_period(a.get("period"))
                        ast, aet = a.get("start_time"), a.get("end_time")
                        if not (asec and aday and ast and aet):
                            continue
                        abase = _base_code_of(a.get("course_code"))
                        for j in range(i + 1, n):
                            b = final_ui_sessions[j]
                            if str(b.get("section", "") or "").strip() != asec:
                                continue
                            if str(b.get("day", "") or "").strip() != aday:
                                continue
                            if normalize_period(b.get("period")) != aper:
                                continue
                            bst, bet = b.get("start_time"), b.get("end_time")
                            if not (bst and bet and _t_overlap(ast, aet, bst, bet)):
                                continue
                            bbase = _base_code_of(b.get("course_code"))
                            pair = (abase, bbase)
                            pair_rev = (bbase, abase)
                            if targets and pair not in targets and pair_rev not in targets:
                                continue
                            candidates.append((i, j))
                    if not candidates:
                        return 0

                    for i, j in sorted(candidates, key=lambda p: p[1], reverse=True):
                        for idx_move in (j, i):
                            row = final_ui_sessions[idx_move]
                            kind = str(row.get("session_type", "L") or "L").strip().upper()
                            sec = str(row.get("section", "") or "").strip()
                            fac = str(row.get("faculty", "") or "").strip()
                            per = normalize_period(row.get("period"))
                            day = str(row.get("day", "") or "").strip()
                            st, et = row.get("start_time"), row.get("end_time")
                            if not (sec and day and st and et):
                                continue
                            dur = _dur_minutes(st, et)
                            _sem = 0
                            for s0 in sections or []:
                                if str(getattr(s0, "label", "") or "") == sec:
                                    _sem = int(getattr(s0, "semester", 0) or 0)
                                    break
                            lw = LUNCH_WINDOWS.get(_sem)
                            day_order = [day] + [d for d in WORKING_DAYS if d != day]
                            for d0 in day_order:
                                for hh in range(9, 18):
                                    for mm in (0, 15, 30, 45):
                                        ns = time(hh, mm)
                                        end_m = hh * 60 + mm + dur
                                        eh, em = divmod(end_m, 60)
                                        if eh > 18 or (eh == 18 and em > 0):
                                            continue
                                        ne = time(eh, em)
                                        if lw and _t_overlap(ns, ne, lw[0], lw[1]):
                                            continue
                                        if _slot_conflicts_for_section(sec, per, d0, ns, ne):
                                            continue
                                        # For combined courses, also verify the new slot is free for all sibling sections
                                        orig_code_chk = str(row.get("course_code", "") or "").strip()
                                        orig_day_chk = str(row.get("day", "") or "").strip()
                                        orig_st_chk = row.get("start_time")
                                        sibling_free = True
                                        for sib in final_ui_sessions:
                                            if sib is row:
                                                continue
                                            if str(sib.get("course_code", "") or "").strip() != orig_code_chk:
                                                continue
                                            if normalize_period(sib.get("period")) != per:
                                                continue
                                            if str(sib.get("day", "") or "").strip() != orig_day_chk:
                                                continue
                                            if sib.get("start_time") != orig_st_chk:
                                                continue
                                            sib_sec = str(sib.get("section", "") or "").strip()
                                            if _slot_conflicts_for_section(sib_sec, per, d0, ns, ne):
                                                sibling_free = False
                                                break
                                        if not sibling_free:
                                            continue
                                        if kind != "P" and fac and _faculty_slot_busy(fac, per, d0, ns, ne, idx_move):
                                            continue
                                        rn = _pick_room_for_slot(
                                            kind,
                                            sec,
                                            per,
                                            d0,
                                            ns,
                                            ne,
                                            exclude_idx=idx_move,
                                            faculty_raw=fac,
                                            allow_faculty_fallback=False,
                                        )
                                        if not rn:
                                            continue
                                        # Move the conflicting row
                                        orig_day = row["day"]
                                        orig_st = row["start_time"]
                                        orig_code = str(row.get("course_code", "") or "").strip()
                                        orig_per = normalize_period(row.get("period"))
                                        row["day"] = d0
                                        row["start_time"] = ns
                                        row["end_time"] = ne
                                        row["time_block"] = TimeBlock(d0, ns, ne)
                                        row["room"] = rn
                                        # Also move sibling rows (combined-class: same code+period+day+start across sections)
                                        for sibling in final_ui_sessions:
                                            if sibling is row:
                                                continue
                                            if str(sibling.get("course_code", "") or "").strip() != orig_code:
                                                continue
                                            if normalize_period(sibling.get("period")) != orig_per:
                                                continue
                                            if str(sibling.get("day", "") or "").strip() != orig_day:
                                                continue
                                            if sibling.get("start_time") != orig_st:
                                                continue
                                            sibling["day"] = d0
                                            sibling["start_time"] = ns
                                            sibling["end_time"] = ne
                                            sibling["time_block"] = TimeBlock(d0, ns, ne)
                                            # Assign room for sibling's section
                                            sib_sec = str(sibling.get("section", "") or "").strip()
                                            sib_room = _pick_room_for_slot(
                                                kind, sib_sec, orig_per, d0, ns, ne,
                                                exclude_idx=None,
                                                faculty_raw=fac,
                                                allow_faculty_fallback=False,
                                            )
                                            sibling["room"] = sib_room or rn
                                        return 1
                    return 0

                def _atomic_fix_one_practical_lab_doublebook(_tasks: Dict[str, List[Dict]]) -> int:
                    entries = []
                    for idx, row in enumerate(final_ui_sessions):
                        if str(row.get("session_type", "L") or "L").strip().upper() != "P":
                            continue
                        per = normalize_period(row.get("period"))
                        day = str(row.get("day", "") or "").strip()
                        st, et = row.get("start_time"), row.get("end_time")
                        sec = str(row.get("section", "") or "").strip()
                        if not (day and st and et and sec):
                            continue
                        for rn in [x.strip() for x in str(row.get("room", "") or "").split(",") if x.strip()]:
                            entries.append((idx, rn, per, day, st, et, sec))
                    conflicts = []
                    for i in range(len(entries)):
                        ai, ar, ap, ad, ast, aet, asec = entries[i]
                        for j in range(i + 1, len(entries)):
                            bi, br, bp, bd, bst, bet, bsec = entries[j]
                            if ar != br or ap != bp or ad != bd:
                                continue
                            if not _t_overlap(ast, aet, bst, bet):
                                continue
                            if ai == bi:
                                continue
                            conflicts.append((ar, ap, ad, min(ai, bi), max(ai, bi)))
                    if not conflicts:
                        return 0
                    conflicts.sort()
                    _room, per, day, _lo, idx_move = conflicts[0]
                    row = final_ui_sessions[idx_move]
                    kind = "P"
                    sec = str(row.get("section", "") or "").strip()
                    st, et = row.get("start_time"), row.get("end_time")
                    if not (sec and st and et):
                        return 0
                    dur = _dur_minutes(st, et)
                    _sem = 0
                    for s0 in sections or []:
                        if str(getattr(s0, "label", "") or "") == sec:
                            _sem = int(getattr(s0, "semester", 0) or 0)
                            break
                    lw = LUNCH_WINDOWS.get(_sem)
                    day_order = [day] + [d for d in WORKING_DAYS if d != day]
                    for d0 in day_order:
                        for hh in range(9, 18):
                            for mm in (0, 15, 30, 45):
                                ns = time(hh, mm)
                                end_m = hh * 60 + mm + dur
                                eh, em = divmod(end_m, 60)
                                if eh > 18 or (eh == 18 and em > 0):
                                    continue
                                ne = time(eh, em)
                                if lw and _t_overlap(ns, ne, lw[0], lw[1]):
                                    continue
                                if _slot_conflicts_for_section(sec, per, d0, ns, ne):
                                    continue
                                rn = _pick_room_for_slot(
                                    kind,
                                    sec,
                                    per,
                                    d0,
                                    ns,
                                    ne,
                                    exclude_idx=idx_move,
                                    faculty_raw="",
                                    allow_faculty_fallback=False,
                                )
                                if not rn:
                                    continue
                                row["day"] = d0
                                row["start_time"] = ns
                                row["end_time"] = ne
                                row["time_block"] = TimeBlock(d0, ns, ne)
                                row["room"] = rn
                                return 1
                    return 0

                for _pass in range(10):
                    occ = {}  # (period, room, day) -> [(start,end)]
                    moved = 0

                    # Deterministic ordering helps convergence:
                    # schedule longer practicals first so they "claim" lab sets early.
                    iter_sessions = sorted(
                        list(final_ui_sessions),
                        key=lambda s: (
                            _np(s.get("period")),
                            _day_order.get(str(s.get("day") or ""), 99),
                            str(s.get("start_time") or ""),
                            0 if str(s.get("session_type") or "L").strip().upper() == "P" else 1,
                            -_dur_minutes(s.get("start_time"), s.get("end_time")),
                            str(s.get("course_code") or ""),
                            str(s.get("section") or ""),
                        ),
                    )

                    for s in iter_sessions:
                        room = str(s.get("room") or "").strip()
                        period = _np(s.get("period"))
                        day = str(s.get("day") or "")
                        st = s.get("start_time")
                        et = s.get("end_time")
                        if not (day and st and et):
                            continue
                        stype = str(s.get("session_type") or "L").strip().upper()
                        sec = str(s.get("section") or "").strip()
                        needed = section_students.get(sec, 0)
                        if not room:
                            # Fill blank rooms before strict verification.
                            candidates = lab_rooms if stype == "P" else class_rooms
                            for r in candidates:
                                rn = getattr(r, "room_number", "")
                                if stype in ("L", "T") and needed and room_caps.get(rn, 0) < needed:
                                    continue
                                ok = True
                                for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                    if _t_overlap(st, et, occ_start, occ_end):
                                        ok = False
                                        break
                                if ok:
                                    s["room"] = rn
                                    room = rn
                                    moved += 1
                                    break
                            if not room and candidates:
                                # Final fallback: keep any non-conflicting room even if under-capacity.
                                for r in candidates:
                                    rn = getattr(r, "room_number", "")
                                    ok = True
                                    for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                        if _t_overlap(st, et, occ_start, occ_end):
                                            ok = False
                                            break
                                    if ok:
                                        s["room"] = rn
                                        room = rn
                                        moved += 1
                                        break
                            if not room:
                                continue

                        rooms = [x.strip() for x in room.split(",") if x.strip()]
                        if not rooms:
                            rooms = [room]
                        # Preserve canonical Phase 4 combined C004 sessions; all other C004 uses
                        # are eligible for reassignment if they create strict conflicts.
                        is_phase4_combined = str(s.get("phase") or "").strip().lower() == "phase 4"
                        if room == "C004" and is_phase4_combined and stype in ("L", "T"):
                            for rname in rooms:
                                occ.setdefault((period, rname, day), []).append((st, et))
                            continue

                        # Capacity check: if L/T room is under-capacity, treat as needing reassignment.
                        under_capacity = False
                        if stype in ("L", "T") and needed:
                            # pick max cap for comma-separated room list (defensive)
                            cap_now = max(room_caps.get(rn, 0) for rn in rooms) if rooms else 0
                            if cap_now and cap_now < needed:
                                under_capacity = True

                        # If any listed room conflicts, reassign whole session room
                        conflict = False
                        for rname in rooms:
                            for (occ_start, occ_end) in occ.get((period, rname, day), []):
                                if _t_overlap(st, et, occ_start, occ_end):
                                    conflict = True
                                    break
                            if conflict:
                                break

                        if not conflict and not under_capacity:
                            for rname in rooms:
                                occ.setdefault((period, rname, day), []).append((st, et))
                            continue
                        candidates = lab_rooms if stype == "P" else class_rooms
                        assigned = False

                        # For practicals, prioritize eliminating conflicts even if it means
                        # reducing a multi-lab assignment to a single lab (strict verify cares
                        # about double-booking, not lab-count).
                        if stype == "P":
                            # Try single-lab first
                            new_room = ""
                            for r in candidates:
                                rn = getattr(r, "room_number", "")
                                ok = True
                                for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                    if _t_overlap(st, et, occ_start, occ_end):
                                        ok = False
                                        break
                                if ok:
                                    new_room = rn
                                    break
                            if new_room:
                                s["room"] = new_room
                                occ.setdefault((period, new_room, day), []).append((st, et))
                                moved += 1
                                assigned = True
                            # If no non-research lab is free, retry with fallback pool (may include research labs).
                            if not assigned:
                                new_room = ""
                                for r in lab_rooms_fallback:
                                    rn = getattr(r, "room_number", "")
                                    ok = True
                                    for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                        if _t_overlap(st, et, occ_start, occ_end):
                                            ok = False
                                            break
                                    if ok:
                                        new_room = rn
                                        break
                                if new_room:
                                    s["room"] = new_room
                                    occ.setdefault((period, new_room, day), []).append((st, et))
                                    moved += 1
                                    assigned = True
                            # If still not assigned and original had multiple labs, try to find an N-lab set all free.
                            if (not assigned) and len(rooms) >= 2:
                                needed_n = len(rooms)
                                chosen = []
                                for r in candidates:
                                    rn = getattr(r, "room_number", "")
                                    ok = True
                                    for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                        if _t_overlap(st, et, occ_start, occ_end):
                                            ok = False
                                            break
                                    if not ok:
                                        continue
                                    chosen.append(rn)
                                    if len(chosen) >= needed_n:
                                        break
                                if len(chosen) >= needed_n:
                                    new_room = ", ".join(chosen[:needed_n])
                                    s["room"] = new_room
                                    for rn in chosen[:needed_n]:
                                        occ.setdefault((period, rn, day), []).append((st, et))
                                    moved += 1
                                    assigned = True

                        # Otherwise (or fallback), try to move to a single free candidate room
                        if not assigned:
                            new_room = ""
                            for r in candidates:
                                rn = getattr(r, "room_number", "")
                                cap = room_caps.get(rn, 0)
                                if stype in ("L", "T") and needed and cap < needed:
                                    continue
                                ok = True
                                for (occ_start, occ_end) in occ.get((period, rn, day), []):
                                    if _t_overlap(st, et, occ_start, occ_end):
                                        ok = False
                                        break
                                if ok:
                                    new_room = rn
                                    break
                            if new_room:
                                s["room"] = new_room
                                occ.setdefault((period, new_room, day), []).append((st, et))
                                moved += 1
                                assigned = True

                        if not assigned:
                            # Keep original; record occupancy to reduce cascading
                            for rname in rooms:
                                occ.setdefault((period, rname, day), []).append((st, et))

                    if moved == 0:
                        break

                # Final targeted sweep: eliminate any remaining lab double-bookings
                # (especially comma-separated lab lists) before strict verification.
                def _normalize_room_list(v):
                    return [x.strip() for x in str(v or "").split(",") if x and str(x).strip()]

                for _pass2 in range(6):
                    changed2 = 0
                    occ2 = {}  # (period, room, day) -> list[(start,end,idx)]
                    p_rows = []
                    for idx, s in enumerate(final_ui_sessions):
                        if str(s.get("session_type", "L") or "L").strip().upper() != "P":
                            continue
                        day = str(s.get("day") or "")
                        st = s.get("start_time")
                        et = s.get("end_time")
                        if not (day and st and et):
                            continue
                        period = _np(s.get("period"))
                        rooms = _normalize_room_list(s.get("room"))
                        if not rooms:
                            continue
                        p_rows.append((idx, s, period, day, st, et, rooms))
                        for rn in rooms:
                            occ2.setdefault((period, rn, day), []).append((st, et, idx))

                    conflict_idx = set()
                    for (_k, rows2) in occ2.items():
                        for i in range(len(rows2)):
                            a_s, a_e, a_i = rows2[i]
                            for j in range(i + 1, len(rows2)):
                                b_s, b_e, b_i = rows2[j]
                                if _t_overlap(a_s, a_e, b_s, b_e):
                                    conflict_idx.add(max(a_i, b_i))  # move deterministic later row

                    if not conflict_idx:
                        break

                    lab_pool = list(lab_rooms) + [r for r in lab_rooms_fallback if getattr(r, "room_number", "") not in {getattr(x, "room_number", "") for x in lab_rooms}]
                    for idx, s, period, day, st, et, rooms in p_rows:
                        if idx not in conflict_idx:
                            continue
                        new_room = ""
                        for r in lab_pool:
                            rn = getattr(r, "room_number", "")
                            if not rn:
                                continue
                            blocked = False
                            for (occ_s, occ_e, occ_i) in occ2.get((period, rn, day), []):
                                if occ_i == idx:
                                    continue
                                if _t_overlap(st, et, occ_s, occ_e):
                                    blocked = True
                                    break
                            if not blocked:
                                new_room = rn
                                break
                        if new_room and str(s.get("room") or "").strip() != new_room:
                            s["room"] = new_room
                            changed2 += 1
                    if changed2 == 0:
                        break
            except Exception as _room_fix_e:
                print(f"WARNING: room double-booking repair pass failed: {_room_fix_e}")

            print("\nStrict verification (zero violations required)...")
            try:
                # Sem2-focused audit lens requested by user (reporting only).
                room_caps = {getattr(r, "room_number", ""): int(getattr(r, "capacity", 0) or 0) for r in (classrooms or [])}
                sem2_rows = [
                    s for s in final_ui_sessions
                    if "Sem2" in str(s.get("section", "") or "")
                    and str(s.get("session_type", "L") or "L").strip().upper() in ("L", "T")
                ]
                sem2_under = 0
                for s in sem2_rows:
                    sec = str(s.get("section", "") or "")
                    needed = 0
                    for _sec in (sections or []):
                        if getattr(_sec, "label", "") == sec:
                            needed = int(getattr(_sec, "students", 0) or 0)
                            break
                    rms = [x.strip() for x in str(s.get("room", "") or "").split(",") if x.strip()]
                    cap = max([room_caps.get(rn, 0) for rn in rms], default=0)
                    if needed and cap and cap < needed:
                        sem2_under += 1
                print(f"[Sem2 audit] LT rows={len(sem2_rows)}, under-capacity rows={sem2_under}")
            except Exception as _sem2_audit_e:
                print(f"WARNING: Sem2 audit skipped: {_sem2_audit_e}")
            ok_verify, ver_errors = run_strict_verification_on_final_ui(
                final_ui_sessions, courses, sections, classrooms
            )
            if not ok_verify:
                # One deterministic repair per micro-iteration, then re-verify (reduces cascade).
                _attempted_room_conflict_keys = set()
                _initial_errs = len(ver_errors or [])
                _micro_budget = _scaled_budget(
                    max(
                        18,
                        (_initial_errs * 3) + (len(final_ui_sessions or []) // 25) + (len(classrooms or []) // 2)
                    ),
                    minimum=12,
                )
                for _micro in range(_micro_budget):
                    _guard_runtime_or_raise(f"strict_fix_micro_{_micro + 1}")
                    _tasks = _parse_strict_tasks(ver_errors)
                    _has_room = bool(_tasks.get("room_conflicts"))
                    _has_lunch = bool(_tasks.get("time_constraints"))
                    _has_ltpsc = bool(_tasks.get("ltpsc"))
                    _has_faculty = bool(_tasks.get("faculty_conflicts"))
                    _has_section_overlap = bool(_tasks.get("section_overlaps")) or _count_errors_with("section overlap", ver_errors) > 0
                    _has_lab_doublebook = _count_errors_with("classroom conflict", ver_errors) > 0
                    _step = 0
                    _step_tag = ""
                    _before = None
                    _pre_total = len(ver_errors)
                    _pre_section = _count_errors_with("section overlap", ver_errors)
                    _pre_room = _count_errors_with("classroom conflict", ver_errors)

                    if _has_section_overlap or _has_lab_doublebook:
                        from copy import deepcopy
                        _before = deepcopy(final_ui_sessions)

                    _step += _atomic_fill_blank_room(_tasks)
                    if _step and not _step_tag:
                        _step_tag = "fill"
                    if (not _step) and _has_ltpsc:
                        _step += _atomic_ltpsc_trim_one(_tasks)
                        if _step:
                            _step_tag = "ltpsc"
                    if (not _step) and _has_ltpsc:
                        _step += _atomic_ltpsc_one(_tasks)
                        if _step:
                            _step_tag = "ltpsc"
                    if (not _step) and _has_faculty:
                        _step += _atomic_fix_parsed_faculty_conflict(_tasks)
                        if _step:
                            _step_tag = "faculty"
                    if (not _step) and _has_faculty:
                        _step += _atomic_fix_any_faculty_overlap()
                        if _step:
                            _step_tag = "faculty_fallback"
                    if (not _step) and _has_section_overlap:
                        _step += _atomic_fix_one_section_overlap(_tasks)
                        if _step:
                            _step_tag = "section"
                    if (not _step) and _has_lab_doublebook:
                        _step += _atomic_fix_one_practical_lab_doublebook(_tasks)
                        if _step:
                            _step_tag = "lab"
                    if (not _step) and _has_lunch:
                        _step += _atomic_fix_parsed_lunch_conflict(_tasks)
                        if _step:
                            _step_tag = "lunch_parsed"
                    if (not _step) and _has_lunch:
                        _step += _atomic_fix_one_lunch(_tasks)
                        if _step:
                            _step_tag = "lunch"
                    if (not _step) and _has_room:
                        _step += _force_resolve_one_room_conflict()
                        if _step:
                            _step_tag = "room"
                    if (not _step) and _has_lunch:
                        _step += _atomic_fix_one_lunch(_tasks)
                        if _step:
                            _step_tag = "lunch"
                    if (not _step) and _has_ltpsc:
                        _step += _atomic_ltpsc_trim_one(_tasks)
                        if _step:
                            _step_tag = "ltpsc"
                    if (not _step) and _has_ltpsc:
                        _step += _atomic_ltpsc_one(_tasks)
                        if _step:
                            _step_tag = "ltpsc"
                    if (not _step) and (not _has_lunch):
                        # Last-resort deterministic nudge only when typed tasks cannot progress.
                        _step += _force_resolve_one_room_conflict()
                        if _step:
                            _step_tag = "fallback_room"
                    ok_verify, ver_errors = run_strict_verification_on_final_ui(
                        final_ui_sessions, courses, sections, classrooms
                    )

                    # Transactional guard for high-oscillation repairs.
                    if _step and _before is not None and _step_tag in ("section", "lab"):
                        _post_section = _count_errors_with("section overlap", ver_errors)
                        _post_room = _count_errors_with("classroom conflict", ver_errors)
                        _improved = True
                        # Section moves can trade overlap location before reducing total;
                        # don't auto-revert them unless they increase total errors.
                        if _step_tag == "section" and len(ver_errors) > _pre_total:
                            _improved = False
                        if _step_tag == "lab" and not (
                            (_post_room < _pre_room) or
                            (_post_room == _pre_room and len(ver_errors) < _pre_total)
                        ):
                            _improved = False
                        if not _improved:
                            final_ui_sessions[:] = _before
                            ok_verify, ver_errors = run_strict_verification_on_final_ui(
                                final_ui_sessions, courses, sections, classrooms
                            )
                            _step = 0
                            _step_tag = f"{_step_tag}_reverted"

                    _rule_counts = {}
                    for _err in ver_errors or []:
                        _r = str(_err.get("rule", "") or "")
                        _rule_counts[_r] = _rule_counts.get(_r, 0) + 1
                    print(
                        f"  [strict-fix micro {_micro + 1}] step={_step} tag={_step_tag}, "
                        f"remaining={len(ver_errors)} {_rule_counts}"
                    )
                    if ok_verify or _step == 0:
                        break
            # Strict moves run only on final_ui_sessions; Phase 6.8 repaired pipeline sessions earlier.
            # Clear instructor double-bookings that appear as flat CSV rows (e.g. shared CS163 slots).
            if ok_verify:
                _faculty_post_budget = _scaled_budget(
                    max(8, len(final_ui_sessions or []) // 30),
                    minimum=5,
                )
                for _fpass in range(_faculty_post_budget):
                    _fidx = _faculty_overlap_move_idx()
                    if _fidx is None:
                        break
                    _row_ref = final_ui_sessions[_fidx]
                    _old_day = _row_ref.get("day")
                    _old_st = _row_ref.get("start_time")
                    _old_et = _row_ref.get("end_time")
                    _old_tb = _row_ref.get("time_block")
                    _old_room = _row_ref.get("room")
                    if not _try_relocate_row_faculty(_fidx):
                        break
                    ok_verify, ver_errors = run_strict_verification_on_final_ui(
                        final_ui_sessions, courses, sections, classrooms
                    )
                    if not ok_verify:
                        # Keep strict-safe state; do not retain a faculty relocation that
                        # introduces section/room/LTPSC violations.
                        _row_ref["day"] = _old_day
                        _row_ref["start_time"] = _old_st
                        _row_ref["end_time"] = _old_et
                        _row_ref["time_block"] = _old_tb
                        _row_ref["room"] = _old_room
                        ok_verify, ver_errors = run_strict_verification_on_final_ui(
                            final_ui_sessions, courses, sections, classrooms
                        )
                        break
                    print(f"  [post-strict faculty] pass {_fpass + 1}: relocated row index {_fidx}")
            if not ok_verify:
                print(f"FAILED: {len(ver_errors)} verification violation(s). Output not saved.")
                for err in ver_errors[:40]:
                    print(f"  [{err.get('rule', '')}] {err.get('message', '')}")
                if len(ver_errors) > 40:
                    print(f"  ... and {len(ver_errors) - 40} more.")
                # Always dump a reproducible debug artifact for strict-verify failures:
                # - the exact final UI session rows used for verification
                # - a CSV in time_slot_log format so CLI/tools can re-verify deterministically
                try:
                    import json
                    import csv
                    from datetime import datetime as _dt

                    base_out_dir = "DATA/EDITED OUTPUT" if sessions_from_log is not None else "DATA/OUTPUT"
                    debug_ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                    debug_dir = f"{base_out_dir}/strict_verify_DEBUG_{debug_ts}"
                    os.makedirs(debug_dir, exist_ok=True)

                    json_path = f"{debug_dir}/final_ui_sessions.json"
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(final_ui_sessions, f, indent=2, default=str)

                    err_path = f"{debug_dir}/strict_verify_errors.json"
                    with open(err_path, "w", encoding="utf-8") as f:
                        json.dump(ver_errors, f, indent=2)

                    csv_path = f"{debug_dir}/time_slot_log_{debug_ts}.csv"
                    with open(csv_path, "w", encoding="utf-8", newline="") as f:
                        w = csv.DictWriter(
                            f,
                            fieldnames=[
                                "Phase",
                                "Course Code",
                                "Course Name",
                                "Section",
                                "Day",
                                "Start Time",
                                "End Time",
                                "Room",
                                "Faculty",
                                "Session Type",
                                "Period",
                            ],
                        )
                        w.writeheader()
                        for s in final_ui_sessions:
                            tb = s.get("time_block")
                            day = getattr(tb, "day", "") if tb else ""
                            st = getattr(tb, "start", None)
                            et = getattr(tb, "end", None)
                            w.writerow(
                                {
                                    "Phase": s.get("phase", "") or "",
                                    "Course Code": s.get("course_code", "") or "",
                                    "Course Name": s.get("course_name", "") or "",
                                    "Section": s.get("section", "") or "",
                                    "Day": day or "",
                                    "Start Time": st.strftime("%H:%M") if hasattr(st, "strftime") else "",
                                    "End Time": et.strftime("%H:%M") if hasattr(et, "strftime") else "",
                                    "Room": s.get("room", "") or "",
                                    "Faculty": s.get("faculty", "") or "",
                                    "Session Type": s.get("session_type", "") or "L",
                                    "Period": s.get("period", "") or "",
                                }
                            )

                    print(f"DEBUG strict-verify dump written: {debug_dir}")
                except Exception as dump_ex:
                    print(f"WARNING: Failed to write strict-verify debug dump: {dump_ex}")
                if sessions_from_log is not None or _macro_i >= _macro_max - 1:
                    debug_faculty_path = None
                    try:
                        from modules_v2.phase6_faculty_conflicts import detect_faculty_conflicts

                        base_out_dir = "DATA/EDITED OUTPUT" if sessions_from_log is not None else "DATA/OUTPUT"
                        debug_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        debug_faculty_path = f"{base_out_dir}/faculty_timetables_DEBUG_{debug_ts}.xlsx"
                        os.makedirs(os.path.dirname(debug_faculty_path), exist_ok=True)

                        internal_verify_sessions = final_ui_rows_to_verify_sessions(final_ui_sessions)
                        debug_faculty_conflicts = detect_faculty_conflicts(internal_verify_sessions)
                        debug_written = write_faculty_timetables(
                            final_ui_sessions,
                            debug_faculty_conflicts,
                            debug_faculty_path,
                        )
                        if debug_written:
                            debug_faculty_path = debug_written
                            print(f"DEBUG faculty workbook written: {debug_faculty_path}")
                        else:
                            debug_faculty_path = None
                            print("DEBUG faculty workbook skipped (no faculty sessions found).")
                    except Exception as debug_ex:
                        debug_faculty_path = None
                        print(f"WARNING: Failed to write DEBUG faculty workbook: {debug_ex}")
                    raise GenerationViolationError(
                        f"{len(ver_errors)} verification violation(s)",
                        ver_errors,
                        debug_faculty_path=debug_faculty_path,
                    )
                print(
                    "Retrying after macro repair (reshuffle + faculty/section overlap passes)..."
                )
                continue
            print("[OK] Strict verification passed.")
            break
        else:
            break

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
    
    # Set base output directory
    base_out_dir = "DATA/EDITED OUTPUT" if sessions_from_log is not None else "DATA/OUTPUT"
    
    # Save the main timetable workbook
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"{base_out_dir}/IIITDWD_24_Sheets_v2_{timestamp}.xlsx"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    writer.save_timetable(output_path)
    
    print(f"\nGenerated 24 sheets successfully!")
    print(f"Output file: {output_path}")
    print(f"Total sheets: {len(writer.workbook.worksheets)}")
    
    # Print time slot logging summary
    from utils.time_slot_logger import get_logger
    logger = get_logger()
    
    # RECONSTRUCT LOGGER FROM FINAL EXCEL GRIDS TO ENSURE 1:1 UI SYNC
    logger.entries = []
    logger._seen = set()
    for s in final_ui_sessions:
        course_display = s['course_code']
        base_code = course_display.replace('-TUT', '').replace('-LAB', '').split('-')[0]
        section_key = s['section']
        
        comb_sess = None
        # Always evaluate comb_sess to correctly tag Phase 4 sessions
        stype = (s.get("session_type") or "L").strip().upper()
        comb_sess = next(
            (
                cs
                for cs in combined_sessions
                if isinstance(cs, dict)
                and str(cs.get("course_code", "") or "").split("-")[0].strip() == base_code
                and normalize_period(cs.get("period")) == normalize_period(s.get("period", "PRE"))
                and str(cs.get("session_type", cs.get("kind", "L")) or "L").strip().upper() == stype
                and any(str(sec).startswith(section_key) for sec in cs.get("sections", []))
            ),
            None,
        )

        # Determine room
        room = s.get('room', "")
        if not room:
            if comb_sess and comb_sess.get("room"):
                room = comb_sess["room"]


            # For practicals, prefer the session's own room (Phase 8 may assign per-session labs).
            if sessions_from_log is None and stype == "P" and not room:
                try:
                    _pkey = normalize_period(s.get("period", "PRE"))
                    _st = s.get("start_time")
                    _et = s.get("end_time")
                    _day = s.get("day")
                    _match = next(
                        (
                            ss for ss in (phase5_sessions + phase7_sessions)
                            if getattr(ss, "section", "") == section_key
                            and (getattr(ss, "course_code", "").split("-")[0] == base_code)
                            and normalize_period(getattr(ss, "period", "PRE")) == _pkey
                            and getattr(getattr(ss, "block", None), "day", None) == _day
                            and getattr(getattr(ss, "block", None), "start", None) == _st
                            and getattr(getattr(ss, "block", None), "end", None) == _et
                        ),
                        None,
                    )
                    if _match and getattr(_match, "room", None):
                        room = str(_match.room).strip()
                except Exception:
                    pass

            # For lectures/tutorials, Phase 8 room assignments are authoritative.
            if sessions_from_log is None and (not room) and room_assignments and stype != "P":
                pkey = normalize_period(s.get("period", "PRE"))
                a = room_assignments.get((base_code, section_key, pkey))
                if isinstance(a, dict) and a.get("classroom"):
                    room = a.get("classroom") or room

            # Combined sessions already handled above via authoritative comb_sess room.
            if not room and room_assignments:
                pkey = normalize_period(s.get("period", "PRE"))
                a = room_assignments.get((base_code, section_key, pkey))
                if isinstance(a, dict):
                    stype = (s.get("session_type") or "L").strip().upper()
                    if stype == "P":
                        labs = a.get("labs") or []
                        labs = [str(x).strip() for x in labs if str(x).strip()]
                        if labs:
                            room = ", ".join(labs)
                    else:
                        room = a.get("classroom") or ""
            
        # Determine faculty
        faculty = s.get('faculty', "")
        if not faculty:
            if comb_sess and comb_sess.get('instructor'):
                faculty = comb_sess['instructor']
            elif sessions_from_log is None:
                # Only fall back to course object when NOT in log-replay mode
                course_obj = next((c for c in courses if getattr(c, 'code', '') == base_code), None)
                if course_obj and hasattr(course_obj, 'instructor_name'):
                    faculty = course_obj.instructor_name
                
        # If generating from log (UI edits), the session's faculty/room are already authoritative
        # (set by create_integrated_schedule fast path).  Only do a secondary lookup if truly empty.
        if sessions_from_log is not None and (not room or not faculty):
             orig_s = next((os_item for os_item in sessions_from_log if 
                  (os_item.get('Section') == section_key or os_item.get('section') == section_key) and 
                  (os_item.get('Course Code') == course_display or os_item.get('course_code') == course_display) and 
                  str(os_item.get('Day') or os_item.get('day')).strip().upper() == str(s['day']).strip().upper() and 
                  str(os_item.get('Start Time') or os_item.get('start_time')).replace(':', '') == s['start_time'].strftime("%H%M")
             ), None)
             if orig_s:
                 if not room:
                     room = orig_s.get('Room') or orig_s.get('room') or room
                 if not faculty:
                     faculty = orig_s.get('Faculty') or orig_s.get('faculty') or faculty
                
                
        # Determine Phase for UI syncing
        orig_phase = "Final"
        if course_display.startswith("ELECTIVE"):
            orig_phase = "Phase 3"
        elif comb_sess:
            orig_phase = "Phase 4"
        elif "-TUT" in course_display or "-LAB" in course_display:
            orig_phase = "Phase 5"  # Default to Phase 5; will be overridden below if session is from Phase 7
            # Check if this course is actually a Phase 7 session (<=2 credits, not in combined)
            if phase7_sessions:
                base_for_phase = course_display.replace("-TUT", "").replace("-LAB", "").split("-")[0].strip().upper()
                for _p7s in phase7_sessions:
                    p7_base = str(getattr(_p7s, "course_code", "") or "").split("-")[0].strip().upper()
                    if p7_base == base_for_phase:
                        orig_phase = "Phase 7"
                        break
        else:
            orig_phase = "Phase 7"
            
        # If generating from log (UI edits), preserve original Phase if available
        if sessions_from_log is not None and orig_s and orig_s.get('Phase'):
            orig_phase = orig_s.get('Phase')

        logger.log_slot(
            phase=orig_phase,
            course_code=course_display,
            section=s['section'],
            day=s['day'],
            start_time=s['start_time'],
            end_time=s['end_time'],
            period=s['period'],
            session_type=s['session_type'],
            room=room,
            faculty=faculty
        )

    logger.print_summary()
    
    # Export time slot log to CSV
    log_output_path = f"{base_out_dir}/time_slot_log_{timestamp}.csv"
    logger.export_to_csv(log_output_path)

    # ------------------------------------------------------------------
    # Faculty-level outputs: per-faculty timetables + conflict summary
    # ------------------------------------------------------------------
    try:
        faculty_tt_path = f"{base_out_dir}/faculty_timetables_{timestamp}.xlsx"
        print(f"\nCreating per-faculty timetables at: {faculty_tt_path}")

        # If regenerating from UI, final_ui_sessions holds the exact grid state 
        # (including parsed periods, edited classrooms, and edited faculty).
        if sessions_from_log is not None:
            faculty_all_sessions = final_ui_sessions
        else:
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

        # Always use final_ui_sessions as authoritative source for room clash reporting.
        # These rows are produced after all repair/strict phases. Mixing in raw elective
        # rows again can duplicate occupancy and create false classroom clashes.
        room_all_sessions = list(final_ui_sessions)

        # Defensive dedupe for room-report path only.
        # Key by physical occupancy identity to eliminate repeated rows without
        # altering actual timetable generation.
        deduped_room_rows = []
        seen_room_keys = set()
        for s in room_all_sessions:
            if not isinstance(s, dict):
                continue
            tb = s.get("time_block")
            day = str(getattr(tb, "day", s.get("day", "")) or "").strip()
            st = str(getattr(tb, "start", s.get("start_time", "")) or "").strip()
            et = str(getattr(tb, "end", s.get("end_time", "")) or "").strip()
            room = str(s.get("room", "") or "").strip().upper()
            if not room:
                continue
            code = str(s.get("course_code", "") or "").strip().upper()
            sec = str(s.get("section", "") or "").strip().upper()
            period = str(s.get("period", "") or "").strip().upper()
            skey = (room, day, st, et, code, sec, period)
            if skey in seen_room_keys:
                continue
            seen_room_keys.add(skey)
            deduped_room_rows.append(s)
        room_all_sessions = deduped_room_rows

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


def generate_24_sheets_from_log(log_path: str, timestamp: str) -> str:
    """Regenerate 24 Excel sheets from an existing time_slot_log CSV.
    The caller already wrote the dragged sessions to log_path.
    """
    import os as _os
    import pandas as pd
    log_path = _os.path.abspath(log_path)
    if not _os.path.exists(log_path):
        raise FileNotFoundError(f"Log file not found: {log_path}")
    
    # Load sessions from CSV
    df = pd.read_csv(log_path)
    sessions = df.to_dict('records')
    
    # Call generate_24_sheets with the sessions to skip scheduling steps
    output_path, _ts = generate_24_sheets(sessions_from_log=sessions)
    return output_path


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
        from utils.generation_verify_bridge import GenerationViolationError

        try:
            output_file, _ = generate_24_sheets()
        except GenerationViolationError as gve:
            print("\n" + "=" * 70)
            print("GENERATION FAILED: verification violations (zero-violations mode).")
            print("=" * 70)
            for err in (gve.errors or [])[:60]:
                print(f"  [{err.get('rule', '')}] {err.get('message', '')}")
            if len(gve.errors or []) > 60:
                print(f"  ... and {len(gve.errors) - 60} more.")
            sys.exit(1)

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
