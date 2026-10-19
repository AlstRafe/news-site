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


class LogonStartTests(unittest.TestCase):
    def test_task_definition_starts_the_reader_hidden_at_logon(self):
        tree = launcher.logon_task_xml(Path("C:/Py/pythonw.exe"), '"C:/app/launcher.py" --background', "PC\\user")
        xml = launcher.ElementTree.tostring(tree.getroot(), encoding="unicode")
        for part in ("LogonTrigger", "<ns0:UserId>PC\\user</ns0:UserId>" if "ns0:" in xml else "PC\\user",
                     "pythonw.exe", "--background", "<", "PT0S", "IgnoreNew"):
            self.assertIn(part, xml)

    def test_background_start_does_nothing_when_the_reader_already_runs(self):
        with mock.patch.object(launcher, "background_port_taken", return_value=True), \
                mock.patch.object(launcher, "launch_app") as launch:
            self.assertEqual(launcher.main(["--background"]), 0)
        launch.assert_not_called()

    def test_background_start_opens_no_browser(self):
        with mock.patch.object(launcher, "background_port_taken", return_value=False), \
                mock.patch.object(launcher, "find_runtime_python", return_value=Path("python")), \
                mock.patch.object(launcher, "launch_app", return_value=0) as launch:
            self.assertEqual(launcher.main(["--background"]), 0)
        self.assertTrue(launch.call_args.kwargs["background"])

    def test_no_task_outside_windows(self):
        if launcher.os.name == "nt":
            self.skipTest("Windows")
        self.assertFalse(launcher.ensure_logon_task(Path("python"), True))


if __name__ == "__main__":
    unittest.main()


class StartAtLogonSettingTests(unittest.TestCase):
    def test_default_on_and_saved_off(self):
        import tempfile
        from newsreader.services import settings
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(settings, "SETTINGS_PATH", Path(tmp) / "settings.json"):
            self.assertTrue(settings.start_at_logon())
            settings.save_start_at_logon(False)
            self.assertFalse(settings.start_at_logon())
            self.assertIs(launcher.json.loads((Path(tmp) / "settings.json").read_text(encoding="utf-8"))["start_at_logon"], False)
            settings.save_start_at_logon(True)
            self.assertTrue(settings.start_at_logon())
