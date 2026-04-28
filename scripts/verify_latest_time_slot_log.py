#!/usr/bin/env python3
"""
Regression helper: run the same main-timetable verification as the API on the newest
time_slot_log_*.csv under DATA/OUTPUT (or pass a path as argv[1]).
Usage: py scripts/verify_latest_time_slot_log.py [optional_csv_path]
"""
from __future__ import annotations

import glob
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)


def main() -> int:
    if len(sys.argv) > 1:
        log_path = os.path.abspath(sys.argv[1])
    else:
        pattern = os.path.join(REPO_ROOT, "DATA", "OUTPUT", "time_slot_log_*.csv")
        matches = glob.glob(pattern)
        if not matches:
            print("No time_slot_log_*.csv found under DATA/OUTPUT")
            return 2
        log_path = max(matches, key=os.path.getmtime)

    if not os.path.isfile(log_path):
        print(f"Not a file: {log_path}")
        return 2

    from api.main import load_timetable_from_csv, run_verify

    timetable = load_timetable_from_csv(log_path)
    result = run_verify(timetable)
    ok = result.get("success")
    errors = result.get("errors") or []
    print(f"File: {log_path}")
    print(f"Sessions: {len(timetable)} | success={ok} | error_count={len(errors)}")
    for e in errors[:50]:
        print(f"  [{e.get('rule')}] {e.get('message')}")
    if len(errors) > 50:
        print(f"  ... and {len(errors) - 50} more")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
