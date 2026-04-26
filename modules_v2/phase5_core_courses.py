"""
Phase 5: Core Courses Scheduling (Credits > 2)
Schedule core courses with credits > 2 using LTPSC logic
"""
import re
import pandas as pd
import random
import hashlib
from typing import List, Dict, Tuple, Optional
from datetime import time, datetime, timedelta
from collections import defaultdict

from utils.data_models import Course, Section, ClassRoom, ScheduledSession, TimeBlock, section_has_time_conflict
from config.schedule_config import WORKING_DAYS, DAY_START_TIME, DAY_END_TIME, LUNCH_WINDOWS
from modules_v2.phase2_time_management_v2 import get_lunch_blocks, generate_base_time_slots
from utils.time_slot_logger import get_logger
from utils.session_rules_validator import SessionRulesValidator
from utils.time_validator import validate_time_range, slot_end_within_day, time_to_minutes
from utils.period_utils import normalize_period
from utils.room_priority_policy import (
    ordered_classroom_candidates,
    should_prefer_top_large_rooms,
    top_large_classrooms,
)


def _stable_seed_int(*parts: object) -> int:
    """Generate a process-stable deterministic seed from text parts."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)

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


def normalize_ltpsc_for_merge_key(ltpsc) -> str:
    """Normalize LTPSC for stable Phase-5 deduplication when Offering_ID is not set."""
    if ltpsc is None or (isinstance(ltpsc, float) and pd.isna(ltpsc)):
        return ""
    s = str(ltpsc).strip()
    if not s or s.lower() == "nan":
        return ""
    return " ".join(s.split())


def phase5_merge_key(course: Course) -> Tuple:
    """
    Key for merging cross-department Excel rows into one schedulable Phase-5 course.

    - If offering_id is set: same ID => same merged course (explicit offering / primary key).
    - Otherwise: merge only when code, semester, credits, and normalized LTPSC all match
      (fixes verifier vs scheduler split when duplicate rows had different LTPSC).
    """
    oid = (getattr(course, "offering_id", None) or "").strip()
    if oid:
        return ("id", oid)
    ltpsc_k = normalize_ltpsc_for_merge_key(course.ltpsc)
    # IMPORTANT:
    # For Phase 5 (>2 credit, non-combined) courses, different departments can legitimately have
    # different instructor mappings for the same course code. If we merge across departments here,
    # the scheduler may reuse the wrong faculty across parallel sections, triggering strict verify
    # faculty conflicts. So keep department in the key unless Offering_ID explicitly ties them.
    dept = (getattr(course, "department", None) or "").strip()
    return ("struct", course.code, course.semester, course.credits, ltpsc_k, dept)


def select_phase5_course_for_section(candidates: List[Course], section: Section) -> Course:
    """
    When multiple Phase-5 Course objects share the same code (e.g. duplicate Excel rows
    with different LTPSC for the same department), choose the row authored for this
    section's program so scheduling matches verification (code + semester + department).

    If several rows list the same Department, prefer one with a non-empty Offering_ID,
    then the first in pipeline order, and emit a WARNING so data can be cleaned.
    """
    if not candidates:
        raise ValueError("select_phase5_course_for_section: empty candidates")
    if len(candidates) == 1:
        return candidates[0]

    prog = getattr(section, "program", None) or ""
    dept_match = [c for c in candidates if getattr(c, "department", None) == prog]
    if len(dept_match) == 1:
        return dept_match[0]

    if len(dept_match) > 1:
        with_oid = [c for c in dept_match if (getattr(c, "offering_id", None) or "").strip()]
        if len(with_oid) == 1:
            return with_oid[0]
        ltpscs = [getattr(c, "ltpsc", "") for c in dept_match]
        print(
            f"  WARNING: {dept_match[0].code} has {len(dept_match)} rows for department {prog} "
            f"with different LTPSC {ltpscs}; scheduling first only — fix Excel or set distinct Offering_ID."
        )
        return dept_match[0]

    for c in candidates:
        depts = getattr(c, "_departments_list", None) or [getattr(c, "department", None)]
        if prog in depts:
            return c

    print(
        f"  WARNING: No department match for {candidates[0].code} in {prog}; using first candidate."
    )
    return candidates[0]


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
                        assigned_faculty, slot.day, slot.start, slot.end, period, all_sessions,
                        candidate_course_code=course.code,
                        candidate_section_label=section.label,
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
                
            sec_cap_needed = max(
                int(getattr(section, "students", 0) or 0),
                int(getattr(course, "registered_students", 0) or 0),
            ) or 85
            room = assign_room_for_session("L", sec_cap_needed, classrooms, room_occupancy, slot)
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
                if all_sessions is not None:
                    all_sessions.append(session)
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
                        assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions,
                        candidate_course_code=course.code,
                        candidate_section_label=section.label,
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
                
            sec_cap_needed = max(
                int(getattr(section, "students", 0) or 0),
                int(getattr(course, "registered_students", 0) or 0),
            ) or 85
            room = assign_room_for_session("T", sec_cap_needed, classrooms, room_occupancy, tutorial_slot)
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
                if all_sessions is not None:
                    all_sessions.append(session)
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
                
            lab = assign_room_for_session("P", 40, classrooms, room_occupancy, practical_slot, course.code)
            
            # Fallback: If no lab found, retry only within the strict typed lab pool.
            if not lab:
                all_labs = _typed_lab_pool(classrooms, course.code)
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


def _is_lab_room(room: ClassRoom) -> bool:
    room_type = str(getattr(room, "room_type", "") or "").lower()
    room_no = str(getattr(room, "room_number", "") or "").upper()
    return ("lab" in room_type) or room_no.startswith("L")


def _typed_lab_pool(classrooms: List[ClassRoom], course_code: str) -> List[ClassRoom]:
    code = str(course_code or "").strip().upper()
    wants_hardware = code.startswith("EC")
    if wants_hardware:
        return [
            room for room in classrooms
            if _is_lab_room(room)
            and getattr(room, "lab_type", None) == "Hardware"
            and not getattr(room, "is_research_lab", False)
        ]
    return [
        room for room in classrooms
        if _is_lab_room(room)
        and getattr(room, "lab_type", None) in (None, "Software")
        and not getattr(room, "is_research_lab", False)
    ]

def assign_room_for_session(session_type: str, capacity_needed: int,
                            classrooms: List[ClassRoom],
                            occupied_rooms: Dict[str, List[TimeBlock]],
                            time_block: TimeBlock,
                            course_code: str = "") -> Optional[str]:
    """Assign room based on capacity and availability"""
    
    if session_type == "P":  # Practical - need lab
        # STRICT pool separation:
        #   EC* courses -> Hardware labs only
        #   non-EC      -> Software/unspecified labs only
        candidate_labs = _typed_lab_pool(classrooms, course_code)

        # Keep strict pool first, but do not leave room blank if strict pool is exhausted.
        labs_exact = [room for room in candidate_labs if room.capacity == 40]
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
        
        # 2. Fallback: Any lab from the same strict pool (regardless of capacity)
        if not available_labs:
            labs_any = list(candidate_labs)
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

        # Last-resort: any non-research lab to avoid blank room assignments in strict verify.
        if not available_labs:
            any_labs = [
                room for room in classrooms
                if _is_lab_room(room) and not getattr(room, "is_research_lab", False)
            ]
            for lab in any_labs:
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
        top2 = top_large_classrooms(classrooms, n=2)
        prefer_top = should_prefer_top_large_rooms(capacity_needed, top2)
        candidates = ordered_classroom_candidates(
            classrooms, capacity_needed, prefer_top_large=prefer_top, top_rooms=top2
        )

        # Pass 1: honor requested capacity threshold.
        for room in candidates:
            if int(getattr(room, "capacity", 0) or 0) < int(capacity_needed or 0):
                continue
            if room.room_number not in occupied_rooms:
                return room.room_number
            conflicts = False
            for occupied_block in occupied_rooms[room.room_number]:
                if time_block.overlaps(occupied_block):
                    conflicts = True
                    break
            if not conflicts:
                return room.room_number

        # No under-capacity fallback in strict mode.
        # If no compliant room is available, caller should reschedule.
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

def generate_dynamic_time_slots(semester: int, start_hour: Optional[int] = None, end_hour: Optional[int] = None) -> List[TimeBlock]:
    """
    Dynamically generate time slots based on:
    - Available time window (from config DAY_START_TIME/DAY_END_TIME unless overridden)
    - Lunch break times from config LUNCH_WINDOWS
    - 15-minute intervals
    - Multiple session durations (1h, 1.5h, 2h)
    """

    # Lunch window from config
    lunch_window = LUNCH_WINDOWS.get(semester, (time(12, 30), time(13, 30)))
    lunch_start, lunch_end = lunch_window

    days = WORKING_DAYS
    base_slots = []

    # Build full 15-minute grid using schedule_config (honor minutes, not just .hour)
    if start_hour is None:
        work_start_dt = datetime.combine(datetime.min, DAY_START_TIME)
    else:
        work_start_dt = datetime.combine(datetime.min, time(start_hour, 0))
    if end_hour is None:
        work_end_dt = datetime.combine(datetime.min, DAY_END_TIME)
    else:
        work_end_dt = datetime.combine(datetime.min, time(end_hour, 0))

    # For each day, create:
    # - All 1.5h lecture candidates at every 15-min start that fully fits before lunch / end of day
    # - All 1h tutorial candidates at every 15-min start that fully fits before lunch / end of day
    # - All 2h practical candidates at every 15-min start that fully fits after lunch / before end of day
    for day in days:
        current_dt = work_start_dt
        while current_dt < work_end_dt:
            start = current_dt.time()

            # 1.5h lecture candidates (both morning and afternoon, as long as they don't cross lunch)
            lecture_end_dt = current_dt + timedelta(minutes=90)
            lecture_end = lecture_end_dt.time()
            # Must finish before end of working day
            if lecture_end_dt <= work_end_dt:
                # Do not allow lectures that overlap the lunch window
                if not (start < lunch_end and lecture_end > lunch_start):
                    base_slots.append(TimeBlock(day, start, lecture_end))

            # 1h tutorial candidates (both morning and afternoon, as long as they don't cross lunch)
            tutorial_end_dt = current_dt + timedelta(minutes=60)
            tutorial_end = tutorial_end_dt.time()
            if tutorial_end_dt <= work_end_dt:
                if not (start < lunch_end and tutorial_end > lunch_start):
                    base_slots.append(TimeBlock(day, start, tutorial_end))

            # 2h practical candidates (only after lunch, and must not cross end of day)
            practical_end_dt = current_dt + timedelta(minutes=120)
            practical_end = practical_end_dt.time()
            if current_dt.time() >= lunch_end and practical_end_dt <= work_end_dt:
                base_slots.append(TimeBlock(day, start, practical_end))

            # Move to next 15-minute start
            current_dt += timedelta(minutes=15)

    return base_slots

def get_available_time_slots(semester: int, occupied_slots: Dict[str, List[TimeBlock]], 
                             course_code: str, section: str, period: str) -> List[TimeBlock]:
    """Get available time slots for a course, avoiding conflicts - FULLY DYNAMIC"""
    
    # DEBUG: Track rejected slots
    rejected_slots = []
    lunch_rejected = 0
    overlap_rejected = 0
    
    # Generate time slots dynamically using config-driven day window and lunch
    base_slots = generate_dynamic_time_slots(semester)
    
    # Deterministic shuffle for reproducible scheduling order per (semester, section, course, period).
    shuffled_slots = base_slots.copy()
    _rng = random.Random(_stable_seed_int("phase5_slots", semester, section, course_code, period))
    _rng.shuffle(shuffled_slots)
    
    available_slots = []
    for slot in shuffled_slots:
        # Check lunch conflicts using overlaps_with_lunch (config-driven)
        if slot.overlaps_with_lunch(semester):
            lunch_rejected += 1
            rejected_slots.append((slot, 'LUNCH_CONFLICT'))
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
    section_capacity_needed = int(getattr(section, "students", 0) or 0) or 85
    slots_needed = calculate_slots_needed(course.ltpsc)

    # Phase-5 single-faculty high-credit parallel prevention (aligns with strict verify step 3b).
    # If a course has effectively one real faculty person AND credits > threshold, then parallel
    # sections of the same course cannot overlap at all for that instructor
    # (strict verify treats wall-clock overlap as conflict regardless of PRE/POST).
    from config.schedule_config import FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD
    from utils.faculty_conflict_utils import faculty_token_set

    try:
        course_credits = int(getattr(course, "credits", 0) or 0)
    except Exception:
        course_credits = 0

    course_faculty_tokens = set()
    for inst in faculty_list:
        course_faculty_tokens |= faculty_token_set(inst)

    single_faculty_high_credit = (
        course_credits > FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD
        and len(course_faculty_tokens) == 1
        and assigned_faculty
        and str(assigned_faculty).strip().upper() not in ("TBD", "VARIOUS", "-")
    )

    candidate_fac_tokens = faculty_token_set(assigned_faculty)
    base_course_code = str(course.code or "").split("-")[0].strip().upper()

    def _section_program_from_label(label: str) -> str:
        """ECE-A-Sem6 -> ECE (cross-dept joint offering vs parallel overload)."""
        if not label:
            return ""
        return str(label).split("-", 1)[0].strip().upper()

    def would_violate_single_faculty_parallel(candidate_block: TimeBlock) -> bool:
        if not single_faculty_high_credit or not all_sessions:
            return False
        for existing in all_sessions:
            # Only enforce against ScheduledSession-like objects.
            if isinstance(existing, dict):
                continue
            existing_code = str(getattr(existing, "course_code", "") or "").split("-")[0].strip().upper()
            if existing_code != base_course_code:
                continue
            existing_block = getattr(existing, "block", None)
            if not existing_block:
                continue
            if existing_block.day != candidate_block.day or not existing_block.overlaps(candidate_block):
                continue
            # Wall-clock overlap only (match check_faculty_availability_in_period + strict verify 3b).
            existing_section = getattr(existing, "section", "") or ""
            if existing_section and existing_section == section.label:
                continue  # same section duplication shouldn't happen

            existing_fac = getattr(existing, "faculty", None) or getattr(existing, "instructor", None) or ""
            existing_fac_tokens = faculty_token_set(existing_fac)
            if not (candidate_fac_tokens & existing_fac_tokens):
                continue

            # Same course + different parallel section + overlapping time + same instructor => violation,
            # except cross-department labels (e.g. ECE-A vs DSAI-A): same wall-clock is one joint class.
            if existing_section and existing_section != section.label:
                ep = _section_program_from_label(existing_section)
                sp = _section_program_from_label(section.label)
                if ep and sp and ep != sp:
                    continue
                return True
        return False
    
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

        # CRITICAL: Check section-level time conflicts with other courses (all phases)
        if section_has_time_conflict(occupied_slots, section.label, period, lecture_block):
            continue  # Skip this slot - overlaps with another course in this section/period
        
        # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
        if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
            if would_violate_single_faculty_parallel(lecture_block):
                continue  # Same course + single instructor + parallel section overlap not allowed
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            if not check_faculty_availability_in_period(
                assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions,
                candidate_course_code=course.code,
                candidate_section_label=section.label,
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
        room = assign_room_for_session("L", section_capacity_needed, classrooms, room_occupancy or {}, lecture_block)
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

            if section_has_time_conflict(occupied_slots, section.label, period, lecture_block):
                continue

            # Check faculty availability
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                if would_violate_single_faculty_parallel(lecture_block):
                    continue  # Same course + single instructor + parallel section overlap not allowed
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions,
                    candidate_course_code=course.code,
                    candidate_section_label=section.label,
                ):
                    continue

            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("L", section_capacity_needed, classrooms, room_occupancy or {}, lecture_block)
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

            if section_has_time_conflict(occupied_slots, section.label, period, lecture_block):
                continue

            # Check faculty availability
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                if would_violate_single_faculty_parallel(lecture_block):
                    continue  # Same course + single instructor + parallel section overlap not allowed
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions,
                    candidate_course_code=course.code,
                    candidate_section_label=section.label,
                ):
                    continue

            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("L", section_capacity_needed, classrooms, room_occupancy or {}, lecture_block)
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

    if lecture_count < target_lectures:
        # Pass 3: skip elective-basket overlaps only for missing core lectures (single-section EC307-style).
        for slot in available_slots:
            if lecture_count >= target_lectures:
                break
            if slot.day in used_days_lectures:
                continue
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
            if section_has_time_conflict(occupied_slots, section.label, period, lecture_block):
                continue
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                if would_violate_single_faculty_parallel(lecture_block):
                    continue
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, lecture_end, period, all_sessions,
                    candidate_course_code=course.code,
                    candidate_section_label=section.label,
                ):
                    continue
            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue
            room = assign_room_for_session("L", section_capacity_needed, classrooms, room_occupancy or {}, lecture_block)
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
        # Pass 4 (emergency): prioritize LTPSC completeness over faculty-time preference.
        # Keep section conflict safety; skip faculty availability only in this final pass.
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
            if section_has_time_conflict(occupied_slots, section.label, period, lecture_block):
                continue

            conflict = False
            for existing_session in sessions:
                if lecture_block.overlaps(existing_session.block):
                    conflict = True
                    break
            if conflict:
                continue

            room = assign_room_for_session("L", section_capacity_needed, classrooms, room_occupancy or {}, lecture_block)
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

        if section_has_time_conflict(occupied_slots, section.label, period, tutorial_slot):
            continue
        
        # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
        if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
            if would_violate_single_faculty_parallel(tutorial_slot):
                continue  # Same course + single instructor + parallel section overlap not allowed
            from utils.faculty_conflict_utils import check_faculty_availability_in_period
            if not check_faculty_availability_in_period(
                assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions,
                candidate_course_code=course.code,
                candidate_section_label=section.label,
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
        room = assign_room_for_session("T", section_capacity_needed, classrooms, room_occupancy or {}, tutorial_slot)
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

            if section_has_time_conflict(occupied_slots, section.label, period, tutorial_slot):
                continue

            # CRITICAL: Check faculty availability (prevent conflicts during scheduling)
            if all_sessions and assigned_faculty and assigned_faculty != 'TBD':
                if would_violate_single_faculty_parallel(tutorial_slot):
                    continue  # Same course + single instructor + parallel section overlap not allowed
                from utils.faculty_conflict_utils import check_faculty_availability_in_period
                if not check_faculty_availability_in_period(
                    assigned_faculty, slot.day, slot.start, tutorial_end, period, all_sessions,
                    candidate_course_code=course.code,
                    candidate_section_label=section.label,
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

            room = assign_room_for_session("T", section_capacity_needed, classrooms, room_occupancy or {}, tutorial_slot)
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
            continue
        if not slot_end_within_day(slot.start, 120):
            continue

        practical_slot = TimeBlock(slot.day, slot.start, practical_end)

        # CRITICAL: Check elective basket conflict for adjusted block
        if check_elective_conflict(slot.day, slot.start, practical_end, course.semester):
            continue  # Skip this slot to avoid elective conflict

        if section_has_time_conflict(occupied_slots, section.label, period, practical_slot):
            continue
        
        # CRITICAL: Check if practical slot would overlap with lunch
        # Get semester lunch time
        lunch_blocks_dict = get_lunch_blocks()
        lunch_base = lunch_blocks_dict.get(course.semester)
        if lunch_base:
            day_lunch = TimeBlock(slot.day, lunch_base.start, lunch_base.end)
            if practical_slot.overlaps(day_lunch):
                continue  # Skip this slot - practical would overlap with lunch

        # As requested: do not apply faculty conflict checks for practical sessions.
        
        # Assign lab
        lab = assign_room_for_session("P", 40, classrooms, room_occupancy or {}, practical_slot, course.code)
        
        # Fallback: If no lab found, retry only within the strict typed lab pool.
        if not lab:
            all_labs = _typed_lab_pool(classrooms, course.code)
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
                faculty=None  # Ignore faculty constraints for practicals
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

from modules_v2.phase7_remaining_courses import add_session_to_occupied_slots


def run_phase5(courses: List[Course], sections: List[Section], classrooms: List[ClassRoom],
               elective_sessions: List[ScheduledSession],
               combined_sessions: List[ScheduledSession]) -> List[ScheduledSession]:
    """Run Phase 5: Schedule core courses with credits > 2"""
    
    print("=== PHASE 5: CORE COURSES SCHEDULING (Credits > 2) ===")

    # Ensure Phase-4 combined sessions carry faculty for downstream conflict-prevention.
    # If combined sessions are missing faculty, Phase 5 may schedule a teacher into an overlapping slot,
    # and strict verification later flags it (because UI export fills faculty from course data).
    try:
        course_faculty_fallback = {}
        for c in courses or []:
            code = (getattr(c, "code", None) or "").strip()
            if not code:
                continue
            ins = getattr(c, "instructors", None) or []
            if isinstance(ins, str):
                ins = [ins]
            ins = [str(x).strip() for x in ins if str(x).strip()]
            if ins:
                course_faculty_fallback[code] = ins[0]

        for cs in combined_sessions or []:
            if isinstance(cs, dict):
                cc = (str(cs.get("course_code", "") or "").split("-")[0]).strip()
                fac = (cs.get("faculty") or cs.get("instructor") or "").strip()
                if not fac and cc in course_faculty_fallback:
                    cs["faculty"] = course_faculty_fallback[cc]
                    cs["instructor"] = course_faculty_fallback[cc]
            else:
                cc = (str(getattr(cs, "course_code", "") or "").split("-")[0]).strip()
                fac = (getattr(cs, "faculty", None) or "").strip()
                if not fac and cc in course_faculty_fallback:
                    cs.faculty = course_faculty_fallback[cc]
    except Exception as _phase5_comb_fac_e:
        print(f"  WARNING: could not backfill combined-session faculty: {_phase5_comb_fac_e}")

    # Data-quality: multiple LTPSC variants for same code/sem/credits without Offering_ID
    struct_variants: Dict[Tuple, set] = defaultdict(set)
    for c0 in courses:
        if c0.is_elective or c0.credits <= 2 or getattr(c0, "is_combined", False):
            continue
        if (getattr(c0, "offering_id", None) or "").strip():
            continue
        sk = (c0.code, c0.semester, c0.credits)
        struct_variants[sk].add(normalize_ltpsc_for_merge_key(c0.ltpsc))
    for sk, variants in struct_variants.items():
        non_empty = {v for v in variants if v}
        if len(non_empty) > 1:
            print(
                f"  WARNING: Multiple LTPSC rows for {sk[0]} Sem{sk[1]} ({sk[2]} cr) without Offering_ID — "
                f"scheduling as {len(non_empty)} separate offerings: {sorted(non_empty)}"
            )
    
    # Filter courses for Phase 5 - CRITICAL: Only core courses with >2 credits
    phase5_courses = []
    seen = {}  # Track by phase5_merge_key (Offering_ID or struct+LTPSC)
    
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
        
        key = phase5_merge_key(course)
        if key[0] == "struct" and not key[4]:
            print(
                f"  WARNING: {course.code} Sem{course.semester}: empty or invalid LTPSC; "
                f"Phase-5 merge key may collide — set Offering_ID or fix LTPSC."
            )

        if key not in seen:
            seen[key] = course
            # Store all departments this course belongs to
            course._departments_list = [course.department]
            phase5_courses.append(course)
            oid_note = f" [Offering_ID={course.offering_id}]" if (getattr(course, "offering_id", None) or "").strip() else ""
            print(f"  Phase 5 course: {course.code} ({course.name}) - {course.credits} credits - Sem{course.semester}{oid_note}")
        else:
            existing = seen[key]
            if key[0] == "id":
                a = normalize_ltpsc_for_merge_key(existing.ltpsc)
                b = normalize_ltpsc_for_merge_key(course.ltpsc)
                if a != b:
                    print(
                        f"  WARNING: Offering_ID {key[1]!r}: LTPSC mismatch {existing.ltpsc!r} vs {course.ltpsc!r}; "
                        f"using first row's LTPSC for scheduling."
                    )
            # Append department to the existing course
            if course.department not in existing._departments_list:
                existing._departments_list.append(course.department)
    
    # Legacy-compatible stabilization:
    # If duplicate rows exist for the same (code, dept, semester, credits) without Offering_ID,
    # schedule only one representative row (first-in-order), same spirit as arisefinal.
    # This avoids LTPSC ambiguity causing strict-fix retries on the same logical course.
    collapsed_phase5_courses: List[Course] = []
    by_struct_key: Dict[Tuple[str, str, int, int], List[Course]] = defaultdict(list)
    for c in phase5_courses:
        oid = (getattr(c, "offering_id", None) or "").strip()
        if oid:
            collapsed_phase5_courses.append(c)
            continue
        dept = (getattr(c, "department", None) or "").strip()
        by_struct_key[(c.code, dept, c.semester, c.credits)].append(c)

    for sk, group in by_struct_key.items():
        if len(group) == 1:
            collapsed_phase5_courses.append(group[0])
            continue
        ltpscs = sorted({normalize_ltpsc_for_merge_key(getattr(x, "ltpsc", "")) for x in group})
        if len([x for x in ltpscs if x]) > 1:
            print(
                f"  WARNING: {sk[0]} {sk[1]} Sem{sk[2]} has duplicate rows with LTPSC variants {ltpscs}; "
                f"scheduling first only (legacy-compatible)."
            )
        rep = group[0]
        for dup in group[1:]:
            for d in (getattr(dup, "_departments_list", None) or []):
                if d not in rep._departments_list:
                    rep._departments_list.append(d)
        collapsed_phase5_courses.append(rep)

    phase5_courses = collapsed_phase5_courses
    print(f"Found {len(phase5_courses)} Phase 5 courses to schedule")
    
    # Group courses by semester and departments (a course can belong to multiple)
    courses_by_sem_dept = defaultdict(list)
    for course in phase5_courses:
        for dept in course._departments_list:
            key = (course.semester, dept)
            courses_by_sem_dept[key].append(course)
    
    # Create occupied slots map from existing sessions (electives + combined)
    occupied_slots = defaultdict(list)

    for session in elective_sessions or []:
        add_session_to_occupied_slots(session, occupied_slots)

    for session in combined_sessions or []:
        add_session_to_occupied_slots(session, occupied_slots)
    
    all_phase5_sessions = []
    # Offering_ID merges (phase5_merge_key "id"): schedule once, clone to sibling depts, skip later passes.
    scheduled_offering_keys: set = set()

    # Create room occupancy tracking
    room_occupancy = defaultdict(list)
    
    # Process each semester and department (ECE before DSAI reduces same-instructor clashes, e.g. EC307).
    # DSAI before ECE: EC307 is offered to both with the same instructor — ECE-first starved DSAI.
    _dept_pri = {"CSE": 0, "DSAI": 1, "ECE": 2}
    for (semester, department), sem_courses in sorted(
        courses_by_sem_dept.items(),
        key=lambda kv: (kv[0][0], _dept_pri.get(kv[0][1], 9)),
    ):
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
        unique_codes = {c.code for c in sem_courses}
        total_courses = len(relevant_sections) * len(unique_codes)
        current_course = 0

        for section_idx, section in enumerate(relevant_sections):
            print(f"  Section {section.label}:")

            by_code: Dict[str, List[Course]] = defaultdict(list)
            for c in sem_courses:
                by_code[c.code].append(c)
            chosen_for_section = {
                code: select_phase5_course_for_section(group, section)
                for code, group in by_code.items()
            }

            for course in sem_courses:
                if chosen_for_section.get(course.code) is not course:
                    continue
                omk = phase5_merge_key(course)
                if omk[0] == "id" and omk in scheduled_offering_keys:
                    print(f"    [skip] {course.code} ({section.label}): shared Offering_ID already scheduled")
                    continue
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
                
                # Add all sessions to master list (clone to sibling departments for shared Offering_ID)
                if all_course_sessions:
                    commit_sessions = list(all_course_sessions)
                    if (
                        omk[0] == "id"
                        and len(getattr(course, "_departments_list", []) or []) > 1
                    ):
                        cur_prog = (getattr(section, "program", None) or "").strip()
                        other_depts = [
                            d for d in (getattr(course, "_departments_list", []) or [])
                            if (d or "").strip() and (d or "").strip() != cur_prog
                        ]
                        for base_sess in list(commit_sessions):
                            for od in other_depts:
                                for sec2 in sections:
                                    if getattr(sec2, "semester", None) != semester:
                                        continue
                                    if (getattr(sec2, "program", None) or "").strip() != (od or "").strip():
                                        continue
                                    commit_sessions.append(
                                        ScheduledSession(
                                            course_code=base_sess.course_code,
                                            section=sec2.label,
                                            kind=base_sess.kind,
                                            block=base_sess.block,
                                            room=base_sess.room,
                                            period=base_sess.period,
                                            faculty=base_sess.faculty,
                                        )
                                    )
                    all_phase5_sessions.extend(commit_sessions)
                    for sess in commit_sessions[len(all_course_sessions):]:
                        p = getattr(sess, "period", None) or "PRE"
                        sk = f"{sess.section}_{p}"
                        occupied_slots[sk].append((sess.block, course.code))
                        if sess.room:
                            room_occupancy[sess.room].append(sess.block)
                    if omk[0] == "id":
                        scheduled_offering_keys.add(omk)

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
    
    # Resolve faculty conflicts against ALL existing sessions (electives + combined + phase 5)
    all_context_sessions = (elective_sessions or []) + (combined_sessions or []) + all_phase5_sessions
    detect_and_resolve_faculty_conflicts(
        all_context_sessions, occupied_slots, classrooms, room_occupancy
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
    """Detect and resolve faculty conflicts using wall-clock overlap semantics.
    Treat any same-day overlap for the same instructor as a conflict (PRE/POST agnostic),
    matching strict verification behavior.
    """
    
    # Group sessions by faculty
    # Faculty sessions will store either ScheduledSession objects or DictSessionWrapper objects
    faculty_sessions = defaultdict(list)
    
    print(f"\nDEBUG: detect_and_resolve_faculty_conflicts processing {len(all_sessions)} total sessions")
    
    type_counts = defaultdict(int)
    faculty_names_found = set()
    
    for session in all_sessions:
        type_counts[type(session).__name__] += 1
        
        # Determine faculty, period, and block for this session
        faculty_str = ""
        period = "UNKNOWN"
        block = None
        is_movable = False
        
        if isinstance(session, dict):
            # Combined sessions (Phase 4)
            faculty_str = (session.get('instructor') or session.get('faculty') or '').strip()
            period = session.get('period', 'PRE')
            block = session.get('time_block')
            is_movable = False # We don't move Phase 4 combined dicts here
            
            # Wrap for consistent access in conflict check
            class DictSessionWrapper:
                def __init__(self, d, f, p):
                    self.block = d.get('time_block')
                    self.course_code = d.get('course_code')
                    self.section = str(d.get('sections', ['COMBINED']))
                    self.period = p
                    self.faculty = f
                    self.instructor = f # Add alias for robustness
                    self.is_dict = True
                def __repr__(self):
                    return f"DictSession({self.course_code}, {self.section}, {self.period}, {self.block})"
            
            wrapped_session = DictSessionWrapper(session, faculty_str, period)
            session_to_store = wrapped_session
            
        elif hasattr(session, 'block'):
            # ScheduledSession object (Phase 5 or 7)
            faculty_str = (getattr(session, 'faculty', None) or getattr(session, 'instructor', None) or "").strip()
            period = getattr(session, 'period', 'UNKNOWN')
            block = getattr(session, 'block', None)
            is_movable = True
            session.is_dict = False
            session_to_store = session
        else:
            # Unknown type or missing block
            continue
            
        if not faculty_str or faculty_str.upper() in ['TBD', 'VARIOUS', '']:
            continue
            
        if not block:
            continue
            
        # Register for EACH instructor if comma-separated
        instructors = [i.strip() for i in faculty_str.split(',') if i.strip()]
        for inst in instructors:
            inst_lower = inst.lower()
            faculty_sessions[inst_lower].append(session_to_store)
            faculty_names_found.add(inst_lower)

    print(f"DEBUG: Session types: {dict(type_counts)}")
    print(f"DEBUG: Found {len(faculty_names_found)} unique faculty names in grouping")
    if 'sunil p v' in faculty_names_found:
        print(f"DEBUG: Sunil P V found with {len(faculty_sessions['sunil p v'])} sessions")
    else:
        print(f"DEBUG: Sunil P V NOT FOUND in grouping!")
    
    # Find conflicts by checking overlapping times (same period only)
    conflicts_to_resolve = []
    for faculty, sessions in faculty_sessions.items():
        for i, session1 in enumerate(sessions):
            for j, session2 in enumerate(sessions[i+1:], i+1):
                # Ignore checking dictionary vs dictionary (we can't move them anyway)
                if getattr(session1, 'is_dict', False) and getattr(session2, 'is_dict', False):
                    continue
                    
                if (session1.block.day == session2.block.day and 
                    session1.block.overlaps(session2.block)):
                    # PRE and POST are separate halves of the semester.
                    # A conflict only exists if they are in the same period.
                    p1 = getattr(session1, 'period', 'UNKNOWN')
                    p2 = getattr(session2, 'period', 'UNKNOWN')
                    if p1 != p2 and p1 != 'UNKNOWN' and p2 != 'UNKNOWN':
                        continue

                    # Check if different courses/sections (not duplicate)
                    key1 = f"{session1.course_code}_{session1.section}"
                    key2 = f"{session2.course_code}_{session2.section}"
                    
                    if key1 != key2:
                        # Real conflict - different courses/sections, overlapping times
                        # Avoid duplicates by checking if conflict already recorded
                        conflict_key = tuple(sorted([key1, key2]))
                        conflicts_to_resolve.append({
                            'faculty': faculty,
                            'period': session1.period, # Use period of session1 for logging
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
    max_retries = 200 # Extreme persistence for complex conflict landscapes
    resolved_conflicts = set()
    
    for attempt in range(max_retries):
        # Re-detect conflicts after each resolution attempt
        if attempt > 0:
            faculty_sessions = defaultdict(list)
            for session in all_sessions:
                faculty_str = ""
                period = "UNKNOWN"
                block = None
                wrapped = None
                
                if isinstance(session, dict):
                    faculty_str = (session.get('instructor') or session.get('faculty') or '').strip()
                    period = session.get('period', 'PRE')
                    block = session.get('time_block')
                    
                    class DictSessionWrapper:
                        def __init__(self, d, f, p):
                            self.block = d.get('time_block')
                            self.course_code = d.get('course_code')
                            self.section = str(d.get('sections', ['COMBINED']))
                            self.period = p
                            self.faculty = f
                            self.is_dict = True
                        def __repr__(self):
                            return f"DictSession({self.course_code}, {self.section}, {self.period}, {self.block.day if self.block else '?'})"
                    
                    wrapped = DictSessionWrapper(session, faculty_str, period)
                elif hasattr(session, 'block'):
                    faculty_str = (getattr(session, 'faculty', None) or getattr(session, 'instructor', None) or "").strip()
                    period = getattr(session, 'period', 'UNKNOWN')
                    block = getattr(session, 'block', None)
                    wrapped = session
                    session.is_dict = False
                
                if not faculty_str or faculty_str.upper() in ['TBD', 'VARIOUS', ''] or not block:
                    continue
                    
                for f in faculty_str.split(','):
                    faculty_sessions[f.strip().lower()].append(wrapped)
            
            unique_conflicts = []
            seen_conflict_keys = set()
            for faculty, sessions in faculty_sessions.items():
                for i, session1 in enumerate(sessions):
                    for j, session2 in enumerate(sessions[i+1:], i+1):
                        if getattr(session1, 'is_dict', False) and getattr(session2, 'is_dict', False):
                            continue
                            
                        if (session1.block.day == session2.block.day and 
                            session1.block.overlaps(session2.block) and
                            session1.period == session2.period):
                                key1 = f"{session1.course_code}_{session1.section}"
                                key2 = f"{session2.course_code}_{session2.section}"
                                if key1 != key2:
                                    conflict_key = tuple(sorted([key1, key2]))
                                    if conflict_key not in seen_conflict_keys and conflict_key not in resolved_conflicts:
                                        seen_conflict_keys.add(conflict_key)
                                        unique_conflicts.append({
                                            'faculty': faculty,
                                            'period': session1.period, # Use period of session1 for logging
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
            period = conflict['period'] # Get period for logging
            session1 = conflict['session1']
            session2 = conflict['session2']
            
            # Dictionary sessions (Phase 4 combined) are unmovable here
            if getattr(session1, 'is_dict', False):
                session_to_move = session2
                keep_session = session1
            elif getattr(session2, 'is_dict', False):
                session_to_move = session1
                keep_session = session2
            else:
                # Both are movable Phase 5 ScheduledSessions
                def get_semester(section):
                    try:
                        import re
                        match = re.search(r'Sem(\d+)', str(section))
                        if match: return int(match.group(1))
                    except: pass
                    return 1

                def get_reschedule_priority(session):
                    semester = get_semester(getattr(session, 'section', ''))
                    # Tutorials (T) are easiest to move, Practicals (P) are hardest
                    kind_priority = {'T': 2, 'L': 1, 'P': 0}
                    return (semester, kind_priority.get(getattr(session, 'kind', 'L'), 0), getattr(session, 'course_code', ''))
                
                if get_reschedule_priority(session1) >= get_reschedule_priority(session2):
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
                for day in WORKING_DAYS:
                    lunch_blocks.append(TimeBlock(day, lunch_base.start, lunch_base.end))
            
            # Find alternative slot with multiple attempts
            new_slot = None
            verbose_logging = (attempt > 150)  # Enable verbose tracing on very late attempts
            
            for slot_attempt in range(35): # More search breadth
                new_slot = find_alternative_slot(session_to_move, all_sessions, occupied_slots, classrooms, slot_attempt, verbose=verbose_logging)
                if new_slot:
                    break
            
            # FALLBACK: If first choice fails, try moving the other session
            if not new_slot and not getattr(keep_session, 'is_dict', False):
                if verbose_logging: print(f"    - Choice 1 ({session_to_move.course_code}) failed, trying Fallback: {keep_session.course_code}")
                for slot_attempt in range(25):
                    new_slot = find_alternative_slot(keep_session, all_sessions, occupied_slots, classrooms, slot_attempt, verbose=verbose_logging)
                    if new_slot:
                        session_to_move = keep_session
                        break

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
                print(f"    X Could not find alternative slot for EITHER {session_to_move.course_code} or {getattr(keep_session, 'course_code', 'UNKNOWN')}")
    
    # Final check - report any remaining conflicts
    final_conflicts = []
    final_conflicts = []
    faculty_sessions = defaultdict(list)
    for session in all_sessions:
        if isinstance(session, dict):
            # We already have faculty sessions from the main loop logic if needed, 
            # but for final check we might need to be careful.
            continue
        faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
        if not faculty or faculty in ['TBD', 'Various']:
            continue
        faculty_sessions[faculty].append(session)
    
    for faculty, sessions in faculty_sessions.items():
        for i, session1 in enumerate(sessions):
            for j, session2 in enumerate(sessions[i+1:], i+1):
                if (session1.block.day == session2.block.day and 
                    session1.block.overlaps(session2.block)):
                        key1 = f"{session1.course_code}_{session1.section}"
                        key2 = f"{session2.course_code}_{session2.section}"
                        if key1 != key2:
                            final_conflicts.append((faculty, session1.course_code, session2.course_code))
    
    if final_conflicts:
        print(f"WARNING: {len(final_conflicts)} conflicts remain after resolution attempts")
        for faculty, course1, course2 in final_conflicts[:5]:
            print(f"  - {faculty} : {course1} vs {course2}")
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
            period = normalize_period(s.get("period", "PRE"))
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
            s.section, normalize_period(getattr(s, "period", "PRE")), s.block, s.course_code,
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
    def get_block(s):
        if isinstance(s, dict):
            return s.get("time_block") or s.get("block")
        return getattr(s, "block", None)

    def same_block(b1, b2):
        if not b1 or not b2: return False
        return (b1.day == b2.day and 
                b1.start.hour == b2.start.hour and 
                b1.start.minute == b2.start.minute and 
                b1.end.hour == b2.end.hour and 
                b1.end.minute == b2.end.minute)

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
            return sec, normalize_period(session_ref.get("period", "PRE"))
        return getattr(session_ref, "section", sec), normalize_period(getattr(session_ref, "period", "PRE"))

    def rebuild_occupied_slots_from_all() -> None:
        occupied_slots.clear()
        for sess in all_sessions:
            add_session_to_occupied_slots(sess, occupied_slots)

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
                    import re
                    match = re.search(r'Sem(\d+)', str(sec))
                    sem = int(match.group(1)) if match else 1
                    lunch_blocks_dict = get_lunch_blocks()
                    lunch_base = lunch_blocks_dict.get(sem)
                    lunch_blocks = []
                    if lunch_base:
                        for d in WORKING_DAYS:
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
                period = normalize_period(session_to_move.get("period", "PRE"))
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
                section_key = f"{session_to_move.section}_{normalize_period(getattr(session_to_move, 'period', 'PRE'))}"
                occupied_slots[section_key] = [
                    (blk, course) for blk, course in occupied_slots.get(section_key, [])
                    if not (blk.day == old_block.day and blk.start == old_block.start and blk.end == old_block.end and course == session_to_move.course_code)
                ]
                occupied_slots[section_key].append((new_slot, session_to_move.course_code))
                print(
                    f"[SECTION-OVERLAP] Moved {session_to_move.course_code} ({session_to_move.section} {session_to_move.period}) "
                    f"from {old_block.day} {old_block.start}-{old_block.end} to {new_slot.day} {new_slot.start}-{new_slot.end}"
                )

            # Canonical rebuild after every move prevents stale occupancy drift across passes.
            rebuild_occupied_slots_from_all()

    return all_sessions

def find_alternative_slot(session: ScheduledSession, 
                         all_sessions: List[ScheduledSession],
                         occupied_slots: Dict[str, List[TimeBlock]],
                         classrooms: List[ClassRoom],
                         attempt: int = 0,
                         verbose: bool = False) -> Optional[TimeBlock]:
    """Find alternative time slot with expanded search and multiple strategies"""
    
    # Get available days from configuration
    days = list(WORKING_DAYS)
    # Deterministic day order across processes/runs for the same session+attempt.
    _alt_rng = random.Random(
        _stable_seed_int(
            "phase5_alt_slot",
            attempt,
            getattr(session, "course_code", ""),
            getattr(session, "section", ""),
            getattr(getattr(session, "block", None), "day", ""),
            getattr(session, "period", ""),
        )
    )
    _alt_rng.shuffle(days)
    
    # Get semester for lunch check (robustly)
    semester = 1
    try:
        import re
        match = re.search(r'Sem(\d+)', str(session.section))
        if match: semester = int(match.group(1))
    except: pass
    lunch_blocks_dict = get_lunch_blocks()
    lunch_base = lunch_blocks_dict.get(semester)
    # Create lunch blocks for all days
    lunch_blocks = []
    if lunch_base:
        for day in WORKING_DAYS:
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
    
    ds_h = DAY_START_TIME.hour
    de_h = DAY_END_TIME.hour
    day_start_m = time_to_minutes(DAY_START_TIME)
    day_end_m = time_to_minutes(DAY_END_TIME)
    # Sub-ranges clipped to configured college window (no hardcoded 8–20 overflow)
    time_ranges = [
        (ds_h, min(11, de_h)),
        (11, min(13, de_h)),
        (13, min(16, de_h)),
        (16, de_h),
        (ds_h, min(13, de_h)),
        (13, de_h),
        (ds_h, de_h),
    ]

    if attempt < len(time_ranges):
        start_hour, end_hour = time_ranges[attempt]
    else:
        start_hour, end_hour = DAY_START_TIME.hour, DAY_END_TIME.hour

    # Try each day
    for day in days:
        # Try time slots in the selected range
        for hour in range(start_hour, end_hour + 1):
            for minute in range(0, 60, 5):  # 5-minute granularity
                start_time = time(hour, minute)
                start_m = hour * 60 + minute
                if start_m < day_start_m:
                    continue
                end_minutes = start_m + duration
                if end_minutes > day_end_m:
                    continue

                end_time = time(end_minutes // 60, end_minutes % 60)
                if not validate_time_range(start_time, end_time):
                    continue
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
                if is_slot_available(test_slot, session, all_sessions, occupied_slots, lunch_blocks, verbose=verbose):
                    return test_slot
    
    return None

def is_slot_available(slot: TimeBlock, 
                     session: ScheduledSession,
                     all_sessions: List,
                     occupied_slots: Dict[str, List[TimeBlock]],
                     lunch_blocks: List[TimeBlock],
                     verbose: bool = False) -> bool:
    """Check if a time slot is available for rescheduling.

    Faculty overlap uses the same token rules as check_faculty_availability_in_period /
    strict verification (faculty_token_set), not naive comma-split string equality.
    """
    from utils.faculty_conflict_utils import faculty_token_set, _coerce_session_timeblock_dict

    # DEBUG: Diagnostic for specific session
    is_sunil = "sunil" in (getattr(session, 'faculty', '') or "").lower()
    
    # Check library conflicts
    if is_sunil:
        print(f"      DEBUG: Checking availability for {session.course_code} ({session.section}) at {slot}")

    # Check lunch conflict
    for lunch in lunch_blocks:
        if slot.overlaps(lunch):
            if is_sunil: print(f"      DEBUG: Slot {slot} overlaps with lunch {lunch}")
            return False
    
    # Check occupied slots for this section (exclude the current session being moved)
    section_key = f"{session.section}_{session.period}"
    for blk, course_code in occupied_slots.get(section_key, []):
        # Skip if this is the same session we're trying to move
        if course_code == session.course_code and blk.day == session.block.day and blk.start == session.block.start:
            continue
        if slot.overlaps(blk):
            if is_sunil: print(f"      DEBUG: Slot {slot} overlaps with section {section_key} session {course_code} at {blk}")
            return False
    
    # Check faculty conflicts (align with check_faculty_availability_in_period: wall-clock, token set).
    if str(getattr(session, "kind", "") or "").strip().upper() != "P":
        sess_faculty = getattr(session, "faculty", None) or getattr(session, "instructor", None) or ""
        if sess_faculty and str(sess_faculty).strip().upper() not in ("TBD", "VARIOUS", "-", "MULTIPLE"):
            candidate_tokens = faculty_token_set(sess_faculty)
            if candidate_tokens:
                for other_session in all_sessions:
                    if other_session is session:
                        continue

                    if isinstance(other_session, dict):
                        st = (
                            other_session.get("session_type")
                            or other_session.get("Session Type")
                            or other_session.get("kind")
                            or ""
                        )
                        if str(st).strip().upper() == "P":
                            continue
                        other_faculty_str = other_session.get("instructor") or other_session.get("faculty")
                        other_block = _coerce_session_timeblock_dict(other_session)
                        other_course = other_session.get("course_code", "")
                        other_section = other_session.get("sections", "")
                        if isinstance(other_section, list):
                            other_section = ",".join(str(x) for x in other_section)
                        else:
                            other_section = str(other_section or "")
                    elif hasattr(other_session, "block"):
                        if str(getattr(other_session, "kind", "") or "").strip().upper() == "P":
                            continue
                        other_faculty_str = getattr(other_session, "faculty", None) or getattr(
                            other_session, "instructor", None
                        )
                        other_block = getattr(other_session, "block", None)
                        if not other_block:
                            tb = getattr(other_session, "time_block", None)
                            if isinstance(tb, TimeBlock):
                                other_block = tb
                            elif tb:
                                other_block = _coerce_session_timeblock_dict(
                                    {
                                        "time_block": tb,
                                        "day": getattr(getattr(other_session, "block", None), "day", None) or "",
                                    }
                                )
                        other_course = getattr(other_session, "course_code", "")
                        other_section = getattr(other_session, "section", "") or ""
                    else:
                        continue

                    if not other_faculty_str or not other_block:
                        continue

                    other_tokens = faculty_token_set(
                        other_faculty_str if isinstance(other_faculty_str, str) else str(other_faculty_str or "")
                    )
                    if not (candidate_tokens & other_tokens):
                        continue

                    # Same physical session (old position of the session being moved)
                    if (
                        str(other_course).split("-")[0].strip().upper()
                        == str(session.course_code or "").split("-")[0].strip().upper()
                        and str(other_section).strip() == str(session.section or "").strip()
                        and other_block.day == session.block.day
                        and other_block.start == session.block.start
                        and other_block.end == session.block.end
                    ):
                        continue

                    if other_block.day == slot.day and slot.overlaps(other_block):
                        if verbose:
                            print(
                                f"      DEBUG: Slot {slot} overlaps with faculty session "
                                f"{other_course} ({other_section}) at {other_block}"
                            )
                        return False
    
    # Check elective conflicts
    semester = 1
    try:
        parts = session.section.split('-')
        for p in parts:
            if 'Sem' in p:
                semester = int(p.replace('Sem', ''))
                break
    except:
        pass
        
    if check_elective_conflict(slot.day, slot.start, slot.end, semester):
        if is_sunil: print(f"      DEBUG: Slot {slot} has elective conflict for Sem {semester}")
        return False
    
    return True
