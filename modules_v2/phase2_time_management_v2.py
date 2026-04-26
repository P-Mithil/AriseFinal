"""
Phase 2: Time Management & Dynamic Output Grid
Implements time slot generation using 15-minute base slots (9:00 AM - 6:00 PM) 
with globally fixed staggered lunch breaks.
"""

import os
import sys
from datetime import time, timedelta, datetime
from typing import List, Dict, Tuple

# Add the parent directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_models import TimeSlot, TimeBlock, DayScheduleGrid
from config.schedule_config import WORKING_DAYS, LUNCH_WINDOWS

def generate_base_time_slots() -> List[TimeSlot]:
    """Generate 15-minute base slots from 9:00 AM to 6:00 PM for all days"""
    days = WORKING_DAYS
    all_slots = []
    
    for day in days:
        # Generate slots from 9:00 AM to 6:00 PM (36 slots of 15 minutes each)
        current_time = time(9, 0)  # 9:00 AM
        end_time = time(18, 0)    # 6:00 PM
        
        while current_time < end_time:
            # Calculate end time for this 15-minute slot
            next_time = (datetime.combine(datetime.min, current_time) + timedelta(minutes=15)).time()
            if next_time > end_time:
                next_time = end_time
            
            slot = TimeSlot(
                day=day,
                start_time=current_time,
                end_time=next_time
            )
            all_slots.append(slot)
            
            # Move to next slot
            current_time = next_time
    
    return all_slots

def get_lunch_blocks() -> Dict[int, TimeBlock]:
    """Return lunch blocks for each semester (times from LUNCH_WINDOWS; Monday as anchor day)."""
    return {
        sem: TimeBlock("Monday", start_t, end_t)
        for sem, (start_t, end_t) in LUNCH_WINDOWS.items()
    }

def check_lunch_conflict(session_time: TimeBlock, semester: int) -> bool:
    """Check if a time block conflicts with lunch"""
    return session_time.overlaps_with_lunch(semester)

def merge_consecutive_slots(slots: List[TimeSlot]) -> TimeBlock:
    """Merge consecutive 15-min slots into one time block"""
    if not slots:
        raise ValueError("Cannot merge empty slot list")
    
    if len(slots) == 1:
        return TimeBlock(slots[0].day, slots[0].start_time, slots[0].end_time)
    
    # Sort slots by start time
    sorted_slots = sorted(slots, key=lambda s: s.start_time)
    
    # Check if slots are consecutive
    for i in range(len(sorted_slots) - 1):
        if sorted_slots[i].end_time != sorted_slots[i + 1].start_time:
            raise ValueError("Slots must be consecutive to merge")
    
    # Create merged block
    first_slot = sorted_slots[0]
    last_slot = sorted_slots[-1]
    
    return TimeBlock(
        day=first_slot.day,
        start=first_slot.start_time,
        end=last_slot.end_time
    )

def create_time_block_from_slots(day: str, start_time: time, duration_minutes: int) -> TimeBlock:
    """Create a TimeBlock from start time and duration"""
    start_dt = datetime.combine(datetime.min, start_time)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    end_time = end_dt.time()
    
    return TimeBlock(day, start_time, end_time)

def validate_no_lunch_conflicts(sessions: List[Tuple[TimeBlock, str]], semester: int) -> bool:
    """Ensure no course overlaps with lunch"""
    lunch_blocks = get_lunch_blocks()
    if semester not in lunch_blocks:
        return True  # No lunch defined for this semester
    
    lunch = lunch_blocks[semester]
    for time_block, course in sessions:
        if time_block.overlaps(lunch):
            print(f"Conflict: {course} at {time_block} overlaps with lunch {lunch}")
            return False
    return True

def validate_time_slots(sessions: List[Tuple[TimeBlock, str]]) -> bool:
    """Ensure all slots are within 9:00-18:00"""
    for time_block, course in sessions:
        if time_block.start < time(9, 0) or time_block.end > time(18, 0):
            print(f"Invalid time: {course} at {time_block} is outside 9:00-18:00")
            return False
    return True

def create_day_schedule_grid(day: str, semester: int) -> DayScheduleGrid:
    """Create a new day schedule grid"""
    return DayScheduleGrid(day, semester)

