"""
Phase 5: Core Courses Scheduling (Credits > 2)
Schedule core courses with credits > 2 using LTPSC logic
"""
import re
import pandas as pd
import random
from typing import List, Dict, Tuple, Optional
from datetime import time, datetime, timedelta
from collections import defaultdict

from utils.data_models import Course, Section, ClassRoom, ScheduledSession, TimeBlock, section_has_time_conflict
from modules_v2.phase2_time_management_v2 import get_lunch_blocks, generate_base_time_slots
from utils.time_slot_logger import get_logger
from utils.session_rules_validator import SessionRulesValidator
from utils.time_validator import validate_time_range

def parse_ltpsc(ltpsc: str) -> Dict[str, int]:
    """Parse LTPSC string to get L, T, P values"""
    if not ltpsc or pd.isna(ltpsc):
        return {'L': 0, 'T': 0, 'P': 0}
    
    # Clean the string to keep only digits
    ltpsc_str = ''.join(c for c in str(ltpsc) if c.isdigit())
    
    if len(ltpsc_str) >= 3:
        return {
            'L': int(ltpsc_str[0]),
            'T': int(ltpsc_str[1]),
            'P': int(ltpsc_str[2])
        }
    else:
        return {'L': 0, 'T': 0, 'P': 0}

def calculate_slots_needed(ltpsc: str) -> Dict[str, int]:
    """Calculate number of slots needed based on LTPSC"""
    try:
        # Parse LTPSC string directly (format: "3-0-2-0-4")
        parts = str(ltpsc).split('-')
        if len(parts) != 5:
            return {'lectures': 0, 'tutorials': 0, 'practicals': 0, 'total': 0}
        
        L = int(parts[0]) if parts[0] else 0  # Lecture hours per week
        T = int(parts[1]) if parts[1] else 0  # Tutorial hours per week
        P = int(parts[2]) if parts[2] else 0  # Practical hours per week
        
        # Calculate slots: L/1.5 (ceiled), T/1, P/2
        import math
        lectures = math.ceil(L / 1.5)
        tutorials = int(T / 1)
        practicals = int(P / 2)
        
        return {
            'lectures': lectures,
            'tutorials': tutorials,
            'practicals': practicals,
            'total': lectures + tutorials + practicals
        }
    except:
        return {'lectures': 0, 'tutorials': 0, 'practicals': 0, 'total': 0}

def identify_multi_faculty_courses(courses: List[Course]) -> Tuple[List[Course], List[Course]]:
    """Identify and separate multi-faculty courses from single-faculty courses"""
    multi_faculty = []
    single_faculty = []
    
    for course in courses:
        if course.credits > 2 and not course.is_elective and not course.is_combined:
            # Check if course has multiple instructors
            if hasattr(course, 'instructors') and course.instructors:
                if len(course.instructors) > 1:
                    multi_faculty.append(course)
                else:
                    single_faculty.append(course)
            else:
                single_faculty.append(course)
    
    return multi_faculty, single_faculty

def schedule_synchronized_sessions(course: Course, sections: List[Section], period: str,
                                  occupied_slots: Dict[str, List[TimeBlock]],
                                  classrooms: List[ClassRoom], room_occupancy: Dict[str, List[TimeBlock]],
                                  all_sessions: List = None) -> List[ScheduledSession]:
    """Schedule synchronized sessions for multi-faculty courses (same time, different rooms)"""
    # Parse faculty list and assign to sections
    faculty_list = []
    if hasattr(course, 'instructors') and course.instructors:
        faculty_list = course.instructors
    else:
        faculty_list = ['TBD']
    
    sessions = []
    slots_needed = calculate_slots_needed(course.ltpsc)
    
    # Get available time slots
    available_slots = get_available_time_slots(course.semester, occupied_slots, course.code, sections[0].label, period)
    
    if len(available_slots) < slots_needed['lectures']:
        print(f"WARNING: Not enough slots for synchronized {course.code}")
        return sessions
    
    # Track which days are used for lectures/tutorials
    used_days_lectures = set()
    used_days_tutorials = set()
    
    # Schedule synchronized lectures
    lecture_count = 0
    for slot in available_slots:
        if lecture_count >= slots_needed['lectures']:
            break
            
        if slot.day in used_days_lectures:
            continue  # One lecture per day rule
        
        # CRITICAL: Check elective basket conflict for lecture slot
        # For synchronized sessions, slots from get_available_time_slots should already be checked
        # But we verify here as well to be safe
        if check_elective_conflict(slot.day, slot.start, slot.end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # Check if ANY faculty assigned to these sections would conflict
        # For synchronized sessions, we check all assigned faculty before scheduling
        faculty_conflict_found = False
        if all_sessions:
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            for section_idx, section in enumerate(sections):
                if len(faculty_list) > section_idx:
                    assigned_faculty = faculty_list[section_idx]
                elif len(faculty_list) == 1:
                    assigned_faculty = faculty_list[0]
                else:
                    assigned_faculty = faculty_list[0]
                
                if assigned_faculty and assigned_faculty != 'TBD':
                    if not check_faculty_availability_in_period(
                        assigned_faculty, slot.day, slot.start, slot.end, period, all_sessions
                    ):
                        faculty_conflict_found = True
                        break
        
        if faculty_conflict_found:
            continue  # Skip this slot - at least one faculty already busy
            
        # Assign different rooms to each section
        room_index = 0
        for section_idx, section in enumerate(sections):
            if room_index >= len(classrooms):
                break
                
            # Assign faculty to this section
            if len(faculty_list) > section_idx:
                assigned_faculty = faculty_list[section_idx]
            elif len(faculty_list) == 1:
                assigned_faculty = faculty_list[0]
            else:
                assigned_faculty = faculty_list[0]  # Fallback to first faculty
                
            room = assign_room_for_session("L", 85, classrooms, room_occupancy, slot)
            if room:
                session = ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="L",
                    block=slot,
                    room=room,
                    period=period,
                    faculty=assigned_faculty
                )
                sessions.append(session)
                room_index += 1
        
        if room_index > 0:  # If at least one section was scheduled
            used_days_lectures.add(slot.day)
            lecture_count += 1
    
    # Schedule tutorials (can be synchronized or different times)
    tutorial_count = 0
    for slot in available_slots:
        if tutorial_count >= slots_needed['tutorials']:
            break
            
        if slot.day in used_days_tutorials or slot.day in used_days_lectures:
            continue  # Avoid days with lectures
            
        # Tutorials are EXACTLY 1 hour - create new TimeBlock with correct duration
        tutorial_end = time(slot.start.hour + 1, slot.start.minute)
        
        # CRITICAL: Validate time range (9:00-18:00)
        if not validate_time_range(slot.start, tutorial_end):
            continue  # Skip this slot - extends beyond 18:00
        
        tutorial_slot = TimeBlock(slot.day, slot.start, tutorial_end)
        
        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, tutorial_end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # Check if ANY faculty assigned to these sections would conflict
        faculty_conflict_found = False
        if all_sessions:
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            for section_idx, section in enumerate(sections):
                if len(faculty_list) > section_idx:
                    assigned_faculty = faculty_list[section_idx]
                elif len(faculty_list) == 1:
                    assigned_faculty = faculty_list[0]
                else:
                    assigned_faculty = faculty_list[0]
                
                if assigned_faculty and assigned_faculty != 'TBD':
                    if not check_faculty_availability_in_period(
                        assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions
                    ):
                        faculty_conflict_found = True
                        break
        
        if faculty_conflict_found:
            continue  # Skip this slot - at least one faculty already busy
        
        # Assign rooms to each section
        room_index = 0
        for section_idx, section in enumerate(sections):
            if room_index >= len(classrooms):
                break
                
            # Assign faculty to this section
            if len(faculty_list) > section_idx:
                assigned_faculty = faculty_list[section_idx]
            elif len(faculty_list) == 1:
                assigned_faculty = faculty_list[0]
            else:
                assigned_faculty = faculty_list[0]  # Fallback to first faculty
                
            room = assign_room_for_session("T", 85, classrooms, room_occupancy, tutorial_slot)
            if room:
                session = ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="T",
                    block=tutorial_slot,
                    room=room,
                    period=period,
                    faculty=assigned_faculty
                )
                sessions.append(session)
                room_index += 1
        
        if room_index > 0:
            used_days_tutorials.add(slot.day)
            tutorial_count += 1
    
    # Schedule practicals (labs can be on same day)
    practical_count = 0
    lab_number = 1
    for slot in available_slots:
        if practical_count >= slots_needed['practicals']:
            break
            
        # Practicals are 2 hours
        practical_end = time(slot.start.hour + 2, slot.start.minute)
        
        # CRITICAL: Validate time range (9:00-18:00)
        # For 2-hour practicals, ensure start time allows completion before 18:00
        # Practical starting at 16:30 would end at 18:30, which is invalid
        # Practical starting at 17:00 would end at 19:00, which is invalid
        if not validate_time_range(slot.start, practical_end):
            continue  # Skip this slot - extends beyond 18:00
        
        # Additional check: For 2-hour practicals, start must be <= 16:00
        if slot.start.hour > 16 or (slot.start.hour == 16 and slot.start.minute > 0):
            continue  # Skip - practical would extend beyond 18:00
        
        practical_slot = TimeBlock(slot.day, slot.start, practical_end)
        
        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, practical_end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # Assign labs to each section
        room_index = 0
        for section_idx, section in enumerate(sections):
            if room_index >= len(classrooms):
                break
                
            # Assign faculty to this section
            if len(faculty_list) > section_idx:
                assigned_faculty = faculty_list[section_idx]
            elif len(faculty_list) == 1:
                assigned_faculty = faculty_list[0]
            else:
                assigned_faculty = faculty_list[0]  # Fallback to first faculty
                
            lab = assign_room_for_session("P", 40, classrooms, room_occupancy, practical_slot)
            
            # Fallback: If no lab found, try to find any available lab room
            if not lab:
                # Try to find any lab room that's available (even if occupied_rooms check fails)
                all_labs = [room for room in classrooms 
                           if (room.room_type.lower() == 'lab' or 
                               str(room.room_number).upper().startswith('L'))]
                for lab_room in all_labs:
                    # Check if room is free
                    if lab_room.room_number not in room_occupancy:
                        lab = lab_room.room_number
                        break
                    else:
                        conflicts = False
                        for occupied_block in room_occupancy[lab_room.room_number]:
                            if practical_slot.overlaps(occupied_block):
                                conflicts = True
                                break
                        if not conflicts:
                            lab = lab_room.room_number
                            break
            
            if lab:
                session = ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="P",
                    block=practical_slot,
                    room=lab,
                    period=period,
                    lab_number=f"LAB{lab_number}",
                    faculty=None  # Labs don't need faculty
                )
                sessions.append(session)
                room_index += 1
            else:
                # Log warning but continue - this should be rare with improved fallback
                print(f"WARNING: Could not assign lab room for {course.code} {section.label} {period} practical at {practical_slot}")
        
        if room_index > 0:
            practical_count += 1
            lab_number += 1
    
    return sessions

