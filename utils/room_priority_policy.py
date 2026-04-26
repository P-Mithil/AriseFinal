from typing import Iterable, List, Optional

from utils.data_models import ClassRoom


def _is_lab_room(room: ClassRoom) -> bool:
    rt = str(getattr(room, "room_type", "") or "").strip().lower()
    return rt == "lab" or "lab" in rt


def classroom_rooms(classrooms: Iterable[ClassRoom]) -> List[ClassRoom]:
    """Return non-lab, non-research, non-auditorium classrooms in deterministic order.
    C004 (240-seater auditorium) is strictly reserved for Phase 4 combined courses only.
    """
    out = [
        r
        for r in (classrooms or [])
        if not _is_lab_room(r)
        and not bool(getattr(r, "is_research_lab", False))
        and str(getattr(r, "room_number", "") or "").strip().upper() != "C004"
    ]
    out.sort(key=lambda r: (-int(getattr(r, "capacity", 0) or 0), str(getattr(r, "room_number", "") or "")))
    return out


def top_large_classrooms(classrooms: Iterable[ClassRoom], n: int = 2) -> List[ClassRoom]:
    """Pick top-n highest-capacity classrooms (deterministic tie-break by room_number)."""
    if n <= 0:
        return []
    return classroom_rooms(classrooms)[:n]


def should_prefer_top_large_rooms(capacity_needed: int, top_rooms: Iterable[ClassRoom]) -> bool:
    """
    Strength-based trigger:
    if required strength reaches the smaller room among top-N, prioritize top-N first.
    """
    rooms = list(top_rooms or [])
    if not rooms:
        return False
    min_top_capacity = min(int(getattr(r, "capacity", 0) or 0) for r in rooms)
    return int(capacity_needed or 0) >= min_top_capacity


def ordered_classroom_candidates(
    classrooms: Iterable[ClassRoom],
    capacity_needed: int,
    prefer_top_large: bool,
    top_rooms: Optional[Iterable[ClassRoom]] = None,
) -> List[ClassRoom]:
    """
    Deterministic candidate order:
    1) optionally top large rooms first,
    2) then best-fit classrooms by absolute capacity delta, then capacity, then room number.
    """
    all_cls = classroom_rooms(classrooms)
    top = list(top_rooms or [])
    top_ids = {str(getattr(r, "room_number", "") or "") for r in top}

    from config.schedule_config import LARGE_ROOM_CAPACITY_THRESHOLD
    large_threshold = int(LARGE_ROOM_CAPACITY_THRESHOLD or 240)
    
    cap_needed = int(capacity_needed or 0)
    requires_large_room = cap_needed >= large_threshold
    is_medium_large = cap_needed >= 105 and not requires_large_room

    def _best_fit_key(r: ClassRoom):
        cap = int(getattr(r, "capacity", 0) or 0)
        # Apply strict tiered penalties matching Phase 8 logic
        if requires_large_room:
            # For large needs, prefer larger rooms first (negative capacity)
            return (-cap, str(getattr(r, "room_number", "") or ""))
        elif is_medium_large:
            # Medium-large needs (120-239): 
            # 1. Best fit within 120-239
            # 2. Fallback to 240+ rooms (penalty)
            return (cap >= large_threshold, abs(cap - cap_needed), cap, str(getattr(r, "room_number", "") or ""))
        else:
            # Normal courses (< 105):
            # 1. Best fit small rooms (< 105)
            # 2. Best fit medium rooms (105-239)
            # 3. 240+ rooms as absolute last resort
            return (cap >= large_threshold, cap >= 105, abs(cap - cap_needed), cap, str(getattr(r, "room_number", "") or ""))

    rest = sorted(all_cls, key=_best_fit_key)
    if not prefer_top_large or not top:
        return rest

    top_sorted = sorted(top, key=lambda r: (-int(getattr(r, "capacity", 0) or 0), str(getattr(r, "room_number", "") or "")))
    merged = top_sorted + [r for r in rest if str(getattr(r, "room_number", "") or "") not in top_ids]
    return merged
