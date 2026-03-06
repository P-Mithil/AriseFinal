"""
Section-wise Time Conflict Verifier

Checks for overlapping sessions for each section and period across all
scheduled sessions from all phases.

Rule:
- For each (section_label, period), on each day, there must NOT be two
  sessions whose TimeBlocks overlap.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Tuple

from utils.data_models import TimeBlock


def _normalize_period(raw: Any) -> str:
    """Normalize various period representations to 'PRE' or 'POST'."""
    if raw is None:
        return "PRE"
    val = str(raw).strip().upper()
    if val in ("PRE", "PREMID"):
        return "PRE"
    if val in ("POST", "POSTMID"):
        return "POST"
    return val or "PRE"


def _normalize_section_label(section_value: Any) -> str:
    """Convert a section representation to a string label."""
    if section_value is None:
        return ""
    return str(section_value).strip()


def _extract_records_from_session(session: Any) -> List[Tuple[str, str, str, TimeBlock]]:
    """
    Convert a mixed-format session into a list of
    (section_label, period, course_code, TimeBlock) records.

    Supports:
    - Dict format from Phase 4 combined sessions (keys: course_code,
      sections or section, period, time_block or block)
    - ScheduledSession objects used elsewhere (attributes: course_code,
      section, period, block)
    """
    records: List[Tuple[str, str, str, TimeBlock]] = []

    # Dict-based combined or elective representations
    if isinstance(session, dict):
        code = session.get("course_code", "")
        code = str(code).split("-")[0] if isinstance(code, str) else str(code)

        period = _normalize_period(session.get("period", "PRE"))

        block = session.get("time_block") or session.get("block")
        if not isinstance(block, TimeBlock):
            return records

        sections = session.get("sections")
        if not sections and "section" in session:
            sections = [session.get("section")]
        if not sections:
            return records

        for sec in sections:
            section_label = _normalize_section_label(sec)
            if not section_label:
                continue
            records.append((section_label, period, code, block))

        return records

    # ScheduledSession-like objects
    if hasattr(session, "block") and hasattr(session, "section"):
        block = getattr(session, "block")
        if not isinstance(block, TimeBlock):
            return records

        code = getattr(session, "course_code", "")
        if isinstance(code, str):
            code = code.split("-")[0]
        else:
            code = str(code)

        period = _normalize_period(getattr(session, "period", "PRE"))
        section_label = _normalize_section_label(getattr(session, "section", ""))
        if not section_label:
            return records

        records.append((section_label, period, code, block))

    return records


def find_section_conflicts(all_sessions: List[Any]) -> Dict[str, Any]:
    """
    Detect overlapping sessions per section and period.

    Returns:
        {
          'conflicts': [ {section, period, day, course1, course2, time1, time2}, ... ],
          'by_section': { (section, period): [...] }
        }
    """
    # Map (section_label, period, day) -> list of (TimeBlock, course_code)
    by_key: Dict[Tuple[str, str, str], List[Tuple[TimeBlock, str]]] = {}

    for session in all_sessions or []:
        records = _extract_records_from_session(session)
        for section_label, period, code, block in records:
            day = block.day
            key = (section_label, period, day)
            by_key.setdefault(key, []).append((block, code))

    conflicts: List[Dict[str, Any]] = []

    # For each section+period+day, check for overlapping TimeBlocks
    for (section_label, period, day), items in by_key.items():
        # Sort by start time to make neighbor comparisons efficient
        items_sorted = sorted(items, key=lambda x: x[0].start)

        for i in range(len(items_sorted) - 1):
            block1, code1 = items_sorted[i]
            for j in range(i + 1, len(items_sorted)):
                block2, code2 = items_sorted[j]

                if not block1.overlaps(block2):
                    continue

                conflicts.append(
                    {
                        "section": section_label,
                        "period": period,
                        "day": day,
                        "course1": code1,
                        "course2": code2,
                        "time1": f"{block1.start.strftime('%H:%M')}-{block1.end.strftime('%H:%M')}",
                        "time2": f"{block2.start.strftime('%H:%M')}-{block2.end.strftime('%H:%M')}",
                    }
                )

    # Group conflicts by (section, period)
    by_section: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for c in conflicts:
        key = (c["section"], c["period"])
        by_section.setdefault(key, []).append(c)

    return {"conflicts": conflicts, "by_section": by_section}


def write_section_conflict_report(
    conflict_result: Dict[str, Any],
    base_dir: str = None,
) -> str:
    """
    Write a human-readable section time conflict report to DATA/OUTPUT.

    Returns:
        Absolute path to the created report file.
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    output_dir = os.path.join(base_dir, "DATA", "OUTPUT")
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_dir, f"section_time_conflicts_{timestamp}.txt")

    conflicts: List[Dict[str, Any]] = conflict_result.get("conflicts", []) or []

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("Section Time Conflict Report\n")
        f.write("=====================================\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        if not conflicts:
            f.write("Result: NO TIME CONFLICTS FOUND across all sections and periods.\n")
            return report_path

        f.write(f"Conflicts ({len(conflicts)}):\n\n")
        for idx, c in enumerate(conflicts, 1):
            f.write(
                f"{idx}) {c['section']} ({c['period']}) {c['day']} "
                f"{c['time1']} vs {c['time2']}\n"
            )
            f.write(f"   - {c['course1']} vs {c['course2']}\n")

    return report_path