def get_room_priority(capacity_needed: int) -> List[int]:
    """Get room capacity priority list for given capacity"""
    # For 85-student sections, prioritize rooms closest to 85
    if capacity_needed <= 85:
        return [85, 90, 95, 100, 110, 120, 130, 150, 200, 240]
    elif capacity_needed <= 100:
        return [100, 110, 120, 130, 150, 200, 240, 85, 90, 95]
    elif capacity_needed <= 120:
        return [120, 130, 150, 200, 240, 100, 110, 85, 90, 95]
    else:
        return [240, 200, 150, 130, 120, 110, 100, 95, 90, 85]

def assign_room_for_session(session_type: str, capacity_needed: int, 
                            classrooms: List[ClassRoom], 
                            occupied_rooms: Dict[str, List[TimeBlock]],
                            time_block: TimeBlock) -> Optional[str]:
    """Assign room based on capacity and availability"""
    
    if session_type == "P":  # Practical - need lab
        # Strategy: Try multiple fallback approaches to find a lab room
        # 1. First try: Exact match (room_type='lab', capacity=40)
        labs_exact = [room for room in classrooms if room.room_type.lower() == 'lab' and room.capacity == 40]
        available_labs = []
        
        for lab in labs_exact:
            if lab.room_number not in occupied_rooms:
                available_labs.append(lab.room_number)
            else:
                # Check if lab is free during this time
                conflicts = False
                for occupied_block in occupied_rooms[lab.room_number]:
                    if time_block.overlaps(occupied_block):
                        conflicts = True
                        break
                if not conflicts:
                    available_labs.append(lab.room_number)
        
        # 2. Fallback: Any lab with room_type='lab' (regardless of capacity)
        if not available_labs:
            labs_any = [room for room in classrooms if room.room_type.lower() == 'lab']
            for lab in labs_any:
                if lab.room_number not in occupied_rooms:
                    available_labs.append(lab.room_number)
                else:
                    conflicts = False
                    for occupied_block in occupied_rooms[lab.room_number]:
                        if time_block.overlaps(occupied_block):
                            conflicts = True
                            break
                    if not conflicts:
                        available_labs.append(lab.room_number)
        
        # 3. Final fallback: Any room with room_number starting with 'L' (lab naming convention)
        if not available_labs:
            labs_by_name = [room for room in classrooms if str(room.room_number).upper().startswith('L')]
            for lab in labs_by_name:
                if lab.room_number not in occupied_rooms:
                    available_labs.append(lab.room_number)
                else:
                    conflicts = False
                    for occupied_block in occupied_rooms[lab.room_number]:
                        if time_block.overlaps(occupied_block):
                            conflicts = True
                            break
                    if not conflicts:
                        available_labs.append(lab.room_number)
        
        # Return first available lab (we'll assign second one separately)
        # If still no lab found, return None (will be handled by caller)
        return available_labs[0] if available_labs else None
    
    else:  # Lecture or Tutorial - need classroom
        # Get room priority based on capacity needed
        priority_capacities = get_room_priority(capacity_needed)
        
        # First try rooms with exact capacity match
        for target_capacity in priority_capacities:
            suitable_rooms = [room for room in classrooms 
                            if room.room_type.lower() == 'classroom' 
                            and room.capacity == target_capacity]
            
            for room in suitable_rooms:
                if room.room_number not in occupied_rooms:
                    return room.room_number
                else:
                    # Check if room is free during this time
                    conflicts = False
                    for occupied_block in occupied_rooms[room.room_number]:
                        if time_block.overlaps(occupied_block):
                            conflicts = True
                            break
                    if not conflicts:
                        return room.room_number
        
        # If no exact match, try any available classroom
        all_classrooms = [room for room in classrooms 
                         if room.room_type.lower() == 'classroom' 
                         and room.capacity >= capacity_needed]
        
        for room in all_classrooms:
            if room.room_number not in occupied_rooms:
                return room.room_number
            else:
                # Check if room is free during this time
                conflicts = False
                for occupied_block in occupied_rooms[room.room_number]:
                    if time_block.overlaps(occupied_block):
                        conflicts = True
                        break
                if not conflicts:
                    return room.room_number
        
        return None

def get_elective_basket_slots(semester: int) -> List[TimeBlock]:
    """Get elective basket time slots for a semester to avoid conflicts (all groups)"""
    try:
        from modules_v2.phase3_elective_baskets_v2 import ELECTIVE_BASKET_SLOTS
        
        # Helper to extract semester from group key
        def extract_semester_from_group(gk: str) -> int:
            try:
                if '.' in str(gk):
                    return int(str(gk).split('.')[0])
                else:
                    return int(gk)
            except (ValueError, AttributeError):
                return -1
        
        # Find all groups for this semester
        matching_groups = [gk for gk in ELECTIVE_BASKET_SLOTS.keys() 
                          if extract_semester_from_group(gk) == semester]
        
        all_slots = []
        for group_key in matching_groups:
            slots = ELECTIVE_BASKET_SLOTS[group_key]
            all_slots.extend([
                slots.get('lecture_1'),
                slots.get('lecture_2'),
                slots.get('tutorial')
            ])
        
        # Filter out None values
        return [s for s in all_slots if s is not None]
    except:
        pass
    return []

def check_elective_conflict(day: str, start: time, end: time, semester: int) -> bool:
    """Check if time slot conflicts with elective basket slots"""
    elective_slots = get_elective_basket_slots(semester)
    test_block = TimeBlock(day, start, end)
    for elective_block in elective_slots:
        if elective_block.day == day and test_block.overlaps(elective_block):
            return True  # Conflict found
    return False  # No conflict

def generate_dynamic_time_slots(semester: int, start_hour: int = 9, end_hour: int = 18) -> List[TimeBlock]:
    """
    Dynamically generate time slots based on:
    - Available time window (default 9:00-18:00)
    - Lunch break times (semester-specific, only hardcoded exception)
    - 15-minute intervals
    - Multiple session durations (1h, 1.5h, 2h)
    """
    
    # Lunch times (only hardcoded exception as per requirements)
    lunch_blocks = {
        1: (time(12, 30), time(13, 30)),  # Sem 1: 12:30-13:30
        3: (time(12, 45), time(13, 45)),  # Sem 3: 12:45-13:45
        5: (time(13, 0), time(14, 0))     # Sem 5: 13:00-14:00
    }
    lunch_start, lunch_end = lunch_blocks.get(semester, (time(12, 30), time(13, 30)))
    
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    base_slots = []
    
    # Generate slots efficiently - only create practical slots (not every 15 min)
    # Morning slots (before lunch): 9:00, 9:15, 10:30, 10:45, 11:00
    # Afternoon slots (after lunch): 14:00, 14:15, 15:00, 15:15, 15:45, 16:00, 16:30, 17:00
    
    morning_start_times = [
        time(9, 0), time(9, 15), time(10, 30), time(10, 45), time(11, 0)
    ]
    afternoon_start_times = [
        time(14, 0), time(14, 15), time(15, 0), time(15, 15), time(15, 45), 
        time(16, 0), time(16, 30), time(17, 0)
    ]
    
    for day in days:
        # Morning 1.5h lecture slots
        for start in morning_start_times:
            if start < lunch_start:
                end_dt = datetime.combine(datetime.min, start) + timedelta(minutes=90)
                end = end_dt.time()
                if end <= lunch_start:
                    base_slots.append(TimeBlock(day, start, end))
        
        # Morning 1h tutorial slots
        for start in morning_start_times:
            if start < lunch_start:
                end_dt = datetime.combine(datetime.min, start) + timedelta(minutes=60)
                end = end_dt.time()
                if end <= lunch_start:
                    base_slots.append(TimeBlock(day, start, end))
        
        # Afternoon 1.5h lecture slots
        for start in afternoon_start_times:
            if start >= lunch_end:
                end_dt = datetime.combine(datetime.min, start) + timedelta(minutes=90)
                end = end_dt.time()
                if end.hour <= end_hour:
                    base_slots.append(TimeBlock(day, start, end))
        
        # Afternoon 1h tutorial slots
        for start in afternoon_start_times:
            if start >= lunch_end:
                end_dt = datetime.combine(datetime.min, start) + timedelta(minutes=60)
                end = end_dt.time()
                if end.hour <= end_hour:
                    base_slots.append(TimeBlock(day, start, end))
        
        # Afternoon 2h practical slots
        for start in afternoon_start_times:
            if start >= lunch_end:
                end_dt = datetime.combine(datetime.min, start) + timedelta(minutes=120)
                end = end_dt.time()
                if end.hour <= end_hour:
                    base_slots.append(TimeBlock(day, start, end))
    
    return base_slots

