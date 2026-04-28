## Verification and Troubleshooting

### Standard verification flow

1. Generate and strict verify:
   - `python generate_and_verify.py`
2. API-style verify on latest log:
   - `python scripts/verify_latest_time_slot_log.py`
3. Strict stability odd/even:
   - `python run_dual_dataset_strict.py --runs 10 --timeout-seconds 600 --seed-mode fixed`

### Common issues

- **No `time_slot_log_*.csv` found**
  - Run generation first (`generate_and_verify.py`).
- **Interactive period prompt appears in automation**
  - Set `ARISE_NONINTERACTIVE=1` for non-interactive runs.
- **Strict verification fails for one dataset**
  - Check latest `DATA/DEBUG/strict_stability_*.json`.
  - Re-run one run with fixed seed for reproducibility:
    - `python run_dual_dataset_strict.py --runs 1 --timeout-seconds 600 --seed-mode fixed`
- **Too many local debug files in git status**
  - Generated outputs are intentionally ignored via `.gitignore`.
  - Keep source/config changes separate from run artifacts.

### Test entry points

- Smoke: `python -m unittest discover -s tests/smoke -p "test_*.py"`
- Regression: `python -m unittest discover -s tests/regression -p "test_*.py"`
- Stability entry (optional): `python -m unittest discover -s tests/stability -p "test_*.py"`
