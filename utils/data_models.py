from dataclasses import dataclass, field
from datetime import time, datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd

from config.schedule_config import LUNCH_WINDOWS

@dataclass(order=True)
class Time:
    hour: int
    minute: int

    def to_datetime(self, day: str = "1970-01-01") -> datetime:
        return datetime.strptime(f"{day} {self.hour:02d}:{self.minute:02d}", "%Y-%m-%d %H:%M")

    def __str__(self):
        return f"{self.hour:02d}:{self.minute:02d}"

@dataclass
class TimeSlot:
    """15-minute base time slot"""
    day: str
    start_time: time
    end_time: time
    
    def __add__(self, other: "TimeSlot") -> "TimeBlock":
        """Allow merging consecutive slots into a TimeBlock"""
        if self.day != other.day:
            raise ValueError("Cannot merge slots from different days")
        if self.end_time != other.start_time:
            raise ValueError("Slots must be consecutive to merge")
        
        return TimeBlock(
            day=self.day,
            start=self.start_time,
            end=other.end_time
        )
    
    def __str__(self):
        return f"{self.day} {self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"

@dataclass(order=True)
class TimeBlock:
    day: str
    start: time
    end: time

    def duration_minutes(self) -> int:
        dt_start = datetime.combine(datetime.min, self.start)
        dt_end = datetime.combine(datetime.min, self.end)
        return int((dt_end - dt_start).total_seconds() / 60)

    def overlaps(self, other: "TimeBlock", buffer_minutes: int = 0) -> bool:
        if self.day != other.day:
            return False
        
        self_start_dt = datetime.combine(datetime.min, self.start)
        self_end_dt = datetime.combine(datetime.min, self.end)
        other_start_dt = datetime.combine(datetime.min, other.start)
        other_end_dt = datetime.combine(datetime.min, other.end)

        # Apply buffer to both ends of the current block
        self_start_buffered = self_start_dt - timedelta(minutes=buffer_minutes)
        self_end_buffered = self_end_dt + timedelta(minutes=buffer_minutes)

        return not (self_end_buffered <= other_start_dt or self_start_buffered >= other_end_dt)

    def overlaps_with_lunch(self, semester: int) -> bool:
        """Check if this time block overlaps with lunch for given semester"""
        window = LUNCH_WINDOWS.get(semester)
        if not window:
            return False

        start_t, end_t = window
        lunch = TimeBlock(self.day, start_t, end_t)
        # Check overlap for the same day
        return self.overlaps(lunch)

    def __str__(self):
        return f"{self.day} {self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


def section_has_time_conflict(
    occupied_slots: Dict,
    section_label: str,
    period: str,
    candidate: "TimeBlock",
) -> bool:
    """
    Check whether a candidate time block overlaps any already-occupied slot
    for the same section+period.

    Supports occupied slot formats used across phases:
    - List[(TimeBlock, course_code)] tuples
    - List[TimeBlock] (legacy)
    """
    key = f"{section_label}_{period}"
    for existing in occupied_slots.get(key, []) or []:
        if isinstance(existing, tuple) and len(existing) >= 1:
            existing_block = existing[0]
        else:
            existing_block = existing
        if existing_block and candidate.overlaps(existing_block):
            return True
    return False

@dataclass
class Course:
    code: str
    name: str
    credits: int
    is_elective: bool
    semester: int
    department: str
    instructors: List[str] = field(default_factory=list)
    num_faculty: int = 0
    is_combined: bool = False
    ltpsc: str = ""
    registered_students: int = 0
    half_semester: bool = False
    elective_group: Optional[str] = None

    def __post_init__(self):
        # Calculate number of faculty
        self.num_faculty = len(self.instructors)
        
        # Determine if course is combined (core, <=2 credits, single instructor)
        self.is_combined = (
            not self.is_elective and 
            self.credits <= 2 and 
            self.num_faculty == 1
        )

@dataclass
class Section:
    program: str  # e.g., "CSE", "DSAI", "ECE"
    group: int    # 1 for CSE-A/B, 2 for DSAI-A/ECE-A
    name: str     # e.g., "A", "B"
    semester: int
    students: int
    courses: List[Course] = field(default_factory=list)
    schedule: List['ScheduledSession'] = field(default_factory=list)
    
    @property
    def label(self) -> str:
        return f"{self.program}-{self.name}-Sem{self.semester}"

@dataclass
class ClassRoom:
    room_number: str
    room_type: str
    capacity: int
    facilities: List[str] = field(default_factory=list)
    lab_type: Optional[str] = None  # "Hardware" or "Software" for lab rooms only
    is_research_lab: bool = False  # True if Description/Type indicates research lab (excluded from course scheduling)

@dataclass
class ScheduledSession:
    course_code: str
    section: str
    kind: str  # "L" for Lecture, "T" for Tutorial, "P" for Practical, "COMBINED", "ELECTIVE", "LUNCH", "BREAK"
    block: TimeBlock
    room: Optional[str] = None
    period: Optional[str] = None  # "PRE" or "POST"
    lab_number: Optional[str] = None  # For tracking which lab (LAB1, LAB2)
    faculty: Optional[str] = None     # For multi-faculty courses

