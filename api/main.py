"""
Timetable Pro API: generate, verify, and reflow endpoints.
Serves the frontend; uses existing pipeline and verification.
"""
import os
import re
import sys
import csv
from datetime import time
from typing import List, Dict, Any, Optional

# Run from repo root so imports work
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Timetable Pro API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Config (from schedule_config and structure_config) ---
def get_config(semesters: Optional[List[int]] = None):
    """Build config for UI. If semesters not provided, derive from run_phase1() courses."""
    from config.schedule_config import WORKING_DAYS, DAY_START_TIME, DAY_END_TIME, LUNCH_WINDOWS
    from config.structure_config import DEPARTMENTS, SECTIONS_BY_DEPT
    if semesters is None:
        try:
            from modules_v2.phase1_data_validation_v2 import run_phase1
            courses, _, _ = run_phase1()
            semesters = sorted(set(c.semester for c in courses if c.department in DEPARTMENTS))
        except Exception:
            semesters = [1, 3, 5]
    program_display = {"CSE": "Computer Science", "DSAI": "Data Science & AI", "ECE": "ECE"}
    programs = []
    for dept in DEPARTMENTS:
        programs.append({
            "id": dept,
            "name": program_display.get(dept, dept),
            "sections": SECTIONS_BY_DEPT.get(dept, []),
        })
    section_labels = []
    for dept in DEPARTMENTS:
        for sem in semesters:
            for sec in SECTIONS_BY_DEPT.get(dept, []):
                section_labels.append({
                    "section": f"{dept}-{sec}-Sem{sem}",
                    "program": dept,
                    "semester": sem,
                    "label": f"Semester {sem} - {dept}",
                })
    return {
        "working_days": WORKING_DAYS,
        "day_start": DAY_START_TIME.strftime("%H:%M"),
        "day_end": DAY_END_TIME.strftime("%H:%M"),
        "lunch_windows": {str(k): [v[0].strftime("%H:%M"), v[1].strftime("%H:%M")] for k, v in LUNCH_WINDOWS.items()},
        "programs": programs,
        "semesters": semesters,
        "section_labels": section_labels,
    }


