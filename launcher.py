"""Windows double-click launcher. Run with --no-browser --no-dialog for maintenance."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Iterator


APP = "soundbox"
VERSION = "0.1.0"
DEFAULT_PORT = 23176
ROOT = Path(__file__).resolve().parent
PORTABLE_MARKER = "soundbox-portable.json"


class LaunchError(RuntimeError):
    """A startup failure that can be shown directly to the user."""


def hidden_process_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def normalized_path(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def probe_health(port: int, timeout: float = 0.8) -> dict | None:
    # A configured system proxy must never receive local health checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            data = json.loads(response.read(16385))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def compatible_health(health: dict | None, root: Path, data_dir: Path | None = None) -> bool:
    if not health or health.get("app") != APP or health.get("version") != VERSION:
        return False
    if not isinstance(health.get("instance"), str) or not health["instance"]:
        return False
    candidate = health.get("root")
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        if data_dir is not None and health.get("data_dir") is not None:
            if not isinstance(health["data_dir"], str) or normalized_path(health["data_dir"]) != normalized_path(data_dir):
                return False
        return normalized_path(candidate) == normalized_path(root)
    except (OSError, ValueError):
        return False


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


@contextlib.contextmanager
def startup_lock(path: Path, timeout: float = 360.0) -> Iterator[None]:
    """OS-owned byte lock is released on exit/crash; stale lock files are harmless."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        locked = False
        while not locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise LaunchError("另一个声匣窗口仍在准备启动。请稍后再双击；若一直没有打开，请查看启动日志。")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def python_candidates(root: Path) -> list[Path]:
    portable = portable_paths(root)
    if portable is not None:
        return [portable[0]]
    found = [root / ".venv" / "Scripts" / "python.exe", Path(r"C:\Python313\python.exe")]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or "windowsapps" in directory.lower():
            continue
        found.append(Path(directory.strip('"')) / ("python.exe" if os.name == "nt" else "python3"))
    found.append(Path(sys.executable))
    result: list[Path] = []
    seen = set()
    for candidate in found:
        if "windowsapps" in str(candidate).lower() or not candidate.is_file():
            continue
        marker = normalized_path(candidate)
        if marker not in seen:
            seen.add(marker)
            result.append(candidate)
    return result


def portable_paths(root: Path) -> tuple[Path, Path] | None:
    """A distribution marker selects fixed sibling runtimes, never a host install."""
    marker = root / PORTABLE_MARKER
    if not marker.exists():
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchError("声匣绿色包标记损坏。请重新完整解压工具包，不要只复制其中的声匣目录。") from exc
    if record != {"schemaVersion": 1, "layout": "hub-essentials"}:
        raise LaunchError("声匣绿色包格式不兼容。请重新完整解压当前版本的工具包。")
    runtime = root.parent.parent / "runtime"
    return runtime / "python" / "python.exe", runtime / "ffmpeg"


@dataclass
class LaunchConfig:
    root: Path = ROOT
    data_dir: Path | None = None
    port: int = DEFAULT_PORT
    startup_timeout: float = 30.0
    port_count: int = 100

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.data_dir = (self.data_dir or self.root / "data").resolve()
        if not 1024 <= self.port <= 65535:
            raise LaunchError("启动端口无效。请将 SOUNDBOX_PORT 设置为 1024 到 65535 之间的整数，或移除该设置。")


