"""
Bridge pipeline / UI session rows to deep_verification and rebuild occupied_slots for repair loops.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from utils.data_models import ScheduledSession, TimeBlock
from utils.period_utils import normalize_period


class GenerationViolationError(Exception):
    """Raised when REQUIRE_ZERO_VERIFICATION_VIOLATIONS is True and verify fails."""

    def __init__(
        self,
        message: str,
        errors: List[Dict[str, Any]],
        debug_faculty_path: Optional[str] = None,
    ):
        super().__init__(message)
        self.errors = errors
        self.debug_faculty_path = debug_faculty_path


def _normalize_faculty_for_grouping(name: Optional[str]) -> str:
    if not name or not str(name).strip():
        return ""
    return " ".join(str(name).strip().split()).lower()


def rebuild_occupied_slots_from_all_sessions(all_sessions: List[Any]) -> defaultdict:
    """Same keying as Phase 6 faculty resolver: section_PRE/POST -> [(TimeBlock, course_code), ...]."""
    occupied_slots: defaultdict = defaultdict(list)

    for session_val in all_sessions:
        if isinstance(session_val, dict):
            sections_val = session_val.get("sections", [])
            period_val = normalize_period(session_val.get("period", "PRE") or "PRE")
            block_val = session_val.get("time_block")
            course_code_val = session_val.get("course_code", "")
            if block_val and sections_val:
                for section_val_inner in sections_val:
                    sk = f"{section_val_inner}_{period_val}"
                    occupied_slots[sk].append((block_val, course_code_val))
        elif hasattr(session_val, "section") and hasattr(session_val, "block"):
            p_obj = normalize_period(getattr(session_val, "period", "PRE") or "PRE")
            sk = f"{session_val.section}_{p_obj}"
            occupied_slots[sk].append((session_val.block, session_val.course_code))

    return occupied_slots


def final_ui_rows_to_verify_sessions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group final_ui_sessions-style rows (one section per row) into internal verify dicts
    (sections list + time_block), matching api/main _sessions_api_to_internal semantics.
    """
    grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    def _txt(v: Any) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("nan", "none") else s

    for s in rows:
        course_code = _txt(s.get("course_code"))
        section = _txt(s.get("section"))
        tb = s.get("time_block")
        if not course_code or not section or not isinstance(tb, TimeBlock):
            continue
        day = tb.day
        session_type = _txt(s.get("session_type") or "L").upper()
        if session_type == "ELECTIVE":
            # Preserve duration semantics for elective rows that use legacy ELECTIVE tag.
            try:
                _dur_m = (tb.end.hour * 60 + tb.end.minute) - (tb.start.hour * 60 + tb.start.minute)
                if _dur_m == 60:
                    session_type = "T"
                elif _dur_m == 120:
                    session_type = "P"
                else:
                    session_type = "L"
            except Exception:
                session_type = "L"
        period = normalize_period(s.get("period"))
        phase = _txt(s.get("phase"))
        phase_lc = phase.lower()
        faculty_raw = _txt(s.get("faculty"))
        room = _txt(s.get("room"))

        # Phase-aware grouping:
        # - Only true Phase 4 rows are merged across sections by slot.
        # - Non-Phase-4 rows (Phase 5/7 etc.) must remain per-section, even if they share room/time,
        #   so Phase 4 synchronization checks do not accidentally include non-Phase-4 sessions.
        # - Phase 3 electives are always per-section rows in verification input.
        cc_upper = course_code.upper()
        room_k = str(room or "").strip().upper()
        is_phase4_combined = phase_lc == "phase 4" and not cc_upper.startswith("ELECTIVE_BASKET")
        if is_phase4_combined:
            # Group across sections for the exact same Phase 4 slot.
            group_key = (course_code, day, tb.start, tb.end, session_type, period, room_k)
        else:
            group_key = (phase, course_code, section, day, tb.start, tb.end, session_type, period)

        if group_key not in grouped:
            grouped[group_key] = {
                "phase": phase,
                "course_code": course_code,
                "sections": set(),
                "period": period,
                "time_block": tb,
                "room": room,
                "session_type": session_type,
                "instructor": faculty_raw or "",
            }
        g = grouped[group_key]
        # Prefer Phase 4 label whenever any contributing row is Phase 4 (strict repair / C004 rules).
        if phase_lc == "phase 4":
            g["phase"] = "Phase 4"
        grouped[group_key]["sections"].add(section)
        rm = _txt(s.get("room"))
        if rm and not (g.get("room") or "").strip():
            g["room"] = rm
        if faculty_raw and not (g.get("instructor") or "").strip():
            g["instructor"] = faculty_raw

    out: List[Dict[str, Any]] = []
    for g_sess in grouped.values():
        g_sess["sections"] = sorted(g_sess["sections"])
        out.append(g_sess)
    return out


def macro_repair_pipeline_sessions(
    all_sessions: List[Any],
    classrooms: List[Any],
    attempt_index: int,
) -> None:
    """
    In-place reshuffle + faculty resolution + section-overlap pass.
    Uses a different RNG stream per attempt_index (like trying another shuffle in real scheduling).
    """
    import random

    from config.schedule_config import (
        GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES,
        GENERATION_REPAIR_SHUFFLE_SEED,
    )
    from modules_v2.phase5_core_courses import detect_and_resolve_section_overlaps
    from modules_v2.phase6_faculty_conflicts import detect_faculty_conflicts
    from utils.faculty_conflict_resolver import resolve_all_faculty_conflicts

    base_seed = GENERATION_REPAIR_SHUFFLE_SEED + attempt_index * 272_737
    for outer in range(GENERATION_FACULTY_REPAIR_MAX_OUTER_PASSES):
        fc_now = len(detect_faculty_conflicts(all_sessions))
        if fc_now == 0:
            break
        seed = base_seed + outer * 10_007
        random.seed(seed)
        rng = random.Random(seed)
        rng.shuffle(all_sessions)
        occupied_repair = rebuild_occupied_slots_from_all_sessions(all_sessions)
        resolved, _rem = resolve_all_faculty_conflicts(
            all_sessions,
            classrooms,
            occupied_repair,
            max_passes=14 + outer * 2 + attempt_index,
            rng=rng,
        )
        all_sessions[:] = resolved
        occupied_ov = rebuild_occupied_slots_from_all_sessions(all_sessions)
        all_sessions[:] = detect_and_resolve_section_overlaps(
            all_sessions, occupied_ov, classrooms
        )


def run_strict_verification_on_final_ui(
    final_ui_sessions: List[Dict[str, Any]],
    courses: List[Any],
    sections: List[Any],
    classrooms: List[Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    from deep_verification import run_verification_on_sessions

    internal = final_ui_rows_to_verify_sessions(final_ui_sessions)
    if not internal:
        return True, []
    return run_verification_on_sessions(internal, courses, sections, classrooms)
