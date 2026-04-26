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

    for s in rows:
        course_code = (s.get("course_code") or "").strip()
        section = (s.get("section") or "").strip()
        tb = s.get("time_block")
        if not course_code or not section or not isinstance(tb, TimeBlock):
            continue
        day = tb.day
        session_type = (s.get("session_type") or "L").strip().upper()
        if session_type == "ELECTIVE":
            session_type = "L"
        period = normalize_period(s.get("period"))
        phase = (s.get("phase") or "").strip()
        phase_lc = phase.lower()
        faculty_raw = (s.get("faculty") or "").strip()
        room = (s.get("room") or "").strip()

        # Phase-aware grouping:
        # - Phase 4 combined sessions merge by slot (multiple sections).
        # - Shared-room joint lectures (historically C004; also C202/C203/…): merge *all* Phase N
        #   (not Phase 3 electives) rows with the same (course, day, time, type, period, room)
        #   into one verify session, even when one sheet row is tagged "Phase 4" and another
        #   "Phase 5"/"Phase 7". If we only merged C004, the same physical joint class in another
        #   room stayed as two one-section sessions and LTPSC compliance under-counted one section.
        # - All other sessions stay per-section so parallel classes with different faculty/rooms stay valid.
        cc_upper = course_code.upper()
        room_k = str(room or "").strip().upper()
        is_shared_room_joint_bucket = (
            bool(room_k)
            and phase_lc.startswith("phase")
            and phase_lc != "phase 3"
            and not cc_upper.startswith("ELECTIVE_BASKET")
        )
        is_phase4_combined = phase_lc == "phase 4" or is_shared_room_joint_bucket
        if is_phase4_combined:
            # Omit *phase* from the merge key for shared-room joint buckets so Phase 4 /
            # Phase 5 / Phase 7 tags for the same slot unify.
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
        rm = (s.get("room") or "").strip()
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
