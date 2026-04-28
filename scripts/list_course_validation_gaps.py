"""Print courses that fail basic Phase-1 style checks (instructor, LTPSC) from course_data.xlsx."""

from __future__ import annotations

import os
import sys

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from config.structure_config import DEPARTMENTS
from utils.data_models import parse_instructors


def main() -> int:
    path = os.path.join(REPO, "DATA", "INPUT", "course_data.xlsx")
    df = pd.read_excel(path)

    no_inst: list[tuple] = []
    bad_ltpsc: list[tuple] = []

    for _, row in df.iterrows():
        dept = str(row.get("Department", "")).strip()
        if dept not in DEPARTMENTS:
            continue
        code = row.get("Course Code")
        name = row.get("Course Name")
        sem = row.get("Semester")

        ins = parse_instructors(row.get("Instructor"))
        if not ins:
            no_inst.append((code, name, sem, dept, row.get("Instructor")))

        lt = row.get("LTPSC")
        if lt is None or (isinstance(lt, float) and pd.isna(lt)):
            bad_ltpsc.append((code, name, sem, dept, lt))
            continue
        s = str(lt).strip().lower()
        if s in ("", "nan", "none"):
            bad_ltpsc.append((code, name, sem, dept, lt))

    print("=== CSE / DSAI / ECE rows with NO instructor (empty Instructor cell) ===")
    if not no_inst:
        print("(none)")
    else:
        for t in no_inst:
            print(f"  {t[0]} | Sem {t[2]} | {t[3]} | Instructor cell: {repr(t[4])}")

    print()
    print("=== Same scope: missing or invalid LTPSC (blank / NaN) ===")
    if not bad_ltpsc:
        print("(none)")
    else:
        for t in bad_ltpsc:
            print(f"  {t[0]} | Sem {t[2]} | {t[3]} | LTPSC: {repr(t[4])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
