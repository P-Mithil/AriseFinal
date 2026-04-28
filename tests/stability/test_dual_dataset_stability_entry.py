import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class DualDatasetStabilityEntryTest(unittest.TestCase):
    def test_dual_dataset_one_run_entry(self):
        # Expensive by nature; run only when explicitly enabled.
        if os.environ.get("ARISE_RUN_STABILITY_ENTRY_TEST", "0").strip() != "1":
            self.skipTest("Set ARISE_RUN_STABILITY_ENTRY_TEST=1 to run stability entry test.")

        proc = subprocess.run(
            [
                sys.executable,
                "run_dual_dataset_strict.py",
                "--runs",
                "1",
                "--timeout-seconds",
                "600",
                "--seed-mode",
                "fixed",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, msg=output[-4000:])


if __name__ == "__main__":
    unittest.main()
