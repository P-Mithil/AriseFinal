"""
Room conflict resolver: achieve 0 classroom conflicts by reassigning rooms or rescheduling.
Uses same "precaution better than cure" approach as faculty conflict resolution.
"""

from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from datetime import time

from utils.data_models import TimeBlock, ScheduledSession, ClassRoom
from modules_v2.phase8_classroom_assignment import (
    detect_room_conflicts,
    check_room_conflict,
    mark_room_occupied,
    find_available_classroom,
)


def _parse_time_range(time_str: str) -> Tuple[Optional[time], Optional[time]]:
    """Parse 'HH:MM-HH:MM' to (start, end) time objects."""
    if not time_str or "-" not in time_str:
        return None, None
    parts = time_str.strip().split("-")
    if len(parts) != 2:
        return None, None
    try:
        start_parts = parts[0].strip().split(":")
        end_parts = parts[1].strip().split(":")
        start = time(int(start_parts[0]), int(start_parts[1]) if len(start_parts) > 1 else 0)
        end = time(int(end_parts[0]), int(end_parts[1]) if len(end_parts) > 1 else 0)
        return start, end
    except (ValueError, IndexError):
        return None, None


def _normalize_period(raw: str) -> str:
    if not raw:
        return "PRE"
    val = str(raw).strip().upper()
    if val in ("PREMID", "PRE"):
        return "PRE"
    if val in ("POSTMID", "POST"):
        return "POST"
    return val


def _normalize_section_part(part: str) -> str:
    """Normalize section for comparison (e.g. strip -SemN suffix)."""
    if not part:
        return ""
    s = str(part).strip()
    if "-Sem" in s:
        s = s.split("-Sem")[0].strip()
    return s


def _sections_overlap(conflict_section: str, sess_section: str) -> bool:
    """True if conflict section and session section refer to the same or overlapping sections."""
    if not conflict_section and not sess_section:
        return True
    if not conflict_section or not sess_section:
        return False
    conflict_parts = {_normalize_section_part(p) for p in conflict_section.split(",") if p.strip()}
    sess_parts = {_normalize_section_part(p) for p in sess_section.split(",") if p.strip()}
    return bool(conflict_parts & sess_parts)


def _session_match(session, course: str, section: str, period: str, day: str,
                   start: time, end: time) -> bool:
    """Return True if this session matches (course, section, period, day, time)."""
    period_norm = _normalize_period(period)
    if isinstance(session, dict):
        sess_course = (session.get("course_code") or session.get("course", "") or "").split("-")[0]
        # Combined sessions use 'sections' (list); electives use 'section'
        sess_section = session.get("section") or ", ".join(session.get("sections", []))
        if isinstance(sess_section, list):
            sess_section = ", ".join(sess_section) if sess_section else ""
        sess_period = _normalize_period(session.get("period", ""))
        block = session.get("time_block")
        if not block or not hasattr(block, "day"):
            return False
        if (sess_course != course.split("-")[0] or sess_period != period_norm or block.day != day):
            return False
        if section or sess_section:
            if not _sections_overlap(section, sess_section):
                return False
        if not (end <= block.start or start >= block.end):
            return True
        return False
    if not hasattr(session, "course_code") or not hasattr(session, "block") or not session.block:
        return False
    sess_course = (getattr(session, "course_code", "") or "").split("-")[0]
    if sess_course != course.split("-")[0]:
        return False
    if _normalize_period(getattr(session, "period", "")) != period_norm:
        return False
    if session.block.day != day:
        return False
    sess_section = getattr(session, "section", "") or ""
    if section or sess_section:
        if not _sections_overlap(section, sess_section):
            return False
    if end <= session.block.start or start >= session.block.end:
        return False
    return True


