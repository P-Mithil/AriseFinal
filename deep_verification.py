"""
Deep Verification Script for Timetable Generation
Comprehensive verification of all courses, LTPSC compliance, and phase rules
"""

import os
import sys
from collections import defaultdict
from datetime import time
import openpyxl
from typing import Dict, List, Tuple, Set

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules_v2.phase1_data_validation_v2 import run_phase1
from modules_v2.phase3_elective_baskets_v2 import run_phase3, ELECTIVE_BASKET_SLOTS
from modules_v2.phase4_combined_classes_v2_corrected import run_phase4_corrected as run_phase4
from modules_v2.phase5_core_courses import run_phase5, calculate_slots_needed, get_lunch_blocks
from modules_v2.phase6_faculty_conflicts import run_phase6_faculty_conflicts
from modules_v2.phase7_remaining_courses import run_phase7
from modules_v2.phase8_classroom_assignment import run_phase8
from utils.data_models import Course, Section, ScheduledSession, ClassRoom, TimeBlock


def find_latest_excel_file():
    """Find the most recently generated Excel file"""
    output_dir = "DATA/OUTPUT"
    if not os.path.exists(output_dir):
        return None
    
    excel_files = [f for f in os.listdir(output_dir) if f.endswith('.xlsx') and 'IIITDWD_24_Sheets' in f]
    if not excel_files:
        return None
    
    excel_files.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
    return os.path.join(output_dir, excel_files[0])


