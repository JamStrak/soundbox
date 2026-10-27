"""Portable launch must use bundled dependencies and never install on a host."""
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from launcher import LaunchConfig, LaunchError, Launcher, portable_paths, python_candidates


def portable_tree(tmp_path):
    root = tmp_path / "综合 工具's 中文" / "tools" / "soundbox"
    root.mkdir(parents=True)
    (root / 'soundbox-portable.json').write_text(json.dumps({'schemaVersion': 1, 'layout': 'hub-essentials'}))
    python, ffmpeg = portable_paths(root)
    for file in (python, ffmpeg / 'ffmpeg.exe', ffmpeg / 'ffprobe.exe'):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'test fixture, not executable')
    return root, python, ffmpeg


def test_portable_python_precedes_host_and_has_no_fallback(tmp_path):
    root, python, _ = portable_tree(tmp_path)
    assert python_candidates(root) == [python]
    python.unlink()
    assert python_candidates(root) == [python]
    with patch('launcher.subprocess.run') as run, pytest.raises(LaunchError, match='完整解压'):
        Launcher(LaunchConfig(root=root)).ensure_python(io.BytesIO())
    run.assert_not_called()


def test_portable_missing_modules_never_attempts_pip(tmp_path):
    root, _, _ = portable_tree(tmp_path)
    with patch('launcher.subprocess.run', return_value=subprocess.CompletedProcess([], 1)) as run:
        with pytest.raises(LaunchError, match='不会联网安装'):
            Launcher(LaunchConfig(root=root)).ensure_python(io.BytesIO())
    assert run.call_count == 1
    assert run.call_args.args[0][1:4] == ['-I', '-B', '-c']


def test_embedded_bootstrap_passes_path_as_arg_not_executable_code(tmp_path):
    root, python, _ = portable_tree(tmp_path)
    command = Launcher(LaunchConfig(root=root)).server_command(python, 24760)
    assert command[0] == str(python)
    assert command[1:4] == ['-I', '-B', '-c']
    assert str(root) not in command[4]
    assert command[5:] == [str(root.resolve()), '--port', '24760']


def test_malformed_marker_is_not_treated_as_development_install(tmp_path):
    root, _, _ = portable_tree(tmp_path)
    (root / 'soundbox-portable.json').write_text('{broken')
    with pytest.raises(LaunchError, match='标记损坏'):
        python_candidates(root)