def _find_session_in_lists(
    phase5_sessions: List,
    phase7_sessions: List,
    combined_sessions: List,
    elective_sessions: List,
    course: str,
    section: str,
    period: str,
    day: str,
    start: time,
    end: time,
):
    """Return (session, source) where source is 'phase5'|'phase7'|'combined'|'elective'."""
    for s in phase5_sessions:
        if _session_match(s, course, section, period, day, start, end):
            return s, "phase5"
    for s in phase7_sessions:
        if _session_match(s, course, section, period, day, start, end):
            return s, "phase7"
    for s in combined_sessions:
        if _session_match(s, course, section, period, day, start, end):
            return s, "combined"
    for s in elective_sessions or []:
        if _session_match(s, course, section, period, day, start, end):
            return s, "elective"
    return None, None


def _move_priority(source: str) -> int:
    """Lower = move this one first (elective first, then phase7, phase5, combined last)."""
    return {"elective": 0, "phase7": 1, "phase5": 2, "combined": 3}.get(source, 4)


def _build_room_occupancy_excluding(
    phase5_sessions: List,
    phase7_sessions: List,
    combined_sessions: List,
    elective_sessions: List,
    exclude_session,
    exclude_source: str,
    classrooms: List[ClassRoom],
) -> Dict:
    """Build room_occupancy from all sessions except the one to move. Structure: period -> room -> day -> [(start, end, course)]."""
    from modules_v2.phase8_classroom_assignment import extract_combined_room_occupancy

    room_occupancy = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    classroom_numbers = set()
    if classrooms:
        classroom_numbers = {r.room_number for r in classrooms if r.room_type.lower() != "lab" and "lab" not in r.room_type.lower()}

    def add_session(session, period: str, room: str, day: str, start: time, end: time, course_code: str):
        if session is exclude_session and exclude_source in ("phase5", "phase7", "combined", "elective"):
            return
        if not classrooms or room in classroom_numbers:
            room_occupancy[_normalize_period(period)][room][day].append((start, end, course_code))

    for session in phase5_sessions:
        if not getattr(session, "room", None) or not getattr(session, "block", None):
            continue
        if session is exclude_session and exclude_source == "phase5":
            continue
        add_session(
            session,
            _normalize_period(getattr(session, "period", "PRE")),
            session.room,
            session.block.day,
            session.block.start,
            session.block.end,
            getattr(session, "course_code", ""),
        )
    for session in phase7_sessions:
        if not getattr(session, "room", None) or not getattr(session, "block", None):
            continue
        if session is exclude_session and exclude_source == "phase7":
            continue
        add_session(
            session,
            _normalize_period(getattr(session, "period", "PRE")),
            session.room,
            session.block.day,
            session.block.start,
            session.block.end,
            getattr(session, "course_code", ""),
        )
    for session in combined_sessions or []:
        if isinstance(session, dict):
            room = session.get("room")
            period = _normalize_period(session.get("period", "PRE"))
            tb = session.get("time_block")
            if not room or not tb:
                continue
            if session is exclude_session and exclude_source == "combined":
                continue
            add_session(session, period, room, tb.day, tb.start, tb.end, session.get("course_code", ""))
    for session in elective_sessions or []:
        if isinstance(session, dict):
            room = session.get("room")
            period = _normalize_period(session.get("period", "PRE"))
            tb = session.get("time_block")
            if not room or not tb:
                continue
            if session is exclude_session and exclude_source == "elective":
                continue
            add_session(session, period, room, tb.day, tb.start, tb.end, session.get("course_code", ""))
        elif hasattr(session, "room") and session.room and getattr(session, "block", None):
            if session is exclude_session and exclude_source == "elective":
                continue
            add_session(
                session,
                _normalize_period(getattr(session, "period", "PRE")),
                session.room,
                session.block.day,
                session.block.start,
                session.block.end,
                getattr(session, "course_code", ""),
            )
    return room_occupancy


def _get_session_type_and_capacity(session, default_capacity: int = 60) -> Tuple[str, int]:
    """Infer session_type (L/T/P) and capacity from session. Returns (session_type, capacity_needed)."""
    st = "L"
    cap = default_capacity
    if isinstance(session, dict):
        st = session.get("session_type") or session.get("kind") or "L"
        cap = session.get("capacity") or default_capacity
    else:
        st = getattr(session, "kind", None) or getattr(session, "session_type", None) or "L"
        cap = getattr(session, "capacity", None) or default_capacity
    if st not in ("L", "T", "P"):
        st = "L"
    return st, cap