class DeepVerification:
    def __init__(self):
        self.issues = []
        self.warnings = []
        self.course_details = {}
        self.session_details = defaultdict(list)
        
    def log_issue(self, category: str, course_code: str, message: str, details: dict = None):
        """Log an issue"""
        issue = {
            'category': category,
            'course_code': course_code,
            'message': message,
            'details': details or {}
        }
        self.issues.append(issue)
        print(f"  [ISSUE] {course_code}: {message}")
        if details:
            for key, value in details.items():
                print(f"    - {key}: {value}")
    
    def log_warning(self, category: str, course_code: str, message: str):
        """Log a warning"""
        warning = {
            'category': category,
            'course_code': course_code,
            'message': message
        }
        self.warnings.append(warning)
        print(f"  [WARNING] {course_code}: {message}")
    
    def verify_ltpsc_compliance(self, course: Course, sessions: List, 
                                section: Section, period: str) -> Dict:
        """Verify LTPSC compliance for a course"""
        ltpsc = course.ltpsc
        slots_needed = calculate_slots_needed(ltpsc)
        
        # Section format: "CSE-A-Sem1" (from section.label)
        section_label = section.label  # e.g., "CSE-A-Sem1"
        section_patterns = [
            section_label,
            f"{section.program}-{section.name}",
            f"{section.program}-{section.name}-Sem{section.semester}"
        ]
        
        course_sessions = []
        for s in sessions:
            # Handle different session formats
            session_course_code = None
            
            # Check if it's a dict (combined sessions)
            if isinstance(s, dict):
                session_course_code = s.get('course_code', '').split('-')[0]  # Remove -TUT/-LAB suffix
                session_section = s.get('sections', [])
                session_period = s.get('period', '')
            # Check if it's a ScheduledSession object
            elif hasattr(s, 'course_code'):
                session_course_code = s.course_code.split('-')[0]  # Remove -TUT/-LAB suffix
                session_section = getattr(s, 'section', '')
                session_period = getattr(s, 'period', '')
            else:
                continue
            
            # Check course code match
            if session_course_code != course.code:
                continue
            
            # Check section match
            section_match = False
            if isinstance(s, dict):
                # For dict format, check if section_label is in the sections list
                if section_label in session_section:
                    section_match = True
            elif hasattr(s, 'section'):
                session_section_str = str(session_section)
                for pattern in section_patterns:
                    if pattern in session_section_str or session_section_str == pattern:
                        section_match = True
                        break
            
            # Check period match
            period_match = False
            session_period_str = str(session_period).upper()
            period_upper = period.upper()
            if session_period_str == period_upper or \
               (period_upper == 'PRE' and session_period_str in ['PRE', 'PREMID']) or \
               (period_upper == 'POST' and session_period_str in ['POST', 'POSTMID']):
                period_match = True
            
            if section_match and period_match:
                course_sessions.append(s)
        
        # Count scheduled sessions by type
        scheduled_lectures = 0
        scheduled_tutorials = 0
        scheduled_labs = 0
        
        for s in course_sessions:
            # Handle dict format (combined sessions)
            if isinstance(s, dict):
                session_type = s.get('session_type', 'L')
                if session_type == 'T':
                    scheduled_tutorials += 1
                elif session_type == 'P':
                    scheduled_labs += 1
                else:
                    scheduled_lectures += 1
            # Handle ScheduledSession objects
            elif hasattr(s, 'kind'):
                if s.kind == 'T':
                    scheduled_tutorials += 1
                elif s.kind == 'P':
                    scheduled_labs += 1
                else:
                    scheduled_lectures += 1
        
        # Determine requirements based on course type
        if course.credits > 2:
            # Phase 5: Full requirement in EACH period
            required_lectures = slots_needed['lectures']
            required_tutorials = slots_needed['tutorials']
            required_labs = slots_needed['practicals']
        else:
            # Phase 7: Full requirement in ONE period (half-semester)
            required_lectures = slots_needed['lectures']
            required_tutorials = slots_needed['tutorials']
            required_labs = slots_needed['practicals']
        
        compliance = {
            'ltpsc': ltpsc,
            'required': {
                'lectures': required_lectures,
                'tutorials': required_tutorials,
                'labs': required_labs
            },
            'scheduled': {
                'lectures': scheduled_lectures,
                'tutorials': scheduled_tutorials,
                'labs': scheduled_labs
            },
            'satisfied': {
                'lectures': scheduled_lectures >= required_lectures,
                'tutorials': scheduled_tutorials >= required_tutorials,
                'labs': scheduled_labs >= required_labs
            },
            'sessions': course_sessions
        }
        
        return compliance
    
    def verify_phase_rules(self, course: Course, sessions: List[ScheduledSession], 
                          section: Section, all_sessions: List[ScheduledSession],
                          elective_sessions: List, combined_sessions: List,
                          classrooms: List[ClassRoom]) -> List[str]:
        """Verify phase-specific rules for a course"""
        violations = []
        course_sessions = [s for s in sessions if hasattr(s, 'course_code') and s.course_code == course.code]
        
        # Phase 3: Elective basket rules
        if course.is_elective:
            # Check if scheduled in correct elective slots
            semester = course.semester
            if semester in ELECTIVE_BASKET_SLOTS:
                slots = ELECTIVE_BASKET_SLOTS[semester]
                for session in course_sessions:
                    if hasattr(session, 'block'):
                        day = session.block.day
                        start = session.block.start
                        # Check if matches elective slot times
                        if 'lecture_1' in slots:
                            if slots['lecture_1'].day == day and slots['lecture_1'].start != start:
                                violations.append(f"Elective not in correct lecture slot")
        
        # Phase 4: Combined class rules
        if course.is_combined:
            # Check if scheduled across multiple groups
            sections_in_combined = set()
            for session in combined_sessions:
                if isinstance(session, dict):
                    if session.get('course_code', '').split('-')[0] == course.code:
                        sections_in_combined.update(session.get('sections', []))
                elif hasattr(session, 'course_code') and session.course_code.split('-')[0] == course.code:
                    if hasattr(session, 'sections'):
                        sections_in_combined.update(session.sections)
            
            if len(sections_in_combined) < 2:
                violations.append(f"Combined course not scheduled across multiple groups")
        
        # Phase 5: Core course rules
        if course.credits > 2 and not course.is_elective and not course.is_combined:
            # Check if labs are in lab rooms
            lab_sessions = [s for s in course_sessions if hasattr(s, 'kind') and s.kind == 'P']
            for lab_session in lab_sessions:
                if hasattr(lab_session, 'room') and lab_session.room:
                    room_str = str(lab_session.room).upper()
                    if not ('LAB' in room_str or room_str.startswith('L')):
                        violations.append(f"Lab session not in lab room: {lab_session.room}")
                else:
                    violations.append(f"Lab session has no room assignment")
        
        # Phase 6: Faculty conflict check
        faculty_sessions = defaultdict(list)
        for session in course_sessions:
            if hasattr(session, 'faculty') and session.faculty and session.faculty not in ['TBD', 'Various']:
                if hasattr(session, 'block'):
                    faculty_sessions[session.faculty].append(session)
        
        for faculty, fac_sessions in faculty_sessions.items():
            # Check for time overlaps
            for i, s1 in enumerate(fac_sessions):
                for s2 in fac_sessions[i+1:]:
                    if (hasattr(s1, 'block') and hasattr(s2, 'block') and
                        s1.block.day == s2.block.day and s1.block.overlaps(s2.block)):
                        violations.append(f"Faculty {faculty} has overlapping sessions")
        
        # Phase 7: Half-semester course rules
        if course.credits <= 2 and not course.is_elective and not course.is_combined:
            # Should be scheduled in only one period
            periods = set()
            for session in course_sessions:
                if hasattr(session, 'period'):
                    periods.add(session.period)
            if len(periods) > 1:
                violations.append(f"Phase 7 course scheduled in multiple periods: {periods}")
        
        # Phase 8: Room assignment rules
        for session in course_sessions:
            if hasattr(session, 'room') and session.room:
                # Check if room capacity is sufficient
                room = next((r for r in classrooms if r.room_number == session.room), None)
                if room:
                    section_size = section.size
                    if room.capacity < section_size:
                        violations.append(f"Room {session.room} capacity {room.capacity} < section size {section_size}")
        
        return violations
    
    def verify_time_constraints(self, session: ScheduledSession) -> List[str]:
        """Verify time constraints for a session"""
        violations = []
        
        if not hasattr(session, 'block'):
            return violations
        
        block = session.block
        
        # Check time range (9:00-18:00)
        if block.start < time(9, 0) or block.end > time(18, 0):
            violations.append(f"Session outside college hours: {block.start}-{block.end}")
        
        # Check lunch break
        semester = int(session.section.split('-')[2].replace('Sem', '')) if hasattr(session, 'section') else 1
        lunch_blocks_dict = get_lunch_blocks()
        lunch_base = lunch_blocks_dict.get(semester)
        if lunch_base:
            lunch_block = TimeBlock(block.day, lunch_base.start, lunch_base.end)
            if block.overlaps(lunch_block):
                violations.append(f"Session overlaps lunch break: {lunch_block}")
        
        return violations
    
    def run_deep_verification(self, excel_path: str = None):
        """Run comprehensive deep verification"""
        print("="*100)
        print("DEEP VERIFICATION - COMPREHENSIVE TIMETABLE ANALYSIS")
        print("="*100)
        
        # Step 1: Load data
        print("\n[STEP 1] Loading course data...")
        courses, classrooms, sections = run_phase1()
        print(f"  Loaded {len(courses)} courses, {len(classrooms)} classrooms, {len(sections)} sections")
        
        # Step 2: Find Excel file
        if not excel_path:
            excel_path = find_latest_excel_file()
        
        if not excel_path or not os.path.exists(excel_path):
            print(f"\n[ERROR] Excel file not found: {excel_path}")
            print("Please generate timetable first using generate_24_sheets.py")
            return
        
        print(f"\n[STEP 2] Analyzing Excel file: {excel_path}")
        
        # Step 3: Re-run phases to get all sessions
        print("\n[STEP 3] Re-running phases to collect all sessions...")
        
        # Phase 3: Electives
        elective_sessions = []
        try:
            elective_baskets, elective_sessions = run_phase3(courses, sections)
            print(f"  Phase 3: {len(elective_sessions)} elective sessions")
        except Exception as e:
            print(f"  Phase 3 failed: {e}")
        
        # Phase 4: Combined classes
        combined_sessions = []
        try:
            phase4_result = run_phase4(courses, sections)
            # Extract sessions from phase4 result (it returns a dict with 'sessions' key)
            if isinstance(phase4_result, dict):
                combined_sessions = phase4_result.get('sessions', [])
            elif isinstance(phase4_result, list):
                combined_sessions = phase4_result
            print(f"  Phase 4: {len(combined_sessions)} combined sessions")
        except Exception as e:
            print(f"  Phase 4 failed: {e}")
        
        # Phase 5: Core courses
        phase5_sessions = []
        try:
            occupied_slots = defaultdict(list)
            # Add elective slots
            for session in elective_sessions:
                if hasattr(session, 'section') and hasattr(session, 'block'):
                    section_key = f"{session.section}_{getattr(session, 'period', 'PRE')}"
                    occupied_slots[section_key].append((session.block, session.course_code))
            
            phase5_sessions = run_phase5(courses, sections, classrooms, elective_sessions, 
                                        combined_sessions, occupied_slots, {})
            print(f"  Phase 5: {len(phase5_sessions)} core sessions")
        except Exception as e:
            print(f"  Phase 5 failed: {e}")
        
        # Phase 7: Remaining courses
        phase7_sessions = []
        try:
            # Update occupied_slots with Phase 5
            for session in phase5_sessions:
                if hasattr(session, 'section') and hasattr(session, 'block'):
                    section_key = f"{session.section}_{getattr(session, 'period', 'PRE')}"
                    occupied_slots[section_key].append((session.block, session.course_code))
            
            room_occupancy = {}
            phase7_sessions = run_phase7(courses, sections, classrooms, occupied_slots, 
                                        room_occupancy, combined_sessions, timeout_seconds=60)
            print(f"  Phase 7: {len(phase7_sessions)} remaining sessions")
        except Exception as e:
            print(f"  Phase 7 failed: {e}")
        
        all_sessions = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions
        
        # Step 4: Course-by-course verification
        print("\n[STEP 4] Verifying each course in detail...")
        print("-"*100)
        
        total_courses = len(courses)
        scheduled_courses = set()
        unscheduled_courses = []
        
        for course in courses:
            if course.is_elective:
                continue  # Skip electives for now
            
            # Find sessions for this course - handle different session formats
            course_sessions = []
            for s in all_sessions:
                # Handle dict format (combined sessions)
                if isinstance(s, dict):
                    session_code = s.get('course_code', '')
                    if isinstance(session_code, str):
                        # Remove suffixes like -TUT, -LAB
                        base_code = session_code.split('-')[0]
                        if base_code == course.code:
                            course_sessions.append(s)
                # Handle ScheduledSession objects
                elif hasattr(s, 'course_code'):
                    if s.course_code == course.code:
                        course_sessions.append(s)
                # Handle string course codes
                elif isinstance(s, str) and s == course.code:
                    course_sessions.append(s)
            
            if not course_sessions:
                # Check if it's a combined course that might be scheduled differently
                if not course.is_combined:
                    unscheduled_courses.append(course)
                continue
            
            scheduled_courses.add(course.code)
            
            # Verify for each section and period
            for section in sections:
                if section.program != course.department or section.semester != course.semester:
                    continue
                
                section_name = f"{section.program}-{section.name}-Sem{section.semester}"
                
                # For combined courses, check if scheduled across sections
                if course.is_combined:
                    # Combined courses are scheduled once for all sections
                    # Check if course appears in combined_sessions
                    found_in_combined = False
                    for cs in combined_sessions:
                        if isinstance(cs, dict):
                            cs_code = cs.get('course_code', '').split('-')[0]
                            cs_sections = cs.get('sections', [])
                            if cs_code == course.code and section_name in cs_sections:
                                found_in_combined = True
                                break
                        elif hasattr(cs, 'course_code'):
                            if cs.course_code.split('-')[0] == course.code:
                                found_in_combined = True
                                break
                    
                    if found_in_combined:
                        # Combined course is scheduled - verify once
                        compliance = self.verify_ltpsc_compliance(course, all_sessions, section, 'PRE')
                        # Skip per-period check for combined courses
                        continue
                
                for period in ['PRE', 'POST']:
                    # LTPSC compliance
                    compliance = self.verify_ltpsc_compliance(course, all_sessions, section, period)
                    
                    # Check if satisfied
                    all_satisfied = (compliance['satisfied']['lectures'] and 
                                    compliance['satisfied']['tutorials'] and 
                                    compliance['satisfied']['labs'])
                    
                    if not all_satisfied:
                        details = {
                            'section': section_name,
                            'period': period,
                            'ltpsc': compliance['ltpsc'],
                            'required': compliance['required'],
                            'scheduled': compliance['scheduled']
                        }
                        self.log_issue('LTPSC_COMPLIANCE', course.code, 
                                     f"LTPSC requirements not met in {section_name} {period}", details)
                    
                    # Phase rules
                    violations = self.verify_phase_rules(course, all_sessions, section, 
                                                        all_sessions, elective_sessions, 
                                                        combined_sessions, classrooms)
                    for violation in violations:
                        self.log_issue('PHASE_RULES', course.code, 
                                     f"{violation} in {section_name} {period}")
                    
                    # Time constraints
                    for session in compliance['sessions']:
                        time_violations = self.verify_time_constraints(session)
                        for violation in time_violations:
                            self.log_issue('TIME_CONSTRAINTS', course.code, 
                                         f"{violation} in {section_name} {period}")
        
        # Step 5: Summary statistics
        print("\n" + "="*100)
        print("VERIFICATION SUMMARY")
        print("="*100)
        
        print(f"\n[COURSE STATISTICS]")
        print(f"  Total courses: {total_courses}")
        print(f"  Scheduled courses: {len(scheduled_courses)}")
        print(f"  Unscheduled courses: {len(unscheduled_courses)}")
        
        if unscheduled_courses:
            print(f"\n  Unscheduled courses:")
            for course in unscheduled_courses:
                print(f"    - {course.code}: {course.name} ({course.department} Sem{course.semester})")
        
        print(f"\n[SESSION STATISTICS]")
        print(f"  Total sessions: {len(all_sessions)}")
        print(f"  Elective sessions: {len(elective_sessions)}")
        print(f"  Combined sessions: {len(combined_sessions)}")
        print(f"  Phase 5 sessions: {len(phase5_sessions)}")
        print(f"  Phase 7 sessions: {len(phase7_sessions)}")
        
        # Count by type
        lectures = sum(1 for s in all_sessions if hasattr(s, 'kind') and s.kind == 'L')
        tutorials = sum(1 for s in all_sessions if hasattr(s, 'kind') and s.kind == 'T')
        labs = sum(1 for s in all_sessions if hasattr(s, 'kind') and s.kind == 'P')
        print(f"  By type: {lectures} lectures, {tutorials} tutorials, {labs} labs")
        
        # Step 6: Issues report
        print(f"\n[ISSUES REPORT]")
        print(f"  Total issues: {len(self.issues)}")
        print(f"  Total warnings: {len(self.warnings)}")
        
        if self.issues:
            print(f"\n  Issues by category:")
            by_category = defaultdict(list)
            for issue in self.issues:
                by_category[issue['category']].append(issue)
            
            for category, issues in by_category.items():
                print(f"    {category}: {len(issues)} issues")
                for issue in issues[:5]:  # Show first 5
                    print(f"      - {issue['course_code']}: {issue['message']}")
                if len(issues) > 5:
                    print(f"      ... and {len(issues) - 5} more")
        
        if self.warnings:
            print(f"\n  Warnings:")
            for warning in self.warnings[:10]:
                print(f"    - {warning['course_code']}: {warning['message']}")
            if len(self.warnings) > 10:
                print(f"    ... and {len(self.warnings) - 10} more")
        
        # Step 7: Detailed course report
        print(f"\n[DETAILED COURSE REPORT]")
        print("-"*100)
        
        for course in sorted(courses, key=lambda c: (c.department, c.semester, c.code)):
            if course.is_elective:
                continue
            
            course_sessions = [s for s in all_sessions if hasattr(s, 'course_code') and s.course_code == course.code]
            
            print(f"\n{course.code}: {course.name}")
            print(f"  Department: {course.department}, Semester: {course.semester}, Credits: {course.credits}")
            print(f"  LTPSC: {course.ltpsc}")
            slots_needed = calculate_slots_needed(course.ltpsc)
            print(f"  Required: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P")
            print(f"  Scheduled sessions: {len(course_sessions)}")
            
            # Count by type
            course_lectures = sum(1 for s in course_sessions if hasattr(s, 'kind') and s.kind == 'L')
            course_tutorials = sum(1 for s in course_sessions if hasattr(s, 'kind') and s.kind == 'T')
            course_labs = sum(1 for s in course_sessions if hasattr(s, 'kind') and s.kind == 'P')
            print(f"  Scheduled: {course_lectures}L + {course_tutorials}T + {course_labs}P")
            
            # Check compliance
            if course.credits > 2:
                # Phase 5: Check both periods
                total_lectures = course_lectures
                total_tutorials = course_tutorials
                total_labs = course_labs
                if total_lectures >= slots_needed['lectures'] * 2 and \
                   total_tutorials >= slots_needed['tutorials'] * 2 and \
                   total_labs >= slots_needed['practicals'] * 2:
                    print(f"  Status: [OK] LTPSC requirements met")
                else:
                    print(f"  Status: [ISSUE] LTPSC requirements NOT met")
                    print(f"    Expected: {slots_needed['lectures']*2}L + {slots_needed['tutorials']*2}T + {slots_needed['practicals']*2}P")
                    print(f"    Got: {total_lectures}L + {total_tutorials}T + {total_labs}P")
            else:
                # Phase 7: Check one period
                if course_lectures >= slots_needed['lectures'] and \
                   course_tutorials >= slots_needed['tutorials'] and \
                   course_labs >= slots_needed['practicals']:
                    print(f"  Status: [OK] LTPSC requirements met")
                else:
                    print(f"  Status: [ISSUE] LTPSC requirements NOT met")
                    print(f"    Expected: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P")
                    print(f"    Got: {course_lectures}L + {course_tutorials}T + {course_labs}P")
        
        print("\n" + "="*100)
        print("DEEP VERIFICATION COMPLETE")
        print("="*100)
        
        if len(self.issues) == 0:
            print("\n[SUCCESS] No issues found! All courses are properly scheduled and compliant.")
        else:
            print(f"\n[ATTENTION] Found {len(self.issues)} issues that need to be addressed.")
        
        return {
            'total_courses': total_courses,
            'scheduled_courses': len(scheduled_courses),
            'unscheduled_courses': len(unscheduled_courses),
            'total_sessions': len(all_sessions),
            'issues': self.issues,
            'warnings': self.warnings
        }


if __name__ == "__main__":
    import sys
    
    excel_path = sys.argv[1] if len(sys.argv) > 1 else None
    verifier = DeepVerification()
    results = verifier.run_deep_verification(excel_path)
    
    # Exit with error code if issues found
    if results['issues']:
        sys.exit(1)
    else:
        sys.exit(0)
