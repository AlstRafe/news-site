"""launcher.py keeps the app running across deployments without looping on a broken build."""
import os
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
                mock.patch.object(launcher, "open_background_log", return_value=sys.stdout), \
                mock.patch.object(launcher, "launch_app") as launch:
            self.assertEqual(launcher.main(["--background"]), 0)
        launch.assert_not_called()

    def test_background_start_opens_no_browser(self):
        with mock.patch.object(launcher, "background_port_taken", return_value=False), \
                mock.patch.object(launcher, "open_background_log", return_value=sys.stdout), \
                mock.patch.object(launcher, "wait_for_network"), \
                mock.patch.object(launcher, "find_runtime_python", return_value=Path("python")), \
                mock.patch.object(launcher, "launch_app", return_value=0) as launch:
            self.assertEqual(launcher.main(["--background"]), 0)
        self.assertTrue(launch.call_args.kwargs["background"])

    def test_a_running_reader_is_found_by_connecting_not_by_binding(self):
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with mock.patch.dict(os.environ, {"NEWS_PORT": str(port), "NEWS_BIND_HOST": ""}), \
                    mock.patch.object(launcher, "detect_bind_host", return_value="100.70.63.91"):
                self.assertTrue(launcher.background_port_taken({}))
        with mock.patch.dict(os.environ, {"NEWS_PORT": str(port), "NEWS_BIND_HOST": ""}), \
                mock.patch.object(launcher, "detect_bind_host", return_value="100.70.63.91"):
            # the Tailscale address is not up yet: that is not "already running"
            self.assertFalse(launcher.background_port_taken({}))

    def test_logon_start_waits_for_the_network(self):
        detected = iter(["127.0.0.1", "127.0.0.1", "192.168.3.12"])
        with mock.patch.dict(os.environ, {"NEWS_BIND_HOST": ""}), \
                mock.patch.object(launcher, "detect_bind_host", side_effect=lambda: next(detected)), \
                mock.patch.object(launcher, "_address_assigned", return_value=True), \
                mock.patch.object(launcher.time, "sleep") as sleep:
            launcher.wait_for_network({}, limit=300)
        self.assertEqual(sleep.call_count, 2)
        with mock.patch.dict(os.environ, {"NEWS_BIND_HOST": ""}), \
                mock.patch.object(launcher, "detect_bind_host", return_value="100.70.63.91"), \
                mock.patch.object(launcher, "_address_assigned", return_value=False), \
                mock.patch.object(launcher.time, "sleep"), \
                mock.patch.object(launcher.time, "monotonic", side_effect=[0, 10, 400]):
            launcher.wait_for_network({}, limit=300)  # gives up in the end and starts anyway

    def test_old_shared_task_of_this_copy_is_replaced_by_a_per_copy_task(self):
        import json, os, tempfile, types
        tmp = Path(tempfile.mkdtemp())
        (tmp / "pythonw.exe").write_text("")
        record = tmp / "server-task.json"
        record.write_text(json.dumps({"task_name": launcher.LEGACY_LOGON_TASK_NAME}), encoding="utf-8")
        calls = []

        def run(args, **kwargs):
            calls.append(args[1:4])
            return types.SimpleNamespace(returncode=0, stdout=f"<Arguments>{launcher.PROJECT_ROOT}</Arguments>", stderr="")

        fake_os = types.SimpleNamespace(name="nt", environ={"USERNAME": "u", "USERDOMAIN": "PC"}, getpid=os.getpid)
        with mock.patch.object(launcher, "os", fake_os), mock.patch.object(launcher.shutil, "which", return_value="schtasks"), \
                mock.patch.object(launcher.subprocess, "run", run), mock.patch.object(launcher, "LOGON_REGISTRATION_PATH", record), \
                mock.patch.object(launcher, "INSTANCE_DIRECTORY", tmp):
            self.assertTrue(launcher.ensure_logon_task(tmp / "python.exe", True))
        self.assertIn(["/Delete", "/TN", launcher.LEGACY_LOGON_TASK_NAME], calls)
        self.assertIn(["/Create", "/TN", launcher.LOGON_TASK_NAME], calls)
        self.assertTrue(launcher.LOGON_TASK_NAME.startswith(launcher.LEGACY_LOGON_TASK_NAME + "-"))
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["task_name"], launcher.LOGON_TASK_NAME)

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