@dataclass
class TimeModel:
    start_time: time
    end_time: time
    slot_duration_minutes: int
    days: List[str]
    lunch_breaks: Dict[int, Dict[str, TimeBlock]]  # {semester: {day: TimeBlock}}

class DayScheduleGrid:
    """Dynamic schedule grid for a single day"""
    def __init__(self, day: str, semester: int):
        self.day = day
        self.semester = semester
        self.sessions = []  # List of (time_block, course_code)
        self.lunch_block = self._get_lunch_for_semester()
    
    def _get_lunch_for_semester(self) -> TimeBlock:
        """Get lunch block for the semester"""
        lunch_blocks = {
            1: TimeBlock(self.day, time(12, 30), time(13, 30)),  # Sem 1
            3: TimeBlock(self.day, time(12, 45), time(13, 45)),  # Sem 3  
            5: TimeBlock(self.day, time(13, 0), time(14, 0)),   # Sem 5
        }
        return lunch_blocks.get(self.semester, TimeBlock(self.day, time(12, 30), time(13, 30)))
    
    def add_session(self, time_block: TimeBlock, course: str):
        """Add a session, check for conflicts"""
        # Allow classes right after lunch - no lunch conflict check
        self.sessions.append((time_block, course))
    
    def get_dynamic_time_slots(self) -> List[str]:
        """Return only the time slots that have sessions"""
        time_slots = []
        for time_block, _ in self.sessions:
            time_slots.append(f"{time_block.start.strftime('%H:%M')}-{time_block.end.strftime('%H:%M')}")
        return time_slots
    
    def get_sessions_with_times(self) -> List[tuple]:
        """Return sessions with their time slot strings"""
        sessions_with_times = []
        for time_block, course in self.sessions:
            time_str = f"{time_block.start.strftime('%H:%M')}-{time_block.end.strftime('%H:%M')}"
            sessions_with_times.append((time_str, course))
        return sessions_with_times

def parse_instructors(instructor_string: str) -> List[str]:
    """Parse instructor string by splitting on comma and cleaning whitespace"""
    if not instructor_string or pd.isna(instructor_string):
        return []
    
    instructors = [instructor.strip() for instructor in str(instructor_string).split(',')]
    return [inst for inst in instructors if inst]  # Remove empty strings

def create_course_from_row(row) -> Course:
    """Create Course object from Excel row data"""
    # Parse instructors
    instructors = parse_instructors(row['Instructor'])
    
    # Determine if elective
    is_elective = str(row['Elective (Yes/No)']).lower() == 'yes'
    
    # Determine if half semester
    half_semester = str(row['Half Semester (Yes/No)']).lower() == 'yes'
    
    # Read elective group (handle missing column gracefully)
    elective_group = None
    if 'Elective groups' in row and pd.notna(row['Elective groups']):
        elective_group = str(row['Elective groups']).strip()
        if elective_group == '' or elective_group.lower() == 'nan':
            elective_group = None
    
    return Course(
        code=row['Course Code'],
        name=row['Course Name'],
        credits=int(row['Credits']),
        is_elective=is_elective,
        semester=int(row['Semester']),
        department=row['Department'],
        instructors=instructors,
        ltpsc=row['LTPSC'],
        registered_students=int(row['Registered Students']),
        half_semester=half_semester,
        elective_group=elective_group
    )

def create_classroom_from_row(row) -> ClassRoom:
    """Create ClassRoom object from Excel row data.
    Optional column 'Lab Type' or 'Type 2': 'Hardware' or 'Software' for lab rows.
    If room_type is lab and Lab Type is missing, lab_type is left None (treated as Software).
    """
    lab_type = None
    # Handle different column name formats
    if 'Room Number' in row:
        room_number = row['Room Number']
        room_type = row['Type']
        capacity = int(row['Capacity'])
        facilities = []
        if 'Facilities' in row and pd.notna(row['Facilities']):
            facilities = [f.strip() for f in str(row['Facilities']).split(',')]
    elif 'Room' in row:
        # New format: Room, Description, Seating Capacity
        room_number = row['Room']
        room_type = row.get('Description', 'Classroom')
        capacity = int(row['Seating Capacity']) if pd.notna(row['Seating Capacity']) else 0
        facilities = []
    else:
        raise ValueError(f"Unknown classroom data format. Available columns: {list(row.keys())}")
    
    # Lab type (Hardware/Software) from Description only
    if room_type and 'lab' in str(room_type).lower():
        desc_text = None
        if 'Description' in row and pd.notna(row.get('Description')):
            desc_text = str(row['Description']).lower()
        elif room_type:
            desc_text = str(room_type).lower()  # e.g. "Room, Description, Seating" format uses Description as type
        if desc_text:
            if 'hardware' in desc_text:
                lab_type = 'Hardware'
            elif 'software' in desc_text:
                lab_type = 'Software'
    
    # Mark research labs (excluded from course practical scheduling)
    is_research_lab = False
    if room_type and 'research' in str(room_type).lower():
        is_research_lab = True
    if 'Description' in row and pd.notna(row.get('Description')) and 'research' in str(row['Description']).lower():
        is_research_lab = True
    
    return ClassRoom(
        room_number=room_number,
        room_type=room_type,
        capacity=capacity,
        facilities=facilities,
        lab_type=lab_type,
        is_research_lab=is_research_lab
    )