def get_available_time_slots(semester: int, occupied_slots: Dict[str, List[TimeBlock]], 
                             course_code: str, section: str, period: str) -> List[TimeBlock]:
    """Get available time slots for a course, avoiding conflicts - FULLY DYNAMIC"""
    
    # DEBUG: Track rejected slots
    rejected_slots = []
    lunch_rejected = 0
    overlap_rejected = 0
    
    # Generate time slots dynamically (no hardcoded values except lunch)
    base_slots = generate_dynamic_time_slots(semester, start_hour=9, end_hour=18)
    
    # Lunch times
    lunch_blocks = {
        1: TimeBlock("Monday", time(12, 30), time(13, 30)),
        3: TimeBlock("Monday", time(12, 45), time(13, 45)),
        5: TimeBlock("Monday", time(13, 0), time(14, 0))
    }
    lunch_block = lunch_blocks.get(semester, TimeBlock("Monday", time(12, 30), time(13, 30)))
    
    # DYNAMIC: Shuffle slots for variety
    shuffled_slots = base_slots.copy()
    random.shuffle(shuffled_slots)
    
    available_slots = []
    for slot in shuffled_slots:
        # Check lunch conflicts - use overlaps_with_lunch method
        lunch_conflict = False
        # Create lunch block for this day
        day_lunch = TimeBlock(slot.day, lunch_block.start, lunch_block.end)
        if slot.overlaps(day_lunch):
                lunch_conflict = True
                lunch_rejected += 1
                rejected_slots.append((slot, 'LUNCH_CONFLICT'))
        
        if lunch_conflict:
            continue
        
        # CRITICAL: Check elective basket conflict
        if check_elective_conflict(slot.day, slot.start, slot.end, semester):
            continue  # Skip this slot to avoid elective conflict
            
        # No lunch buffer - classes can be scheduled right after lunch
            
        # Check conflicts with existing sessions for this section
        conflict = False
        conflict_reason = None
        for existing_data in occupied_slots.get(f"{section}_{period}", []):
            if isinstance(existing_data, tuple):
                existing_slot, existing_course = existing_data
            else:
                existing_slot = existing_data  # Handle old format
            if slot.overlaps(existing_slot):
                conflict = True
                conflict_reason = f'OVERLAP_{existing_course if isinstance(existing_data, tuple) else "UNKNOWN"}'
                overlap_rejected += 1
                rejected_slots.append((slot, conflict_reason))
                break
        
        # ENFORCE ONE-DAY-ONE-SESSION RULE: Check if this course already has a session on this day
        if not conflict and course_code:
            course_code_str = str(course_code)
            for other_key, other_slots in occupied_slots.items():
                # Check if same course already scheduled on this day for this section (same period only)
                if other_key == f"{section}_{period}":  # Same section, same period only
                    for existing_data in other_slots:
                        if isinstance(existing_data, tuple):
                            existing_slot, existing_course = existing_data
                            # Check if same course already scheduled on this day
                            if existing_course == course_code_str and existing_slot.day == slot.day:
                                conflict = True
                                conflict_reason = f'ONE_DAY_ONE_SESSION_{existing_course}'
                                overlap_rejected += 1
                                rejected_slots.append((slot, conflict_reason))
                                break
                        else:
                            # Handle old format - check if same day
                            if existing_data.day == slot.day:
                                conflict = True
                                conflict_reason = 'ONE_DAY_ONE_SESSION_OLD_FORMAT'
                                overlap_rejected += 1
                                rejected_slots.append((slot, conflict_reason))
                                break
                    if conflict:
                        break
        
        # Check conflicts with other sections of the same course AND within same section
        # BUT ONLY for the same section (not across different departments)
        if not conflict:
            course_code_str = str(course_code) if course_code else ""
            for other_key, other_slots in occupied_slots.items():
                # Only check conflicts within the same section, not across departments
                if other_key == f"{section}_{period}":
                    for existing_data in other_slots:
                        if isinstance(existing_data, tuple):
                            existing_slot, existing_course = existing_data
                            # Check if same course at same time within the same section
                            if existing_course == course_code_str and slot.overlaps(existing_slot):
                                conflict = True
                                conflict_reason = f'INTRA_SECTION_{existing_course}'
                                overlap_rejected += 1
                                rejected_slots.append((slot, conflict_reason))
                                break
                        else:
                            # Handle old format - just check time overlap within same section
                            if slot.overlaps(existing_data):
                                conflict = True
                                conflict_reason = 'INTRA_SECTION_OLD_FORMAT'
                                overlap_rejected += 1
                                rejected_slots.append((slot, conflict_reason))
                                break
                    if conflict:
                        break
        
        if not conflict:
            available_slots.append(slot)
    
    # DEBUG: Print diagnostic info for problematic cases
    if len(available_slots) < 3:
        print(f"DEBUG: {section}_{period} {course_code} - Only {len(available_slots)} slots available")
        print(f"  Total base slots: {len(base_slots)}")
        print(f"  Rejected by lunch: {lunch_rejected}")
        print(f"  Rejected by overlaps: {overlap_rejected}")
        print(f"  Available slots:")
        for slot in available_slots:
            print(f"    {slot.day} {slot.start}-{slot.end}")
        print(f"  Sample rejected slots:")
        for slot, reason in rejected_slots[:5]:  # Show first 5 rejected
            print(f"    {slot.day} {slot.start}-{slot.end} ({reason})")
    
    return available_slots

