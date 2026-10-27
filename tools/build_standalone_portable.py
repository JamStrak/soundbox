"""Build an offline Windows Soundbox distribution from a verified runtime.

The runtime is an explicit input, never downloaded implicitly. Output must not
already exist. No user data is included. Run the frontend build first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

import build_portable_component

SOURCE = Path(__file__).resolve().parents[1]
README = '''声匣 Windows 独立便携版

一、开始使用
1. 把压缩包完整解压到可写文件夹，例如 D:\\声匣。不要在压缩包内部直接运行。
2. 双击“启动声匣.exe”。首次启动可能需要稍等，程序会自动打开浏览器。
3. 无需安装 Python、Node.js、FFmpeg，也无需安装或启动创作工具 Hub。
4. 再次启动会尝试复用已经运行的声匣；默认端口被占用时会自动寻找可用端口。

二、联网与数据
本地试听、编辑和导出可离线使用；联网搜索和自行下载素材需要网络。可选 AI 服务由用户自行配置，费用以其服务商为准。
自己的数据保存在 tools\\soundbox\\data。请勿删除该目录；备份前先停止正在运行的声匣，或重启电脑后、重新启动声匣之前复制 data 整个目录。换电脑后密钥可能需要重新填写。
本包不包含制作者的个人数据、素材库或密钥。

三、注意
请保留 runtime 和 tools 两个目录；搬迁时复制整个便携包。请勿覆盖旧版本的 data，升级前先备份。
程序没有数字签名；Windows 可能提示未知发布者，请核对下载来源后自行决定是否运行。
源码：https://github.com/JamStrak/soundbox
许可证及第三方组件说明：LICENSE、第三方组件说明.md、runtime 文件夹。
本测试包的 FFmpeg 是 GPL 构建，面向公开分发前需核查其完整对应源码及发行义务。
'''


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def compile_launcher(destination: Path) -> None:
    """Build the tiny GUI entry point with Windows .NET Framework's C# compiler."""
    compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise RuntimeError(f'Windows .NET Framework C# compiler not found: {compiler}')
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:anycpu',
                    '/codepage:65001', f'/out:{destination}', str(SOURCE / 'tools/standalone_launcher.cs')],
                   check=True)


def verified_runtime(runtime: Path) -> list[tuple[Path, PurePosixPath]]:
    runtime = runtime.resolve(strict=True)
    manifest = json.loads((runtime / 'files.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 'creator-hub-runtime-files' or manifest.get('schemaVersion') != 1:
        raise ValueError('Unsupported runtime manifest')
    files = []
    seen = set()
    for item in manifest['files']:
        rel = PurePosixPath(item['path'])
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or ':' in str(rel) or str(rel) in seen:
            raise ValueError(f'Unsafe or duplicate runtime path: {rel}')
        seen.add(str(rel))
        path = runtime.joinpath(*rel.parts)
        cur = path
        while cur != runtime:
            info = cur.lstat()
            if cur.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError(f'Reparse point in runtime: {rel}')
            cur = cur.parent
        if not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
            raise ValueError(f'Runtime file failed verification: {rel}')
        files.append((path, rel))
    for required in ('python/pythonw.exe', 'python/python.exe', 'ffmpeg/ffmpeg.exe', 'ffmpeg/ffprobe.exe'):
        if required not in seen:
            raise ValueError(f'Missing runtime file: {required}')
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--zip', required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    archive = args.zip.resolve()
    if out.exists() or archive.exists() or out == SOURCE or out.is_relative_to(SOURCE) or archive.is_relative_to(out):
        raise ValueError('Output and archive must be new and outside the source workspace')
    files = verified_runtime(args.runtime)
    out.mkdir(parents=True)
    for src, rel in files:
        dest = out / 'runtime' / Path(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    shutil.copy2(args.runtime / 'files.json', out / 'runtime' / 'files.json')
    build_portable_component.build_component(SOURCE, out / 'tools' / 'soundbox', standalone_exe=True)
    compile_launcher(out / '启动声匣.exe')
    (out / '使用说明.txt').write_bytes(README.replace('\n', '\r\n').encode('utf-8-sig'))
    shutil.copy2(SOURCE / 'LICENSE', out / 'LICENSE')
    shutil.copy2(SOURCE / 'THIRD_PARTY.md', out / '第三方组件说明.md')
    commit = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
    (out / '便携包版本.json').write_text(json.dumps({'platform': 'windows-x64', 'productSourceCommit': commit, 'runtimeManifestSha256': sha256(out / 'runtime' / 'files.json')}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as z:
        for file in sorted(out.rglob('*')):
            if file.is_file():
                z.write(file, file.relative_to(out).as_posix())
    digest = sha256(archive)
    Path(str(archive) + '.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(json.dumps({'output': str(out), 'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest, 'sourceCommit': commit}, ensure_ascii=False))


if __name__ == '__main__':
    main()