def add_lunch_to_schedule(grid: DayScheduleGrid):
    """Add lunch break to the schedule grid"""
    lunch_block = grid.lunch_block
    grid.sessions.append((lunch_block, "LUNCH"))

def add_break_after_session(grid: DayScheduleGrid, session_end_time: time, day: str):
    """Add 15-minute break after a session"""
    break_start = session_end_time
    break_end = (datetime.combine(datetime.min, break_start) + timedelta(minutes=15)).time()
    
    break_block = TimeBlock(day, break_start, break_end)
    grid.sessions.append((break_block, "Break(15min)"))

def get_available_time_slots(day: str, semester: int, existing_sessions: List[Tuple[TimeBlock, str]]) -> List[TimeBlock]:
    """Get available time slots for a day, avoiding lunch and existing sessions"""
    # Generate all possible 15-minute slots for the day
    base_slots = [slot for slot in generate_base_time_slots() if slot.day == day]
    
    # Get lunch block
    lunch_blocks = get_lunch_blocks()
    lunch = lunch_blocks.get(semester)
    
    available_slots = []
    for slot in base_slots:
        slot_block = TimeBlock(slot.day, slot.start_time, slot.end_time)
        
        # Check if slot conflicts with lunch
        if lunch and slot_block.overlaps(lunch):
            continue
            
        # Check if slot conflicts with existing sessions
        conflicts = False
        for existing_block, _ in existing_sessions:
            if slot_block.overlaps(existing_block):
                conflicts = True
                break
        
        if not conflicts:
            available_slots.append(slot_block)
    
    return available_slots

def run_phase2() -> Dict[str, any]:
    """Run Phase 2: Time Management"""
    print("=== PHASE 2: TIME MANAGEMENT ===")
    
    # Generate base time slots
    base_slots = generate_base_time_slots()
    print(f"Generated {len(base_slots)} base time slots (15-min each)")
    
    # Test lunch blocks
    lunch_blocks = get_lunch_blocks()
    print(f"Lunch blocks configured for {len(lunch_blocks)} semesters")
    for sem, lunch in lunch_blocks.items():
        print(f"  Semester {sem}: {lunch}")
    
    # Test merging consecutive slots
    test_slots = [
        TimeSlot("Monday", time(9, 0), time(9, 15)),
        TimeSlot("Monday", time(9, 15), time(9, 30)),
        TimeSlot("Monday", time(9, 30), time(9, 45)),
        TimeSlot("Monday", time(9, 45), time(10, 0)),
        TimeSlot("Monday", time(10, 0), time(10, 15)),
        TimeSlot("Monday", time(10, 15), time(10, 30))
    ]
    
    merged_block = merge_consecutive_slots(test_slots)
    print(f"Test merge: 6x15min slots = {merged_block} ({merged_block.duration_minutes()} minutes)")
    
    # Test day schedule grid
    test_grid = create_day_schedule_grid("Monday", 1)
    print(f"Created schedule grid for {test_grid.day}, Semester {test_grid.semester}")
    
    # Test adding sessions
    test_session = TimeBlock("Monday", time(9, 0), time(10, 30))
    test_grid.add_session(test_session, "MA161")
    print(f"Added session: MA161 at {test_session}")
    
    # Test lunch conflict
    lunch_conflict_session = TimeBlock("Monday", time(12, 0), time(13, 0))
    try:
        test_grid.add_session(lunch_conflict_session, "CONFLICT_COURSE")
        print("ERROR: Should have failed due to lunch conflict")
    except ValueError as e:
        print(f"Correctly caught lunch conflict: {e}")
    
    # Test validation
    sessions = [(test_session, "MA161")]
    valid_lunch = validate_no_lunch_conflicts(sessions, 1)
    valid_time = validate_time_slots(sessions)
    print(f"Validation - No lunch conflicts: {valid_lunch}, Valid times: {valid_time}")
    
    return {
        "base_slots": base_slots,
        "lunch_blocks": lunch_blocks,
        "test_grid": test_grid
    }

if __name__ == "__main__":
    from datetime import datetime
    result = run_phase2()
    print("\nPhase 2 completed successfully!")
