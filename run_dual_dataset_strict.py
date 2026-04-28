#!/usr/bin/env python3
"""
Run strict timetable generation repeatedly on odd/even datasets.
Default behavior executes 10 runs x 2 datasets with a hard runtime cap per run.
"""

import argparse
import json
import multiprocessing as mp
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List


def _reset_course_data_cache() -> None:
    """Reset phase1 dataset selection cache between runs."""
    try:
        import modules_v2.phase1_data_validation_v2 as p1

        p1._COURSE_DATA_SELECTION_CACHE = None
    except Exception:
        pass


def _group_errors(errors: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in errors or []:
        k = str(e.get("rule", "UNKNOWN") or "UNKNOWN")
        counts[k] = counts.get(k, 0) + 1
    return counts


def _worker_generate(queue: mp.Queue, variant: str, seed: int, timeout_s: int) -> None:
    """Isolated child process so parent can hard-timeout the run."""
    started = datetime.now()
    try:
        os.environ["ARISE_COURSE_DATA_VARIANT"] = variant
        os.environ["ARISE_OFFERING"] = variant
        os.environ["ARISE_RUNTIME_MODE"] = "strict"
        os.environ["ARISE_GENERATION_SEED"] = str(seed)
        os.environ["ARISE_MAX_RUNTIME_SECONDS"] = str(timeout_s)
        _reset_course_data_cache()

        from generate_24_sheets import generate_24_sheets
        from utils.generation_verify_bridge import GenerationViolationError

        output_path, timestamp = generate_24_sheets()
        elapsed = (datetime.now() - started).total_seconds()
        queue.put(
            {
                "variant": variant,
                "seed": seed,
                "ok": True,
                "violations": 0,
                "output_path": output_path,
                "timestamp": timestamp,
                "debug_faculty_path": None,
                "elapsed_s": elapsed,
                "errors": [],
                "error_groups": {},
            }
        )
    except GenerationViolationError as gve:
        elapsed = (datetime.now() - started).total_seconds()
        errs = (gve.errors or [])
        queue.put(
            {
                "variant": variant,
                "seed": seed,
                "ok": False,
                "violations": len(errs),
                "output_path": None,
                "timestamp": None,
                "debug_faculty_path": getattr(gve, "debug_faculty_path", None),
                "elapsed_s": elapsed,
                "errors": errs,
                "error_groups": _group_errors(errs),
            }
        )
    except Exception as ex:
        elapsed = (datetime.now() - started).total_seconds()
        errs = [{"rule": "UNHANDLED_EXCEPTION", "message": str(ex)}]
        queue.put(
            {
                "variant": variant,
                "seed": seed,
                "ok": False,
                "violations": -1,
                "output_path": None,
                "timestamp": None,
                "debug_faculty_path": None,
                "elapsed_s": elapsed,
                "errors": errs,
                "error_groups": _group_errors(errs),
            }
        )


def _run_variant_once(variant: str, seed: int, timeout_s: int) -> Dict[str, Any]:
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(target=_worker_generate, args=(queue, variant, seed, timeout_s))
    started = datetime.now()
    proc.start()
    proc.join(timeout_s)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        elapsed = (datetime.now() - started).total_seconds()
        msg = f"Run timed out ({elapsed:.1f}s > {timeout_s}s)"
        return {
            "variant": variant,
            "seed": seed,
            "ok": False,
            "violations": -1,
            "output_path": None,
            "timestamp": None,
            "debug_faculty_path": None,
            "elapsed_s": elapsed,
            "errors": [{"rule": "RUNTIME_TIMEOUT", "message": msg}],
            "error_groups": {"RUNTIME_TIMEOUT": 1},
            "timed_out": True,
        }

    elapsed = (datetime.now() - started).total_seconds()
    if queue.empty():
        msg = "Child process exited without structured result."
        return {
            "variant": variant,
            "seed": seed,
            "ok": False,
            "violations": -1,
            "output_path": None,
            "timestamp": None,
            "debug_faculty_path": None,
            "elapsed_s": elapsed,
            "errors": [{"rule": "MISSING_RESULT", "message": msg}],
            "error_groups": {"MISSING_RESULT": 1},
            "timed_out": False,
        }

    result = queue.get()
    result["timed_out"] = False
    return result


def _print_summary(results: List[Dict[str, Any]], runs: int, timeout_s: int) -> None:
    print("\n" + "=" * 96)
    print(f"DUAL-DATASET STRICT STABILITY SUMMARY ({runs} runs, max {timeout_s}s each)")
    print("=" * 96)
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(
            f"[{status}] dataset={r['variant']} run={r['run_idx']} seed={r['seed']} "
            f"violations={r['violations']} elapsed={r['elapsed_s']:.1f}s"
        )
        if r["output_path"]:
            print(f"       output: {r['output_path']}")
        if r["debug_faculty_path"]:
            print(f"       debug_faculty: {r['debug_faculty_path']}")
        if r.get("error_groups"):
            print(f"       error_groups: {r['error_groups']}")
        if not r["ok"] and r["errors"]:
            for err in r["errors"][:8]:
                print(f"       - [{err.get('rule', '')}] {err.get('message', '')}")
            if len(r["errors"]) > 8:
                print(f"       ... and {len(r['errors']) - 8} more")

    by_variant = {"odd": {"pass": 0, "fail": 0}, "even": {"pass": 0, "fail": 0}}
    for r in results:
        k = r["variant"]
        if r["ok"]:
            by_variant[k]["pass"] += 1
        else:
            by_variant[k]["fail"] += 1
    print("\nAggregate:")
    for v in ("odd", "even"):
        print(
            f"  {v}: pass={by_variant[v]['pass']} fail={by_variant[v]['fail']} "
            f"(total={by_variant[v]['pass'] + by_variant[v]['fail']})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Repeated strict generation stability runner.")
    parser.add_argument("--runs", type=int, default=10, help="Runs per dataset (default: 10)")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="Hard per-run timeout seconds")
    parser.add_argument("--base-seed", type=int, default=1729, help="Base seed for deterministic sweep")
    parser.add_argument(
        "--seed-mode",
        type=str,
        choices=["fixed", "sweep"],
        default="fixed",
        help="fixed: same stable seed each run per dataset; sweep: vary seed per run",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default="",
        help="Optional path for JSON report (default under DATA/DEBUG).",
    )
    args = parser.parse_args()

    runs = max(1, int(args.runs))
    timeout_s = max(60, int(args.timeout_seconds))
    base_seed = int(args.base_seed)
    variants = ["odd", "even"]
    results: List[Dict[str, Any]] = []

    print("=" * 96)
    print(
        "RUNNING STRICT STABILITY CHECK FOR DATASET 1 (odd) AND DATASET 2 (even) "
        f"| runs={runs} | timeout={timeout_s}s"
    )
    print("=" * 96)

    for run_idx in range(1, runs + 1):
        for variant in variants:
            if args.seed_mode == "sweep":
                seed = base_seed + (run_idx * 100) + (0 if variant == "odd" else 1)
            else:
                seed = 1829 if variant == "odd" else 1830
            print(f"\n--- run {run_idx}/{runs} | dataset={variant} | seed={seed} ---")
            res = _run_variant_once(variant, seed=seed, timeout_s=timeout_s)
            res["run_idx"] = run_idx
            results.append(res)

    _print_summary(results, runs=runs, timeout_s=timeout_s)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.report_json:
        report_path = Path(args.report_json)
    else:
        report_path = Path("DATA/DEBUG") / f"strict_stability_{runs}runs_{ts}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now().isoformat(),
        "runs_per_dataset": runs,
        "timeout_seconds": timeout_s,
        "base_seed": base_seed,
        "results": results,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written: {report_path}")

    failed = [r for r in results if not r["ok"]]
    timed_out = [r for r in results if r.get("timed_out")]
    too_slow = [r for r in results if float(r.get("elapsed_s", 0.0)) > timeout_s]
    if failed or timed_out or too_slow:
        print("\n[RESULT] FAILED: Stability criterion not met for all runs.")
        return 1
    print("\n[RESULT] SUCCESS: All runs passed with zero violations under timeout.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