def schedule_course_sessions(course: Course, section: Section, period: str, 
                           occupied_slots: Dict[str, List[TimeBlock]],
                           classrooms: List[ClassRoom], room_occupancy: Dict[str, List[TimeBlock]] = None,
                           section_idx: int = 0,
                           max_lectures: int = None, max_tutorials: int = None, max_practicals: int = None,
                           all_sessions: List = None) -> List[ScheduledSession]:
    """Schedule sessions for a course following one-per-day rule
    
    Args:
        max_lectures: Maximum number of lectures to schedule (None = schedule all required)
        max_tutorials: Maximum number of tutorials to schedule (None = schedule all required)
        max_practicals: Maximum number of practicals to schedule (None = schedule all required)
    """
    
    # Parse faculty list and assign to this section
    faculty_list = []
    if hasattr(course, 'instructors') and course.instructors:
        # Use the instructors list directly
        faculty_list = course.instructors
    else:
        faculty_list = ['TBD']
    
    # Determine which faculty for this section
    if len(faculty_list) > section_idx:
        assigned_faculty = faculty_list[section_idx]
    elif len(faculty_list) == 1:
        assigned_faculty = faculty_list[0]
    else:
        print(f"ERROR: Not enough faculty for {course.code}. Sections: {section_idx+1}, Faculty: {len(faculty_list)}")
        assigned_faculty = faculty_list[0]  # Fallback to first faculty
    
    sessions = []
    slots_needed = calculate_slots_needed(course.ltpsc)
    
    # Apply limits if provided
    # If max_* is None, schedule full requirement. If 0, schedule 0. Otherwise, use the limit.
    if max_lectures is None:
        target_lectures = slots_needed['lectures']
    else:
        target_lectures = max(0, min(slots_needed['lectures'], max_lectures))
    
    if max_tutorials is None:
        target_tutorials = slots_needed['tutorials']
    else:
        target_tutorials = max(0, min(slots_needed['tutorials'], max_tutorials))
    
    if max_practicals is None:
        target_practicals = slots_needed['practicals']
    else:
        target_practicals = max(0, min(slots_needed['practicals'], max_practicals))
    
    target_total = target_lectures + target_tutorials + target_practicals
    
    # If all targets are 0, return empty list immediately
    if target_total == 0:
        return sessions
    
    # Get available time slots
    available_slots = get_available_time_slots(
        course.semester, occupied_slots, course.code, section.label, period
    )
    
    if len(available_slots) < target_total:
        # Don't fail if we're only scheduling partial requirement
        if max_lectures is None and max_tutorials is None and max_practicals is None:
            print(f"WARNING: Not enough slots for {course.code} in {section.label} {period}")
            print(f"  Required: {target_total} slots (L:{target_lectures}, T:{target_tutorials}, P:{target_practicals})")
            print(f"  Available: {len(available_slots)} slots")
            return sessions
    
    # Track which days are used for lectures/tutorials (one per day)
    # Labs can be on same day, so track separately
    # Use session rules validator to track properly
    used_days_by_course = SessionRulesValidator.get_used_days_tracker()
    used_days_lectures = set()
    used_days_tutorials = set()
    
    # Schedule lectures
    lecture_count = 0
    for slot in available_slots:
        if lecture_count >= target_lectures:
            break
            
        # Check one-session-per-day rule - check BOTH validators
        if slot.day in used_days_lectures:
            continue  # One lecture per day rule (already scheduled a lecture today)
        
        if not SessionRulesValidator.can_schedule_session_type(
            course.code, slot.day, "L", used_days_by_course
        ):
            continue  # Cannot schedule lecture on this day (already has lecture or tutorial)
            
        # Ensure lecture slot is at least 1.5 hours
        slot_duration = (slot.end.hour * 60 + slot.end.minute) - (slot.start.hour * 60 + slot.start.minute)
        if slot_duration < 90:
            continue
        
        # Build 1.5-hour lecture block
        end_hour = slot.start.hour + 1
        end_minute = slot.start.minute + 30
        if end_minute >= 60:
            end_hour += 1
            end_minute -= 60
        lecture_end = time(end_hour, end_minute)
        
        # CRITICAL: Validate time range (9:00-18:00)
        if not validate_time_range(slot.start, lecture_end):
            continue  # Skip this slot - extends beyond 18:00
        
        lecture_block = TimeBlock(slot.day, slot.start, lecture_end)
        
        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, lecture_end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
        if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            if not check_faculty_availability_in_period(
                assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions
            ):
                continue  # Skip this slot - faculty already busy at this time in this period
        
        # Check if this lecture block conflicts with already scheduled sessions
        conflict = False
        for existing_session in sessions:
            if lecture_block.overlaps(existing_session.block):
                conflict = True
                break
        
        if conflict:
            continue
            
        # Assign room with proper room tracking
        room = assign_room_for_session("L", 85, classrooms, room_occupancy or {}, lecture_block)
        if room:
            session = ScheduledSession(
                course_code=course.code,
                section=section.label,
                kind="L",
                block=lecture_block,
                room=room,
                period=period,
                faculty=assigned_faculty
            )
            sessions.append(session)
            used_days_lectures.add(slot.day)
            # Mark day as used in validator
            SessionRulesValidator.mark_day_used(course.code, slot.day, "L", used_days_by_course)
            lecture_count += 1

    # Fallback: if lectures are still missing, relax the per-course validator (but still avoid time overlaps).
    # This helps prevent Phase 5 courses from being UNSATISFIED due to overly strict one-session-per-day rules.
    if lecture_count < target_lectures:
        # Pass 1: allow lectures on days that already have this course's tutorial, but keep 1 lecture/day
        for slot in available_slots:
            if lecture_count >= target_lectures:
                break

            if slot.day in used_days_lectures:
                continue  # keep at most one lecture per day in this pass

            slot_duration = (slot.end.hour * 60 + slot.end.minute) - (slot.start.hour * 60 + slot.start.minute)
            if slot_duration < 90:
                continue

            end_hour = slot.start.hour + 1
            end_minute = slot.start.minute + 30
            if end_minute >= 60:
                end_hour += 1
                end_minute -= 60
            lecture_end = time(end_hour, end_minute)

            if not validate_time_range(slot.start, lecture_end):
                continue

            lecture_block = TimeBlock(slot.day, slot.start, lecture_end)

            if check_elective_conflict(slot.day, slot.start, lecture_end, course.semester):
                continue

            # Check faculty availability
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions
                ):
                    continue

            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("L", 85, classrooms, room_occupancy or {}, lecture_block)
            if room:
                sessions.append(ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="L",
                    block=lecture_block,
                    room=room,
                    period=period,
                    faculty=assigned_faculty
                ))
                used_days_lectures.add(slot.day)
                SessionRulesValidator.mark_day_used(course.code, slot.day, "L", used_days_by_course)
                lecture_count += 1

    if lecture_count < target_lectures:
        # Pass 2 (last resort): allow multiple lectures on same day (different times) if still missing
        for slot in available_slots:
            if lecture_count >= target_lectures:
                break

            slot_duration = (slot.end.hour * 60 + slot.end.minute) - (slot.start.hour * 60 + slot.start.minute)
            if slot_duration < 90:
                continue

            end_hour = slot.start.hour + 1
            end_minute = slot.start.minute + 30
            if end_minute >= 60:
                end_hour += 1
                end_minute -= 60
            lecture_end = time(end_hour, end_minute)

            if not validate_time_range(slot.start, lecture_end):
                continue

            lecture_block = TimeBlock(slot.day, slot.start, lecture_end)

            if check_elective_conflict(slot.day, slot.start, lecture_end, course.semester):
                continue

            # Check faculty availability
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions
                ):
                    continue

            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("L", 85, classrooms, room_occupancy or {}, lecture_block)
            if room:
                sessions.append(ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="L",
                    block=lecture_block,
                    room=room,
                    period=period,
                    faculty=assigned_faculty
                ))
                # do not enforce used_days_lectures uniqueness here
                SessionRulesValidator.mark_day_used(course.code, slot.day, "L", used_days_by_course)
                lecture_count += 1
    
    # Schedule tutorials
    tutorial_count = 0
    for slot in available_slots:
        if tutorial_count >= target_tutorials:
            break
            
        # Skip if day already used for lectures or tutorials
        if slot.day in used_days_tutorials or slot.day in used_days_lectures:
            continue
            
        # Check one-session-per-day rule - check BOTH validators
        if not SessionRulesValidator.can_schedule_session_type(
            course.code, slot.day, "T", used_days_by_course
        ):
            continue  # Cannot schedule tutorial on this day (already has lecture or tutorial)
        
        # Create 1-hour tutorial slot from the available slot
        # Check if slot duration is >= 1 hour
        slot_duration = (slot.end.hour * 60 + slot.end.minute) - (slot.start.hour * 60 + slot.start.minute)
        if slot_duration < 60:
            continue  # Skip slots shorter than 1 hour
        
        # Create 1-hour tutorial slot
        tutorial_end = time(slot.start.hour + 1, slot.start.minute)
        
        # CRITICAL: Validate time range (9:00-18:00)
        if not validate_time_range(slot.start, tutorial_end):
            continue  # Skip this slot - extends beyond 18:00
        
        tutorial_slot = TimeBlock(slot.day, slot.start, tutorial_end)
        
        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, tutorial_end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
        if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            if not check_faculty_availability_in_period(
                assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions
            ):
                continue  # Skip this slot - faculty already busy at this time in this period
        
        # Check conflicts
        conflict = False
        for existing_session in sessions:
            if tutorial_slot.overlaps(existing_session.block):
                conflict = True
                break
        
        if conflict:
            continue
        
        # Assign room
        room = assign_room_for_session("T", 85, classrooms, room_occupancy or {}, tutorial_slot)
        if room:
            session = ScheduledSession(
                course_code=course.code,
                section=section.label,
                kind="T",
                block=tutorial_slot,
                room=room,
                period=period,
                faculty=assigned_faculty
            )
            sessions.append(session)
            used_days_tutorials.add(slot.day)
            # Mark day as used in validator
            SessionRulesValidator.mark_day_used(course.code, slot.day, "T", used_days_by_course)
            tutorial_count += 1

    # Fallback: if tutorials are still missing, relax the "no lecture day" rule for this course.
    # This prevents Phase 5 courses from being UNSATISFIED due to lack of a free tutorial day.
    # We still avoid time overlaps with this course’s already scheduled sessions and elective conflicts.
    if tutorial_count < target_tutorials:
        for slot in available_slots:
            if tutorial_count >= target_tutorials:
                break

            # Allow tutorial on a lecture day, but do not place two tutorials on same day
            if slot.day in used_days_tutorials:
                continue

            # Slot must be at least 1 hour
            slot_duration = (slot.end.hour * 60 + slot.end.minute) - (slot.start.hour * 60 + slot.start.minute)
            if slot_duration < 60:
                continue

            tutorial_end = time(slot.start.hour + 1, slot.start.minute)
            if not validate_time_range(slot.start, tutorial_end):
                continue

            tutorial_slot = TimeBlock(slot.day, slot.start, tutorial_end)

            if check_elective_conflict(slot.day, slot.start, tutorial_end, course.semester):
                continue

            # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions
                ):
                    continue  # Skip this slot - faculty already busy at this time in this period

            # Avoid overlaps with already scheduled sessions of this course in this period/section
            conflict = False
            for existing_session in sessions:
                if tutorial_slot.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("T", 85, classrooms, room_occupancy or {}, tutorial_slot)
            if room:
                session = ScheduledSession(
                    course_code=course.code,
                    section=section.label,
                    kind="T",
                    block=tutorial_slot,
                    room=room,
                    period=period,
                    faculty=assigned_faculty
                )
                sessions.append(session)
                used_days_tutorials.add(slot.day)
                # Mark day as used in validator as a tutorial day (best-effort)
                SessionRulesValidator.mark_day_used(course.code, slot.day, "T", used_days_by_course)
                tutorial_count += 1
    
    # Schedule practicals
    practical_count = 0
    lab_number = 1
    for slot in available_slots:
        if practical_count >= target_practicals:
            break
            
        # Check if this slot conflicts with already scheduled sessions
        conflict = False
        for existing_session in sessions:
            if slot.overlaps(existing_session.block):
                conflict = True
                break
        
        if conflict:
            continue
            
        # Labs can be on same day - no day restriction for practicals
            
        # Practicals are 2 hours, adjust slot
        practical_end = time(slot.start.hour + 2, slot.start.minute)
        
        # CRITICAL: Validate time range (9:00-18:00)
        # For 2-hour practicals, ensure start time allows completion before 18:00
        # Practical starting at 16:30 would end at 18:30, which is invalid
        # Practical starting at 17:00 would end at 19:00, which is invalid
        if not validate_time_range(slot.start, practical_end):
            continue  # Skip this slot - extends beyond 18:00
        
        # Additional check: For 2-hour practicals, start must be <= 16:00
        if slot.start.hour > 16 or (slot.start.hour == 16 and slot.start.minute > 0):
            continue  # Skip - practical would extend beyond 18:00
        
        practical_slot = TimeBlock(slot.day, slot.start, practical_end)
        
        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, practical_end, course.semester):
            continue  # Skip this slot to avoid elective conflict
        
        # CRITICAL: Check if practical slot would overlap with lunch
        # Get semester lunch time
        lunch_blocks_dict = get_lunch_blocks()
        lunch_base = lunch_blocks_dict.get(course.semester)
        if lunch_base:
            day_lunch = TimeBlock(slot.day, lunch_base.start, lunch_base.end)
            if practical_slot.overlaps(day_lunch):
                continue  # Skip this slot - practical would overlap with lunch
        
        # Assign lab
        lab = assign_room_for_session("P", 40, classrooms, room_occupancy or {}, practical_slot)
        
        # Fallback: If no lab found, try to find any available lab room
        if not lab:
            # Try to find any lab room that's available
            all_labs = [room for room in classrooms 
                       if (room.room_type.lower() == 'lab' or 
                           str(room.room_number).upper().startswith('L'))]
            room_occ = room_occupancy or {}
            for lab_room in all_labs:
                # Check if room is free
                if lab_room.room_number not in room_occ:
                    lab = lab_room.room_number
                    break
                else:
                    conflicts = False
                    for occupied_block in room_occ[lab_room.room_number]:
                        if practical_slot.overlaps(occupied_block):
                            conflicts = True
                            break
                    if not conflicts:
                        lab = lab_room.room_number
                        break
        
        if lab:
            session = ScheduledSession(
                course_code=course.code,
                section=section.label,
                kind="P",
                block=practical_slot,
                room=lab,
                period=period,
                lab_number=f"LAB{lab_number}",
                faculty=None
            )
            sessions.append(session)
            practical_count += 1
            lab_number += 1
        else:
            # Log warning but continue - this should be rare with improved fallback
            print(f"WARNING: Could not assign lab room for {course.code} {section.label} {period} practical at {practical_slot}")
    
    # Verify all target sessions were scheduled
    if lecture_count < target_lectures:
        print(f"WARNING: {course.code} in {section.label} {period} - Only scheduled {lecture_count}/{target_lectures} lectures")
    if tutorial_count < target_tutorials:
        print(f"WARNING: {course.code} in {section.label} {period} - Only scheduled {tutorial_count}/{target_tutorials} tutorials")
    if practical_count < target_practicals:
        print(f"WARNING: {course.code} in {section.label} {period} - Only scheduled {practical_count}/{target_practicals} practicals")
    
    # CRITICAL: For CS161, log detailed information if not all sessions scheduled
    if course.code == 'CS161':
        total_scheduled = lecture_count + tutorial_count + practical_count
        total_required = target_lectures + target_tutorials + target_practicals
        if total_scheduled < total_required:
            print(f"ERROR: CS161 in {section.label} {period} - Missing sessions!")
            print(f"  Required: {total_required} (L:{target_lectures}, T:{target_tutorials}, P:{target_practicals})")
            print(f"  Scheduled: {total_scheduled} (L:{lecture_count}, T:{tutorial_count}, P:{practical_count})")
            print(f"  Available slots: {len(available_slots)}")
    
    return sessions

