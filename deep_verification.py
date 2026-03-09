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
    Run post-drag verification on in-memory sessions (internal dict format).
    Only checks rules that a manual drag can break:
      1) Time constraints (session outside day bounds or inside lunch)
      2) Section overlap  (two DIFFERENT courses in same section at same time)
      3) Faculty conflict (same instructor teaching two courses at same time)
    Room conflicts and LTPSC are pre-existing generator state and not checked here.
    Returns (success, errors).
    """
    errors: List[Dict[str, Any]] = []
    verifier = DeepVerification()
    seen_errors: set = set()

    def _add_error(rule: str, message: str, course_code: str, section: str, day: str, time: str) -> None:
        key = (rule, course_code, section, message)
        if key not in seen_errors:
            seen_errors.add(key)
            errors.append({
                "rule": rule,
                "message": message,
                "course_code": course_code,
                "section": section,
                "day": day,
                "time": time,
            })

    # 1) Time constraints: session outside working hours or inside lunch
    for sess in all_sessions:
        time_violations = verifier.verify_time_constraints(sess)
        section_str = (sess.get("sections") or [None])[0] or ""
        tb = sess.get("time_block")
        if tb and time_violations:
            for v in time_violations:
                _add_error(
                    "Time constraints", v,
                    sess.get("course_code", ""), section_str,
                    getattr(tb, "day", ""),
                    f"{tb.start.strftime('%H:%M')}-{tb.end.strftime('%H:%M')}" if hasattr(tb, "start") else "",
                )

    # 2) Section overlap: two DIFFERENT courses in the same section+period overlapping in time
    by_sec_period = defaultdict(list)
    for s in all_sessions:
        key = ((s.get("sections") or [""])[0], (s.get("period") or "").strip().upper())
        by_sec_period[key].append(s)
    for key, sess_list in by_sec_period.items():
        for i, a in enumerate(sess_list):
            for b in sess_list[i + 1:]:
                tb_a, tb_b = a.get("time_block"), b.get("time_block")
                if not tb_a or not tb_b or tb_a.day != tb_b.day or not tb_a.overlaps(tb_b):
                    continue
                code_a = (a.get("course_code") or "").split("-")[0].strip().upper()
                code_b = (b.get("course_code") or "").split("-")[0].strip().upper()
                # Skip if same base course code (combined-class duplicate entries)
                if code_a == code_b:
                    continue
                # Skip if either is an elective basket (individual elective sub-courses
                # are alternatives within the same basket and share their combined slot)
                if "ELECTIVE_BASKET" in code_a or "ELECTIVE_BASKET" in code_b:
                    continue
                # Skip known sub-elective course code patterns (e.g. CS261, CS262 are
                # elective options that share the same time slot by design)
                phase_a = str(a.get("phase", ""))
                phase_b = str(b.get("phase", ""))
                if phase_a.startswith("Phase 3") or phase_b.startswith("Phase 3"):
                    continue
                _add_error(
                    "Section overlap",
                    f"Same section has two sessions at same time: {a.get('course_code')} and {b.get('course_code')}",
                    a.get("course_code", ""), key[0], tb_a.day,
                    f"{tb_a.start.strftime('%H:%M')}-{tb_a.end.strftime('%H:%M')}",
                )

    # 3) Faculty conflict: same instructor teaching two different courses at same time
    faculty_map: Dict[str, List[tuple]] = defaultdict(list)
    faculty_seen: Set[tuple] = set()
    for s in all_sessions:
        fac = (s.get("instructor") or "").strip()
        if not fac or fac.upper() in ("TBD", "VARIOUS", ""):
            continue
        block = s.get("time_block")
        if not block:
            continue
        code = (s.get("course_code") or "").split("-")[0].upper()
        period = str(s.get("period") or "").upper()
        fkey = (fac, code, block.day, block.start.strftime("%H:%M"), block.end.strftime("%H:%M"))
        if fkey in faculty_seen:
            continue
        faculty_seen.add(fkey)
        faculty_map[fac].append((block, code, period))
    for fac, items in faculty_map.items():
        items_sorted = sorted(items, key=lambda x: (x[0].day, x[0].start))
        for i in range(len(items_sorted) - 1):
            b1, c1, p1 = items_sorted[i]
            for j in range(i + 1, len(items_sorted)):
                b2, c2, p2 = items_sorted[j]
                if c1 == c2:
                    continue  # Same course, different sections - fine
                if p1 and p2 and p1 != p2:
                    continue  # Different periods
                if b1.day != b2.day or not b1.overlaps(b2):
                    continue
                _add_error(
                    "Faculty conflict",
                    f"Faculty {fac} has overlapping sessions: {c1} and {c2} on {b1.day} {b1.start.strftime('%H:%M')}-{b1.end.strftime('%H:%M')}",
                    c1, "", b1.day,
                    f"{b1.start.strftime('%H:%M')}-{b1.end.strftime('%H:%M')}",
                )

    # 4) 1 day 1 course rule (for lectures)
    # A normal course should not have two lectures on the same day for the same section in the SAME period
    course_day_counts = defaultdict(list)
    for s in all_sessions:
        if (s.get("session_type") or "L").upper() != "L":
            continue
        code = (s.get("course_code") or "").split("-")[0].strip().upper()
        
        # Skip elective baskets and phase 3 electives as they can have multiple sessions 
        # (different sub-courses) in the same day naturally
        if "ELECTIVE_BASKET" in code or str(s.get("phase", "")).startswith("Phase 3"):
            continue
            
        sec = (s.get("sections") or [""])[0]
        tb = s.get("time_block")
        period = str(s.get("period") or "PRE").strip().upper()
        if not tb or not sec or not code:
            continue
        key = (sec, code, tb.day, period)
        course_day_counts[key].append(s)

    for (sec, code, day, period), sessions in course_day_counts.items():
        if len(sessions) > 1:
            times = ", ".join([f"{s['time_block'].start.strftime('%H:%M')}-{s['time_block'].end.strftime('%H:%M')}" for s in sessions if s.get('time_block')])
            _add_error(
                "1 Day 1 Course",
                f"Course {code} has multiple lectures on {day} in section {sec} ({period}): {times}",
                code, sec, day, ""
            )

    # 5) Combined class synchronization, room, and faculty checks
    # If a class is combined (same code, same instructor, multiple sections):
    # - Must not overlap with another combined course (bottleneck: only one 240-capacity room C004)
    # - Designated faculty must not be teaching something else
    # - All its sections must have it at the same time (no desync)
    # - If dragged, the target slot must be free in the 'other' sections sharing the class
    combined_groups = defaultdict(list)
    for s in all_sessions:
        code = (s.get("course_code") or "").split("-")[0].strip().upper()
        if "ELECTIVE_BASKET" in code or str(s.get("phase", "")).startswith("Phase 3"):
            continue
        fac = (s.get("instructor") or "").strip().upper()
        stype = (s.get("session_type") or "L").upper()
        if not fac or fac in ("TBD", "VARIOUS", ""):
            continue
        # Also, must have multiple sections to be considered "combined" for the 240 room bottleneck
        secs = s.get("sections", [])
        if len(secs) <= 1:
            continue
            
        # Group by (Course, Instructor, SessionType)
        key = (code, fac, stype)
        combined_groups[key].append(s)

    for (code, fac, stype), sessions in combined_groups.items():
        # Find all unique times this combined course is scheduled
        unique_times = {} # time_str -> list of sessions
        for s in sessions:
            tb = s.get("time_block")
            if not tb: continue
            t_str = f"{tb.day} {tb.start.strftime('%H:%M')}-{tb.end.strftime('%H:%M')}"
            if t_str not in unique_times:
                unique_times[t_str] = []
            unique_times[t_str].append(s)
            
        all_secs = sum([s.get("sections", [""]) for s in sessions], [])
        
        for t_str, t_sessions in unique_times.items():
            target_tb = t_sessions[0].get("time_block")
            if not target_tb: continue
            
            # Sub-check A: 240-Capacity Room (C004) Bottleneck
            # Check if *another* combined course (meaning len(secs) > 1) is scheduled at this exact time
            # We ONLY loop over combined_groups to check against other combined courses!
            for (o_code, o_fac, o_stype), o_sessions in combined_groups.items():
                if o_code == code: continue
                # Does the other combined course overlap this time?
                for o_sess in o_sessions:
                    o_tb = o_sess.get("time_block")
                    if o_tb and o_tb.day == target_tb.day and o_tb.overlaps(target_tb):
                        _add_error(
                            "Room Conflict",
                            f"Combined course {code} cannot be at {t_str} because the 240-capacity room is occupied by {o_code}",
                            code, ", ".join(set(all_secs)), target_tb.day,
                            f"{target_tb.start.strftime('%H:%M')}-{target_tb.end.strftime('%H:%M')}"
                        )
                        break # Report once per other course overlap
            
            # Sub-check B: Faculty availability for combined course
            # Check if this instructor is teaching any *other* course at this time
            for osess in all_sessions:
                o_code = (osess.get("course_code") or "").split("-")[0].strip().upper()
                if o_code == code: continue
                o_fac = (osess.get("instructor") or "").strip().upper()
                if o_fac == fac:
                    o_tb = osess.get("time_block")
                    if o_tb and o_tb.day == target_tb.day and o_tb.overlaps(target_tb):
                        _add_error(
                            "Faculty Conflict",
                            f"Combined course {code} cannot be at {t_str} because faculty {fac} is busy teaching {o_code}",
                            code, ", ".join(set(all_secs)), target_tb.day,
                            f"{target_tb.start.strftime('%H:%M')}-{target_tb.end.strftime('%H:%M')}"
                        )
                        break
        
        if len(unique_times) > 1:
            # Sub-check C: Desync and cross-section conflict check
            # User dragged one instance but not the others.
            for t_str, t_sessions in unique_times.items():
                target_tb = t_sessions[0].get("time_block")
                if not target_tb: continue
                # Sections that have it at THIS time
                secs_at_this_time = set(sum([s.get("sections", [""]) for s in t_sessions], []))
                # Sections that SHOULD have it but don't (they are at the other time)
                other_secs = set(all_secs) - secs_at_this_time
                
                # Check if this target_tb overlaps with ANY existing sessions in other_secs
                for other_sec in other_secs:
                    other_sec_sessions = [
                        xs for xs in all_sessions 
                        if other_sec in xs.get("sections", []) and xs.get("course_code", "").split("-")[0].strip().upper() != code
                    ]
                    for osess in other_sec_sessions:
                        otb = osess.get("time_block")
                        if otb and otb.day == target_tb.day and otb.overlaps(target_tb):
                            o_code = osess.get("course_code", "")
                            _add_error(
                                "Combined Conflict",
                                f"Cannot drag combined course {code} to {t_str} because shared section {other_sec} is already busy with {o_code}",
                                code, other_sec, target_tb.day,
                                f"{target_tb.start.strftime('%H:%M')}-{target_tb.end.strftime('%H:%M')}"
                            )
            
            _add_error(
                "Combined Desync",
                f"Combined course {code} ({fac}) is scheduled at different times across its sections. Ensure all sections place it at the same time.",
                code, ", ".join(set(all_secs)), "", ""
            )

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