def _reassign_room(
    session,
    source: str,
    period: str,
    day: str,
    start: time,
    end: time,
    room_occupancy: Dict,
    classrooms: List[ClassRoom],
    capacity_needed: int = 60,
    session_type: str = "L",
) -> Optional[str]:
    """Find another free room at same time and return room number; otherwise None."""
    period_norm = _normalize_period(period)
    time_block = TimeBlock(day, start, end)
    new_room = find_available_classroom(
        capacity_needed, session_type, period_norm, day, time_block, room_occupancy, classrooms
    )
    return new_room


def _find_available_240_seater(
    period: str,
    day: str,
    start: time,
    end: time,
    room_occupancy: Dict,
    classrooms: List[ClassRoom],
) -> Optional[str]:
    """Last resort: find an available 240-seater at (period, day, time). For non-combined sessions only."""
    period_norm = _normalize_period(period)
    large_rooms = [
        r for r in classrooms
        if r.room_type.lower() != "lab"
        and "lab" not in r.room_type.lower()
        and r.capacity >= 240
    ]
    for room in large_rooms:
        if not check_room_conflict(room.room_number, period_norm, day, start, end, room_occupancy):
            return room.room_number
    return None


def _find_any_available_room(
    period: str,
    day: str,
    start: time,
    end: time,
    room_occupancy: Dict,
    classrooms: List[ClassRoom],
) -> Optional[str]:
    """Last resort: any non-lab room free at (period, day, time)."""
    period_norm = _normalize_period(period)
    time_block = TimeBlock(day, start, end)
    return find_available_classroom(1, "L", period_norm, day, time_block, room_occupancy, classrooms)


def _update_session_room(session, source: str, new_room: str, elective_sessions: List = None):
    if isinstance(session, dict):
        session["room"] = new_room
        if "_assignment" in session:
            session["_assignment"]["room"] = new_room
        if elective_sessions and source == "elective":
            assign_ref = session.get("_assignment")
            for s in elective_sessions:
                if isinstance(s, dict) and s.get("_assignment") is assign_ref:
                    s["room"] = new_room
    else:
        session.room = new_room


def _update_session_time(session, source: str, new_block: TimeBlock, elective_sessions: List = None):
    """Update session's time block to new_block (for reschedule)."""
    if isinstance(session, dict):
        session["time_block"] = new_block
    else:
        session.block = new_block


