"""Python-only application bootstrapper for Windows."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import venv
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree


APP_CONFIG = {
    "name": "NewsHtmlReader",
    "minimum_python": (3, 10),
    "bootstrap_python": "3.13.16",
    "default_bind_host": "127.0.0.1",
    "port_range": (5000, 5100),
    "pip_timeout_seconds": 10,
    "pip_retries": 1,
}

PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_ROOT / "instance" / "settings.json"
INSTANCE_DIRECTORY = PROJECT_ROOT / "instance"
COLLECTOR_SETTINGS_PATH = INSTANCE_DIRECTORY / "collector.json"
TASK_REGISTRATION_PATH = INSTANCE_DIRECTORY / "collector-task.json"


def read_settings() -> dict[str, object]:
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def write_settings(settings: dict[str, object]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.stem}-{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def validate_bind_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError("待受けIPはIPv4アドレスで指定してください。") from exc
    # Same rule as newsreader/netpolicy.py (kept inline: the launcher runs before the app's
    # dependencies exist). ipaddress calls 0.0.0.0 "private", but binding to it listens on
    # every interface including public ones, so unspecified/reserved/multicast are refused.
    # tests/test_web.py checks that this copy and netpolicy agree.
    tailscale_range = ipaddress.ip_network("100.64.0.0/10")
    if (address.version != 4 or address.is_unspecified or address.is_multicast or address.is_reserved
            or address in ipaddress.ip_network("0.0.0.0/8")
            or not (address.is_loopback or address.is_private or address in tailscale_range)):
        raise ValueError("待受けIPにはloopback、private IPv4、またはTailscale IPv4を指定してください。")
    return str(address)


def is_supported_python(version: tuple[int, int] = sys.version_info[:2]) -> bool:
    return version >= APP_CONFIG["minimum_python"]


def detect_tailscale() -> tuple[str | None, str | None]:
    executable = shutil.which("tailscale.exe") or shutil.which("tailscale")
    if not executable:
        try:
            for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                candidate = result[4][0]
                if is_tailscale_ip(candidate):
                    return candidate, None
        except OSError:
            pass
        return None, None
    try:
        output = subprocess.run(
            [executable, "status", "--json"], capture_output=True, text=True,
            timeout=4, check=True,
        ).stdout
        self_status = json.loads(output).get("Self", {})
        addresses = self_status.get("TailscaleIPs", [])
        host = next((item for item in addresses if is_tailscale_ip(item)), None)
        dns_name = str(self_status.get("DNSName", "")).rstrip(".") or None
        return host, dns_name
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None, None


def is_tailscale_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def detect_lan_ip() -> str | None:
    # Ask Windows routing which local address it would use for an outbound connection.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            candidate = probe.getsockname()[0]
            address = ipaddress.ip_address(candidate)
            if address.is_private and not address.is_loopback:
                return candidate
    except OSError:
        pass
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = result[4][0]
            address = ipaddress.ip_address(candidate)
            if address.is_private and not address.is_loopback:
                return candidate
    except OSError:
        pass
    return None


def detect_bind_host() -> str:
    tailscale_ip, _ = detect_tailscale()
    return tailscale_ip or detect_lan_ip() or APP_CONFIG["default_bind_host"]


def is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
        return True
    except OSError:
        return False


def select_connection(settings: dict[str, object]) -> tuple[str, int, set[str]]:
    saved_host = settings.get("network_bind_host", "")
    configured_host = os.environ.get("NEWS_BIND_HOST", "").strip()
    bind_host = validate_bind_host(configured_host or (saved_host if isinstance(saved_host, str) else "") or detect_bind_host())

    saved_port = settings.get("network_port", "")
    configured_port = os.environ.get("NEWS_PORT", "").strip()
    requested_port = configured_port or (str(saved_port).strip() if isinstance(saved_port, (str, int)) else "")
    if requested_port:
        try:
            port = int(requested_port)
        except ValueError as exc:
            raise ValueError("ポート番号は1から65535の整数で指定してください。") from exc
        if not 1 <= port <= 65535:
            raise ValueError("ポート番号は1から65535の整数で指定してください。")
        if not is_port_available(bind_host, port):
            raise OSError(f"Port {port} is already in use on {bind_host}.")
    else:
        first_port, last_port = APP_CONFIG["port_range"]
        port = next((candidate for candidate in range(first_port, last_port + 1) if is_port_available(bind_host, candidate)), 0)
        if not port:
            raise OSError(f"No available port from {first_port} through {last_port} was found on {bind_host}.")

    tailscale_ip, tailscale_dns = detect_tailscale()
    allowed_hosts = {bind_host, "localhost", "127.0.0.1", "::1"}
    if tailscale_dns:
        allowed_hosts.add(tailscale_dns.lower())
    extras = os.environ.get("NEWS_ALLOWED_HOSTS_EXTRA", "")
    allowed_hosts.update(item.strip().lower() for item in extras.split(",") if item.strip())
    if tailscale_ip:
        allowed_hosts.add(tailscale_ip)
    return bind_host, port, allowed_hosts


def prepare_first_run(settings: dict[str, object], marker: Path) -> bool:
    if marker.exists():
        return False
    print("初回設定: APIキーは起動後にブラウザーで登録できます。キーをこの画面へ入力しないでください。")
    custom_host = input("このPCの待受けIPを固定しますか (Tailscale / private IPv4、空欄なら自動検出): ").strip()
    if custom_host:
        settings["network_bind_host"] = validate_bind_host(custom_host)
        write_settings(settings)
    answer = input("起動後にブラウザーで翻訳設定を開き、APIキー等を登録しますか (Y/n): ").strip().lower()
    return answer not in {"n", "no"}


def find_runtime_python() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    root_hash = hashlib.sha256(str(PROJECT_ROOT).casefold().encode("utf-8")).hexdigest()[:12]
    local_runtime = local_app_data / APP_CONFIG["name"] / "venvs" / root_hash / "Scripts" / "python.exe"
    project_runtime = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    for candidate in (project_runtime, local_runtime):
        if candidate.is_file():
            version_probe = subprocess.run(
                [str(candidate), "-c", "import sys; raise SystemExit(sys.version_info < (3, 10))"],
                capture_output=True,
            )
            pip_probe = subprocess.run([str(candidate), "-m", "pip", "--version"], capture_output=True)
            if version_probe.returncode == 0 and pip_probe.returncode == 0:
                return candidate

    runtime_root = local_runtime.parent.parent
    if runtime_root.exists():
        runtime_root = runtime_root.parent / f"{root_hash}-repair-{os.getpid()}"
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    print(f"Python {platform.python_version()} でこのPC用の仮想環境を準備します...")
    venv.EnvBuilder(with_pip=True).create(runtime_root)
    return runtime_root / "Scripts" / "python.exe"


def update_dependencies(runtime_python: Path) -> None:
    pip_command = [
        str(runtime_python), "-m", "pip", "install", "--upgrade", "pip",
        "--disable-pip-version-check", "--timeout", str(APP_CONFIG["pip_timeout_seconds"]),
        "--retries", str(APP_CONFIG["pip_retries"]),
    ]
    print("pipの最新版を確認・更新しています...")
    result = subprocess.run(pip_command, cwd=PROJECT_ROOT)
    if result.returncode:
        print("警告: pipを更新できませんでした。現在のpipで起動を続けます。")
    print("依存パッケージを確認しています...")
    subprocess.run(
        [str(runtime_python), "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")],
        cwd=PROJECT_ROOT, check=True,
    )


def resolve_article_directory() -> Path:
    configured = os.environ.get("HTML_DIRECTORY", "").strip()
    if not configured:
        return (PROJECT_ROOT / "articles").resolve()
    directory = Path(configured).expanduser()
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    return directory.resolve()


def persist_collector_config(library_root: Path) -> None:
    INSTANCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    temporary = COLLECTOR_SETTINGS_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"library_root": str(library_root)}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, COLLECTOR_SETTINGS_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_hourly_collector_task(runtime_python: Path, library_root: Path) -> bool:
    """Create/update the per-installation hourly task; return whether it changed."""
    if os.name != "nt":
        print("Windows Task Scheduler is unavailable; automatic collection task was not registered.")
        return False
    scheduler = shutil.which("schtasks.exe") or shutil.which("schtasks")
    if not scheduler:
        print("Warning: schtasks.exe was not found; automatic article collection is not scheduled.")
        return False

    project_id = hashlib.sha256(str(PROJECT_ROOT).casefold().encode("utf-8")).hexdigest()[:8]
    task_name = f"{APP_CONFIG['name']}-HourlyCollector-{project_id}"
    collector_script = PROJECT_ROOT / "collector.py"
    # python.exe attaches a console window when Task Scheduler starts the job.
    # Use the matching pythonw.exe so hourly collection stays invisible while
    # the rotating file logger remains the source of diagnostics.
    collector_python = runtime_python.with_name("pythonw.exe")
    if not collector_python.is_file():
        print("Warning: pythonw.exe was not found; refusing to register a task that could show a console window.")
        return False
    task_action = subprocess.list2cmdline([str(collector_python), str(collector_script), "--once"])
    registration = {
        "definition_version": 2,
        "task_name": task_name,
        "action": task_action,
        "library_root": str(library_root),
    }
    try:
        previous = json.loads(TASK_REGISTRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        previous = {}
    query = subprocess.run(
        [scheduler, "/Query", "/TN", task_name], capture_output=True, text=True, errors="replace",
    )
    if query.returncode == 0 and previous == registration:
        return False

    xml_namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ElementTree.register_namespace("", xml_namespace)

    def element(parent, name: str, text: str | None = None, **attributes):
        node = ElementTree.SubElement(parent, f"{{{xml_namespace}}}{name}", attributes)
        if text is not None:
            node.text = text
        return node

    task = ElementTree.Element(f"{{{xml_namespace}}}Task", {"version": "1.3"})
    registration_info = element(task, "RegistrationInfo")
    element(registration_info, "Description", "Collect supported news article HTML once per hour without starting the web app.")

    triggers = element(task, "Triggers")
    trigger = element(triggers, "CalendarTrigger")
    element(trigger, "Enabled", "true")
    start_boundary = (datetime.now() + timedelta(minutes=2)).replace(second=0, microsecond=0).isoformat()
    element(trigger, "StartBoundary", start_boundary)
    repetition = element(trigger, "Repetition")
    element(repetition, "Interval", "PT1H")
    element(repetition, "Duration", "P1D")
    element(repetition, "StopAtDurationEnd", "false")
    schedule = element(trigger, "ScheduleByDay")
    element(schedule, "DaysInterval", "1")

    principals = element(task, "Principals")
    principal = element(principals, "Principal", id="CollectorUser")
    user_name = os.environ.get("USERNAME", "")
    domain_name = os.environ.get("USERDOMAIN", "")
    account = f"{domain_name}\\{user_name}" if domain_name else user_name
    if not account:
        print("Warning: could not determine the current Windows user; collection task was not registered.")
        return False
    element(principal, "UserId", account)
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")

    task_settings = element(task, "Settings")
    element(task_settings, "MultipleInstancesPolicy", "IgnoreNew")
    element(task_settings, "DisallowStartIfOnBatteries", "false")
    element(task_settings, "StopIfGoingOnBatteries", "false")
    element(task_settings, "AllowHardTerminate", "true")
    element(task_settings, "StartWhenAvailable", "true")
    element(task_settings, "RunOnlyIfNetworkAvailable", "false")
    element(task_settings, "WakeToRun", "false")
    element(task_settings, "Enabled", "true")
    element(task_settings, "Hidden", "false")
    element(task_settings, "ExecutionTimeLimit", "PT1H")
    element(task_settings, "Priority", "7")

    actions = element(task, "Actions", Context="CollectorUser")
    execute = element(actions, "Exec")
    element(execute, "Command", str(collector_python))
    element(execute, "Arguments", f'"{collector_script}" --once')
    element(execute, "WorkingDirectory", str(PROJECT_ROOT))

    temporary_xml = INSTANCE_DIRECTORY / f"collector-task-{os.getpid()}.xml"
    try:
        ElementTree.ElementTree(task).write(temporary_xml, encoding="utf-16", xml_declaration=True)
        created = subprocess.run(
            [scheduler, "/Create", "/TN", task_name, "/XML", str(temporary_xml), "/F"],
            capture_output=True, text=True, errors="replace",
        )
    finally:
        temporary_xml.unlink(missing_ok=True)
    if created.returncode:
        details = (created.stderr or created.stdout).strip()
        print(f"Warning: could not register the hourly article collection task: {details}")
        return False

    temporary = TASK_REGISTRATION_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(registration, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, TASK_REGISTRATION_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    started = subprocess.run([scheduler, "/Run", "/TN", task_name], capture_output=True, text=True, errors="replace")
    if started.returncode:
        details = (started.stderr or started.stdout).strip()
        print(f"Warning: task was registered but its initial run could not be started: {details}")
    else:
        print(f"Hourly article collection task registered: {task_name}")
    return True


def launch_app(runtime_python: Path, first_run: bool, open_first_run_settings: bool) -> int:
    settings = read_settings()
    bind_host, port, allowed_hosts = select_connection(settings)
    env = os.environ.copy()
    env["NEWS_BIND_HOST"] = bind_host
    env["NEWS_PORT"] = str(port)
    env["NEWS_ALLOWED_HOSTS"] = ",".join(sorted(allowed_hosts))
    env["HTML_DIRECTORY"] = str(resolve_article_directory())

    first_port, last_port = APP_CONFIG["port_range"]
    print(f"Starting the reader at http://{bind_host}:{port}/")
    print(f"自動選択するポート範囲: {first_port}–{last_port}。固定したい場合はブラウザー設定で変更してください。")
    browser_path = "/translation-settings?first_run=1" if first_run and open_first_run_settings else "/"
    start_url = f"http://{bind_host}:{port}{browser_path}"
    threading.Timer(2, webbrowser.open, args=(start_url,)).start()
    if bind_host != APP_CONFIG["default_bind_host"]:
        print(f"For another device, use http://{bind_host}:{port}/ (Tailscale or the same private network).")
    print("Keep this window open while using the reader. Press Ctrl+C to stop it.")
    env["NEWS_SUPERVISED"] = "1"  # lets app.py restart itself into newly deployed code
    return supervise_app(runtime_python, env)


RESTART_EXIT_CODE = 75  # app.py: "a new version was deployed, start me again"
VERSION_PATH = PROJECT_ROOT / "VERSION"
QUICK_FAILURE_SECONDS = 30
MAX_QUICK_FAILURES = 3


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def supervise_app(runtime_python: Path, env: dict[str, str]) -> int:
    """Run app.py and keep it running across deployments (the PC stays on while the user is out).

    - exit 75: a new version was deployed -> refresh dependencies if requirements.txt changed, restart;
    - exit 0: stopped normally (Ctrl+C) -> stop;
    - any other exit: restart with back-off; after repeated quick failures, wait until the next
      deployment (VERSION changes) instead of restarting in a tight loop.
    """
    requirements = PROJECT_ROOT / "requirements.txt"
    requirements_digest = _file_digest(requirements)
    quick_failures = 0
    while True:
        started = time.monotonic()
        code = subprocess.run([str(runtime_python), str(PROJECT_ROOT / "app.py")], cwd=PROJECT_ROOT, env=env).returncode
        if code == 0:
            return 0
        if code == RESTART_EXIT_CODE:
            quick_failures = 0
            if _file_digest(requirements) != requirements_digest:
                try:
                    update_dependencies(runtime_python)
                    requirements_digest = _file_digest(requirements)
                except (OSError, subprocess.CalledProcessError) as error:
                    print(f"警告: 依存パッケージを更新できませんでした（{error}）。現在の環境で再起動します。")
            print("新しいバージョンで再起動します...")
            continue
        quick_failures = quick_failures + 1 if time.monotonic() - started < QUICK_FAILURE_SECONDS else 1
        if quick_failures >= MAX_QUICK_FAILURES:
            print("アプリの起動に続けて失敗しました。次の更新を待って自動で再試行します（Ctrl+Cで終了）。")
            seen = _file_digest(VERSION_PATH)
            while _file_digest(VERSION_PATH) == seen:
                time.sleep(30)
            quick_failures = 0
            continue
        delay = min(60, 5 * quick_failures)
        print(f"アプリが終了しました（コード {code}）。{delay}秒後に再起動します（Ctrl+Cで終了）。")
        time.sleep(delay)


def main() -> int:
    if not is_supported_python():
        print(f"Python 3.10 or newer is required; found {platform.python_version()}.", file=sys.stderr)
        return 1
    settings = read_settings()
    profile_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_CONFIG["name"]
    first_run_marker = profile_root / "first-run-complete"
    first_run = not first_run_marker.exists()
    open_first_run_settings = prepare_first_run(settings, first_run_marker) if first_run else False
    select_connection(settings)
    runtime_python = find_runtime_python()
    update_dependencies(runtime_python)
    library_root = resolve_article_directory()
    persist_collector_config(library_root)
    ensure_hourly_collector_task(runtime_python, library_root)
    if not first_run_marker.exists():
        profile_root.mkdir(parents=True, exist_ok=True)
        first_run_marker.write_text("completed", encoding="ascii")
    # Recheck after setup so a changed interface/port error is reported before browser launch.
    settings = read_settings()
    select_connection(settings)
    return launch_app(runtime_python, first_run, open_first_run_settings)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"起動できませんでした: {error}", file=sys.stderr)
        raise SystemExit(1)
