"""
Central faculty conflict resolver.
Handles rescheduling of faculty conflicts across all session types
while respecting rules: protect combined/elective baskets, prefer moving regular/core.
"""

import random
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from datetime import time

from utils.data_models import TimeBlock, ScheduledSession
from utils.faculty_conflict_utils import (
    check_faculty_availability_in_period,
    faculty_name_tokens,
    find_alternative_slot_for_faculty,
    get_session_move_priority,
    try_reassign_away_from_busy_instructor,
    _normalize_period,
)
from modules_v2.phase6_faculty_conflicts import detect_faculty_conflicts, FacultyConflict


def resolve_all_faculty_conflicts(
    all_sessions: List,
    classrooms: List,
    occupied_slots: Dict[str, List],
    max_passes: int = 10,
    rng: Optional[random.Random] = None,
) -> Tuple[List, List[FacultyConflict]]:
    """
    Resolve all faculty conflicts by rescheduling sessions.
    
    Rules:
    - Protect combined-class sessions and elective basket time slots
    - Prefer moving regular/core sessions first
    - Move within same period (PreMid/PostMid)
    - Never violate section overlaps or room constraints
    
    Args:
        all_sessions: All scheduled sessions (including elective, combined, core)
        classrooms: List of available classrooms
        occupied_slots: Dict mapping section_period -> list of (TimeBlock, course_code)
        max_passes: Maximum number of resolution passes
        
    Returns:
        Tuple of (resolved_all_sessions, remaining_conflicts)
    """
    print("\n=== CENTRAL FACULTY CONFLICT RESOLUTION ===")
    
    # Separate sessions by type for move priority
    regular_sessions = []  # Phase 5, 7 core courses
    elective_sessions = []  # Elective courses (not baskets)
    combined_sessions = []  # Phase 4 combined courses (dicts)
    elective_basket_sessions = []  # ELECTIVE_BASKET_* sessions
    
    for session in all_sessions:
        if isinstance(session, dict):
            combined_sessions.append(session)
        else:
            course_code = getattr(session, 'course_code', '')
            if course_code.startswith('ELECTIVE_BASKET_'):
                elective_basket_sessions.append(session)
            elif hasattr(session, 'faculty') or hasattr(session, 'instructor'):
                # Check if it's an elective course (not basket)
                # This is a heuristic - electives typically have different patterns
                # For now, treat all ScheduledSession objects as potentially movable
                regular_sessions.append(session)
            else:
                regular_sessions.append(session)
    
    consecutive_without_progress = 0
    for pass_num in range(max_passes):
        # Reset moved_sessions each pass so previously-tried sessions can be retried
        # after other sessions have moved (enabling cascading resolution)
        moved_sessions = set()
        
        # Detect current conflicts
        conflicts = detect_faculty_conflicts(all_sessions)
        
        if not conflicts:
            print(f"[OK] All faculty conflicts resolved after {pass_num} pass(es)!")
            return all_sessions, []
        
        print(f"\nResolution pass {pass_num + 1}: {len(conflicts)} conflicts detected")
        
        # Group conflicts by faculty and period for better handling
        conflicts_by_faculty_period = defaultdict(list)
        for conflict in conflicts:
            faculty = conflict.faculty_name
            # Extract period from time_slot string
            period = "PRE"
            if "(" in conflict.time_slot and ")" in conflict.time_slot:
                period_part = conflict.time_slot.split("(")[1].rstrip(")")
                period = _normalize_period(period_part)
            conflicts_by_faculty_period[(faculty, period)].append(conflict)
        
        conflicts_resolved_this_pass = 0

        # Optional shuffle explores different resolution orders (escape local minima)
        group_items = list(conflicts_by_faculty_period.items())
        if rng is not None:
            rng.shuffle(group_items)

        # Process conflicts, prioritizing by move cost
        for (faculty, period), conflict_list in group_items:
            conflict_iter = list(conflict_list)
            if rng is not None:
                rng.shuffle(conflict_iter)
            for conflict in conflict_iter:
                # Parse conflict time slot to get day, start, end
                time_slot_str = conflict.time_slot
                day = conflict.day
                
                try:
                    # Extract time from time_slot_str like "Monday 09:00:00-10:30:00 (PRE)"
                    # Format: "{day} {start}-{end} ({period})"
                    parts = time_slot_str.split()
                    if len(parts) < 2:
                        continue
                    
                    # Find the part with time range (contains "-" and ":")
                    time_part = None
                    for part in parts[1:]:  # Skip day (first part)
                        if "-" in part and ":" in part:
                            time_part = part
                            break
                    
                    if not time_part:
                        continue
                    
                    # Split time range
                    if "-" in time_part:
                        start_str, end_str = time_part.split("-", 1)
                        # Remove period suffix if present in end_str
                        end_str = end_str.split("(")[0].strip()
                        
                        # Parse times (handle both HH:MM:SS and HH:MM formats)
                        from datetime import datetime
                        try:
                            # Try HH:MM:SS first
                            start_time = datetime.strptime(start_str.strip(), "%H:%M:%S").time()
                            end_time = datetime.strptime(end_str.strip(), "%H:%M:%S").time()
                        except ValueError:
                            # Fallback to HH:MM
                            start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
                            end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
                    else:
                        continue
                except Exception as e:
                    print(f"  WARNING: Could not parse conflict time slot '{time_slot_str}': {e}")
                    continue
                
                # Find all sessions that conflict at this time
                conflicting_sessions = []
                for session in all_sessions:
                    if isinstance(session, dict):
                        session_faculty = session.get('instructor') or session.get('faculty')
                        session_period = _normalize_period(session.get('period', 'PRE'))
                        session_block = session.get('time_block')
                    else:
                        session_faculty = getattr(session, 'faculty', None) or getattr(session, 'instructor', None)
                        session_period = _normalize_period(getattr(session, 'period', 'PRE'))
                        session_block = getattr(session, 'block', None)
                    
                    ff = (faculty or "").strip().lower()
                    toks = set(faculty_name_tokens(str(session_faculty or "")))
                    if (
                        ff in toks
                        and session_period == period
                        and session_block
                        and session_block.day == day
                        and session_block.overlaps(TimeBlock(day, start_time, end_time))
                    ):
                        conflicting_sessions.append(session)
                
                if len(conflicting_sessions) < 2:
                    continue  # Not a real conflict or already resolved
                
                # Rank sessions by move priority (lower = easier to move)
                conflicting_sessions.sort(key=lambda s: get_session_move_priority(s))
                # Occasionally try the second-easiest first to escape local minima
                if rng is not None and len(conflicting_sessions) >= 2 and rng.random() < 0.4:
                    conflicting_sessions[0], conflicting_sessions[1] = (
                        conflicting_sessions[1],
                        conflicting_sessions[0],
                    )

                # Try to move the easiest-to-move session (first in sorted list)
                session_to_move = conflicting_sessions[0]
                
                # Skip if we've already tried to move this session
                session_id = id(session_to_move)
                if session_id in moved_sessions:
                    # Try the next one
                    if len(conflicting_sessions) > 1:
                        session_to_move = conflicting_sessions[1]
                        session_id = id(session_to_move)
                        if session_id in moved_sessions:
                            continue
                    else:
                        continue
                
                moved_sessions.add(session_id)
                
                # Extract session details for rescheduling
                if isinstance(session_to_move, dict):
                    course_code = session_to_move.get('course_code', '')
                    sections = session_to_move.get('sections', [])
                    section = sections[0] if sections else None
                    old_block = session_to_move.get('time_block')
                else:
                    course_code = getattr(session_to_move, 'course_code', '')
                    section = getattr(session_to_move, 'section', '')
                    old_block = getattr(session_to_move, 'block')
                
                if not section or not old_block:
                    continue
                
                print(f"  Attempting to resolve conflict for {faculty} on {day} {start_time}-{end_time} ({period})")
                print(f"    Moving: {course_code} ({section})")
                
                # Find alternative slot (aggressive search - try many slots)
                new_slot = find_alternative_slot_for_faculty(
                    session_to_move, all_sessions, occupied_slots, classrooms, period, max_attempts=220
                )
                
                if new_slot:
                    # Update the session
                    if isinstance(session_to_move, dict):
                        session_to_move['time_block'] = new_slot
                        # Update room if needed (simplified - could improve)
                    else:
                        session_to_move.block = new_slot
                        # Room assignment will be re-checked in Phase 8
                    
                    # Update occupied_slots (all sections for combined dict sessions)
                    if isinstance(session_to_move, dict):
                        section_list = session_to_move.get("sections") or []
                        if not section_list:
                            section_list = [section] if section else []
                    else:
                        section_list = [section] if section else []
                    for sec in section_list:
                        if not sec:
                            continue
                        sk = f"{sec}_{period}"
                        occupied_slots[sk] = [
                            (blk, c)
                            for blk, c in occupied_slots.get(sk, [])
                            if not (
                                blk.day == old_block.day
                                and blk.start == old_block.start
                                and blk.end == old_block.end
                                and c == course_code
                            )
                        ]
                        occupied_slots[sk].append((new_slot, course_code))
                    
                    conflicts_resolved_this_pass += 1
                    print(f"    SUCCESS: Moved to {new_slot.day} {new_slot.start}-{new_slot.end}")
                else:
                    reassigned = False
                    for cand in conflicting_sessions:
                        if try_reassign_away_from_busy_instructor(
                            cand, ff, all_sessions, period
                        ):
                            conflicts_resolved_this_pass += 1
                            cc = (
                                cand.get("course_code", "")
                                if isinstance(cand, dict)
                                else getattr(cand, "course_code", "")
                            )
                            print(
                                f"    SUCCESS: Reassigned co-instructor on {cc} "
                                f"(freed {ff} at same slot)"
                            )
                            reassigned = True
                            break
                    if not reassigned:
                        print(f"    WARNING: Could not find alternative slot for {course_code}")
        
        if conflicts_resolved_this_pass == 0:
            consecutive_without_progress += 1
            print(
                f"  No conflicts resolved in pass {pass_num + 1} "
                f"({consecutive_without_progress} consecutive) — continuing..."
            )
            if consecutive_without_progress >= 8:
                break
        else:
            consecutive_without_progress = 0
    
    # Final conflict check
    final_conflicts = detect_faculty_conflicts(all_sessions)
    
    if final_conflicts:
        print(f"\nWARNING: {len(final_conflicts)} conflicts remain after {max_passes} resolution passes")
        print("  These may require manual intervention or indicate scheduling constraints")
    else:
        print(f"\n[OK] All conflicts resolved!")
    
    return all_sessions, final_conflicts
