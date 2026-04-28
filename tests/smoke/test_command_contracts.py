import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class CommandContractsSmokeTest(unittest.TestCase):
    def _run(self, args):
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_dual_dataset_runner_help(self):
        code, output = self._run(["run_dual_dataset_strict.py", "--help"])
        self.assertEqual(code, 0, msg=output)
        self.assertIn("Repeated strict generation stability runner", output)

    def test_cli_then_verify_help(self):
        code, output = self._run(["scripts/run_arise_cli_then_strict_verify.py", "--help"])
        self.assertEqual(code, 0, msg=output)
        self.assertIn("skip-arise", output)


if __name__ == "__main__":
    unittest.main()