def load_timetable_from_csv(log_path: str) -> List[Dict[str, Any]]:
    """Load timetable sessions from time_slot_log CSV. Returns list of session dicts (API schema).
    Deduplicates sessions that appear multiple times for the same slot (common with combined classes).
    """
    seen: set = set()
    rows = []
    with open(log_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            section = row.get("Section", "")
            course_code = row.get("Course Code", "")
            day = row.get("Day", "")
            start_time = row.get("Start Time", "")
            session_type = row.get("Session Type", "")
            period = row.get("Period", "")
            # Key for deduplication - same session shouldn't appear twice for the same section
            # Include End Time to ensure sessions with different durations are not accidentally merged/dropped
            dedup_key = (section, course_code, day, start_time, row.get("End Time", ""), session_type, period)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            rows.append({
                "Phase": row.get("Phase", ""),
                "Course Code": course_code,
                "Section": section,
                "Day": day,
                "Start Time": start_time,
                "End Time": row.get("End Time", ""),
                "Room": row.get("Room", ""),
                "Period": period,
                "Session Type": session_type,
                "Faculty": row.get("Faculty", ""),
            })
    return rows


# Verification table column indices (0-based) matching write_verification_table headers
_VERIFICATION_HEADERS = [
    "Code", "Course Name", "Instructor", "LTPSC", "Assigned Lab", "Assigned Classroom",
    "Lectures (Req/Sched)", "Tutorials (Req/Sched)", "Labs (Req/Sched)", "Status",
    "Time Slot Issues", "Room Conflicts", "Colour",
]


def _sheet_name_to_key(sheet_name: str) -> Optional[str]:
    """Convert Excel sheet name to frontend key. e.g. 'CSE-A Sem1 PreMid' -> 'CSE-A-Sem1-PRE'."""
    # Sheet name format: "{section_name} Sem{semester} {period}" e.g. "CSE-A Sem1 PreMid"
    m = re.match(r"^(.+?)\s+Sem(\d+)\s+(PreMid|PostMid)$", sheet_name.strip(), re.IGNORECASE)
    if not m:
        return None
    section_name, sem, period = m.group(1).strip(), m.group(2), m.group(3)
    period_key = "PRE" if period.lower() == "premid" else "POST"
    return f"{section_name}-Sem{sem}-{period_key}"


def parse_verification_tables_from_excel(excel_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse verification tables from generated Excel. Returns dict keyed by section-period
    e.g. "CSE-A-Sem1-PRE" -> list of { code, course_name, ltpsc, lectures, tutorials, labs, status, ... }.
    """
    import openpyxl
    result: Dict[str, List[Dict[str, Any]]] = {}
    if not os.path.isfile(excel_path):
        return result
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    for sheet_name in wb.sheetnames:
        key = _sheet_name_to_key(sheet_name)
        if not key:
            continue
        sheet = wb[sheet_name]
        # Find header row (row with "Code" in first column)
        header_row_idx = None
        for row_idx in range(1, min(sheet.max_row + 1, 500)):
            cell_val = sheet.cell(row=row_idx, column=1).value
            if cell_val is not None and str(cell_val).strip() == "Code":
                header_row_idx = row_idx
                break
        if header_row_idx is None:
            continue
        # Read data rows (columns: Code, Course Name, Instructor, LTPSC, Assigned Lab, Assigned Classroom,
        # Lectures (Req/Sched), Tutorials (Req/Sched), Labs (Req/Sched), Status, Time Slot Issues, Room Conflicts, Colour)
        rows_data = []
        for row_idx in range(header_row_idx + 1, sheet.max_row + 1):
            code_cell = sheet.cell(row=row_idx, column=1).value
            if code_cell is None or (isinstance(code_cell, str) and not code_cell.strip()):
                break
            def _cell(r: int, c: int) -> str:
                v = sheet.cell(row=r, column=c).value
                return str(v).strip() if v is not None else ""
            row_dict = {
                "code": _cell(row_idx, 1),
                "course_name": _cell(row_idx, 2),
                "instructor": _cell(row_idx, 3),
                "ltpsc": _cell(row_idx, 4),
                "assigned_lab": _cell(row_idx, 5),
                "assigned_classroom": _cell(row_idx, 6),
                "lectures": _cell(row_idx, 7),
                "tutorials": _cell(row_idx, 8),
                "labs": _cell(row_idx, 9),
                "status": _cell(row_idx, 10),
                "time_slot_issues": _cell(row_idx, 11),
                "room_conflicts": _cell(row_idx, 12),
            }
            if row_dict["code"]:
                rows_data.append(row_dict)
        result[key] = rows_data
    return result


@app.get("/api/config")
def api_config():
    """Working days, day start/end, lunch windows, programs and section labels for UI."""
    return get_config()


@app.post("/api/generate")
async def api_generate():
    """
    First-time Generate: run full pipeline, return timetable + structure for UI labels.
    Runs generate_24_sheets in a thread pool to avoid blocking the uvicorn event loop
    (which causes Windows signal-handler to kill the process mid-request).
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    try:
        from generate_24_sheets import generate_24_sheets
        # Run the heavy synchronous pipeline in a thread pool so uvicorn stays alive
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            output_path, ts = await loop.run_in_executor(pool, generate_24_sheets)
        log_path = os.path.join(REPO_ROOT, "DATA", "OUTPUT", f"time_slot_log_{ts}.csv")
        if not os.path.exists(log_path):
            raise HTTPException(status_code=500, detail=f"Time slot log not found: {log_path}")
        timetable = load_timetable_from_csv(log_path)
        labels = get_config()
        excel_full_path = os.path.join(REPO_ROOT, output_path)
        verification_table = parse_verification_tables_from_excel(excel_full_path)
        return {
            "success": True,
            "timetable": timetable,
            "labels": labels,
            "verification_table": verification_table,
            "log_timestamp": ts,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))



class GenerateFromSessionsRequest(BaseModel):
    sessions: List[Dict[str, Any]]

@app.post("/api/generate-from-sessions")
async def api_generate_from_sessions(req: GenerateFromSessionsRequest):
    """
    Re-generate all 24 Excel sheets from the current (possibly dragged/edited) session list.
    This preserves all manual drag changes while following existing scheduling rules for
    room assignment and sheet formatting.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    sessions = req.sessions

    def _do_generate():
        import pandas as pd
        from datetime import datetime
        from generate_24_sheets import generate_24_sheets_from_log

        # Write the sessions to a new time-slot-log CSV so generate_24_sheets
        # can pick it up. We reuse the existing CSV format.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(REPO_ROOT, "DATA", "OUTPUT", f"time_slot_log_{ts}.csv")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        # Build a clean DataFrame from the session list
        rows = []
        for s in sessions:
            rows.append({
                "Phase": s.get("Phase", "Manual"),
                "Course Code": s.get("Course Code", ""),
                "Course Name": s.get("Course Name", ""),
                "Section": s.get("Section", ""),
                "Day": s.get("Day", ""),
                "Start Time": s.get("Start Time", ""),
                "End Time": s.get("End Time", ""),
                "Room": s.get("Room", ""),
                "Faculty": s.get("Faculty", ""),
                "Session Type": s.get("Session Type", "L"),
                "Period": s.get("Period", "PRE"),
            })
        df = pd.DataFrame(rows)
        df.to_csv(log_path, index=False)

        # Regenerate the 24 Excel sheets from this log
        output_path = generate_24_sheets_from_log(log_path, ts)
        return output_path, ts, log_path

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            output_path, ts, log_path = await loop.run_in_executor(pool, _do_generate)

        if not os.path.exists(log_path):
            raise HTTPException(status_code=500, detail=f"Time slot log not found: {log_path}")

        timetable = load_timetable_from_csv(log_path)
        labels = get_config()
        excel_full_path = os.path.join(REPO_ROOT, output_path)
        verification_table = parse_verification_tables_from_excel(excel_full_path)
        return {
            "success": True,
            "timetable": timetable,
            "labels": labels,
            "verification_table": verification_table,
            "log_timestamp": ts,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


class VerifyRequest(BaseModel):
    sessions: List[Dict[str, Any]]  # each: Course Code, Section, Day, Start Time, End Time, Room, Period, Session Type, Faculty, Phase


def _sessions_api_to_internal(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert API time_slot_log schema to internal format (time_block, course_code, sections, etc.).
    Deduplicates by (section, course_code, day, start_time, session_type, period) to avoid
    false overlap errors from combined-class sessions logged multiple times.
    """
    from utils.data_models import TimeBlock
    seen: set = set()
    out = []
    for s in sessions:
        course_code = (s.get("Course Code") or "").strip()
        section = (s.get("Section") or "").strip()
        day = (s.get("Day") or "").strip()
        start_s = (s.get("Start Time") or "").strip()
        end_s = (s.get("End Time") or "").strip()
        session_type = (s.get("Session Type") or "L").strip().upper()
        period = (s.get("Period") or "").strip().upper() or "PRE"
        if not (course_code and section and day and start_s and end_s):
            continue
        # Deduplicate: same section/course/day/start/type should only appear once
        dedup_key = (section, course_code, day, start_s, session_type, period)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        try:
            hh, mm = start_s.split(":")
            start_t = time(int(hh), int(mm))
            hh, mm = end_s.split(":")
            end_t = time(int(hh), int(mm))
        except Exception:
            continue
        out.append({
            "phase": s.get("Phase", ""),
            "course_code": course_code,
            "sections": [section],
            "period": period,
            "time_block": TimeBlock(day, start_t, end_t),
            "room": s.get("Room") or "",
            "session_type": session_type,
            "instructor": s.get("Faculty") or "",
        })
    return out


def run_verify(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Run verification on in-memory sessions (API schema).
    Uses deep_verification.run_verification_on_sessions for single source of truth.
    Returns { "success": bool, "errors": [ { "rule", "message", "course_code", "section", "day", "time", ... } ] }.
    """
    from modules_v2.phase1_data_validation_v2 import run_phase1
    from config.structure_config import DEPARTMENTS, SECTIONS_BY_DEPT, STUDENTS_PER_SECTION, get_group_for_section
    from utils.data_models import Section
    from deep_verification import run_verification_on_sessions as run_dv_verify

    all_sessions = _sessions_api_to_internal(sessions)
    if not all_sessions:
        return {"success": True, "errors": []}
    courses, classrooms, _ = run_phase1()
    unique_semesters = sorted(set(c.semester for c in courses if c.department in DEPARTMENTS))
    sections = []
    for dept in DEPARTMENTS:
        for sem in unique_semesters:
            for sec_label in SECTIONS_BY_DEPT.get(dept, []):
                group = get_group_for_section(dept, sec_label)
                sections.append(Section(dept, group, sec_label, sem, STUDENTS_PER_SECTION))
    success, errors = run_dv_verify(all_sessions, courses, sections, classrooms)
    # Deduplicate errors: same (rule, course_code, section, message) should not appear twice
    seen_errors: set = set()
    unique_errors = []
    for e in errors:
        key = (e.get("rule", ""), e.get("course_code", ""), e.get("section", ""), e.get("message", ""))
        if key not in seen_errors:
            seen_errors.add(key)
            unique_errors.append(e)
    return {"success": len(unique_errors) == 0, "errors": unique_errors}


@app.post("/api/verify")
def api_verify(req: VerifyRequest):
    """Verify proposed sessions (after drag). Returns success or list of detailed errors."""
    return run_verify(req.sessions)


class ReflowRequest(BaseModel):
    sessions: List[Dict[str, Any]]
    movedSession: Dict[str, Any]


@app.post("/api/reflow")
def api_reflow(req: ReflowRequest):
    """
    Try to reflow: keep user-moved sessions, reschedule others to satisfy rules.
    Minimal implementation: if the move introduced conflicts, try to relocate only
    the directly-conflicting sessions to the next available 15-minute slot.
    If still not possible, return not_possible so frontend can revert.
    """
    try:
        sessions = list(req.sessions or [])
        moved = req.movedSession or {}

        def _norm_period(p: str) -> str:
            v = (p or "").strip().upper()
            if v in ("PREMID", "PRE"):
                return "PRE"
            if v in ("POSTMID", "POST"):
                return "POST"
            return v or "PRE"

        def _parse_hhmm(t: str) -> int:
            hh, mm = (t or "").strip().split(":")
            return int(hh) * 60 + int(mm)

        def _fmt_hhmm(m: int) -> str:
            return f"{m // 60:02d}:{m % 60:02d}"

        def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
            return a_start < b_end and b_start < a_end

        moved_day = (moved.get("Day") or "").strip()
        moved_start = moved.get("Start Time") or ""
        moved_end = moved.get("End Time") or ""
        moved_section = (moved.get("Section") or "").strip()
        moved_room = (moved.get("Room") or "").strip()
        moved_faculty = (moved.get("Faculty") or "").strip()
        moved_period = _norm_period(moved.get("Period") or "PRE")
        if not (moved_day and moved_start and moved_end and moved_section):
            return {"success": False, "not_possible": True}
        moved_start_m = _parse_hhmm(moved_start)
        moved_end_m = _parse_hhmm(moved_end)

        # Build slot grid
        from config.schedule_config import WORKING_DAYS, DAY_START_TIME, DAY_END_TIME, LUNCH_WINDOWS
        day_start_m = DAY_START_TIME.hour * 60 + DAY_START_TIME.minute
        day_end_m = DAY_END_TIME.hour * 60 + DAY_END_TIME.minute

        # Lunch windows by semester (SemX parsed from section label)
        sem_re = re.compile(r"Sem(\d+)", re.IGNORECASE)

        def _get_lunch_window(section_label: str):
            m = sem_re.search(section_label or "")
            if not m:
                return None
            sem = int(m.group(1))
            win = LUNCH_WINDOWS.get(sem)
            if not win:
                return None
            start_t, end_t = win
            return (start_t.hour * 60 + start_t.minute, end_t.hour * 60 + end_t.minute)

        def _session_conflicts(s: Dict[str, Any], other: Dict[str, Any]) -> bool:
            if _norm_period(s.get("Period")) != _norm_period(other.get("Period")):
                return False
            if (s.get("Day") or "").strip() != (other.get("Day") or "").strip():
                return False
            try:
                s1, e1 = _parse_hhmm(s.get("Start Time")), _parse_hhmm(s.get("End Time"))
                s2, e2 = _parse_hhmm(other.get("Start Time")), _parse_hhmm(other.get("End Time"))
            except Exception:
                return False
            if not _overlap(s1, e1, s2, e2):
                return False

            # Conflicts if share section OR room OR faculty
            same_section = (s.get("Section") or "").strip() == (other.get("Section") or "").strip()
            same_room = (s.get("Room") or "").strip() and (s.get("Room") or "").strip() == (other.get("Room") or "").strip()
            same_faculty = (s.get("Faculty") or "").strip() and (s.get("Faculty") or "").strip() == (other.get("Faculty") or "").strip()
            return same_section or same_room or same_faculty

        # Find sessions directly conflicting with the moved one
        moved_key = (
            (moved.get("Course Code") or "").strip(),
            moved_section,
            moved_day,
            moved_start,
            moved_end,
            moved_period,
        )

        def _is_same_session(a: Dict[str, Any], key) -> bool:
            return (
                ((a.get("Course Code") or "").strip(), (a.get("Section") or "").strip(),
                 (a.get("Day") or "").strip(), a.get("Start Time") or "", a.get("End Time") or "",
                 _norm_period(a.get("Period") or "PRE")) == key
            )

        conflicts = []
        for s in sessions:
            if _is_same_session(s, moved_key):
                continue
            # Ensure we only move sessions within the same period as moved (UI is scoped by period)
            if _norm_period(s.get("Period") or "PRE") != moved_period:
                continue
            if _session_conflicts(moved, s):
                conflicts.append(s)

        if not conflicts:
            # Already ok (or conflicts not detected here); let verifier be source of truth
            v = run_verify(sessions)
            if v.get("success"):
                return {"success": True, "timetable": sessions}
            return {"success": False, "not_possible": True}

        # Occupancy check against current sessions (keeps moved fixed)
        def _can_place(sess: Dict[str, Any], cand_day: str, cand_start: int) -> bool:
            duration = _parse_hhmm(sess.get("End Time")) - _parse_hhmm(sess.get("Start Time"))
            cand_end = cand_start + duration
            if cand_start < day_start_m or cand_end > day_end_m:
                return False

            lunch = _get_lunch_window((sess.get("Section") or "").strip())
            if lunch and _overlap(cand_start, cand_end, lunch[0], lunch[1]):
                return False

            # Check overlap vs all others (including moved)
            for o in sessions:
                if o is sess:
                    continue
                if _norm_period(o.get("Period") or "PRE") != moved_period:
                    continue
                if (o.get("Day") or "").strip() != cand_day:
                    continue
                try:
                    o_s, o_e = _parse_hhmm(o.get("Start Time")), _parse_hhmm(o.get("End Time"))
                except Exception:
                    continue
                if not _overlap(cand_start, cand_end, o_s, o_e):
                    continue
                same_section = (sess.get("Section") or "").strip() == (o.get("Section") or "").strip()
                same_room = (sess.get("Room") or "").strip() and (sess.get("Room") or "").strip() == (o.get("Room") or "").strip()
                same_faculty = (sess.get("Faculty") or "").strip() and (sess.get("Faculty") or "").strip() == (o.get("Faculty") or "").strip()
                if same_section or same_room or same_faculty:
                    return False
            return True

        # Try to relocate each conflicting session greedily
        for sess in conflicts:
            orig_day = (sess.get("Day") or "").strip()
            try:
                duration = _parse_hhmm(sess.get("End Time")) - _parse_hhmm(sess.get("Start Time"))
            except Exception:
                continue

            day_order = [orig_day] + [d for d in WORKING_DAYS if d != orig_day]
            placed = False
            for d in day_order:
                for start in range(day_start_m, day_end_m - duration + 1, 15):
                    # avoid placing exactly on moved's start time if it would still conflict
                    if d == moved_day and _overlap(start, start + duration, moved_start_m, moved_end_m):
                        continue
                    if _can_place(sess, d, start):
                        sess["Day"] = d
                        sess["Start Time"] = _fmt_hhmm(start)
                        sess["End Time"] = _fmt_hhmm(start + duration)
                        placed = True
                        break
                if placed:
                    break

            if not placed:
                return {"success": False, "not_possible": True}

        # Final deep verification
        v = run_verify(sessions)
        if v.get("success"):
            return {"success": True, "timetable": sessions}
        return {"success": False, "not_possible": True}
    except Exception:
        import traceback
        traceback.print_exc()
        return {"success": False, "not_possible": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
