from typing import Dict, List, Iterator, Tuple
import json

"""
Structure configuration for branches, sections, and capacities.

Edit these values to change which departments / sections exist and how many
students each section has, without touching the phase logic.
"""

# Core departments considered in timetable generation
DEPARTMENTS: List[str] = ["CSE", "DSAI", "ECE","AIC"]

# Sections per department (labels only)
SECTIONS_BY_DEPT: Dict[str, List[str]] = {
    "CSE": ["A", "B"],
    "DSAI": ["A"],
    "ECE": ["A"],
    "AIC": ["A"],
}

# Group mapping per section (used for combined / group1 vs group2 logic)
# 1 = Group 1 (e.g. CSE-A/B), 2 = Group 2 (e.g. DSAI-A, ECE-A)
# This is keyed by department and section label so you can control grouping for
# each section explicitly (e.g., "CSE"-"A", "CSE"-"B", etc.).
SECTION_GROUPS: Dict[str, Dict[str, int]] = {
    "CSE": {"A": 1, "B": 1},
    "DSAI": {"A": 2},
    "ECE": {"A": 2},
    "AIC": {"A": 3},
}

# Default number of students per section
STUDENTS_PER_SECTION: int = 85


def iter_sections(semesters: List[int]) -> Iterator[Tuple[str, str, int, int, int]]:
    """
    Yield (department, section_label, group, semester, students_per_section)
    for all configured departments, sections, and semesters.
    """
    for dept in DEPARTMENTS:
        for sem in semesters:
            for sec_label in SECTIONS_BY_DEPT.get(dept, []):
                group = get_group_for_section(dept, sec_label)
                yield dept, sec_label, group, sem, STUDENTS_PER_SECTION


def get_group_for_section(dept: str, sec_label: str) -> int:
    """
    Return group id for a specific section (dept + label).

    Default: 1 if not configured.
    """
    return SECTION_GROUPS.get(dept, {}).get(sec_label, 1)


def get_group_for_department(dept: str) -> int:
    """
    Return a representative group id for a department.

    If the department has multiple sections with different groups, this returns
    the minimum group id found (usually 1).
    """
    groups = list((SECTION_GROUPS.get(dept) or {}).values())
    if not groups:
        return 1
    return min(groups)


def get_grouping_signature() -> str:
    """
    Return a stable signature for the current grouping + section structure.

    Used to invalidate persisted period assignments when section grouping changes.
    """
    payload = {
        "departments": list(DEPARTMENTS),
        "sections_by_dept": {k: list(v) for k, v in SECTIONS_BY_DEPT.items()},
        "section_groups": {k: dict(v) for k, v in SECTION_GROUPS.items()},
        "students_per_section": int(STUDENTS_PER_SECTION),
    }
    # Sort keys for stability across runs
    return json.dumps(payload, sort_keys=True)

