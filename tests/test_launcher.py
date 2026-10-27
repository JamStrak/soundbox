"""Exercise real hidden child processes on loopback, without network/API keys."""

import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from launcher import LaunchConfig, LaunchError, Launcher, compatible_health, hidden_process_options, main, port_is_free, probe_health

# A Windows venv python.exe is a forwarding process; these stdlib-only fixtures
# must own the actual server process so termination/wait covers its log handles.
FIXTURE_PYTHON = getattr(sys, '_base_executable', sys.executable)

FAKE_SERVER = r"""
import json, os, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
root, port, version = sys.argv[1:]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({'app':'soundbox', 'version':version, 'instance':str(os.getpid()), 'root':root, 'data_dir':os.environ.get('SOUNDBOX_DATA_DIR',str(Path(root)/'data'))}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(port)), Handler).serve_forever()
"""


class FakeLauncher(Launcher):
    def ensure_python(self, log):
        return Path(FIXTURE_PYTHON)

    def server_command(self, python, port):
        return [str(python), "-c", FAKE_SERVER, str(self.config.root), str(port), "0.1.0"]


class FailingLauncher(FakeLauncher):
    def server_command(self, python, port):
        return [str(python), "-c", "import sys; print('intentional startup failure', file=sys.stderr); sys.exit(27)"]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="soundbox-launcher-")
        self.root = Path(self.temp.name) / "带空格 中文 项目"
        (self.root / "web" / "dist").mkdir(parents=True)
        (self.root / "web" / "dist" / "index.html").write_text("hello", encoding="utf-8")
        self.children = []
        self.launchers = []
        self.port = self.find_port_range()

    def tearDown(self):
        children = self.children + [item.spawned_process for item in self.launchers if item.spawned_process is not None]
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        self.temp.cleanup()

    @staticmethod
    def find_port_range():
        for _ in range(100):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            if port <= 65000 and all(port_is_free(value) for value in range(port, port + 5)):
                return port
        raise RuntimeError("Could not allocate loopback test ports")

    def launcher(self, cls=FakeLauncher, **kwargs):
        config = LaunchConfig(root=self.root, port=self.port, startup_timeout=4, port_count=5, **kwargs)
        launcher = cls(config)
        self.launchers.append(launcher)
        return launcher

    def external_server(self, root=None, version="0.1.0"):
        child = subprocess.Popen([FIXTURE_PYTHON, "-c", FAKE_SERVER, str(root or self.root), str(self.port), version], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_process_options())
        self.children.append(child)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if probe_health(self.port, timeout=0.1):
                return child
            time.sleep(0.05)
        self.fail("Fake server did not become healthy")

    def test_normal_start_and_reuse_persist_runtime(self):
        initial = self.launcher().start()
        self.assertFalse(initial["reused"])
        self.assertEqual(initial["port"], self.port)
        self.assertEqual(initial["root"], str(self.root.resolve()))
        self.assertTrue(compatible_health(probe_health(self.port), self.root))
        record = json.loads((self.root / "data" / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], initial["pid"])
        self.assertTrue((self.root / "data" / "logs" / "server.log").is_file())
        second = self.launcher().start()
        self.assertTrue(second["reused"])
        self.assertEqual(second["instance"], initial["instance"])
        self.assertEqual(second["pid"], initial["pid"])

    def test_other_workspace_on_port_is_left_running_and_fallback_reused(self):
        other = self.external_server(root=self.root / "other")
        initial = self.launcher().start()
        self.assertEqual(initial["port"], self.port + 1)
        self.assertIsNone(other.poll())
        second = self.launcher().start()
        self.assertTrue(second["reused"])
        self.assertEqual(second["port"], self.port + 1)

    def test_other_version_is_not_reused_or_stopped(self):
        other = self.external_server(version="9.9.9")
        result = self.launcher().start()
        self.assertEqual(result["port"], self.port + 1)
        self.assertIsNone(other.poll())

    def test_non_http_port_occupation_uses_next_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            result = self.launcher().start()
            self.assertEqual(result["port"], self.port + 1)

    def test_startup_failure_has_actionable_message_and_log(self):
        launcher = self.launcher(FailingLauncher)
        with self.assertRaisesRegex(LaunchError, "启动日志"):
            launcher.start()
        self.assertFalse(launcher.runtime_path.exists())
        self.assertIn("intentional startup failure", launcher.log_path.read_text(encoding="utf-8"))
        self.assertEqual(launcher.spawned_process.returncode, 27)

    def test_missing_dist_does_not_start_server(self):
        (self.root / "web" / "dist" / "index.html").unlink()
        launcher = self.launcher()
        with self.assertRaisesRegex(LaunchError, "web/dist/index.html"):
            launcher.start()
        self.assertIsNone(launcher.spawned_process)

    def test_concurrent_double_click_starts_only_one_server(self):
        launchers = [self.launcher(), self.launcher()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: item.start(), launchers))
        self.assertEqual(results[0]["instance"], results[1]["instance"])
        self.assertEqual(sorted(item["reused"] for item in results), [False, True])
        self.assertEqual(sum(item.spawned_process is not None for item in launchers), 1)

    def test_custom_data_dir_is_respected(self):
        data_dir = Path(self.temp.name) / "isolated-data"
        result = self.launcher(data_dir=data_dir).start()
        self.assertTrue((data_dir / "runtime.json").is_file())
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(result["data_dir"], str(data_dir.resolve()))

    def test_separate_data_dir_does_not_reuse_same_project_instance(self):
        first = self.launcher().start()
        data_dir = Path(self.temp.name) / "second-library"
        second = self.launcher(data_dir=data_dir).start()
        self.assertFalse(second["reused"])
        self.assertEqual(second["port"], first["port"] + 1)
        self.assertNotEqual(second["instance"], first["instance"])

    def test_missing_dependencies_install_into_project_venv(self):
        (self.root / "requirements.txt").write_text("fastapi==0.116.1\n", encoding="utf-8")
        launcher = self.launcher(Launcher)
        responses = [subprocess.CompletedProcess([], code) for code in (0, 1, 0, 0)]
        with patch("launcher.python_candidates", return_value=[Path(sys.executable)]), patch("launcher.subprocess.run", side_effect=responses) as run:
            selected = launcher.ensure_python(io.BytesIO())
        self.assertTrue(selected.is_relative_to(self.root / ".venv"))
        creation = run.call_args_list[2].args[0]
        installation = run.call_args_list[3].args[0]
        self.assertEqual(creation[1:3], ["-m", "venv"])
        self.assertEqual(installation[0], str(selected))
        self.assertEqual(installation[-1], str(self.root / "requirements.txt"))

    def test_missing_dependencies_failure_is_actionable(self):
        (self.root / "requirements.txt").write_text("fastapi==0.116.1\n", encoding="utf-8")
        launcher = self.launcher(Launcher)
        responses = [subprocess.CompletedProcess([], code) for code in (0, 1, 0, 1)]
        with patch("launcher.python_candidates", return_value=[Path(sys.executable)]), patch("launcher.subprocess.run", side_effect=responses), self.assertRaisesRegex(LaunchError, "网络连接"):
            launcher.ensure_python(io.BytesIO())

    def test_invalid_port_reports_without_dialog(self):
        output = io.StringIO()
        with patch("launcher.ROOT", self.root), patch.dict(os.environ, {"SOUNDBOX_PORT": "not-a-port"}), contextlib.redirect_stderr(output):
            status = main(["--no-browser", "--no-dialog"])
        self.assertEqual(status, 1)
        self.assertIn("SOUNDBOX_PORT", output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_vbs_handles_chinese_and_spaces_without_terminal(self):
        source = Path(__file__).resolve().parents[1] / "启动声匣.vbs"
        destination = self.root / source.name
        shutil.copy2(source, destination)
        self.assertTrue(destination.read_bytes().startswith(b"\xff\xfe"))
        stub = self.root / "launcher.py"
        stub.write_text("from pathlib import Path\nPath(__file__).with_suffix('.started').write_text('ok')\n", encoding="utf-8")
        run = subprocess.run(["cscript.exe", "//nologo", str(destination)], capture_output=True, timeout=5, **hidden_process_options())
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors="replace"))
        marker = stub.with_suffix(".started")
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.is_file(), "Hidden VBS launch never ran Python stub")


if __name__ == "__main__":
    unittest.main()