class Launcher:
    def __init__(self, config: LaunchConfig):
        self.config = config
        self.runtime_path = config.data_dir / "runtime.json"
        self.log_path = config.data_dir / "logs" / "server.log"
        self.spawned_process: subprocess.Popen | None = None

    def read_runtime(self) -> dict:
        try:
            data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_runtime(self, health: dict, port: int, pid: int | None = None) -> dict:
        old = self.read_runtime()
        record = {
            "app": APP,
            "version": VERSION,
            "root": str(self.config.root),
            "data_dir": str(self.config.data_dir),
            "instance": health["instance"],
            "pid": pid if pid is not None else (old.get("pid") if old.get("instance") == health["instance"] else None),
            "port": port,
            "url": f"http://127.0.0.1:{port}",
            "started_at": old.get("started_at", time.time()) if old.get("instance") == health["instance"] else time.time(),
        }
        temporary = self.runtime_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.runtime_path)
        return record

    def candidate_ports(self) -> list[int]:
        runtime = self.read_runtime()
        ports = []
        port = runtime.get("port")
        if isinstance(port, int) and 1024 <= port <= 65535:
            if isinstance(runtime.get("root"), str) and normalized_path(runtime["root"]) == normalized_path(self.config.root):
                ports.append(port)
        ports.extend(range(self.config.port, min(65536, self.config.port + self.config.port_count)))
        return list(dict.fromkeys(ports))

    def run_setup(self, command: list[str], log, timeout: float = 300) -> None:
        try:
            result = subprocess.run(command, cwd=self.config.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=timeout, **hidden_process_options())
        except subprocess.TimeoutExpired as exc:
            raise LaunchError("首次启动安装依赖超时。请检查网络连接后重新双击；详细原因已写入日志。") from exc
        except OSError as exc:
            raise LaunchError("Python 无法运行。请安装 Python 3.11 或更新版本后重新双击。") from exc
        if result.returncode:
            raise LaunchError("首次启动未能安装所需组件。请检查网络连接和项目目录写入权限后重试；详细原因已写入日志。")

    def ensure_python(self, log) -> Path:
        portable = portable_paths(self.config.root)
        if portable is not None:
            python, ffmpeg = portable
            if not all(path.is_file() for path in (python, ffmpeg / "ffmpeg.exe", ffmpeg / "ffprobe.exe")):
                raise LaunchError("绿色包运行组件不完整。请完整解压整个工具包，保留 runtime 文件夹；不需要另行安装 Python 或 FFmpeg。")
            try:
                result = subprocess.run([str(python), "-I", "-B", "-c", "import sys; assert sys.version_info >= (3,11); import fastapi, uvicorn, httpx, urllib3, multipart, pydantic, sqlite3"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=20, **hidden_process_options())
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LaunchError("无法读取绿色包内的运行组件。请把完整工具包解压到可写文件夹，再试一次。") from exc
            if result.returncode:
                raise LaunchError("绿色包内的 Python 组件缺失或损坏。请重新完整解压工具包；启动器不会联网安装组件。")
            return python
        selected = None
        for candidate in python_candidates(self.config.root):
            try:
                result = subprocess.run([str(candidate), "-c", "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **hidden_process_options())
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                selected = candidate
                break
        if selected is None:
            raise LaunchError("没有找到可用的 Python 3.11 或更新版本。请安装 Python 后重新双击“启动声匣”。")
        result = subprocess.run([str(selected), "-c", "import fastapi, uvicorn, httpx, urllib3, multipart, pydantic"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=20, **hidden_process_options())
        if result.returncode == 0:
            return selected
        requirements = self.config.root / "requirements.txt"
        if not requirements.is_file():
            raise LaunchError("缺少 requirements.txt，无法准备运行组件。请恢复完整项目文件后再启动。")
        venv = self.config.root / ".venv"
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not venv_python.is_file() or normalized_path(selected) != normalized_path(venv_python):
            self.run_setup([str(selected), "-m", "venv", str(venv)], log)
        self.run_setup([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)], log)
        return venv_python

    def server_command(self, python: Path, port: int) -> list[str]:
        if portable_paths(self.config.root) is not None:
            # Embedded Python ignores cwd/PYTHONPATH. Only add this exact tool's
            # source directory; the shared runtime stays isolated from the host.
            bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));sys.argv[0]='server.runner';runpy.run_module('server.runner',run_name='__main__')"
            return [str(python), "-I", "-B", "-c", bootstrap, str(self.config.root), "--port", str(port)]
        return [str(python), "-m", "server.runner", "--port", str(port)]

    def start(self) -> dict:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with startup_lock(self.config.data_dir / "startup.lock"):
            ports = self.candidate_ports()
            for port in ports:
                # Skip free sockets without paying an HTTP timeout.
                if port_is_free(port):
                    continue
                health = probe_health(port)
                if compatible_health(health, self.config.root, self.config.data_dir):
                    record = self.write_runtime(health, port)
                    return {**record, "reused": True}

            if not (self.config.root / "web" / "dist" / "index.html").is_file():
                raise LaunchError("缺少已构建的界面文件 web/dist/index.html。请恢复完整的声匣程序包；开发维护时可在 web 目录执行 npm install 和 npm run build。")

            with self.log_path.open("ab", buffering=0) as log:
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Preparing Soundbox {VERSION}\n".encode())
                python = self.ensure_python(log)
                for port in ports:
                    if not port_is_free(port):
                        continue
                    env = os.environ.copy()
                    env.update({"SOUNDBOX_PORT": str(port), "SOUNDBOX_DATA_DIR": str(self.config.data_dir), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
                    portable = portable_paths(self.config.root)
                    if portable is not None:
                        env["PATH"] = str(portable[1]) + os.pathsep + env.get("PATH", "")
                        env.pop("PYTHONHOME", None)
                        env.pop("PYTHONPATH", None)
                    try:
                        process = subprocess.Popen(self.server_command(python, port), cwd=self.config.root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **hidden_process_options())
                    except OSError as exc:
                        raise LaunchError("无法启动声匣服务。请确认 Python 和项目文件仍然存在，且当前用户可以访问此目录。") from exc
                    self.spawned_process = process
                    deadline = time.monotonic() + self.config.startup_timeout
                    while time.monotonic() < deadline:
                        health = probe_health(port, timeout=0.4)
                        if compatible_health(health, self.config.root, self.config.data_dir):
                            # We only record our PID while our own child remains alive.
                            child_pid = process.pid if process.poll() is None else None
                            return {**self.write_runtime(health, port, child_pid), "reused": False}
                        if process.poll() is not None:
                            break
                        time.sleep(0.15)
                    if process.poll() is None:
                        # This is the exact child we spawned, never a process discovered by port/PID.
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise LaunchError("声匣服务启动超时，已停止本次启动的服务。请重新双击；如仍失败，请查看启动日志。")
                    if not port_is_free(port):
                        # Another process won the bind race; leave it alone and choose another port.
                        continue
                    raise LaunchError("声匣服务未能启动。请检查项目文件和运行组件；错误详情保存在启动日志中。")
                raise LaunchError("没有找到可用的本地端口。请关闭不需要的本地服务，或将 SOUNDBOX_PORT 设置为其他可用端口后重新启动。")


def show_error(message: str, no_dialog: bool = False) -> None:
    if not no_dialog and os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "声匣 · 启动失败", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    if (ROOT / PORTABLE_MARKER).is_file():
        sys.dont_write_bytecode = True
        # Embedded Python ignores PYTHONUTF8; Hub still needs UTF-8 JSON/errors
        # when a migrated folder contains Chinese characters.
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="声匣本地启动器")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-dialog", action="store_true")
    args = parser.parse_args(argv)
    log_path = ROOT / "data" / "logs" / "server.log"
    try:
        try:
            port = int(os.environ.get("SOUNDBOX_PORT", DEFAULT_PORT))
        except ValueError as exc:
            raise LaunchError("SOUNDBOX_PORT 必须是 1024 到 65535 之间的整数。请修正或移除该设置后重试。") from exc
        data_dir = Path(os.environ["SOUNDBOX_DATA_DIR"]) if os.environ.get("SOUNDBOX_DATA_DIR") else None
        launcher = Launcher(LaunchConfig(data_dir=data_dir, port=port))
        log_path = launcher.log_path
        result = launcher.start()
        if not args.no_browser:
            if not webbrowser.open(result["url"]):
                raise LaunchError(f"声匣已启动，但未能打开默认浏览器。请在浏览器访问：\n{result['url']}")
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except LaunchError as exc:
        message = str(exc)
    except PermissionError:
        message = "声匣无法写入数据目录。请将项目放在可写入的文件夹，或检查文件夹权限后重试。"
    except Exception as exc:
        message = f"声匣启动遇到错误：{type(exc).__name__}: {exc}"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass
    show_error(f"{message}\n\n启动日志：{log_path}", args.no_dialog)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
