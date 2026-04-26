"""
Parse program-semester cohort markers from section labels (dynamic, data-driven).

Examples: "ECE-A-Sem3" -> 3, "CSE-B-Sem1" -> 1.
Used by verification when FACULTY_VERIFY_REQUIRE_SHARED_PROGRAM_SEMESTER is enabled.
"""
from __future__ import annotations

import re
from typing import Any, FrozenSet

_SEM_RE = re.compile(r"Sem\s*(\d+)", re.IGNORECASE)


def extract_program_semester_numbers_from_section_label(label: str) -> FrozenSet[int]:
    """Return all SemN integers found in one section string."""
    if not label:
        return frozenset()
    return frozenset(int(m.group(1)) for m in _SEM_RE.finditer(str(label)))


def program_semester_numbers_from_session_payload(s: Any) -> FrozenSet[int]:
    """
    Union of SemN integers across all section labels for a session.

    Supports dict sessions (key 'sections': list or str) and objects with .section
    (str or iterable of str).
    """
    if isinstance(s, dict):
        secs = s.get("sections") or []
        if isinstance(secs, str):
            secs = [secs]
    else:
        one = getattr(s, "section", None)
        if one is None:
            secs = []
        elif isinstance(one, (list, tuple)):
            secs = list(one)
        else:
            secs = [one]
    out: set[int] = set()
    for sec in secs:
        out.update(extract_program_semester_numbers_from_section_label(str(sec)))
    return frozenset(out)
