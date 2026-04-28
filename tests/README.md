## Test Layout

This project keeps core behavior in existing scripts and adds a clean entry layer under `tests/`.

- `tests/smoke/`: fast checks (imports, command wiring, script contracts)
- `tests/regression/`: API/verification contract checks
- `tests/stability/`: heavier dual-dataset stability entry points

## Run Commands

- Fast smoke:
  - `python -m unittest discover -s tests/smoke -p "test_*.py"`
- Regression:
  - `python -m unittest discover -s tests/regression -p "test_*.py"`
- Stability entry tests:
  - `python -m unittest discover -s tests/stability -p "test_*.py"`

## Notes

- Tests in `tests/stability` are guarded to avoid expensive runs by default.
- Ground-truth generation/verification behavior still comes from:
  - `generate_and_verify.py`
  - `scripts/verify_latest_time_slot_log.py`
  - `run_dual_dataset_strict.py`