def run_phase5(courses: List[Course], sections: List[Section], classrooms: List[ClassRoom],
               elective_sessions: List[ScheduledSession], 
               combined_sessions: List[ScheduledSession]) -> List[ScheduledSession]:
    """Run Phase 5: Schedule core courses with credits > 2"""
    
    print("=== PHASE 5: CORE COURSES SCHEDULING (Credits > 2) ===")
    
    # Filter courses for Phase 5 - CRITICAL: Only core courses with >2 credits
    phase5_courses = []
    seen = {}  # Track by (code, department, semester, credits) to avoid duplicates
    
    for course in courses:
        # Must be core (not elective)
        if course.is_elective:
            continue
        
        # Must have >2 credits (this excludes 2-credit electives like "Introduction to C Programming")
        if course.credits <= 2:
            continue
        
        # Must not be combined
        if course.is_combined:
            continue
        
        # Deduplicate by code+dept+sem+credits to avoid picking up wrong course with same code
        key = (course.code, course.department, course.semester, course.credits)
        if key not in seen:
            seen[key] = course
            phase5_courses.append(course)
            print(f"  Phase 5 course: {course.code} ({course.name}) - {course.credits} credits - {course.department} Sem{course.semester}")
    
    print(f"Found {len(phase5_courses)} Phase 5 courses to schedule")
    
    # Group courses by semester and department
    courses_by_sem_dept = defaultdict(list)
    for course in phase5_courses:
        key = (course.semester, course.department)
        courses_by_sem_dept[key].append(course)
    
    # Create occupied slots map from existing sessions
    occupied_slots = defaultdict(list)
    
    # Phase 4 occupied slots are already included in combined_sessions
    # No need for additional occupied_slots conversion
    
    # Add elective sessions
    for session in elective_sessions:
        if hasattr(session, 'section') and hasattr(session, 'period'):
            key = f"{session.section}_{session.period}"
            course_code = getattr(session, 'course_code', 'ELECTIVE')
            occupied_slots[key].append((session.block, course_code))
    
    # Add combined sessions
    for session in combined_sessions:
        if isinstance(session, dict):
            # Combined sessions are dictionaries
            session_sections = session.get('sections', [])
            session_period = session.get('period', '')
            session_block = session.get('time_block')
            course_code = session.get('course_code', 'COMBINED')
            if session_block:
                # Add to occupied slots for each section
                for section in session_sections:
                    key = f"{section}_{session_period}"
                    occupied_slots[key].append((session_block, course_code))
        elif hasattr(session, 'section') and hasattr(session, 'period'):
            key = f"{session.section}_{session.period}"
            course_code = getattr(session, 'course_code', 'COMBINED')
            occupied_slots[key].append((session.block, course_code))
    
    all_phase5_sessions = []
    
    # Create room occupancy tracking
    room_occupancy = defaultdict(list)
    
    # Process each semester and department
    for (semester, department), sem_courses in courses_by_sem_dept.items():
        print(f"\nProcessing Semester {semester}, {department}: {len(sem_courses)} courses")
        
        # Get sections for this semester and department
        relevant_sections = [s for s in sections if s.semester == semester and s.program == department]
        
        # DEBUG: Print sections being processed
        print(f"DEBUG: Processing {len(relevant_sections)} sections for {department} Sem{semester}:")
        for idx, section in enumerate(relevant_sections):
            print(f"  {idx}: {section.label}")
        
        # Validate faculty counts before scheduling
        for course in sem_courses:
            faculty_list = []
            if hasattr(course, 'instructors') and course.instructors:
                faculty_list = course.instructors
            
            if len(relevant_sections) > len(faculty_list) and len(faculty_list) > 1:
                print(f"WARNING: Course {course.code} has {len(faculty_list)} faculty but {len(relevant_sections)} sections")
                print(f"         Faculty will be reused: {faculty_list}")
        
        # Schedule ALL courses section by section (no synchronization)
        total_courses = len(relevant_sections) * len(sem_courses)
        current_course = 0
        
        for section_idx, section in enumerate(relevant_sections):
            print(f"  Section {section.label}:")
            
            for course in sem_courses:
                current_course += 1
                print(f"    [{current_course}/{total_courses}] Scheduling {course.code} ({course.name})")
                
                # CRITICAL: For credits > 2 courses, schedule in BOTH PreMid and PostMid periods
                # These are full-semester courses that run in both periods
                all_course_sessions = []
                periods_scheduled = []
                slots_info = calculate_slots_needed(course.ltpsc)
                total_required = slots_info['total']
                
                # Schedule in BOTH PRE and POST periods (full semester course)
                for period in ["PRE", "POST"]:
                    # Build all_sessions list for faculty conflict checking (includes elective + combined + already scheduled)
                    # CRITICAL: Include all_course_sessions to catch conflicts with other sections of same course
                    all_sessions_for_checking = list(elective_sessions) + list(combined_sessions) + all_phase5_sessions + all_course_sessions
                    
                    # Schedule all required sessions in this period
                    sessions = schedule_course_sessions(
                        course, section, period, occupied_slots, classrooms, room_occupancy, section_idx,
                        all_sessions=all_sessions_for_checking
                    )
                    
                    if sessions:
                        all_course_sessions.extend(sessions)
                        periods_scheduled.append(period)
                        
                        # Update occupied slots and room occupancy
                        key = f"{section.label}_{period}"
                        for session in sessions:
                            occupied_slots[key].append((session.block, course.code))
                            if session.room:
                                room_occupancy[session.room].append(session.block)
                        
                        scheduled_lectures = len([s for s in sessions if s.kind=='L'])
                        scheduled_tutorials = len([s for s in sessions if s.kind=='T'])
                        scheduled_practicals = len([s for s in sessions if s.kind=='P'])
                        print(f"      {period}: {len(sessions)} sessions scheduled (L:{scheduled_lectures}, T:{scheduled_tutorials}, P:{scheduled_practicals})")
                    else:
                        print(f"      {period}: No sessions scheduled (conflicts)")
                
                # Verify we got sessions in both periods
                if len(periods_scheduled) < 2:
                    print(f"      WARNING: {course.code} only scheduled in {len(periods_scheduled)} period(s) - should be in both PRE and POST")
                
                # Add all sessions to master list
                if all_course_sessions:
                    all_phase5_sessions.extend(all_course_sessions)
                    
                    # Verify total scheduled matches requirement
                    total_scheduled = len(all_course_sessions)
                    total_sched_lectures = len([s for s in all_course_sessions if s.kind=='L'])
                    total_sched_tutorials = len([s for s in all_course_sessions if s.kind=='T'])
                    total_sched_practicals = len([s for s in all_course_sessions if s.kind=='P'])
                    
                    print(f"      TOTAL: {total_scheduled}/{total_required} sessions in {len(periods_scheduled)} period(s) (L:{total_sched_lectures}/{slots_info['lectures']}, T:{total_sched_tutorials}/{slots_info['tutorials']}, P:{total_sched_practicals}/{slots_info['practicals']})")
                    if total_scheduled < total_required:
                        print(f"      WARNING: {course.code} in {section.label} is missing {total_required - total_scheduled} session(s)")
                    
                    # CRITICAL: For CS161, log all scheduled sessions for debugging
                    if course.code == 'CS161':
                        print(f"      DEBUG CS161 {section.label}: All scheduled sessions:")
                        for s in all_course_sessions:
                            print(f"        - {s.kind} on {s.block.day} {s.block.start}-{s.block.end} in {s.room} ({s.period})")
                else:
                    # SAFETY CHECK: If no sessions scheduled, try to schedule in at least one period without restrictions
                    print(f"      ERROR: {course.code} in {section.label} - No sessions scheduled! Trying fallback...")
                    # Build all_sessions list for faculty conflict checking (include all_course_sessions for same-course conflicts)
                    all_sessions_for_checking = list(elective_sessions) + list(combined_sessions) + all_phase5_sessions + all_course_sessions
                    for period in ["PRE", "POST"]:
                        fallback_sessions = schedule_course_sessions(
                            course, section, period, occupied_slots, classrooms, room_occupancy, section_idx,
                            all_sessions=all_sessions_for_checking
                        )
                        if fallback_sessions:
                            all_phase5_sessions.extend(fallback_sessions)
                            key = f"{section.label}_{period}"
                            for session in fallback_sessions:
                                occupied_slots[key].append((session.block, course.code))
                                if session.room:
                                    room_occupancy[session.room].append(session.block)
                            print(f"      FALLBACK: {period}: {len(fallback_sessions)} sessions scheduled")
                            break
    
    print(f"\nPhase 5 completed: {len(all_phase5_sessions)} sessions scheduled")
    
    # Log time slots
    logger = get_logger()
    for session in all_phase5_sessions:
        logger.log_session("Phase 5", session)
    
    # Resolve faculty conflicts
    print("\n=== RESOLVING FACULTY CONFLICTS ===")
    all_phase5_sessions = detect_and_resolve_faculty_conflicts(
        all_phase5_sessions, occupied_slots, classrooms, room_occupancy
    )
    
    # Print time slot logging summary
    print("\nTime slot logging summary...")
    logger = get_logger()
    phase5_entries = logger.get_entries_by_phase("Phase 5")
    if phase5_entries:
        phase5_summary = logger.get_phase_summary("Phase 5")
        print(f"Phase 5 logged {phase5_summary['total_slots']} time slots")
        print(f"  - Unique courses: {phase5_summary['unique_courses']}")
        print(f"  - Unique sections: {phase5_summary['unique_sections']}")
        print(f"  - By day: {phase5_summary['by_day']}")
        print(f"  - By session type: {phase5_summary['by_session_type']}")
    
    print("\nPhase 5 completed successfully!")
    return all_phase5_sessions

