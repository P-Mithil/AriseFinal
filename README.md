# ARISE Timetable System v2

Smart timetable generation, strict verification, API/UI integration, and stability testing for IIIT Dharwad workflows.

## Why This Project

ARISE generates timetables and validates them against hard academic constraints so outputs are usable in real operations, not just visually correct spreadsheets.

Core goals:
- Zero hard conflicts and violations in final outputs
- Stable behavior across odd/even datasets
- Strong API + UI editing and verification flow
- Maintainable handover-ready project structure

## Highlights

- **Phase-based scheduler** in `modules_v2/`
- **Strict verification engine** in `deep_verification.py`
- **End-to-end generator** in `generate_24_sheets.py`
- **API backend** in `api/main.py`
- **Professional docs and runbooks** under `docs/`
- **Test entry layers** under `tests/`
- **Stability runner** via `run_dual_dataset_strict.py`

## Repository Map

- `api/` - FastAPI endpoints for generation/verification
- `modules_v2/` - phase-wise scheduling logic
- `utils/` - shared models, validators, writers, resolvers
- `config/` - schedule/structure configuration
- `DATA/INPUT/` - input datasets and assignment files
- `DATA/OUTPUT/` - generated timetable outputs
- `DATA/DEBUG/` - verification/stability debug reports
- `tests/` - smoke, regression, stability entry tests
- `docs/` - manual + runbooks
- `artifacts/archive/` - archived logs/exports/text artifacts

## Quick Start

### 1) Install dependencies

- Backend:
  - `pip install -r requirements.txt`
  - `pip install -r api/requirements.txt`
- Frontend:
  - `cd frontend`
  - `npm install`

### 2) Run API

- From repo root:
  - `python api/main.py`

### 3) Generate + strict verify (CLI)

- `python generate_and_verify.py`

### 4) Verify latest time-slot log

- `python scripts/verify_latest_time_slot_log.py`

## Stability Validation

Smoke check:
- `python run_dual_dataset_strict.py --runs 1 --timeout-seconds 600 --seed-mode fixed`

Full certification run:
- `python run_dual_dataset_strict.py --runs 10 --timeout-seconds 600 --seed-mode fixed`

## Documentation

- Full manual: `docs/PROJECT_MANUAL.md`
- Quick setup: `docs/runbooks/quick_start.md`
- Verification + troubleshooting: `docs/runbooks/verification_and_troubleshooting.md`

## Testing

- Test guide: `tests/README.md`
- Smoke tests: `tests/smoke/`
- Regression tests: `tests/regression/`
- Stability entry tests: `tests/stability/`
- Legacy moved tests: `tests/legacy/`

## Notes for Contributors

- Keep runtime logic config-driven using files in `config/`
- Avoid hardcoded branch/room assumptions in non-config modules
- Treat strict verification as the release gate
- Keep generated noise in `artifacts/archive/` or ignored paths

---

If you are onboarding this project, start with `docs/PROJECT_MANUAL.md` first, then follow `docs/runbooks/quick_start.md`.
