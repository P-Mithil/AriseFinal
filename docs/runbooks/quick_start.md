## Quick Start (Handover)

### 1) Environment

- Python: use the same version used for current project runs.
- Install backend deps:
  - `pip install -r requirements.txt`
  - `pip install -r api/requirements.txt`
- Frontend:
  - `cd frontend`
  - `npm install`

### 2) Run API

- From repo root:
  - `python api/main.py`

### 3) Generate + Verify (CLI ground truth)

- `python generate_and_verify.py`

This runs generation and strict verification with current project defaults.

### 4) Verify latest exported timetable log

- `python scripts/verify_latest_time_slot_log.py`

### 5) Odd/Even strict stability

- Smoke:
  - `python run_dual_dataset_strict.py --runs 1 --timeout-seconds 600 --seed-mode fixed`
- Full handover check:
  - `python run_dual_dataset_strict.py --runs 10 --timeout-seconds 600 --seed-mode fixed`
