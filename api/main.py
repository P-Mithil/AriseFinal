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
    """Load timetable sessions from time_slot_log CSV. Returns list of session dicts (API schema)."""
    rows = []
    with open(log_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "Phase": row.get("Phase", ""),
                "Course Code": row.get("Course Code", ""),
                "Section": row.get("Section", ""),
                "Day": row.get("Day", ""),
                "Start Time": row.get("Start Time", ""),
                "End Time": row.get("End Time", ""),
                "Room": row.get("Room", ""),
                "Period": row.get("Period", ""),
                "Session Type": row.get("Session Type", ""),
                "Faculty": row.get("Faculty", ""),
            })
    return rows


@app.get("/api/config")
def api_config():
    """Working days, day start/end, lunch windows, programs and section labels for UI."""
    return get_config()


@app.post("/api/generate")
def api_generate():
    """
    First-time Generate: run full pipeline, return timetable + structure for UI labels.
    Uses existing DATA/INPUT/ data.
    """
    try:
        from generate_24_sheets import generate_24_sheets
        # Run pipeline (may take a while)
        output_path, ts = generate_24_sheets()
        log_path = os.path.join(REPO_ROOT, "DATA", "OUTPUT", f"time_slot_log_{ts}.csv")
        if not os.path.exists(log_path):
            raise HTTPException(status_code=500, detail=f"Time slot log not found: {log_path}")
        timetable = load_timetable_from_csv(log_path)
        labels = get_config()
        return {
            "success": True,
            "timetable": timetable,
            "labels": labels,
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
    """Convert API time_slot_log schema to internal format (time_block, course_code, sections, etc.)."""
    from utils.data_models import TimeBlock
    out = []
    for s in sessions:
        course_code = (s.get("Course Code") or "").strip()
        section = (s.get("Section") or "").strip()
        day = (s.get("Day") or "").strip()
        start_s = (s.get("Start Time") or "").strip()
        end_s = (s.get("End Time") or "").strip()
        if not (course_code and section and day and start_s and end_s):
            continue
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
            "period": (s.get("Period") or "").strip().upper() or "PRE",
            "time_block": TimeBlock(day, start_t, end_t),
            "room": s.get("Room") or "",
            "session_type": (s.get("Session Type") or "L").strip().upper(),
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
    return {"success": success, "errors": errors}


@app.post("/api/verify")
def api_verify(req: VerifyRequest):
    """Verify proposed sessions (after drag). Returns success or list of detailed errors."""
    return run_verify(req.sessions)


class ReflowRequest(BaseModel):
    sessions: List[Dict[str, Any]]


@app.post("/api/reflow")
def api_reflow(req: ReflowRequest):
    """
    Try to reflow: keep user-moved sessions, reschedule others to satisfy rules.
    For now returns not_possible; full reflow can be implemented later.
    """
    # Placeholder: attempt would require re-running parts of the pipeline with fixed blocks.
    # Return not_possible so frontend reverts to first-generated timetable.
    return {"success": False, "not_possible": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
