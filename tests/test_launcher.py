"""launcher.py keeps the app running across deployments without looping on a broken build."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher


class SuperviseTests(unittest.TestCase):
    def run_with(self, codes, durations=None):
        codes = list(codes)
        calls = []
        clock = iter(range(0, 10_000, 100))  # every run "lasts" 100 s unless durations say otherwise

        def fake_run(args, cwd, env):
            calls.append(env.get("NEWS_SUPERVISED"))
            return mock.Mock(returncode=codes.pop(0))

        with mock.patch.object(launcher.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(launcher.time, "sleep"), \
                mock.patch.object(launcher.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(launcher, "update_dependencies") as deps:
            result = launcher.supervise_app(Path("python"), {"NEWS_SUPERVISED": "1"})
        return result, calls, deps

    def test_restarts_on_redeploy_and_stops_on_clean_exit(self):
        result, calls, deps = self.run_with([75, 75, 0])
        self.assertEqual((result, len(calls)), (0, 3))
        deps.assert_not_called()

    def test_reinstalls_dependencies_only_when_requirements_changed(self):
        digests = iter(["a", "b", "b", "b"])
        with mock.patch.object(launcher, "_file_digest", side_effect=lambda path: next(digests)):
            result, calls, deps = self.run_with([75, 0])
        deps.assert_called_once()

    def test_crash_after_running_a_while_is_restarted(self):
        result, calls, deps = self.run_with([1, 1, 0])
        self.assertEqual((result, len(calls)), (0, 3))


if __name__ == "__main__":
    unittest.main()
