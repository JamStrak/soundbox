"""Whitelist-only Soundbox component builder. Does not bundle any personal data.

The parent distribution supplies runtime/python and runtime/ffmpeg. Import
build_component(source, output) or use --source SOURCE --output tools/soundbox.
The output directory must be absent/empty; never overlay an existing user copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


PYTHON_REQUIREMENTS = [
    'annotated-types==0.8.0', 'anyio==4.15.1', 'certifi==2026.7.22',
    'click==8.5.0', 'colorama==0.4.6', 'fastapi==0.141.1', 'h11==0.16.0',
    'httpcore==1.0.9', 'httpx==0.28.1', 'idna==3.19', 'pydantic==2.13.5',
    'pydantic-core==2.46.5', 'python-multipart==0.0.32', 'sniffio==1.3.1',
    'starlette==1.6.0', 'typing-extensions==4.16.0',
    'typing-inspection==0.4.4', 'urllib3==2.6.3', 'uvicorn==0.52.4',
]

VBS = '''Option Explicit
Dim fso, shell, root, bundle, python, launcher, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
bundle = fso.GetParentFolderName(fso.GetParentFolderName(root))
python = fso.BuildPath(bundle, "runtime\\python\\pythonw.exe")
launcher = fso.BuildPath(root, "launcher.py")
If Not fso.FileExists(python) Or Not fso.FileExists(launcher) Then
    MsgBox "运行组件不完整。请完整解压声匣便携包，保留 runtime 文件夹，然后双击根目录的启动声匣。无需安装 Python。", vbCritical, "声匣 · 启动失败"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
command = Chr(34) & python & Chr(34) & " -I -B " & Chr(34) & launcher & Chr(34)
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "无法启动声匣。请检查工具包是否完整、文件夹是否可写。" & vbCrLf & Err.Description, vbCritical, "声匣 · 启动失败"
    WScript.Quit 1
End If
'''

README = '''声匣 · 独立便携包组件

普通使用：双击便携包根目录的“启动声匣.vbs”。
也可双击本目录“启动声匣.vbs”。不要只移动 tools/soundbox；移动时复制整个便携包。
组件使用随包 runtime/python 与 runtime/ffmpeg，不联网安装组件，不要求本机 Python/Node。

首次运行会在本目录 data/ 创建自己的素材库和 12 条 CC0 程序合成示例。
首次游戏素材库为空；进入“我的素材库”可直接试听示例、裁剪拼接、另存或导出。
Kenney 游戏音效包由用户在游戏素材库中主动下载，需要联网；未预装个人收藏或开发者素材。
Openverse 搜索需要联网，无需 Key。可选 AI 生成/文本整理须自行配置服务及密钥，按供应商规则计费。

退出前保存正在编辑的音效。个人资料在 data/；备份前先停止声匣，再复制完整目录。数据库不能在服务运行中仅复制主文件。
API 密钥保险库绑定当前 Windows 账户，换机后重新填写自己的密钥。
本分发不包含原作者个人 data/、密钥、日志、下载历史或媒体文件。
本组件的 Python 与 FFmpeg 第三方许可证、来源和版本见便携包 runtime 及第三方说明。
React、React DOM、Scheduler、Lucide 的许可证见本目录 licenses/。
'''


def checked_read(source: Path, path: Path) -> bytes:
    current = path
    while current != source:
        if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Reparse path is not distributable: {path.relative_to(source)}')
        current = current.parent
        if not current.is_relative_to(source):
            raise ValueError('Source escapes component root')
    if not path.is_file():
        raise ValueError(f'Required file missing: {path.relative_to(source)}')
    return path.read_bytes()


def collect_component(source: Path, *, standalone_exe: bool = False) -> dict[str, bytes]:
    source = source.resolve(strict=True)
    payload = {}
    for name in ('launcher.py',):
        payload[name] = checked_read(source, source / name)
    for path in sorted((source / 'server').iterdir()):
        if path.suffix in ('.py', '.js', '.json') and path.is_file():
            payload[path.relative_to(source).as_posix()] = checked_read(source, path)
    for name in ('web/dist/index.html', 'web/dist/favicon.svg'):
        payload[name] = checked_read(source, source / name)
    for path in sorted((source / 'web/dist/assets').iterdir()):
        if path.suffix in ('.js', '.css', '.woff2', '.svg', '.png', '.webp'):
            payload[path.relative_to(source).as_posix()] = checked_read(source, path)
    html = payload['web/dist/index.html'].decode('utf-8')
    for reference in re.findall(r'(?:src|href)="(/assets/[^"?]+)', html):
        if f'web/dist{reference}' not in payload:
            raise ValueError(f'Frontend asset missing: {reference}')
    for package in ('react', 'react-dom', 'scheduler', 'lucide-react'):
        license_path = source / 'web/node_modules' / package / 'LICENSE'
        payload[f'licenses/{package}-LICENSE.txt'] = checked_read(source, license_path)
    payload['soundbox-portable.json'] = b'{"schemaVersion":1,"layout":"hub-essentials"}\n'
    payload['启动声匣.vbs'] = VBS.replace('\n', '\r\n').encode('utf-16')
    readme = README.replace('“启动声匣.vbs”', '“启动声匣.exe”', 1) if standalone_exe else README
    payload['使用说明.txt'] = readme.replace('\n', '\r\n').encode('utf-8-sig')
    payload['licenses/python-compatibility.txt'] = (
        'Shared-runtime packages used for Soundbox compatibility verification.\n'
        'The full runtime manifest at the distribution root is authoritative.\n'
        'These are not an instruction to install additional dependencies.\n\n'
        + '\n'.join(PYTHON_REQUIREMENTS) + '\n').encode()
    return payload


def build_component(source: Path, output: Path, *, standalone_exe: bool = False) -> dict:
    source = source.resolve(strict=True)
    output = output.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError('Output must be outside the source workspace')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty; refusing to overwrite an existing tool/data directory')
    payload = collect_component(source, standalone_exe=standalone_exe)
    manifest = {
        'format': 'creator-hub-essential-component', 'schemaVersion': 1,
        'id': 'soundbox', 'name': '声匣', 'version': '0.1.0',
        'platform': 'windows-x64', 'layout': 'tools/soundbox',
        'runtime': ['runtime/python/python.exe', 'runtime/ffmpeg/ffmpeg.exe', 'runtime/ffmpeg/ffprobe.exe'],
        'pythonRequirements': PYTHON_REQUIREMENTS,
        'dataDirectory': 'tools/soundbox/data',
        'files': [{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(payload.items())],
    }
    for name, data in payload.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (output / 'component.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'id': 'soundbox', 'output': str(output), 'files': len(payload) + 1,
            'bytes': sum(len(data) for data in payload.values()), 'manifest': manifest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_component(args.source, args.output)
    print(json.dumps({key: value for key, value in result.items() if key != 'manifest'}, ensure_ascii=False))