def _try_reschedule(
    session_to_move,
    source_to_move: str,
    period: str,
    room_occupancy: Dict,
    phase5_sessions: List,
    phase7_sessions: List,
    combined_sessions: List,
    elective_sessions: List,
    classrooms: List[ClassRoom],
    capacity_needed: int,
    session_type: str,
    max_slot_attempts: int = 60,
) -> bool:
    """Try to move the session to another time slot in the same period where a room is free. Returns True if moved."""
    period_norm = _normalize_period(period)
    section = None
    semester = 1
    course_code = ""
    if isinstance(session_to_move, dict):
        course_code = session_to_move.get("course_code", "")
        section = session_to_move.get("section", "")
        if isinstance(section, list):
            section = section[0] if section else ""
        # Elective section is e.g. "ELECTIVE_BASKET_5.1" -> semester 5
        if section and "ELECTIVE_BASKET_" in str(section):
            try:
                part = str(section).replace("ELECTIVE_BASKET_", "").strip()
                if "." in part:
                    semester = int(part.split(".")[0])
                else:
                    semester = int(part)
            except (ValueError, IndexError):
                semester = 1
        tb = session_to_move.get("time_block")
        if tb:
            pass
    else:
        course_code = getattr(session_to_move, "course_code", "")
        section = getattr(session_to_move, "section", "")
        if section and "Sem" in str(section):
            try:
                semester = int(str(section).split("Sem")[1].split("-")[0])
            except (ValueError, IndexError):
                semester = 1
    if not section:
        return False
    try:
        from modules_v2.phase5_core_courses import get_available_time_slots, get_lunch_blocks
    except ImportError:
        return False
    occupied_slots = defaultdict(list)
    for s in phase5_sessions + phase7_sessions:
        if hasattr(s, "section") and hasattr(s, "block"):
            key = f"{s.section}_{_normalize_period(getattr(s, 'period', 'PRE'))}"
            occupied_slots[key].append((s.block, getattr(s, "course_code", "")))
    for s in combined_sessions or []:
        if isinstance(s, dict):
            blocks = s.get("sections", [])
            period_s = _normalize_period(s.get("period", ""))
            tb = s.get("time_block")
            if tb and blocks:
                for sec in blocks:
                    occupied_slots[f"{sec}_{period_s}"].append((tb, s.get("course_code", "")))
    for s in elective_sessions or []:
        if isinstance(s, dict) and s.get("time_block"):
            sec = s.get("section", "")
            period_s = _normalize_period(s.get("period", ""))
            occupied_slots[f"{sec}_{period_s}"].append((s["time_block"], s.get("course_code", "")))
    section_key = f"{section}_{period_norm}"
    available_slots = get_available_time_slots(semester, occupied_slots, course_code, section, period_norm)
    lunch_blocks_dict = get_lunch_blocks()
    lunch_base = lunch_blocks_dict.get(semester)
    lunch_blocks = []
    if lunch_base:
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            lunch_blocks.append(TimeBlock(day, lunch_base.start, lunch_base.end))
    for slot in available_slots[:max_slot_attempts]:
        if not slot or not hasattr(slot, "day"):
            continue
        lunch_ok = True
        for lb in lunch_blocks:
            if slot.day == lb.day and slot.overlaps(lb):
                lunch_ok = False
                break
        if not lunch_ok:
            continue
        occ = _build_room_occupancy_excluding(
            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
            session_to_move, source_to_move, classrooms,
        )
        new_room = find_available_classroom(
            capacity_needed, session_type, period_norm, slot.day, slot, occ, classrooms
        )
        if new_room:
            _update_session_time(session_to_move, source_to_move, slot, elective_sessions)
            _update_session_room(session_to_move, source_to_move, new_room, elective_sessions)
            occupied_slots[section_key].append((slot, course_code))
            return True
    return False


