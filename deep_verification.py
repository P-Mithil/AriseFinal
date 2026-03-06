"""
Deep Verification Script for Timetable Generation
Comprehensive verification of all courses, LTPSC compliance, and phase rules
"""

import os
import sys
import re
import csv
from collections import defaultdict
from datetime import time
import openpyxl
from typing import Dict, List, Tuple, Set, Optional, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules_v2.phase1_data_validation_v2 import run_phase1
from modules_v2.phase3_elective_baskets_v2 import run_phase3, ELECTIVE_BASKET_SLOTS
from modules_v2.phase4_combined_classes_v2_corrected import run_phase4_corrected as run_phase4
from modules_v2.phase5_core_courses import run_phase5, calculate_slots_needed, get_lunch_blocks
from modules_v2.phase6_faculty_conflicts import run_phase6_faculty_conflicts
from modules_v2.phase7_remaining_courses import run_phase7, add_session_to_occupied_slots
from modules_v2.phase8_classroom_assignment import run_phase8
from utils.data_models import Course, Section, ScheduledSession, ClassRoom, TimeBlock
from config.structure_config import (
    DEPARTMENTS,
    SECTIONS_BY_DEPT,
    STUDENTS_PER_SECTION,
    get_group_for_section,
)


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
    
    def verify_ltpsc_compliance(
        self,
        course: Course,
        sessions: List,
        section: Section,
        period: Optional[str] = None,
    ) -> Dict:
        """
        Verify LTPSC compliance for a course.

        period:
          - None -> count across BOTH PRE+POST (full semester)
          - 'PRE'/'POST' -> filter to that period only
        """
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
            
            # Check period match (optional)
            if period is None:
                period_match = True
            else:
                period_match = False
                session_period_str = str(session_period).upper()
                period_upper = str(period).upper()
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
        
        # Requirements are defined per course for the full semester.
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
    
    def verify_phase_rules(self, course: Course, sessions: List[Any], 
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
        # NOTE: We no longer treat "not scheduled across multiple groups" as a
        # hard violation for combined courses. The main pipeline already
        # enforces synchronization for configured SECTION_GROUPS, and the Excel
        # output plus conflict verifier are the source of truth. Here we focus
        # only on *real* per-section conflicts and capacity issues.
        
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
        
        # Faculty conflicts are verified globally across ALL sessions (see run_deep_verification),
        # so we do not duplicate them here per-course (avoids double counting).
        
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
                    # Only enforce capacity for lecture/tutorial rooms.
                    room_type_str = str(getattr(room, "room_type", "")).lower()
                    if "lab" not in room_type_str:
                        # Prefer real per-course registered_students for capacity checks.
                        raw_enrollment = getattr(course, "registered_students", None)
                        try:
                            course_enrollment = int(raw_enrollment) if raw_enrollment is not None else 0
                        except (TypeError, ValueError):
                            course_enrollment = 0

                        # Fall back to section.students only if course-level data is missing.
                        if course_enrollment and course_enrollment > 0:
                            capacity_needed = course_enrollment
                        else:
                            section_size = getattr(section, "students", None)
                            try:
                                capacity_needed = int(section_size) if section_size is not None else 0
                            except (TypeError, ValueError):
                                capacity_needed = 0

                        if capacity_needed and room.capacity < capacity_needed:
                            violations.append(
                                f"Room {session.room} capacity {room.capacity} < needed {capacity_needed} (based on registered students)"
                            )
        
        return violations
    
    def verify_time_constraints(self, session: Any) -> List[str]:
        """Verify time constraints for a session (dict or ScheduledSession)."""
        violations = []

        block = None
        section_str = None
        if isinstance(session, dict):
            block = session.get("time_block") or session.get("block")
            # pick a representative section for lunch/semester
            secs = session.get("sections") or []
            section_str = secs[0] if secs else None
        elif hasattr(session, "block"):
            block = getattr(session, "block")
            section_str = getattr(session, "section", None)

        if not block:
            return violations
        
        # Check time range (9:00-18:00)
        if block.start < time(9, 0) or block.end > time(18, 0):
            violations.append(f"Session outside college hours: {block.start}-{block.end}")
        
        # Check lunch break
        semester = 1
        if section_str and isinstance(section_str, str) and "Sem" in section_str:
            try:
                semester = int(section_str.split("-")[2].replace("Sem", ""))
            except Exception:
                semester = 1
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
        print("\n[STEP 1] Loading course and structure data...")
        courses, classrooms, statistics = run_phase1()
        print(f"  Loaded {len(courses)} courses, {len(classrooms)} classrooms")

        # Build sections dynamically from structure_config so verification always
        # matches the current configuration (no hardcoded sections here).
        unique_semesters = sorted(
            set(course.semester for course in courses if course.department in DEPARTMENTS)
        )

        sections: List[Section] = []
        for dept in DEPARTMENTS:
            for sem in unique_semesters:
                for sec_label in SECTIONS_BY_DEPT.get(dept, []):
                    group = get_group_for_section(dept, sec_label)
                    sections.append(
                        Section(dept, group, sec_label, sem, STUDENTS_PER_SECTION)
                    )

        print(f"  Constructed {len(sections)} sections from structure_config")
        
        # Step 2: Find Excel file
        if not excel_path:
            excel_path = find_latest_excel_file()
        
        if not excel_path or not os.path.exists(excel_path):
            print(f"\n[ERROR] Excel file not found: {excel_path}")
            print("Please generate timetable first using generate_24_sheets.py")
            return
        
        print(f"\n[STEP 2] Analyzing Excel file: {excel_path}")

        # Step 3: Prefer verifying the EXACT generated schedule via time_slot_log CSV.
        # This avoids false alarms from re-running stochastic scheduling phases.
        print("\n[STEP 3] Loading sessions for verification...")

        elective_sessions: List[Any] = []
        combined_sessions: List[Any] = []
        phase5_sessions: List[Any] = []
        phase7_sessions: List[Any] = []
        all_sessions: List[Any] = []

        ts_match = re.search(r"IIITDWD_24_Sheets_v2_(\d{8}_\d{6})\.xlsx$", str(excel_path))
        log_loaded = False
        if ts_match:
            ts = ts_match.group(1)
            log_path = os.path.join("DATA", "OUTPUT", f"time_slot_log_{ts}.csv")
            if os.path.exists(log_path):
                try:
                    from utils.data_models import TimeBlock
                    with open(log_path, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            course_code = (row.get("Course Code") or "").strip()
                            section = (row.get("Section") or "").strip()
                            day = (row.get("Day") or "").strip()
                            start_s = (row.get("Start Time") or "").strip()
                            end_s = (row.get("End Time") or "").strip()
                            room = (row.get("Room") or "").strip()
                            period = (row.get("Period") or "").strip().upper()
                            stype = (row.get("Session Type") or "L").strip().upper()
                            faculty = (row.get("Faculty") or "").strip() or None
                            phase = (row.get("Phase") or "").strip()

                            if not (course_code and section and day and start_s and end_s):
                                continue

                            try:
                                hh, mm = start_s.split(":")
                                start_t = time(int(hh), int(mm))
                                hh, mm = end_s.split(":")
                                end_t = time(int(hh), int(mm))
                            except Exception:
                                continue

                            sess = {
                                "phase": phase,
                                "course_code": course_code,
                                "sections": [section],
                                "period": period,
                                "time_block": TimeBlock(day, start_t, end_t),
                                "room": room,
                                "session_type": stype,
                                "instructor": faculty,
                            }
                            all_sessions.append(sess)

                    elective_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 3")]
                    combined_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 4")]
                    phase5_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 5")]
                    phase7_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 7")]

                    print(f"  Loaded sessions from: {log_path}")
                    print(f"    Phase 3: {len(elective_sessions)} sessions")
                    print(f"    Phase 4: {len(combined_sessions)} sessions")
                    print(f"    Phase 5: {len(phase5_sessions)} sessions")
                    print(f"    Phase 7: {len(phase7_sessions)} sessions")
                    log_loaded = True
                except Exception as e:
                    print(f"  WARNING: Failed to load time_slot_log CSV: {e}")

        if not log_loaded:
            print("  No matching time_slot_log found; falling back to re-running phases.")

            # Phase 3: Electives
            try:
                elective_baskets, elective_sessions = run_phase3(courses, sections)
                print(f"  Phase 3: {len(elective_sessions)} elective sessions")
            except Exception as e:
                print(f"  Phase 3 failed: {e}")

            # Phase 4: Combined classes
            try:
                phase4_result = run_phase4(courses, sections, classrooms)
                schedule = phase4_result.get("schedule", {}) if isinstance(phase4_result, dict) else {}

                from generate_24_sheets import map_corrected_schedule_to_sessions_v2
                from modules_v2.phase8_classroom_assignment import assign_labs_to_combined_practicals

                combined_sessions = map_corrected_schedule_to_sessions_v2(
                    schedule,
                    sections,
                    ["PreMid", "PostMid"],
                    courses,
                    classrooms,
                )
                combined_sessions = assign_labs_to_combined_practicals(combined_sessions, classrooms)
                print(f"  Phase 4: {len(combined_sessions)} combined sessions")
            except Exception as e:
                print(f"  Phase 4 failed: {e}")

            # Phase 5: Core courses
            occupied_slots = defaultdict(list)
            try:
                phase5_sessions = run_phase5(
                    courses, sections, classrooms, elective_sessions, combined_sessions
                )
                print(f"  Phase 5: {len(phase5_sessions)} core sessions")
            except Exception as e:
                print(f"  Phase 5 failed: {e}")

            # Phase 7: Remaining courses
            try:
                room_occupancy = {}
                phase7_sessions = run_phase7(
                    courses,
                    sections,
                    classrooms,
                    occupied_slots,
                    room_occupancy,
                    combined_sessions,
                    timeout_seconds=60,
                )
                print(f"  Phase 7: {len(phase7_sessions)} remaining sessions")
            except Exception as e:
                print(f"  Phase 7 failed: {e}")

            all_sessions = elective_sessions + combined_sessions + phase5_sessions + phase7_sessions

        # Global faculty conflict check across ALL sessions.
        # De-duplicate combined sessions that appear once per section.
        faculty_map: Dict[str, List[Tuple[TimeBlock, str, str]]] = defaultdict(list)
        seen_faculty_entries: Set[Tuple[str, str, str, str, str]] = set()

        for s in all_sessions:
            faculty = None
            block = None
            code = None
            period = None
            room = None

            if isinstance(s, dict):
                faculty = s.get("instructor")
                block = s.get("time_block") or s.get("block")
                code = (s.get("course_code") or "").split("-")[0]
                period = str(s.get("period") or "").upper()
                room = s.get("room")
            else:
                faculty = getattr(s, "faculty", None)
                block = getattr(s, "block", None)
                code = (getattr(s, "course_code", "") or "").split("-")[0]
                period = str(getattr(s, "period", "") or "").upper()
                room = getattr(s, "room", None)

            if not faculty or faculty in ["TBD", "Various"]:
                continue
            if not block:
                continue

            # Unique key (treat one combined slot as one teaching commitment)
            key = (
                str(faculty),
                str(code),
                str(block.day),
                block.start.strftime("%H:%M"),
                block.end.strftime("%H:%M"),
            )
            if key in seen_faculty_entries:
                continue
            seen_faculty_entries.add(key)
            faculty_map[str(faculty)].append((block, str(code), period))

        for faculty, items in faculty_map.items():
            items_sorted = sorted(items, key=lambda x: (x[0].day, x[0].start))
            for i in range(len(items_sorted) - 1):
                b1, c1, p1 = items_sorted[i]
                for j in range(i + 1, len(items_sorted)):
                    b2, c2, p2 = items_sorted[j]
                    # PRE and POST happen in different halves of the semester,
                    # so they are not concurrent. Only flag overlaps within the same period.
                    if p1 and p2 and p1 != p2:
                        continue
                    if b1.day != b2.day:
                        continue
                    if not b1.overlaps(b2):
                        continue
                    self.log_issue(
                        "FACULTY_CONFLICTS",
                        c1,
                        f"Faculty {faculty} overlap: {c1} ({p1}) vs {c2} ({p2}) on {b1.day} {b1.start}-{b1.end}",
                    )
        
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
                
                # LTPSC compliance (full semester across PRE+POST)
                compliance = self.verify_ltpsc_compliance(course, all_sessions, section, period=None)

                all_satisfied = (
                    compliance["satisfied"]["lectures"]
                    and compliance["satisfied"]["tutorials"]
                    and compliance["satisfied"]["labs"]
                )

                if not all_satisfied:
                    details = {
                        "section": section_name,
                        "ltpsc": compliance["ltpsc"],
                        "required": compliance["required"],
                        "scheduled": compliance["scheduled"],
                    }
                    self.log_issue(
                        "LTPSC_COMPLIANCE",
                        course.code,
                        f"LTPSC requirements not met in {section_name}",
                        details,
                    )

                # Phase rules (do not duplicate per-period)
                violations = self.verify_phase_rules(
                    course,
                    all_sessions,
                    section,
                    all_sessions,
                    elective_sessions,
                    combined_sessions,
                    classrooms,
                )
                for violation in violations:
                    self.log_issue("PHASE_RULES", course.code, f"{violation} in {section_name}")

                # Time constraints across all scheduled sessions for this course+section
                for session in compliance["sessions"]:
                    time_violations = self.verify_time_constraints(session)
                    for violation in time_violations:
                        self.log_issue("TIME_CONSTRAINTS", course.code, f"{violation} in {section_name}")
        
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
            
            # Include both ScheduledSession objects and dict-based sessions (Phase 4 combined)
            course_sessions: List[Any] = []
            for s in all_sessions:
                if isinstance(s, dict):
                    code = str(s.get("course_code", "")).split("-")[0]
                    if code == course.code:
                        course_sessions.append(s)
                elif hasattr(s, "course_code"):
                    code = str(getattr(s, "course_code", "")).split("-")[0]
                    if code == course.code:
                        course_sessions.append(s)
            
            print(f"\n{course.code}: {course.name}")
            print(f"  Department: {course.department}, Semester: {course.semester}, Credits: {course.credits}")
            print(f"  LTPSC: {course.ltpsc}")
            slots_needed = calculate_slots_needed(course.ltpsc)
            print(f"  Required: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P")
            print(f"  Scheduled sessions: {len(course_sessions)}")
            
            # Count by type
            course_lectures = 0
            course_tutorials = 0
            course_labs = 0
            for s in course_sessions:
                if isinstance(s, dict):
                    st = str(s.get("session_type", "L")).upper()
                    if st == "P":
                        course_labs += 1
                    elif st == "T":
                        course_tutorials += 1
                    else:
                        course_lectures += 1
                else:
                    kind = str(getattr(s, "kind", "L")).upper()
                    if kind == "P":
                        course_labs += 1
                    elif kind == "T":
                        course_tutorials += 1
                    else:
                        course_lectures += 1
            print(f"  Scheduled: {course_lectures}L + {course_tutorials}T + {course_labs}P")
            
            # Check compliance
            # This report is full-semester: required LTPSC is compared against totals
            # across both PRE and POST.
            if (
                course_lectures >= slots_needed["lectures"]
                and course_tutorials >= slots_needed["tutorials"]
                and course_labs >= slots_needed["practicals"]
            ):
                print(f"  Status: [OK] LTPSC requirements met")
            else:
                print(f"  Status: [ISSUE] LTPSC requirements NOT met")
                print(
                    f"    Expected: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P"
                )
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


def run_verification_on_sessions(
    all_sessions: List[Dict[str, Any]],
    courses: List[Course],
    sections: List[Section],
    classrooms: List,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Run verification on in-memory sessions (internal dict format).
    Returns (success, errors) where errors is a list of
    { "rule", "message", "course_code", "section", "day", "time", ... }.
    Reuses DeepVerification checks: time constraints, faculty overlap, LTPSC, phase rules.
    Also runs section overlap and room conflict checks.
    """
    from config.schedule_config import LUNCH_WINDOWS
    errors: List[Dict[str, Any]] = []

    # Classify by phase for LTPSC/phase rules
    elective_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 3")]
    combined_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 4")]
    phase5_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 5")]
    phase7_sessions = [s for s in all_sessions if str(s.get("phase", "")).startswith("Phase 7")]

    verifier = DeepVerification()

    # 1) Time constraints (bounds + lunch) per session
    for sess in all_sessions:
        time_violations = verifier.verify_time_constraints(sess)
        section_str = (sess.get("sections") or [None])[0] or ""
        tb = sess.get("time_block")
        if tb and time_violations:
            for v in time_violations:
                errors.append({
                    "rule": "Time constraints",
                    "message": v,
                    "course_code": sess.get("course_code", ""),
                    "section": section_str,
                    "day": getattr(tb, "day", ""),
                    "time": f"{tb.start.strftime('%H:%M')}-{tb.end.strftime('%H:%M')}" if hasattr(tb, "start") else "",
                })

    # 2) Section overlap: same section+period, same day, overlapping time
    by_key = defaultdict(list)
    for s in all_sessions:
        key = ((s.get("sections") or [""])[0], (s.get("period") or "").strip().upper())
        by_key[key].append(s)
    for key, sess_list in by_key.items():
        for i, a in enumerate(sess_list):
            for b in sess_list[i + 1 :]:
                tb_a, tb_b = a.get("time_block"), b.get("time_block")
                if not tb_a or not tb_b or tb_a.day != tb_b.day or not tb_a.overlaps(tb_b):
                    continue
                errors.append({
                    "rule": "Section overlap",
                    "message": f"Same section has two sessions at same time: {a.get('course_code')} and {b.get('course_code')}",
                    "course_code": a.get("course_code", ""),
                    "section": key[0],
                    "day": tb_a.day,
                    "time": f"{tb_a.start.strftime('%H:%M')}-{tb_a.end.strftime('%H:%M')}",
                })

    # 3) Room conflict: same room, same day, overlapping time, different course
    by_room = defaultdict(list)
    for s in all_sessions:
        if s.get("room"):
            by_room[s["room"]].append(s)
    for room, sess_list in by_room.items():
        for i, a in enumerate(sess_list):
            for b in sess_list[i + 1 :]:
                tb_a, tb_b = a.get("time_block"), b.get("time_block")
                if not tb_a or not tb_b or tb_a.day != tb_b.day or not tb_a.overlaps(tb_b):
                    continue
                base_a = (a.get("course_code") or "").split("-")[0]
                base_b = (b.get("course_code") or "").split("-")[0]
                if base_a == base_b:
                    continue
                errors.append({
                    "rule": "Room conflict",
                    "message": f"Room {room} double-booked: {a.get('course_code')} and {b.get('course_code')}",
                    "course_code": a.get("course_code", ""),
                    "section": (a.get("sections") or [""])[0],
                    "day": tb_a.day,
                    "time": f"{tb_a.start.strftime('%H:%M')}-{tb_a.end.strftime('%H:%M')}",
                })

    # 4) Faculty conflict: same instructor, same period, same day, overlapping
    faculty_map: Dict[str, List[Tuple[TimeBlock, str, str]]] = defaultdict(list)
    seen: Set[Tuple[str, str, str, str, str]] = set()
    for s in all_sessions:
        fac = (s.get("instructor") or "").strip()
        if not fac or fac in ("TBD", "Various"):
            continue
        block = s.get("time_block")
        if not block:
            continue
        code = (s.get("course_code") or "").split("-")[0]
        period = str(s.get("period") or "").upper()
        key = (str(fac), str(code), str(block.day), block.start.strftime("%H:%M"), block.end.strftime("%H:%M"))
        if key in seen:
            continue
        seen.add(key)
        faculty_map[fac].append((block, code, period))
    for fac, items in faculty_map.items():
        items_sorted = sorted(items, key=lambda x: (x[0].day, x[0].start))
        for i in range(len(items_sorted) - 1):
            b1, c1, p1 = items_sorted[i]
            for j in range(i + 1, len(items_sorted)):
                b2, c2, p2 = items_sorted[j]
                if p1 and p2 and p1 != p2:
                    continue
                if b1.day != b2.day or not b1.overlaps(b2):
                    continue
                errors.append({
                    "rule": "Faculty conflict",
                    "message": f"Faculty {fac} overlap: {c1} ({p1}) vs {c2} ({p2}) on {b1.day} {b1.start}-{b1.end}",
                    "course_code": c1,
                    "section": "",
                    "day": b1.day,
                    "time": f"{b1.start.strftime('%H:%M')}-{b1.end.strftime('%H:%M')}",
                })

    # 5) LTPSC and phase rules per course/section (reuse verifier)
    for course in courses:
        if course.is_elective:
            continue
        for section in sections:
            if section.program != course.department or section.semester != course.semester:
                continue
            section_name = f"{section.program}-{section.name}-Sem{section.semester}"
            if course.is_combined:
                found = any(
                    isinstance(cs, dict)
                    and (cs.get("course_code") or "").split("-")[0] == course.code
                    and section_name in (cs.get("sections") or [])
                    for cs in combined_sessions
                )
                if found:
                    compliance = verifier.verify_ltpsc_compliance(course, all_sessions, section, "PRE")
                    if not (compliance["satisfied"]["lectures"] and compliance["satisfied"]["tutorials"] and compliance["satisfied"]["labs"]):
                        errors.append({
                            "rule": "LTPSC compliance",
                            "message": f"LTPSC requirements not met in {section_name}",
                            "course_code": course.code,
                            "section": section_name,
                            "day": "",
                            "time": "",
                        })
                    continue
            compliance = verifier.verify_ltpsc_compliance(course, all_sessions, section, period=None)
            if not (compliance["satisfied"]["lectures"] and compliance["satisfied"]["tutorials"] and compliance["satisfied"]["labs"]):
                errors.append({
                    "rule": "LTPSC compliance",
                    "message": f"LTPSC requirements not met in {section_name}",
                    "course_code": course.code,
                    "section": section_name,
                    "day": "",
                    "time": "",
                })
            for violation in verifier.verify_phase_rules(
                course, all_sessions, section, all_sessions,
                elective_sessions, combined_sessions, classrooms,
            ):
                errors.append({
                    "rule": "Phase rules",
                    "message": violation,
                    "course_code": course.code,
                    "section": section_name,
                    "day": "",
                    "time": "",
                })

    return (len(errors) == 0, errors)


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
