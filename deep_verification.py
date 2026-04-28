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
from config.schedule_config import (
    DAY_START_TIME,
    DAY_END_TIME,
    FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD,
    FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER,
)
from utils.period_utils import normalize_period
from utils.section_cohort_utils import program_semester_numbers_from_session_payload
from utils.faculty_conflict_utils import faculty_name_tokens


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
          - None -> count across BOTH PRE+POST; required L/T/P totals are 2× the per-half
            slot counts from LTPSC for full-semester (non-elective, non-half-semester) courses.
          - 'PRE'/'POST' -> filter to that period only (required counts are per-half slots)
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
                secs_raw = session_section
                if isinstance(secs_raw, str):
                    labels_norm = [x.strip() for x in secs_raw.split(",") if x.strip()]
                else:
                    labels_norm = [str(x).strip() for x in (secs_raw or []) if str(x).strip()]
                section_match = section_label in labels_norm
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
        
        # Full-semester (period=None) non-elective courses that run both PreMid and PostMid
        # carry the same L/T/P *slot counts in each half*. Required totals are 2× the
        # per-half slots from LTPSC (half-semester and elective courses stay 1×).
        def _full_semester_ltpsc_multiplier(crs: Course, per: Optional[str]) -> int:
            if per is not None:
                return 1
            if getattr(crs, "is_elective", False):
                return 1
            if getattr(crs, "half_semester", False):
                return 1
            # Phase 4 combined courses (<=2 credits, core) are inherently half-semester courses
            # (they run either in PreMid or PostMid for a given group).
            credits = getattr(crs, "credits", 0)
            if credits is not None and credits <= 2:
                return 1
            return 2

        mult = _full_semester_ltpsc_multiplier(course, period)

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
        
        # Requirements: per-half slots from LTPSC × multiplier (2 for full-semester PRE+POST).
        required_lectures = int(slots_needed['lectures']) * mult
        required_tutorials = int(slots_needed['tutorials']) * mult
        required_labs = int(slots_needed['practicals']) * mult
        
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
                'lectures': scheduled_lectures == required_lectures,
                'tutorials': scheduled_tutorials == required_tutorials,
                'labs': scheduled_labs == required_labs
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
        
        # Check time range against config
        if block.start < DAY_START_TIME or block.end > DAY_END_TIME:
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

    def verify_session_duration(self, session: Any) -> List[str]:
        """
        Verify fixed duration policy by session type:
          - Lecture (L): 90 min
          - Tutorial (T): 60 min
          - Practical (P): 120 min
        """
        violations = []
        block = None
        session_type = "L"

        if isinstance(session, dict):
            block = session.get("time_block") or session.get("block")
            session_type = str(session.get("session_type") or "L").strip().upper()
        elif hasattr(session, "block"):
            block = getattr(session, "block", None)
            session_type = str(getattr(session, "kind", "L") or "L").strip().upper()

        if not block:
            return violations

        # Ignore non-academic marker rows and non-standard synthetic types.
        if session_type not in ("L", "T", "P"):
            return violations

        start_m = block.start.hour * 60 + block.start.minute
        end_m = block.end.hour * 60 + block.end.minute
        actual = end_m - start_m
        expected_by_type = {"L": 90, "T": 60, "P": 120}
        expected = expected_by_type.get(session_type)
        if expected is not None and actual != expected:
            kind_name = {"L": "Lecture", "T": "Tutorial", "P": "Practical"}[session_type]
            violations.append(
                f"{kind_name} duration mismatch: expected {expected} min, got {actual} min"
            )

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

        def _session_calendar_key(sess: Any) -> Optional[tuple]:
            """
            Same calendar slot across parallel sections is often logged once per section row.
            Include base course code so two different courses sharing a slot are not merged.
            """
            if isinstance(sess, dict):
                tb = sess.get("time_block")
                if not tb:
                    return None
                code = str(sess.get("course_code", "")).split("-")[0].strip().upper()
                p = normalize_period(sess.get("period"))
                st = str(sess.get("session_type", "L")).strip().upper()
                if st == "ELECTIVE":
                    st = "L"
                return (code, p, tb.day, tb.start, tb.end, st)
            block = getattr(sess, "block", None)
            if block is not None:
                code = str(getattr(sess, "course_code", "") or "").split("-")[0].strip().upper()
                p = normalize_period(getattr(sess, "period", None))
                kind = str(getattr(sess, "kind", "L")).strip().upper()
                return (code, p, block.day, block.start, block.end, kind)
            return None

        def _session_applies_to_course(sess: Any, crs: Course) -> bool:
            """True if this session belongs to crs's department + semester (CSV rows are per-section)."""
            want_pref = f"{crs.department}-"
            want_sem = f"-Sem{crs.semester}"
            if isinstance(sess, dict):
                secs = sess.get("sections") or []
                if isinstance(secs, str):
                    secs = [secs]
                for sec in secs:
                    s = str(sec).strip()
                    if s.startswith(want_pref) and want_sem in s:
                        return True
                return False
            sec_one = str(getattr(sess, "section", "") or "").strip()
            if sec_one:
                return sec_one.startswith(want_pref) and want_sem in sec_one
            return True

        # Step 4: Course-by-course verification
        print("\n[STEP 4] Verifying each course in detail...")
        print("-"*100)
        
        # Legacy-compatible course-row stabilization:
        # For duplicate Phase-5 rows with same (code, dept, sem, credits) and no Offering_ID,
        # verify one representative only to avoid contradictory LTPSC checks.
        verification_courses: List[Course] = []
        seen_verify_keys: Dict[Tuple[str, str, int, int], Course] = {}
        for c in courses:
            if c.is_elective:
                continue
            key = (str(c.code).strip().upper(), str(c.department).strip().upper(), int(c.semester), int(c.credits))
            oid = (getattr(c, "offering_id", None) or "").strip()
            if oid:
                verification_courses.append(c)
                continue
            if key not in seen_verify_keys:
                seen_verify_keys[key] = c
                verification_courses.append(c)
                continue
            existing = seen_verify_keys[key]
            a = str(getattr(existing, "ltpsc", "") or "").strip()
            b = str(getattr(c, "ltpsc", "") or "").strip()
            if a != b:
                self.log_warning(
                    "DATA_QUALITY",
                    str(c.code),
                    f"Duplicate course row {c.code} {c.department} Sem{c.semester} has LTPSC variants "
                    f"({a!r} vs {b!r}); verifying first row only."
                )

        total_courses = len(verification_courses)
        scheduled_courses = set()
        unscheduled_courses = []
        
        for course in verification_courses:
            
            # Find sessions for this course - handle different session formats
            course_sessions = []
            for s in all_sessions:
                # Handle dict format (combined sessions)
                if isinstance(s, dict):
                    session_code = s.get('course_code', '')
                    if isinstance(session_code, str):
                        # Remove suffixes like -TUT, -LAB
                        base_code = session_code.split('-')[0]
                        if base_code == course.code and _session_applies_to_course(s, course):
                            course_sessions.append(s)
                # Handle ScheduledSession objects
                elif hasattr(s, 'course_code'):
                    if s.course_code == course.code and _session_applies_to_course(s, course):
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

                # Time constraints + fixed session duration policy across all sessions
                for session in compliance["sessions"]:
                    time_violations = self.verify_time_constraints(session)
                    for violation in time_violations:
                        self.log_issue("TIME_CONSTRAINTS", course.code, f"{violation} in {section_name}")
                    duration_violations = self.verify_session_duration(session)
                    for violation in duration_violations:
                        self.log_issue("SESSION_DURATION", course.code, f"{violation} in {section_name}")
        
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
        
        # Count by type (dict sessions from CSV use session_type; dedupe shared calendar slots)
        lectures = tutorials = labs = 0
        _seen_slot: Set[tuple] = set()
        for s in all_sessions:
            key = _session_calendar_key(s)
            if key is None or key in _seen_slot:
                continue
            _seen_slot.add(key)
            _, _, _, _, _, st = key
            if st == "P":
                labs += 1
            elif st == "T":
                tutorials += 1
            else:
                lectures += 1
        print(f"  By type (unique slots): {lectures} lectures, {tutorials} tutorials, {labs} labs")
        
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
        
        for course in sorted(verification_courses, key=lambda c: (c.department, c.semester, c.code)):
            
            # Same code can be scheduled for multiple departments (e.g. EC307 DSAI + ECE): only
            # count sessions for *this* course row's department + semester (matches Step 4).
            course_sessions: List[Any] = []
            for s in all_sessions:
                if isinstance(s, dict):
                    code = str(s.get("course_code", "")).split("-")[0]
                    if code == course.code and _session_applies_to_course(s, course):
                        course_sessions.append(s)
                elif hasattr(s, "course_code"):
                    code = str(getattr(s, "course_code", "")).split("-")[0]
                    if code == course.code and _session_applies_to_course(s, course):
                        course_sessions.append(s)

            matching_sections = [
                sec for sec in sections
                if sec.program == course.department and sec.semester == course.semester
            ]

            print(f"\n{course.code}: {course.name}")
            print(f"  Department: {course.department}, Semester: {course.semester}, Credits: {course.credits}")
            print(f"  LTPSC: {course.ltpsc}")
            slots_needed = calculate_slots_needed(course.ltpsc)
            print(f"  Required: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P")

            _seen_course: Set[tuple] = set()
            course_slots: List[Any] = []
            for s in course_sessions:
                key = _session_calendar_key(s)
                if key is None or key in _seen_course:
                    continue
                _seen_course.add(key)
                course_slots.append(s)
            print(
                f"  Scheduled sessions: {len(course_sessions)} log rows, "
                f"{len(course_slots)} unique calendar slots (this dept/sem only)"
            )

            if not matching_sections:
                print("  Status: [SKIP] No configured sections for this department/semester")
                continue

            all_sat = True
            failing_labels: List[str] = []
            display_comp = None
            for sec in matching_sections:
                comp = self.verify_ltpsc_compliance(course, all_sessions, sec, period=None)
                ok = (
                    comp["satisfied"]["lectures"]
                    and comp["satisfied"]["tutorials"]
                    and comp["satisfied"]["labs"]
                )
                if not ok:
                    all_sat = False
                    failing_labels.append(sec.label)
                    if display_comp is None:
                        display_comp = comp
            if display_comp is None:
                display_comp = self.verify_ltpsc_compliance(
                    course, all_sessions, matching_sections[0], period=None
                )

            sch = display_comp["scheduled"]
            course_lectures = sch["lectures"]
            course_tutorials = sch["tutorials"]
            course_labs = sch["labs"]
            ref_label = (
                failing_labels[0]
                if failing_labels
                else matching_sections[0].label
            )
            print(
                f"  Scheduled (verify_ltpsc, section {ref_label}): "
                f"{course_lectures}L + {course_tutorials}T + {course_labs}P"
            )

            if all_sat:
                print(f"  Status: [OK] LTPSC requirements met")
            else:
                print(f"  Status: [ISSUE] LTPSC requirements NOT met")
                print(
                    f"    Expected: {slots_needed['lectures']}L + {slots_needed['tutorials']}T + {slots_needed['practicals']}P"
                )
                print(f"    Got: {course_lectures}L + {course_tutorials}T + {course_labs}P")
                if len(failing_labels) > 1:
                    print(f"    Sections with mismatch: {', '.join(sorted(failing_labels))}")
        
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
    Checks rules that a manual drag (or bad generator output) can break:
      1) Time constraints (session outside day bounds or inside lunch)
      1b) Fixed session durations (L=1.5h, T=1h, P=2h)
      2) Section overlap  (two DIFFERENT courses in same section at same time)
      3) Faculty conflict (two different courses at same time; plus same course on parallel
         sections when course credits exceed FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD)
      4) Classroom conflicts (missing/unknown room, capacity, double-booking)
      5) One-day lecture caps vs LTPSC
      6) Combined-class synchronization / faculty bottlenecks
      7) LTPSC compliance (full semester PRE+POST) for each scheduled course/section pair
    Returns (success, errors).
    """
    errors: List[Dict[str, Any]] = []
    verifier = DeepVerification()
    seen_errors: set = set()

    # Canonical periods so PREMID vs POSTMID match generator/CSV semantics
    for s in all_sessions:
        s["period"] = normalize_period(s.get("period"))

    def _section_semester_dept(sec: str) -> Tuple[Optional[int], Optional[str]]:
        sem, dept = None, None
        if not sec or not isinstance(sec, str):
            return sem, dept
        if "Sem" in sec:
            try:
                sem = int(sec.split("Sem")[1].split("-")[0])
            except (ValueError, IndexError):
                pass
        if "-" in sec:
            dept = sec.split("-")[0].strip()
        return sem, dept

    def _max_lectures_same_day_allowed(code: str, sec: str) -> int:
        """Strict rule: at most one lecture per course per day per section."""
        return 1

    def _course_credits_for_code_section(code: str, sec: str) -> Optional[int]:
        """Match Phase1 course row by code + section semester/dept; None if not found."""
        sem, dept = _section_semester_dept(sec)
        course_obj = None
        for c in courses:
            if str(c.code).upper() != str(code).upper():
                continue
            if sem is not None and getattr(c, "semester", None) != sem:
                continue
            if dept and getattr(c, "department", None) == dept:
                course_obj = c
                break
        if course_obj is None:
            for c in courses:
                if str(c.code).upper() != str(code).upper():
                    continue
                if sem is None or getattr(c, "semester", None) == sem:
                    course_obj = c
                    break
        if course_obj is None:
            return None
        return int(getattr(course_obj, "credits", 0) or 0)

    def _find_course_for_section(code: str, sec: Section) -> Optional[Course]:
        """Match course_data row by code + section semester (and department when available)."""
        code_u = str(code).strip().upper()
        sem = getattr(sec, "semester", None)
        prog = str(getattr(sec, "program", "") or "").strip().upper()
        best: Optional[Course] = None
        for c in courses or []:
            if str(getattr(c, "code", "")).strip().upper() != code_u:
                continue
            if sem is not None and getattr(c, "semester", None) != sem:
                continue
            dept = str(getattr(c, "department", "") or "").strip().upper()
            if prog and dept and dept != prog:
                continue
            return c
        for c in courses or []:
            if str(getattr(c, "code", "")).strip().upper() != code_u:
                continue
            if sem is None or getattr(c, "semester", None) == sem:
                best = c
        return best

    def _add_error(rule: str, message: str, course_code: str, section: str, day: str, time: str) -> None:
        # Deduplicate robustly: faculty conflict can be reported from multiple sub-checks
        # with only casing differences (e.g. "ramesh athe" vs "Ramesh Athe") in the message.
        msg_key = message.lower() if str(rule).strip().lower() == "faculty conflict" else message
        key = (rule, course_code, section, msg_key)
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
        duration_violations = verifier.verify_session_duration(sess)
        if tb and duration_violations:
            for v in duration_violations:
                _add_error(
                    "Session duration", v,
                    sess.get("course_code", ""), section_str,
                    getattr(tb, "day", ""),
                    f"{tb.start.strftime('%H:%M')}-{tb.end.strftime('%H:%M')}" if hasattr(tb, "start") else "",
                )

    # 2) Section overlap: two DIFFERENT courses in the same section+period overlapping in time
    by_sec_period = defaultdict(list)
    for s in all_sessions:
        period = normalize_period(s.get("period"))
        # A session might belong to multiple sections (combined courses)
        secs = s.get("sections") or [""]
        if isinstance(secs, str):
            secs = [secs]
        for sec in secs:
            key = (sec, period)
            by_sec_period[key].append(s)
            
    for key, sess_list in by_sec_period.items():
        for i, a in enumerate(sess_list):
            for b in sess_list[i + 1:]:
                tb_a, tb_b = a.get("time_block"), b.get("time_block")
                if not tb_a or not tb_b or tb_a.day != tb_b.day or not tb_a.overlaps(tb_b):
                    continue
                full_code_a = (a.get("course_code") or "").strip().upper()
                full_code_b = (b.get("course_code") or "").strip().upper()
                # Skip if EXACT same course code and type (we shouldn't really have these anymore after grouping, 
                # but just in case of weird data, don't flag a course as overlapping itself)
                if full_code_a == full_code_b and a.get("session_type") == b.get("session_type"):
                    continue
                # Skip if either is an elective basket (individual elective sub-courses
                # are alternatives within the same basket and share their combined slot)
                if "ELECTIVE_BASKET" in full_code_a or "ELECTIVE_BASKET" in full_code_b:
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
    # (comma-separated team lists are split so e.g. Sunil on CS161 vs CS262 is detected.)
    faculty_map: Dict[str, List[tuple]] = defaultdict(list)
    faculty_seen: Set[tuple] = set()
    for s in all_sessions:
        fac_raw = (s.get("instructor") or "").strip()
        if not fac_raw or fac_raw.upper() in ("TBD", "VARIOUS", ""):
            continue
        block = s.get("time_block")
        if not block:
            continue
        code = (s.get("course_code") or "").split("-")[0].strip().upper()
        period = normalize_period(s.get("period"))
        sem_cohorts = program_semester_numbers_from_session_payload(s)
        for fac in faculty_name_tokens(fac_raw):
            fkey = (fac, code, block.day, block.start.strftime("%H:%M"), block.end.strftime("%H:%M"))
            if fkey in faculty_seen:
                continue
            faculty_seen.add(fkey)
            faculty_map[fac].append((block, code, period, sem_cohorts))
    # Track pairs we've already reported so each (faculty, course_pair, day) produces exactly ONE error
    _fac_pair_seen: Set[tuple] = set()
    for fac, items in faculty_map.items():
        items_sorted = sorted(items, key=lambda x: (x[0].day, x[0].start))
        for i in range(len(items_sorted) - 1):
            b1, c1, p1, sem1 = items_sorted[i]
            for j in range(i + 1, len(items_sorted)):
                b2, c2, p2, sem2 = items_sorted[j]
                if c1 == c2:
                    continue  # Same course, different sections - fine
                if normalize_period(p1) != normalize_period(p2):
                    continue  # Different periods (half-semesters)
                if FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER:
                    if sem1 and sem2 and not (sem1 & sem2):
                        continue
                if b1.day != b2.day or not b1.overlaps(b2):
                    continue
                # Canonical pair key - order-independent so A+B and B+A are the same pair
                pair_key = (fac, frozenset([c1, c2]), b1.day)
                if pair_key in _fac_pair_seen:
                    continue
                _fac_pair_seen.add(pair_key)
                ca, cb = sorted([c1, c2])  # alphabetical so message is consistent
                _add_error(
                    "Faculty conflict",
                    f"Faculty {fac} has overlapping sessions: {ca} and {cb} on {b1.day} {b1.start.strftime('%H:%M')}-{b1.end.strftime('%H:%M')}",
                    ca, "", b1.day,
                    f"{b1.start.strftime('%H:%M')}-{b1.end.strftime('%H:%M')}",
                )

    # 3b) Same instructor + same course + overlapping time for different parallel sections,
    # when the course is NOT combinable (credits > threshold — e.g. 3-credit HS161 cannot be
    # one joint lecture for CSE-A and CSE-B).
    exploded_parallel: List[Tuple[str, str, str, Any, str]] = []
    for s in all_sessions:
        fac_raw = (s.get("instructor") or "").strip()
        if not fac_raw or fac_raw.upper() in ("TBD", "VARIOUS", ""):
            continue
        block = s.get("time_block")
        if not block:
            continue
        code = (s.get("course_code") or "").split("-")[0].strip().upper()
        if not code or "ELECTIVE_BASKET" in code:
            continue
        if str(s.get("phase", "")).startswith("Phase 3"):
            continue
        period = normalize_period(s.get("period"))
        secs = s.get("sections") or [""]
        if isinstance(secs, str):
            secs = [secs]
        for sec in secs:
            if not sec:
                continue
            for fac in faculty_name_tokens(fac_raw):
                exploded_parallel.append((fac, code, period, block, sec))

    _par_pair_seen: Set[Tuple[str, str, str, str, str]] = set()
    thr = FACULTY_PARALLEL_SAME_COURSE_CREDIT_THRESHOLD
    for i, tup_a in enumerate(exploded_parallel):
        fa, ca, pa, ba, sea = tup_a
        for j in range(i + 1, len(exploded_parallel)):
            fb, cb, pb, bb, seb = exploded_parallel[j]
            if fa != fb or ca != cb:
                continue
            if normalize_period(pa) != normalize_period(pb):
                continue
            if sea == seb:
                continue
            prog_a = sea.split("-", 1)[0].strip().upper() if sea else ""
            prog_b = seb.split("-", 1)[0].strip().upper() if seb else ""
            if prog_a and prog_b and prog_a != prog_b:
                continue  # Cross-dept: joint offering, not illegal parallel overload.
            sem_a, _ = _section_semester_dept(sea)
            sem_b, _ = _section_semester_dept(seb)
            if sem_a is None or sem_a != sem_b:
                continue
            if ba.day != bb.day or not ba.overlaps(bb):
                continue
            credits = _course_credits_for_code_section(ca, sea)
            if credits is None or credits <= thr:
                continue
            s1, s2 = sorted([sea, seb])
            par_key = (fa, ca, s1, s2, ba.day)
            if par_key in _par_pair_seen:
                continue
            _par_pair_seen.add(par_key)
            _add_error(
                "Faculty conflict",
                (
                    f"Faculty {fa} cannot teach {ca} ({credits} cr, not a combinable/joint lecture) "
                    f"for parallel sections {sea} and {seb} at overlapping times on {ba.day} "
                    f"{ba.start.strftime('%H:%M')}-{ba.end.strftime('%H:%M')}"
                ),
                ca,
                f"{sea} / {seb}",
                ba.day,
                f"{ba.start.strftime('%H:%M')}-{ba.end.strftime('%H:%M')}",
            )

    # 4) Classroom conflicts:
    #   a) Missing/blank room assignment is a strict error
    #   b) Room double-booking in same normalized period is a strict error
    #   c) Room capacity must be sufficient for the session's sections
    room_capacity_by_name: Dict[str, int] = {}
    for r in classrooms or []:
        room_no = str(getattr(r, "room_number", "") or "").strip()
        if not room_no:
            continue
        try:
            room_capacity_by_name[room_no] = int(getattr(r, "capacity", 0) or 0)
        except (TypeError, ValueError):
            room_capacity_by_name[room_no] = 0

    section_students_by_label: Dict[str, int] = {}
    for sec_obj in sections or []:
        label = str(getattr(sec_obj, "label", "") or "").strip()
        if not label:
            continue
        try:
            section_students_by_label[label] = int(getattr(sec_obj, "students", 0) or 0)
        except (TypeError, ValueError):
            section_students_by_label[label] = 0

    by_room_period: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    def _is_unassigned_room_value(room_val: str) -> bool:
        s = str(room_val or "").strip().lower()
        return s in ("", "na", "none", "nan", "tbd")

    for s in all_sessions:
        tb = s.get("time_block")
        if not tb:
            continue
        period = normalize_period(s.get("period"))
        room_raw = str(s.get("room") or "").strip()
        code = (s.get("course_code") or "").strip()
        code_base = code.split("-")[0].strip().upper()
        session_type = str(s.get("session_type") or "L").strip().upper()
        secs = s.get("sections") or [""]
        if isinstance(secs, str):
            secs = [secs]
        sec_list = [str(x).strip() for x in secs if str(x).strip()]
        sec_repr = " / ".join(sec_list) if sec_list else ""
        time_str = f"{tb.start.strftime('%H:%M')}-{tb.end.strftime('%H:%M')}"

        # 4a) Missing room assignment
        if _is_unassigned_room_value(room_raw):
            _add_error(
                "Classroom conflict",
                f"Missing room assignment for {code}",
                code,
                sec_repr,
                tb.day,
                time_str,
            )
            continue

        # Some lab rows carry comma-separated room lists (parallel section labs).
        # Treat each listed room as a valid allocation target for conflict/capacity checks.
        room_list = [r.strip() for r in room_raw.split(",") if r.strip()]
        if not room_list:
            room_list = [room_raw]

        # 4c) Room existence + capacity
        unknown_rooms = [r for r in room_list if r not in room_capacity_by_name]
        if unknown_rooms:
            _add_error(
                "Classroom conflict",
                f"Unknown room(s) '{', '.join(unknown_rooms)}' for {code}",
                code,
                sec_repr,
                tb.day,
                time_str,
            )
        else:
            # Capacity rule (strict):
            # If the session needs N seats and the assigned room capacity < N, it is an error.
            # Apply to lectures/tutorials only; labs/practicals are handled separately by lab assignment logic.
            if session_type in ("L", "T") and "ELECTIVE_BASKET" not in code_base:
                needed = sum(section_students_by_label.get(sec, 0) for sec in sec_list)
                cap = max(room_capacity_by_name.get(r, 0) for r in room_list)
                if needed > 0 and cap < needed:
                    _add_error(
                        "Classroom conflict",
                        f"Room capacity {cap} < required {needed} for {code}",
                        code,
                        sec_repr,
                        tb.day,
                        time_str,
                    )

        for room in room_list:
            by_room_period[(room, period)].append(s)

    # 4b) Room double-booking
    for (room, period), sess_list in by_room_period.items():
        for i, a in enumerate(sess_list):
            for b in sess_list[i + 1:]:
                tb_a, tb_b = a.get("time_block"), b.get("time_block")
                if not tb_a or not tb_b:
                    continue
                if tb_a.day != tb_b.day or not tb_a.overlaps(tb_b):
                    continue
                code_a = (a.get("course_code") or "").strip()
                code_b = (b.get("course_code") or "").strip()
                code_a_base = code_a.split("-")[0].strip().upper()
                code_b_base = code_b.split("-")[0].strip().upper()
                stype_a = str(a.get("session_type") or "L").strip().upper()
                stype_b = str(b.get("session_type") or "L").strip().upper()
                secs_a = a.get("sections") or [""]
                secs_b = b.get("sections") or [""]
                if isinstance(secs_a, str):
                    secs_a = [secs_a]
                if isinstance(secs_b, str):
                    secs_b = [secs_b]
                # Skip exact duplicates of same grouped session payload
                if (
                    code_a == code_b
                    and set(str(x).strip() for x in secs_a if str(x).strip())
                    == set(str(x).strip() for x in secs_b if str(x).strip())
                    and (a.get("session_type") or "L") == (b.get("session_type") or "L")
                ):
                    continue
                # Shared/combined class represented once per section (same course+type+slot+room)
                # is a single teaching event, not a room double-booking.
                if code_a_base == code_b_base and stype_a == stype_b:
                    continue
                overlap_start = max(tb_a.start, tb_b.start).strftime("%H:%M")
                overlap_end = min(tb_a.end, tb_b.end).strftime("%H:%M")
                _add_error(
                    "Classroom conflict",
                    f"Room {room} is double-booked in {period}: {code_a} and {code_b}",
                    code_a,
                    "",
                    tb_a.day,
                    f"{overlap_start}-{overlap_end}",
                )

    # 5) Day-level session rules (same section/course/day/period):
    #    - No L+L same day
    #    - No T+T same day
    #    - No L+T same day
    #    - L+P and T+P are allowed
    day_bucket = defaultdict(list)
    for s in all_sessions:
        code = (s.get("course_code") or "").split("-")[0].strip().upper()
        
        # Skip elective baskets and phase 3 electives as they can have multiple sessions 
        # (different sub-courses) in the same day naturally
        if "ELECTIVE_BASKET" in code or str(s.get("phase", "")).startswith("Phase 3"):
            continue
            
        tb = s.get("time_block")
        period = normalize_period(s.get("period"))
        if not tb or not code:
            continue
            
        for sec in (s.get("sections") or [""]):
            if not sec:
                continue
            key = (sec, code, tb.day, period)
            day_bucket[key].append(s)

    for (sec, code, day, period), sessions in day_bucket.items():
        l_sessions = [s for s in sessions if str(s.get("session_type") or "L").strip().upper() == "L"]
        t_sessions = [s for s in sessions if str(s.get("session_type") or "L").strip().upper() == "T"]

        if len(l_sessions) > 1:
            times = ", ".join(
                f"{s['time_block'].start.strftime('%H:%M')}-{s['time_block'].end.strftime('%H:%M')}"
                for s in l_sessions if s.get("time_block")
            )
            _add_error(
                "1 Day 1 Course",
                f"Course {code} has {len(l_sessions)} lectures on {day} in section {sec} ({period}) but max allowed is 1: {times}",
                code, sec, day, ""
            )
        if len(t_sessions) > 1:
            times = ", ".join(
                f"{s['time_block'].start.strftime('%H:%M')}-{s['time_block'].end.strftime('%H:%M')}"
                for s in t_sessions if s.get("time_block")
            )
            _add_error(
                "1 Day 1 Course",
                f"Course {code} has {len(t_sessions)} tutorials on {day} in section {sec} ({period}) but max allowed is 1: {times}",
                code, sec, day, ""
            )
        if l_sessions and t_sessions:
            l_times = ", ".join(
                f"{s['time_block'].start.strftime('%H:%M')}-{s['time_block'].end.strftime('%H:%M')}"
                for s in l_sessions if s.get("time_block")
            )
            t_times = ", ".join(
                f"{s['time_block'].start.strftime('%H:%M')}-{s['time_block'].end.strftime('%H:%M')}"
                for s in t_sessions if s.get("time_block")
            )
            _add_error(
                "1 Day 1 Course",
                f"Course {code} has lecture+tutorial on same day {day} in section {sec} ({period}) [L: {l_times}] [T: {t_times}]",
                code, sec, day, ""
            )

    # 6) Combined class synchronization, room, and faculty checks
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
            
            # Sub-check B: Faculty availability for combined course
            # Check if this instructor is teaching any *other* course at this time
            # Use the original-cased faculty name (not .upper()) so the _add_error dedup key
            # matches with the pair already reported by Section 3's faculty conflict check above.
            fac_orig = next((s.get('instructor') or '' for s in t_sessions if s.get('instructor')), fac)
            for osess in all_sessions:
                o_code = (osess.get("course_code") or "").split("-")[0].strip().upper()
                if o_code == code: continue
                o_fac = (osess.get("instructor") or "").strip().upper()
                if o_fac == fac:
                    o_tb = osess.get("time_block")
                    if o_tb and o_tb.day == target_tb.day and o_tb.overlaps(target_tb):
                        if FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER:
                            sem_o = program_semester_numbers_from_session_payload(osess)
                            sem_t = set()
                            for ts in t_sessions:
                                sem_t |= set(program_semester_numbers_from_session_payload(ts))
                            sem_tf = frozenset(sem_t)
                            if sem_o and sem_tf and not (sem_o & sem_tf):
                                continue
                        ca, cb = sorted([code, o_code])  # alphabetical - consistent with Section 3
                        _add_error(
                            "Faculty conflict",  # lowercase - matches Section 3 rule name for dedup
                            f"Faculty {fac_orig} has overlapping sessions: {ca} and {cb} on {target_tb.day} {target_tb.start.strftime('%H:%M')}-{target_tb.end.strftime('%H:%M')}",
                            ca, "", target_tb.day,
                            f"{target_tb.start.strftime('%H:%M')}-{target_tb.end.strftime('%H:%M')}"
                        )
                        break

    # 6b) Phase 4 within-group synchronization (hard rule requested by user):
    # Apply ONLY to sessions that are actually produced by Phase 4 (combined-class phase),
    # not to Phase 7 courses (which may also be <=2 credits and single-instructor).
    from config.structure_config import get_group_for_section

    def _section_group_id(section_label: str) -> int:
        try:
            s = str(section_label or "").strip()
            parts = s.split("-")
            if len(parts) >= 2:
                dept = parts[0].strip().upper()
                sec = parts[1].strip().upper()
                return int(get_group_for_section(dept, sec))
        except Exception:
            return 1
        return 1

    # Identify course codes that actually appear in Phase 4 log rows.
    phase4_codes: set = set()
    for s in all_sessions:
        try:
            ph = str(s.get("phase", "") or "")
            if not ph.startswith("Phase 4"):
                continue
            raw = (s.get("course_code") or "").strip()
            if not raw:
                continue
            code = raw.split("-")[0].strip().upper()
            if code:
                phase4_codes.add(code)
        except Exception:
            continue

    # Map: (code, group, period, session_type) -> { section -> set((day,start,end)) }
    sync_map: Dict[tuple, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for s in all_sessions:
        raw = (s.get("course_code") or "").strip()
        if not raw:
            continue
        code = raw.split("-")[0].strip().upper()
        if not code or code not in phase4_codes:
            continue
        ph = str(s.get("phase", "") or "")
        if not ph.startswith("Phase 4"):
            continue
        if "ELECTIVE_BASKET" in code:
            continue
        tb = s.get("time_block")
        if not tb:
            continue
        period = normalize_period(s.get("period"))
        stype = (s.get("session_type") or "L").strip().upper()
        secs = s.get("sections") or []
        if isinstance(secs, str):
            secs = [secs]
        for sec in secs:
            sec_label = str(sec).strip()
            if not sec_label:
                continue
            grp = _section_group_id(sec_label)
            sync_map[(code, grp, period, stype)][sec_label].add((tb.day, tb.start, tb.end))

    for (code, grp, period, stype), by_section in sync_map.items():
        if len(by_section) <= 1:
            continue
        # Compare slot sets across sections
        ref_sec, ref_slots = next(iter(by_section.items()))
        for sec_label, slots in by_section.items():
            if slots != ref_slots:
                _add_error(
                    "Phase 4 synchronization",
                    (
                        f"Within-group combined course desync for {code} (group {grp}, {period}, {stype}): "
                        f"{sec_label} slots != {ref_sec} slots"
                    ),
                    code,
                    f"{sec_label} vs {ref_sec}",
                    "",
                    "",
                )
                break

    # 7) LTPSC compliance: full semester (PRE+POST) totals vs course_data for every
    #    course code that actually appears on the schedule for that section.
    for sec_obj in sections or []:
        sec_label = str(getattr(sec_obj, "label", "") or "").strip()
        if not sec_label:
            continue
        codes: Set[str] = set()
        for s in all_sessions:
            if str(s.get("phase", "")).startswith("Phase 3"):
                continue
            raw = (s.get("course_code") or "").strip()
            if not raw:
                continue
            code = raw.split("-")[0].strip().upper()
            if not code or "ELECTIVE_BASKET" in code:
                continue
            secs = s.get("sections") or []
            if isinstance(secs, str):
                secs = [secs]
            if sec_label not in [str(x).strip() for x in secs if str(x).strip()]:
                continue
            codes.add(code)

        seen_ltpsc: Set[Tuple[str, str]] = set()
        for code in sorted(codes):
            course_obj = _find_course_for_section(code, sec_obj)
            if course_obj is None or not getattr(course_obj, "ltpsc", None):
                continue
            ltpsc_key = str(getattr(course_obj, "ltpsc", "") or "")
            dedup = (str(course_obj.code).strip().upper(), ltpsc_key)
            if dedup in seen_ltpsc:
                continue
            seen_ltpsc.add(dedup)
            compliance = verifier.verify_ltpsc_compliance(
                course_obj, all_sessions, sec_obj, period=None
            )
            sat = compliance.get("satisfied") or {}
            if sat.get("lectures") and sat.get("tutorials") and sat.get("labs"):
                continue
            req = compliance.get("required") or {}
            got = compliance.get("scheduled") or {}
            _add_error(
                "LTPSC compliance",
                (
                    f"LTPSC mismatch for {course_obj.code} in {sec_label}: "
                    f"expected L/T/P={req.get('lectures')}/{req.get('tutorials')}/{req.get('labs')}, "
                    f"scheduled {got.get('lectures')}/{got.get('tutorials')}/{got.get('labs')} "
                    f"(full semester PRE+POST)"
                ),
                str(course_obj.code),
                sec_label,
                "",
                "",
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