def resolve_room_conflicts(
    phase5_sessions: List,
    phase7_sessions: List,
    combined_sessions: List,
    elective_sessions: List,
    classrooms: List[ClassRoom],
    max_passes: int = 5,
) -> Tuple[int, List[Dict]]:
    """
    Resolve room conflicts by reassigning room (prefer) or rescheduling.
    Move priority: elective > phase7 > phase5 > combined.

    Returns:
        (number_resolved, remaining_conflicts)
    """
    print("\n=== ROOM CONFLICT RESOLUTION ===")
    resolved_count = 0

    # Assign rooms to electives that have no room (Phase 9 may leave None)
    seen_assignments = set()
    for s in elective_sessions or []:
        if not isinstance(s, dict) or not s.get("_assignment"):
            continue
        room_val = s.get("room")
        if room_val is not None and room_val != "":
            continue
        assign_id = id(s["_assignment"])
        if assign_id in seen_assignments:
            continue
        seen_assignments.add(assign_id)
        # Get all slots for this elective (sessions sharing _assignment)
        slots_for_assignment = [x for x in (elective_sessions or []) if isinstance(x, dict) and x.get("_assignment") is s["_assignment"] and x.get("time_block")]
        if not slots_for_assignment:
            continue
        period_norm = _normalize_period(s.get("period", "PRE"))
        room_occupancy = _build_room_occupancy_excluding(
            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
            slots_for_assignment[0], "elective", classrooms,
        )
        session_type, capacity_needed = _get_session_type_and_capacity(slots_for_assignment[0])
        new_room = None
        for room in classrooms:
            if room.room_type.lower() == "lab" or "lab" in room.room_type.lower() or room.capacity >= 240:
                continue
            if room.capacity < capacity_needed:
                continue
            free = True
            for slot_s in slots_for_assignment:
                tb = slot_s.get("time_block")
                if not tb:
                    continue
                if check_room_conflict(room.room_number, period_norm, tb.day, tb.start, tb.end, room_occupancy):
                    free = False
                    break
            if free:
                new_room = room.room_number
                break
        if new_room:
            _update_session_room(slots_for_assignment[0], "elective", new_room, elective_sessions)
            resolved_count += 1
            cc = slots_for_assignment[0].get("course_code", "")
            print(f"    Assigned room {new_room} to unassigned elective {cc}")

    for pass_num in range(max_passes):
        conflicts = detect_room_conflicts(
            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions, classrooms
        )
        if not conflicts:
            if resolved_count > 0:
                print(f"  All room conflicts resolved ({resolved_count} total).")
            else:
                print("  No room conflicts to resolve.")
            return resolved_count, []

        print(f"  Pass {pass_num + 1}: {len(conflicts)} conflict(s) to resolve.")
        resolved_this_pass = 0
        max_per_pass = len(conflicts)
        for idx in range(max_per_pass):
            if idx >= len(conflicts):
                break
            conflict = conflicts[idx]
            room = conflict["room"]
            period = conflict["period"]
            day = conflict["day"]
            time_str = conflict["time"]
            start, end = _parse_time_range(time_str)
            if start is None or end is None:
                print(f"    [skip] invalid time: {time_str}")
                continue
            session1, source1 = _find_session_in_lists(
                phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                conflict["course1"], conflict["section1"], period, day, start, end,
            )
            session2, source2 = _find_session_in_lists(
                phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                conflict["course2"], conflict["section2"], period, day, start, end,
            )
            if not session1 or not session2:
                print(f"    [skip] session not found: {conflict.get('course1')}/{conflict.get('section1')} vs {conflict.get('course2')}/{conflict.get('section2')} @ {room} {day} {time_str}")
                continue
            if _move_priority(source1) > _move_priority(source2):
                session_to_move, source_to_move = session1, source1
            else:
                session_to_move, source_to_move = session2, source2

            session_type, capacity_needed = _get_session_type_and_capacity(session_to_move)
            room_occupancy = _build_room_occupancy_excluding(
                phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                session_to_move, source_to_move, classrooms,
            )
            new_room = _reassign_room(
                session_to_move, source_to_move, period, day, start, end,
                room_occupancy, classrooms, capacity_needed=capacity_needed, session_type=session_type,
            )
            if new_room:
                _update_session_room(session_to_move, source_to_move, new_room, elective_sessions)
                resolved_count += 1
                resolved_this_pass += 1
                course = getattr(session_to_move, "course_code", None) or (session_to_move.get("course_code") if isinstance(session_to_move, dict) else None)
                print(f"    Reassigned {course} to room {new_room} (was {room})")
            else:
                rescheduled = _try_reschedule(
                    session_to_move, source_to_move, period, room_occupancy,
                    phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                    classrooms, capacity_needed, session_type, max_slot_attempts=80,
                )
                if not rescheduled:
                    # Try other period (elective, phase5, phase7 - not combined which is period-bound)
                    if source_to_move != "combined":
                        other_period = "POST" if _normalize_period(period) == "PRE" else "PRE"
                        rescheduled = _try_reschedule(
                            session_to_move, source_to_move, other_period, room_occupancy,
                            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                            classrooms, capacity_needed, session_type, max_slot_attempts=80,
                        )
                    if rescheduled:
                        if isinstance(session_to_move, dict):
                            session_to_move["period"] = other_period
                            if "_assignment" in session_to_move:
                                session_to_move["_assignment"]["period"] = other_period
                                for s in elective_sessions or []:
                                    if isinstance(s, dict) and s.get("_assignment") is session_to_move.get("_assignment"):
                                        s["period"] = other_period
                        else:
                            setattr(session_to_move, "period", other_period)
                if rescheduled:
                    resolved_count += 1
                    resolved_this_pass += 1
                    course = getattr(session_to_move, "course_code", None) or (session_to_move.get("course_code") if isinstance(session_to_move, dict) else None)
                    print(f"    Rescheduled {course} to new slot and assigned room")
                else:
                    # Last resort: 240-seater, then any free classroom
                    fallback_room = _find_available_240_seater(period, day, start, end, room_occupancy, classrooms)
                    if not fallback_room:
                        fallback_room = _find_any_available_room(period, day, start, end, room_occupancy, classrooms)
                    if fallback_room:
                        _update_session_room(session_to_move, source_to_move, fallback_room, elective_sessions)
                        resolved_count += 1
                        resolved_this_pass += 1
                        course = getattr(session_to_move, "course_code", None) or (session_to_move.get("course_code") if isinstance(session_to_move, dict) else None)
                        print(f"    Assigned 240-seater {fallback_room} for {course} (last resort)")
                    else:
                        # Try moving the other session in the conflict
                        other_session, other_source = (session2, source2) if (session_to_move is session1) else (session1, source1)
                        other_type, other_cap = _get_session_type_and_capacity(other_session)
                        other_occupancy = _build_room_occupancy_excluding(
                            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions,
                            other_session, other_source, classrooms,
                        )
                        other_room = _reassign_room(other_session, other_source, period, day, start, end, other_occupancy, classrooms, capacity_needed=other_cap, session_type=other_type)
                        if other_room:
                            _update_session_room(other_session, other_source, other_room, elective_sessions)
                            resolved_count += 1
                            resolved_this_pass += 1
                            course = getattr(other_session, "course_code", None) or (other_session.get("course_code") if isinstance(other_session, dict) else None)
                            print(f"    Reassigned other {course} to room {other_room} (was {room})")
                        else:
                            other_resched = _try_reschedule(other_session, other_source, period, other_occupancy, phase5_sessions, phase7_sessions, combined_sessions, elective_sessions, classrooms, other_cap, other_type, max_slot_attempts=80)
                            if not other_resched and other_source == "elective":
                                other_period = "POST" if _normalize_period(period) == "PRE" else "PRE"
                                other_resched = _try_reschedule(other_session, other_source, other_period, other_occupancy, phase5_sessions, phase7_sessions, combined_sessions, elective_sessions, classrooms, other_cap, other_type, max_slot_attempts=80)
                                if other_resched and isinstance(other_session, dict) and "_assignment" in other_session:
                                    other_session["period"] = other_period
                                    other_session["_assignment"]["period"] = other_period
                                    for s in elective_sessions or []:
                                        if isinstance(s, dict) and s.get("_assignment") is other_session.get("_assignment"):
                                            s["period"] = other_period
                            if other_resched:
                                resolved_count += 1
                                resolved_this_pass += 1
                                course = getattr(other_session, "course_code", None) or (other_session.get("course_code") if isinstance(other_session, dict) else None)
                                print(f"    Rescheduled other {course} to new slot")
                            else:
                                fallback_room = _find_available_240_seater(period, day, start, end, other_occupancy, classrooms)
                                if not fallback_room:
                                    fallback_room = _find_any_available_room(period, day, start, end, other_occupancy, classrooms)
                                if fallback_room:
                                    _update_session_room(other_session, other_source, fallback_room, elective_sessions)
                                    resolved_count += 1
                                    resolved_this_pass += 1
                                    course = getattr(other_session, "course_code", None) or (other_session.get("course_code") if isinstance(other_session, dict) else None)
                                    print(f"    Assigned 240-seater {fallback_room} for other {course} (last resort)")
                                else:
                                    c1 = conflict.get("course1", "")
                                    c2 = conflict.get("course2", "")
                                    print(f"    [no room and no slot] could not resolve conflict {c1} vs {c2} @ {room} {day} {time_str}")
        # Re-detect once per pass instead of after every resolution (fixes severe slowdown)
        conflicts = detect_room_conflicts(
            phase5_sessions, phase7_sessions, combined_sessions, elective_sessions, classrooms
        )

        if resolved_this_pass == 0:
            print(f"  No further progress in pass {pass_num + 1}; stopping.")
            break

    remaining = detect_room_conflicts(
        phase5_sessions, phase7_sessions, combined_sessions, elective_sessions, classrooms
    )
    if remaining:
        print(f"  WARNING: {len(remaining)} room conflict(s) remain after {max_passes} passes.")
    return resolved_count, remaining