def detect_and_resolve_faculty_conflicts(all_sessions: List, 
                                        occupied_slots: Dict[str, List[TimeBlock]],
                                        classrooms: List[ClassRoom],
                                        room_occupancy: Dict[str, List[TimeBlock]]) -> List:
    """Detect faculty conflicts and reschedule - IGNORE PreMid/PostMid overlaps
    Only detects conflicts within the same period (PRE vs PRE or POST vs POST)
    Detects overlapping times, not just exact same times
    Handles both ScheduledSession objects and dictionary sessions (from combined_sessions)
    """
    
    # Group sessions by faculty, day, and period (check for overlapping times)
    faculty_sessions_by_period = defaultdict(lambda: defaultdict(list))
    scheduled_session_objects = []  # Only ScheduledSession objects for rescheduling
    
    for session in all_sessions:
        # Handle both ScheduledSession objects and dictionary sessions
        if isinstance(session, dict):
            # Dictionary session (from combined_sessions) - skip for now (already scheduled in Phase 4)
            # Combined sessions are already scheduled and shouldn't be rescheduled
            continue
        elif not hasattr(session, 'kind'):
            # Not a ScheduledSession object - skip
            continue
        
        if session.kind == 'P':  # Skip lab sessions
            continue
        
        # Get faculty - handle both attribute and property access
        faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        if not faculty or faculty in ['TBD', 'Various']:
            continue
        
        period = getattr(session, 'period', 'UNKNOWN')
        
        # Only process ScheduledSession objects (skip combined_sessions dictionaries)
        # Combined sessions are already scheduled in Phase 4 and shouldn't be rescheduled
        if hasattr(session, 'block') and hasattr(session, 'course_code') and hasattr(session, 'section'):
            # This is a ScheduledSession object - can be rescheduled
            faculty_sessions_by_period[faculty][period].append(session)
            scheduled_session_objects.append(session)
    
    # Find conflicts by checking overlapping times within same period
    conflicts_to_resolve = []
    for faculty, periods_dict in faculty_sessions_by_period.items():
        for period, sessions in periods_dict.items():
            # Check each pair of sessions for overlapping times on same day
            for i, session1 in enumerate(sessions):
                for j, session2 in enumerate(sessions[i+1:], i+1):
                    # Only check if same day and overlapping times
                    if (session1.block.day == session2.block.day and 
                        session1.block.overlaps(session2.block)):
                        # Check if different courses/sections (not duplicate)
                        key1 = f"{session1.course_code}_{session1.section}"
                        key2 = f"{session2.course_code}_{session2.section}"
                        
                        if key1 != key2:
                            # Real conflict - different courses/sections, overlapping times
                            # Avoid duplicates by checking if conflict already recorded
                            conflict_key = tuple(sorted([key1, key2]))
                            conflicts_to_resolve.append({
                                'faculty': faculty,
                                'period': period,
                                'day': session1.block.day,
                                'session1': session1,
                                'session2': session2,
                                'conflict_key': conflict_key
                            })
    
    # Remove duplicate conflicts (same two sessions)
    unique_conflicts = []
    seen_conflict_keys = set()
    for conflict in conflicts_to_resolve:
        if conflict['conflict_key'] not in seen_conflict_keys:
            seen_conflict_keys.add(conflict['conflict_key'])
            unique_conflicts.append(conflict)
    
    print(f"Found {len(unique_conflicts)} real faculty conflicts to resolve (same period, overlapping times)")
    
    if len(unique_conflicts) == 0:
        print("No faculty conflicts found - all good!")
        return all_sessions
    
    # Resolve each conflict - try multiple times if needed
    max_retries = 10  # Increased retries for better resolution
    resolved_conflicts = set()
    
    for attempt in range(max_retries):
        # Re-detect conflicts after each resolution attempt
        if attempt > 0:
            # Rebuild conflict list
            faculty_sessions_by_period = defaultdict(lambda: defaultdict(list))
            for session in scheduled_session_objects:
                if session.kind == 'P':
                    continue
                faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
                if not faculty or faculty in ['TBD', 'Various']:
                    continue
                period = getattr(session, 'period', 'UNKNOWN')
                faculty_sessions_by_period[faculty][period].append(session)
            
            unique_conflicts = []
            seen_conflict_keys = set()
            for faculty, periods_dict in faculty_sessions_by_period.items():
                for period, sessions in periods_dict.items():
                    for i, session1 in enumerate(sessions):
                        for j, session2 in enumerate(sessions[i+1:], i+1):
                            if (session1.block.day == session2.block.day and 
                                session1.block.overlaps(session2.block)):
                                key1 = f"{session1.course_code}_{session1.section}"
                                key2 = f"{session2.course_code}_{session2.section}"
                                if key1 != key2:
                                    conflict_key = tuple(sorted([key1, key2]))
                                    if conflict_key not in seen_conflict_keys and conflict_key not in resolved_conflicts:
                                        seen_conflict_keys.add(conflict_key)
                                        unique_conflicts.append({
                                            'faculty': faculty,
                                            'period': period,
                                            'day': session1.block.day,
                                            'session1': session1,
                                            'session2': session2,
                                            'conflict_key': conflict_key
                                        })
        
        if not unique_conflicts:
            break  # No more conflicts
        
        print(f"Resolution attempt {attempt + 1}: {len(unique_conflicts)} conflicts remaining")
        
        # Resolve each conflict
        for conflict in unique_conflicts:
            faculty = conflict['faculty']
            period = conflict['period']
            session1 = conflict['session1']
            session2 = conflict['session2']
            
            # Decide which session to reschedule
            # Priority: Later semester > Tutorial over Lecture > Alphabetically
            def get_reschedule_priority(session):
                try:
                    semester = int(session.section.split('-')[2].replace('Sem', '')) if len(session.section.split('-')) > 2 else 1
                except:
                    semester = 1
                kind_priority = {'T': 2, 'L': 1, 'P': 0}  # Prefer rescheduling tutorials
                return (semester, kind_priority.get(session.kind, 0), session.course_code)
            
            if get_reschedule_priority(session1) > get_reschedule_priority(session2):
                session_to_move = session1
                keep_session = session2
            else:
                session_to_move = session2
                keep_session = session1
            
            print(f"  Resolving conflict for {faculty} on {conflict['day']} ({period}):")
            print(f"    {session_to_move.course_code} ({session_to_move.section}) at {session_to_move.block.start}-{session_to_move.block.end}")
            print(f"    vs {keep_session.course_code} ({keep_session.section}) at {keep_session.block.start}-{keep_session.block.end}")
            print(f"    -> Rescheduling {session_to_move.course_code} ({session_to_move.section})")
            
            # Get lunch blocks for availability check
            try:
                semester = int(session_to_move.section.split('-')[2].replace('Sem', '')) if len(session_to_move.section.split('-')) > 2 else 1
            except:
                semester = 1
            lunch_blocks_dict = get_lunch_blocks()
            lunch_base = lunch_blocks_dict.get(semester)
            lunch_blocks = []
            if lunch_base:
                for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
                    lunch_blocks.append(TimeBlock(day, lunch_base.start, lunch_base.end))
            
            # Find alternative slot with multiple attempts
            new_slot = None
            for slot_attempt in range(20):  # Try up to 20 different strategies for better resolution
                new_slot = find_alternative_slot(session_to_move, all_sessions, occupied_slots, classrooms, slot_attempt)
                if new_slot:
                    # Verify the new slot doesn't create conflicts
                    if is_slot_available(new_slot, session_to_move, all_sessions, occupied_slots, lunch_blocks):
                        break
                    else:
                        new_slot = None  # Try again
            
            if new_slot:
                # Update session block
                old_block = session_to_move.block
                session_to_move.block = new_slot
                
                # Update occupied slots
                section_key = f"{session_to_move.section}_{session_to_move.period}"
                # Remove old slot
                occupied_slots[section_key] = [
                    (blk, course) for blk, course in occupied_slots.get(section_key, [])
                    if not (blk.day == old_block.day and blk.start == old_block.start and blk.end == old_block.end)
                ]
                # Add new slot
                occupied_slots[section_key].append((new_slot, session_to_move.course_code))
                
                # Mark conflict as resolved
                resolved_conflicts.add(conflict['conflict_key'])
                
                print(f"    SUCCESS: Moved {session_to_move.course_code} from {old_block.day} {old_block.start}-{old_block.end} to {new_slot.day} {new_slot.start}-{new_slot.end}")
            else:
                print(f"    WARNING: Could not find alternative slot for {session_to_move.course_code} after multiple attempts")
    
    # Final check - report any remaining conflicts
    final_conflicts = []
    faculty_sessions_by_period = defaultdict(lambda: defaultdict(list))
    for session in scheduled_session_objects:
        if session.kind == 'P':
            continue
        faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        if not faculty or faculty in ['TBD', 'Various']:
            continue
        period = getattr(session, 'period', 'UNKNOWN')
        faculty_sessions_by_period[faculty][period].append(session)
    
    for faculty, periods_dict in faculty_sessions_by_period.items():
        for period, sessions in periods_dict.items():
            for i, session1 in enumerate(sessions):
                for j, session2 in enumerate(sessions[i+1:], i+1):
                    if (session1.block.day == session2.block.day and 
                        session1.block.overlaps(session2.block)):
                        key1 = f"{session1.course_code}_{session1.section}"
                        key2 = f"{session2.course_code}_{session2.section}"
                        if key1 != key2:
                            final_conflicts.append((faculty, period, session1.course_code, session2.course_code))
    
    if final_conflicts:
        print(f"WARNING: {len(final_conflicts)} conflicts remain after resolution attempts")
        for faculty, period, course1, course2 in final_conflicts[:5]:
            print(f"  - {faculty} ({period}): {course1} vs {course2}")
    else:
        print("All faculty conflicts resolved successfully!")
    
    # Return all sessions (including combined_sessions dictionaries)
    # The ScheduledSession objects have been modified in place
    # Combined sessions (dictionaries) are unchanged
    return all_sessions


