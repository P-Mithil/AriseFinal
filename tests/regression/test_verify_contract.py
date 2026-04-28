import unittest


class VerifyContractRegressionTest(unittest.TestCase):
    def test_verify_latest_log_script_imports(self):
        # Contract check: this module must remain importable for local regression runs.
        import scripts.verify_latest_time_slot_log as mod  # noqa: F401
        self.assertTrue(hasattr(mod, "main"))

    def test_api_verify_entry_imports(self):
        # Contract check: API verification entrypoint should remain available.
        from api.main import run_verify  # noqa: F401
        self.assertTrue(callable(run_verify))


if __name__ == "__main__":
    unittest.main()
