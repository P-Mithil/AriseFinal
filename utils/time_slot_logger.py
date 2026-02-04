"""
Time Slot Logger Utility
Centralized logging for time slots used across all phases
"""

from dataclasses import dataclass, field
from datetime import time
from typing import List, Dict, Optional
from collections import defaultdict
import os
from utils.data_models import TimeBlock

@dataclass
class TimeSlotLogEntry:
    """Single time slot log entry"""
    phase: str  # Phase number (e.g., "Phase 3", "Phase 4")
    course_code: str
    section: str
    day: str
    start_time: time
    end_time: time
    room: Optional[str] = None
    period: Optional[str] = None  # "PRE" or "POST"
    session_type: str = ""  # "L", "T", "P", "ELECTIVE", "COMBINED"
    faculty: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for easy export"""
        return {
            'Phase': self.phase,
            'Course Code': self.course_code,
            'Section': self.section,
            'Day': self.day,
            'Start Time': self.start_time.strftime('%H:%M'),
            'End Time': self.end_time.strftime('%H:%M'),
            'Room': self.room or '',
            'Period': self.period or '',
            'Session Type': self.session_type,
            'Faculty': self.faculty or ''
        }
    
    def get_time_block(self) -> TimeBlock:
        """Get TimeBlock representation"""
        return TimeBlock(self.day, self.start_time, self.end_time)

class TimeSlotLogger:
    """Centralized time slot logger"""
    
    def __init__(self):
        self.entries: List[TimeSlotLogEntry] = []
        self._phase_stats = defaultdict(lambda: {'count': 0, 'courses': set(), 'sections': set()})
    
    def log_slot(self, phase: str, course_code: str, section: str, 
                 day: str, start_time: time, end_time: time,
                 room: Optional[str] = None, period: Optional[str] = None,
                 session_type: str = "", faculty: Optional[str] = None):
        """Log a time slot"""
        entry = TimeSlotLogEntry(
            phase=phase,
            course_code=course_code,
            section=section,
            day=day,
            start_time=start_time,
            end_time=end_time,
            room=room,
            period=period,
            session_type=session_type,
            faculty=faculty
        )
        self.entries.append(entry)
        
        # Update phase statistics
        self._phase_stats[phase]['count'] += 1
        self._phase_stats[phase]['courses'].add(course_code)
        self._phase_stats[phase]['sections'].add(section)
    
    def log_session(self, phase: str, session):
        """Log a ScheduledSession object"""
        if hasattr(session, 'block') and hasattr(session, 'course_code'):
            self.log_slot(
                phase=phase,
                course_code=session.course_code,
                section=session.section,
                day=session.block.day,
                start_time=session.block.start,
                end_time=session.block.end,
                room=session.room,
                period=getattr(session, 'period', None),
                session_type=session.kind,
                faculty=getattr(session, 'faculty', None)
            )
    
    def check_conflict(self, day: str, start_time: time, end_time: time,
                      section: Optional[str] = None,
                      room: Optional[str] = None,
                      period: Optional[str] = None) -> List[TimeSlotLogEntry]:
        """Check for conflicts with existing slots"""
        test_block = TimeBlock(day, start_time, end_time)
        conflicts = []
        
        for entry in self.entries:
            entry_block = entry.get_time_block()
            
            # Check time overlap
            if entry_block.overlaps(test_block):
                # Check if it's a real conflict
                is_conflict = False
                
                # Same section, same period = conflict
                if section and period:
                    if entry.section == section and entry.period == period:
                        is_conflict = True
                
                # Same room = conflict
                if room and entry.room:
                    if entry.room == room and entry_block.overlaps(test_block):
                        is_conflict = True
                
                if is_conflict:
                    conflicts.append(entry)
        
        return conflicts
    
    def check_room_conflict(self, room: str, day: str, start_time: time, end_time: time) -> List[TimeSlotLogEntry]:
        """Check for room conflicts specifically"""
        test_block = TimeBlock(day, start_time, end_time)
        conflicts = []
        
        for entry in self.entries:
            if entry.room == room:
                entry_block = entry.get_time_block()
                if entry_block.overlaps(test_block):
                    conflicts.append(entry)
        
        return conflicts
    
    def get_phase_summary(self, phase: str) -> dict:
        """Get summary statistics for a phase"""
        phase_entries = [e for e in self.entries if e.phase == phase]
        
        if not phase_entries:
            return {
                'phase': phase,
                'total_slots': 0,
                'unique_courses': 0,
                'unique_sections': 0,
                'by_day': {},
                'by_session_type': {}
            }
        
        by_day = defaultdict(int)
        by_session_type = defaultdict(int)
        
        for entry in phase_entries:
            by_day[entry.day] += 1
            by_session_type[entry.session_type] += 1
        
        return {
            'phase': phase,
            'total_slots': len(phase_entries),
            'unique_courses': len(set(e.course_code for e in phase_entries)),
            'unique_sections': len(set(e.section for e in phase_entries)),
            'by_day': dict(by_day),
            'by_session_type': dict(by_session_type)
        }
    
    def get_all_summaries(self) -> List[dict]:
        """Get summaries for all phases"""
        phases = sorted(set(e.phase for e in self.entries))
        return [self.get_phase_summary(phase) for phase in phases]
    
    def print_summary(self):
        """Print a summary of all logged slots"""
        print("\n" + "=" * 80)
        print("TIME SLOT LOGGING SUMMARY")
        print("=" * 80)
        
        for phase in sorted(self._phase_stats.keys()):
            stats = self._phase_stats[phase]
            summary = self.get_phase_summary(phase)
            print(f"\n{phase}:")
            print(f"  Total slots logged: {summary['total_slots']}")
            print(f"  Unique courses: {summary['unique_courses']}")
            print(f"  Unique sections: {summary['unique_sections']}")
            print(f"  By day: {summary['by_day']}")
            print(f"  By session type: {summary['by_session_type']}")
        
        print("\n" + "=" * 80)
    
    def export_to_csv(self, output_path: str):
        """Export all entries to CSV"""
        import pandas as pd
        
        if not self.entries:
            print("No entries to export")
            return
        
        data = [entry.to_dict() for entry in self.entries]
        df = pd.DataFrame(data)
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"Time slot log exported to: {output_path}")
    
    def get_entries_by_phase(self, phase: str) -> List[TimeSlotLogEntry]:
        """Get all entries for a specific phase"""
        return [e for e in self.entries if e.phase == phase]
    
    def get_entries_by_section(self, section: str, period: Optional[str] = None) -> List[TimeSlotLogEntry]:
        """Get all entries for a specific section"""
        entries = [e for e in self.entries if e.section == section]
        if period:
            entries = [e for e in entries if e.period == period]
        return entries
    
    def get_entries_by_room(self, room: str) -> List[TimeSlotLogEntry]:
        """Get all entries for a specific room"""
        return [e for e in self.entries if e.room == room]

# Global logger instance
_global_logger = TimeSlotLogger()

def get_logger() -> TimeSlotLogger:
    """Get the global logger instance"""
    return _global_logger

def reset_logger():
    """Reset the global logger (useful for testing)"""
    global _global_logger
    _global_logger = TimeSlotLogger()