def _session_view(session_ref, section: str, period: str, block: TimeBlock, course_code: str, kind: str):
    """View of a session (ScheduledSession or dict) for find_alternative_slot / is_slot_available."""
    if isinstance(session_ref, dict):
        return type('SessionView', (), {
            'section': section, 'period': period, 'block': block, 'course_code': course_code,
            'kind': kind, 'faculty': session_ref.get('instructor', 'TBD')
        })()
    return session_ref


def _build_overlap_entries(all_sessions: List) -> tuple:
    """Build movable list and overlap_entries from current all_sessions. Combined (dict) sessions are movable."""
    movable: List = []
    overlap_entries: List[tuple] = []
    for s in all_sessions:
        if isinstance(s, dict):
            sections = s.get("sections", [])
            period = s.get("period", "PRE")
            block = s.get("time_block")
            course_code = s.get("course_code", "")
            if not block or not sections:
                continue
            kind = s.get("session_type", "L")
            for section in sections:
                overlap_entries.append((section, period, block, course_code, kind, s, True))
            movable.append(s)
            continue
        if not hasattr(s, "block") or not hasattr(s, "section") or not hasattr(s, "course_code"):
            continue
        if str(getattr(s, "course_code", "")).startswith("ELECTIVE_BASKET_"):
            continue
        movable.append(s)
        overlap_entries.append((
            s.section, getattr(s, "period", "PRE"), s.block, s.course_code,
            getattr(s, "kind", "L"), s, True
        ))
    return movable, overlap_entries


