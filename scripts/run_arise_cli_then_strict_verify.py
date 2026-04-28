#!/usr/bin/env python3
"""
Run the existing ARISE CLI (`generate_and_verify.py`) and then run:
  1) API-style strict verification (deep_verification.run_verification_on_sessions)
  2) DeepVerification report (deep_verification.DeepVerification)

Writes a concise summary + JSON artifact under DATA/DEBUG/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_OUTPUT = REPO_ROOT / "DATA" / "OUTPUT"
DATA_DEBUG = REPO_ROOT / "DATA" / "DEBUG"

# Ensure repo root is importable (so `import api.main`, `import deep_verification`, etc. work)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _latest_time_slot_log_path() -> Optional[Path]:
    if not DATA_OUTPUT.exists():
        return None
    logs = sorted(DATA_OUTPUT.glob("time_slot_log_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _ts_from_log_name(p: Path) -> Optional[str]:
    m = re.search(r"time_slot_log_(\d{8}_\d{6})\.csv$", p.name)
    return m.group(1) if m else None


def _excel_for_ts(ts: str) -> Path:
    return DATA_OUTPUT / f"IIITDWD_24_Sheets_v2_{ts}.xlsx"


def _run_generate_and_verify() -> int:
    script = REPO_ROOT / "generate_and_verify.py"
    if not script.exists():
        raise FileNotFoundError(f"Missing: {script}")
    # Use the same interpreter running this script.
    env = os.environ.copy()
    # Avoid blocking on Phase 4/7 period prompts when driven by this script.
    env.setdefault("ARISE_NONINTERACTIVE", "1")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT), env=env)
    return int(proc.returncode)


def _load_api_sessions_from_csv(csv_path: Path) -> List[Dict[str, Any]]:
    """
    Load time_slot_log CSV into the API session schema used by api/main.py.
    This keeps strict-verify identical to what the frontend /api/verify uses.
    """
    import csv

    sessions: List[Dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sessions.append({
                "Phase": row.get("Phase", "") or row.get("phase", "") or "",
                "Course Code": row.get("Course Code", "") or row.get("course_code", "") or "",
                "Course Name": row.get("Course Name", "") or row.get("course_name", "") or "",
                "Section": row.get("Section", "") or row.get("section", "") or "",
                "Day": row.get("Day", "") or row.get("day", "") or "",
                "Start Time": row.get("Start Time", "") or row.get("start_time", "") or "",
                "End Time": row.get("End Time", "") or row.get("end_time", "") or "",
                "Room": row.get("Room", "") or row.get("room", "") or "",
                "Faculty": row.get("Faculty", "") or row.get("faculty", "") or "",
                "Session Type": row.get("Session Type", "") or row.get("session_type", "") or "L",
                "Period": row.get("Period", "") or row.get("period", "") or "PRE",
            })
    return sessions


def _group_error_summary(errors: List[Dict[str, Any]], examples_per_rule: int = 8) -> str:
    by_rule: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in errors:
        by_rule[str(e.get("rule", "") or "Unknown")].append(e)

    lines: List[str] = []
    counts = Counter({rule: len(es) for rule, es in by_rule.items()})
    for rule, cnt in counts.most_common():
        lines.append(f"- {rule}: {cnt}")
        for ex in by_rule[rule][: max(0, int(examples_per_rule))]:
            msg = (ex.get("message") or "").strip()
            code = (ex.get("course_code") or "").strip()
            sec = (ex.get("section") or "").strip()
            day = (ex.get("day") or "").strip()
            time = (ex.get("time") or "").strip()
            where = " ".join([v for v in [code, sec, day, time] if v])
            if where:
                lines.append(f"  - {msg} [{where}]")
            else:
                lines.append(f"  - {msg}")
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-arise", action="store_true", help="Do not run generate_and_verify.py; just verify latest log.")
    ap.add_argument("--log", type=str, default="", help="Explicit time_slot_log_*.csv path to verify.")
    ap.add_argument("--examples-per-rule", type=int, default=8, help="How many example messages to show per rule.")
    args = ap.parse_args(argv)

    if not args.skip_arise:
        print("Running ARISE CLI (generate_and_verify.py)...")
        rc = _run_generate_and_verify()
        print(f"ARISE CLI exit code: {rc}")

    log_path = Path(args.log).resolve() if args.log else _latest_time_slot_log_path()
    if not log_path or not log_path.exists():
        print("ERROR: No time_slot_log CSV found to verify.")
        return 2

    ts = _ts_from_log_name(log_path) or datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = DATA_DEBUG / f"strict_verify_{ts}"
    debug_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nUsing log: {log_path}")
    print(f"Debug out: {debug_dir}")

    # 1) API-style strict verification (same core rules as /api/verify)
    sessions_api = _load_api_sessions_from_csv(log_path)
    from api.main import run_verify

    print("\nRunning strict verification (API-style)...")
    verify_res = run_verify(sessions_api)
    ok = bool(verify_res.get("success"))
    errors = list(verify_res.get("errors") or [])

    (debug_dir / "strict_verify_result.json").write_text(json.dumps(verify_res, indent=2), encoding="utf-8")
    (debug_dir / "strict_verify_errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")

    summary = _group_error_summary(errors, examples_per_rule=args.examples_per_rule)
    (debug_dir / "strict_verify_summary.txt").write_text(summary, encoding="utf-8")

    if ok:
        print("[OK] Strict verification passed (0 violations).")
    else:
        print(f"[FAIL] Strict verification failed: {len(errors)} violation(s).")
        print(summary)

    # 2) DeepVerification report on matching Excel (if present)
    excel_path = _excel_for_ts(ts)
    if excel_path.exists():
        print(f"\nRunning DeepVerification on: {excel_path}")
        from deep_verification import DeepVerification

        dv = DeepVerification()
        dv_res = dv.run_deep_verification(str(excel_path))
        (debug_dir / "deep_verification_result.json").write_text(json.dumps(dv_res, indent=2), encoding="utf-8")
    else:
        print(f"\n[SKIP] Matching Excel not found for ts={ts}: {excel_path}")

    print("\nDone.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