def detect_and_resolve_section_overlaps(
    all_sessions: List,
    occupied_slots: Dict[str, List[TimeBlock]],
    classrooms: List[ClassRoom],
    max_passes: int = 10,
) -> List:
    """
    Resolve overlaps within the SAME section+period by moving one of the sessions to another free slot.

    - Does not move elective basket sessions (course_code startswith 'ELECTIVE_BASKET_')
    - Combined-session dicts CAN be moved (time_block updated in place) to resolve e.g. MA261 vs CS261.
    - Overlap detection includes BOTH Phase 5/7 sessions and combined (dict) sessions.
    """
    def find_overlaps(overlap_entries: List[tuple]) -> List[tuple]:
        conflicts = []
        by_sec_per = defaultdict(list)
        for entry in overlap_entries:
            sec, per, block, course_code, kind, session_ref, is_movable = entry
            by_sec_per[(sec, per)].append(entry)
        for (sec, per), entries in by_sec_per.items():
            by_day = defaultdict(list)
            for entry in entries:
                _, _, block, _, _, _, _ = entry
                by_day[block.day].append(entry)
            for day, day_entries in by_day.items():
                day_entries.sort(key=lambda e: (e[2].start.hour, e[2].start.minute))
                for i in range(len(day_entries)):
                    for j in range(i + 1, len(day_entries)):
                        a = day_entries[i]
                        b = day_entries[j]
                        if a[2].overlaps(b[2]):
                            conflicts.append((sec, per, day, a, b))
        return conflicts

    def priority_entry(entry):
        kind_pri = {"T": 2, "L": 1, "P": 0}
        _, _, block, _, kind, _, _ = entry
        return (kind_pri.get(kind, 0), block.start.hour, block.start.minute)

    def get_block(session_ref):
        return session_ref.get("time_block") if isinstance(session_ref, dict) else session_ref.block

    def get_section_period(session_ref, sec, per):
        if isinstance(session_ref, dict):
            return sec, session_ref.get("period", "PRE")
        return getattr(session_ref, "section", sec), getattr(session_ref, "period", "PRE")

    for pass_idx in range(max_passes):
        movable, overlap_entries = _build_overlap_entries(all_sessions)
        conflicts = find_overlaps(overlap_entries)
        if not conflicts:
            return all_sessions

        print(f"[SECTION-OVERLAP] Pass {pass_idx+1}: {len(conflicts)} overlaps found")

        def same_block(blk, other):
            return (blk.day == other.day and blk.start == other.start and blk.end == other.end)

        for sec, per, day, e1, e2 in conflicts:
            s1_movable = e1[6]
            s2_movable = e2[6]
            session_to_move = e1[5] if priority_entry(e1) >= priority_entry(e2) else e2[5]
            # Session view for find_alternative_slot / is_slot_available (section from conflict)
            view = _session_view(session_to_move, sec, per, get_block(session_to_move),
                                 e1[3] if session_to_move is e1[5] else e2[3],
                                 e1[4] if session_to_move is e1[5] else e2[4])

            new_slot = None
            for attempt in range(20):
                candidate = find_alternative_slot(view, movable, occupied_slots, classrooms, attempt)
                if not candidate or same_block(candidate, get_block(session_to_move)):
                    continue
                try:
                    sem = int(sec.split('-')[2].replace('Sem', ''))
                except Exception:
                    sem = 1
                lunch_blocks_dict = get_lunch_blocks()
                lunch_base = lunch_blocks_dict.get(sem)
                lunch_blocks = []
                if lunch_base:
                    for d in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
                        lunch_blocks.append(TimeBlock(d, lunch_base.start, lunch_base.end))
                if is_slot_available(candidate, view, movable, occupied_slots, lunch_blocks):
                    new_slot = candidate
                    break

            if not new_slot:
                other_ref = e2[5] if session_to_move is e1[5] else e1[5]
                other_view = _session_view(other_ref, sec, per, get_block(other_ref),
                                          e2[3] if other_ref is e2[5] else e1[3],
                                          e2[4] if other_ref is e2[5] else e1[4])
                for attempt in range(20):
                    candidate = find_alternative_slot(other_view, movable, occupied_slots, classrooms, attempt)
                    if not candidate or same_block(candidate, get_block(other_ref)):
                        continue
                    try:
                        sem = int(sec.split('-')[2].replace('Sem', ''))
                    except Exception:
                        sem = 1
                    lunch_blocks_dict = get_lunch_blocks()
                    lunch_base = lunch_blocks_dict.get(sem)
                    lunch_blocks = []
                    if lunch_base:
                        for d in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
                            lunch_blocks.append(TimeBlock(d, lunch_base.start, lunch_base.end))
                    if is_slot_available(candidate, other_view, movable, occupied_slots, lunch_blocks):
                        new_slot = candidate
                        session_to_move = other_ref
                        view = other_view
                        break

            if not new_slot:
                continue

            old_block = get_block(session_to_move)
            if isinstance(session_to_move, dict):
                session_to_move["time_block"] = new_slot
                course_code = session_to_move.get("course_code", "")
                period = session_to_move.get("period", "PRE")
                for s in session_to_move.get("sections", []):
                    sk = f"{s}_{period}"
                    occupied_slots[sk] = [
                        (blk, c) for blk, c in occupied_slots.get(sk, [])
                        if not (blk.day == old_block.day and blk.start == old_block.start and blk.end == old_block.end and c == course_code)
                    ]
                    occupied_slots[sk].append((new_slot, course_code))
                print(
                    f"[SECTION-OVERLAP] Moved {course_code} (combined {period}) "
                    f"from {old_block.day} {old_block.start}-{old_block.end} to {new_slot.day} {new_slot.start}-{new_slot.end}"
                )
            else:
                session_to_move.block = new_slot
                section_key = f"{session_to_move.section}_{getattr(session_to_move, 'period', 'PRE')}"
                occupied_slots[section_key] = [
                    (blk, course) for blk, course in occupied_slots.get(section_key, [])
                    if not (blk.day == old_block.day and blk.start == old_block.start and blk.end == old_block.end and course == session_to_move.course_code)
                ]
                occupied_slots[section_key].append((new_slot, session_to_move.course_code))
                print(
                    f"[SECTION-OVERLAP] Moved {session_to_move.course_code} ({session_to_move.section} {session_to_move.period}) "
                    f"from {old_block.day} {old_block.start}-{old_block.end} to {new_slot.day} {new_slot.start}-{new_slot.end}"
                )

    return all_sessions

def find_alternative_slot(session: ScheduledSession, 
                         all_sessions: List[ScheduledSession],
                         occupied_slots: Dict[str, List[TimeBlock]],
                         classrooms: List[ClassRoom],
                         attempt: int = 0) -> Optional[TimeBlock]:
    """Find alternative time slot with expanded search and multiple strategies"""
    
    # Get available days (Monday-Friday)
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    
    # Get semester for lunch check
    semester = int(session.section.split('-')[2].replace('Sem', ''))
    lunch_blocks_dict = get_lunch_blocks()
    lunch_base = lunch_blocks_dict.get(semester)
    # Create lunch blocks for all days
    lunch_blocks = []
    if lunch_base:
        for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
            lunch_blocks.append(TimeBlock(day, lunch_base.start, lunch_base.end))
    
    # Calculate duration based on session type (preserve original duration)
    # IMPORTANT: Lectures are 1.5 hours, tutorials are 1 hour, practicals are 2 hours
    if session.kind == 'L':  # Lecture: 1.5 hours
        duration = 90
    elif session.kind == 'T':  # Tutorial: 1 hour
        duration = 60
    elif session.kind == 'P':  # Practical: 2 hours
        duration = 120
    else:
        # Fallback: calculate from existing block
        duration = (session.block.end.hour * 60 + session.block.end.minute) - \
                   (session.block.start.hour * 60 + session.block.start.minute)
    
    # Strategy: Try different time ranges based on attempt number
    # Attempt 0: Try morning slots (9:00-12:00)
    # Attempt 1: Try afternoon slots (14:00-17:00)
    # Attempt 2: Try all slots (9:00-17:00)
    # Attempt 3: Try early morning (9:00-11:00) and late afternoon (15:00-17:00)
    # Attempt 4: Try any available slot
    
    time_ranges = [
        (9, 12),   # Morning
        (14, 17),  # Afternoon
        (9, 17),   # All day
        (9, 11),   # Early morning
        (15, 17),  # Late afternoon
    ]
    
    if attempt < len(time_ranges):
        start_hour, end_hour = time_ranges[attempt]
    else:
        start_hour, end_hour = (9, 17)  # Default: all day
    
    # Try each day
    for day in days:
        # Try time slots in the selected range
        for hour in range(start_hour, end_hour + 1):
            for minute in [0, 15, 30, 45]:
                if hour == end_hour and minute > 0:
                    continue  # Don't go beyond end_hour
                    
                start_time = time(hour, minute)
                end_minutes = hour * 60 + minute + duration
                
                # Check if end time is within college hours (before 18:00)
                if end_minutes > 18 * 60:
                    continue
                
                end_time = time(end_minutes // 60, end_minutes % 60)
                test_slot = TimeBlock(day, start_time, end_time)
                
                # CRITICAL: Check lunch conflict BEFORE checking availability
                # This prevents slots that overlap with lunch
                lunch_conflict = False
                for lunch in lunch_blocks:
                    if lunch.day == day and test_slot.overlaps(lunch):
                        lunch_conflict = True
                        break
                
                if lunch_conflict:
                    continue  # Skip this slot - it overlaps with lunch
                
                # Check if slot is available (including faculty availability)
                if is_slot_available(test_slot, session, all_sessions, occupied_slots, lunch_blocks):
                    return test_slot
    
    return None

def is_slot_available(slot: TimeBlock, 
                     session: ScheduledSession,
                     all_sessions: List,
                     occupied_slots: Dict[str, List[TimeBlock]],
                     lunch_blocks: List[TimeBlock]) -> bool:
    """Check if a time slot is available for rescheduling"""
    
    # Check lunch conflict
    for lunch in lunch_blocks:
        if slot.overlaps(lunch):
            return False
    
    # Check occupied slots for this section (exclude the current session being moved)
    section_key = f"{session.section}_{session.period}"
    for blk, course_code in occupied_slots.get(section_key, []):
        # Skip if this is the same session we're trying to move
        if course_code == session.course_code and blk.day == session.block.day and blk.start == session.block.start:
            continue
        if slot.overlaps(blk):
            return False
    
    # Check faculty conflicts (skip labs and the current session)
    if session.faculty and session.faculty not in ['TBD', 'Various']:
        for other_session in all_sessions:
            # Handle both ScheduledSession objects and dictionaries
            if isinstance(other_session, dict):
                # Dictionary session (from combined_sessions) - skip for faculty conflict check
                # Combined sessions don't have faculty conflicts as they're already scheduled
                continue
            
            # Only process ScheduledSession objects
            if not hasattr(other_session, 'kind'):
                continue
                
            if other_session.kind == 'P':  # Skip labs
                continue
            # Skip the session we're trying to move
            if (hasattr(other_session, 'course_code') and other_session.course_code == session.course_code and 
                hasattr(other_session, 'section') and other_session.section == session.section and
                hasattr(other_session, 'block') and other_session.block.day == session.block.day and
                hasattr(other_session, 'block') and other_session.block.start == session.block.start):
                continue
            
            # Get faculty from other_session
            other_faculty = getattr(other_session, 'faculty', None) or getattr(other_session, 'instructor', None)
            if other_faculty == session.faculty:
                # Check same period
                other_period = getattr(other_session, 'period', 'UNKNOWN')
                session_period = getattr(session, 'period', 'UNKNOWN')
                if (other_period == session_period and 
                    hasattr(other_session, 'block') and 
                    other_session.block.day == slot.day and 
                    other_session.block.overlaps(slot)):
                    return False
    
    # Check elective conflicts
    semester = int(session.section.split('-')[2].replace('Sem', ''))
    if check_elective_conflict(slot.day, slot.start, slot.end, semester):
        return False
    
    return True
