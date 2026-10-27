"""Read a built bundle, create an isolated copy, test real offline audio, then stop.

python tests/verify_portable_bundle.py --bundle <root> --scratch <new empty root>
No provider calls; the original bundle and original Soundbox data stay untouched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request


def verify(bundle, scratch):
    bundle, scratch = bundle.resolve(strict=True), scratch.resolve()
    if scratch.exists() or scratch == bundle or scratch.is_relative_to(bundle):
        raise ValueError('Scratch must be a new directory outside the bundle')
    source = bundle / 'tools/soundbox'
    manifest = json.loads((source / 'component.json').read_text(encoding='utf-8'))
    root = scratch / "tools/soundbox"
    for item in manifest['files']:
        src, dest = source / item['path'], root / item['path']
        content = src.read_bytes()
        assert hashlib.sha256(content).hexdigest() == item['sha256'], item['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    def copy_runtime(src, dest):
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
        return dest
    for name in ('python', 'ffmpeg'):
        shutil.copytree(bundle / 'runtime' / name, scratch / 'runtime' / name, copy_function=copy_runtime)
    assert not (root / 'data').exists()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        reserve.listen()
        occupied = reserve.getsockname()[1]
        if occupied > 65400:
            raise ValueError('Allocated test port is too high')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'SOUNDBOX'))}
        env.update(PATH=os.environ['SystemRoot'] + r'\System32', SOUNDBOX_PORT=str(occupied))
        python = str(scratch / 'runtime/python/python.exe')
        isolated = subprocess.run([python, '-I', '-B', '-c', 'import site,sys,json;print(json.dumps({"userSite":site.ENABLE_USER_SITE,"path":sys.path}))'], capture_output=True, check=True, env=env,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        isolation = json.loads(isolated.stdout)
        assert not isolation['userSite']
        assert all(Path(path).is_relative_to(scratch) for path in isolation['path'])
        command = [python, '-I', '-B', str(root / 'launcher.py'), '--no-browser', '--no-dialog']
        def launch():
            result = subprocess.run(command, cwd=scratch, env=env, capture_output=True, timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
            return json.loads(result.stdout.decode('utf-8'))
        first = launch()
        assert first['port'] != occupied and Path(first['root']) == root
        assert Path(first['data_dir']) == root / 'data'
        again = launch()
        assert again['reused'] and again['pid'] == first['pid'] and again['instance'] == first['instance']
    base, session, state = first['url'], 'portable_verify_01', None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, body=None):
        headers = {'X-Soundbox-Client': 'local', 'X-Hub-Session': session}
        if state:
            headers['X-Hub-Control'] = state['controlToken']
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        with opener.open(req, timeout=40) as response:
            return response.read()
    def json_request(path, body=None):
        return json.loads(request(path, body))
    try:
        state = json_request('/api/hub/status')
        assert Path(state['root']) == root and state['pid'] == first['pid']
        json_request('/api/hub/session', {'sessionId': session, 'action': 'heartbeat', 'instanceId': state['instanceId']})
        settings = json_request('/api/settings')
        assert not any(settings[key] for key in ('freesound_key_set', 'elevenlabs_key_set', 'text_api_key_set'))
        assert not settings['ai_translation'] and not (root / 'data/settings.vault').exists()
        sounds = json_request('/api/library?source=builtin')['items']
        assert len(sounds) == 12 and all(sound['source'] == 'builtin' for sound in sounds)
        rendered = json_request('/api/editor/render', {'clips': [{'sound_id': 'builtin-confirm', 'start': 0, 'end': .2}], 'format': 'mp3'})
        assert rendered['format'] == 'mp3' and rendered['duration'] > .1
        assert len(request(rendered['preview_url'])) > 1000
        saved = json_request('/api/editor/save', {'preview_id': rendered['id'], 'name': '便携兼容验证'})
        assert saved['source'] == 'edited'
        assert len(request('/api/audio/' + saved['id'])) > 1000
        result = {'ok': True, 'root': str(root), 'pid': first['pid'], 'port': first['port'],
                  'tests': ['isolated-no-user-site', 'no-host-python-path', 'occupied-port-fallback', 'repeat-launch-reuse', 'fresh-keyless-settings',
                            'twelve-generated-examples', 'real-mp3-render-play-save', 'cooperative-stop']}
    finally:
        if state:
            current = json_request('/api/hub/status')
            assert Path(current['root']) == root
            json_request('/api/hub/session', {'sessionId': session, 'action': 'release', 'instanceId': current['instanceId']})
            current = json_request('/api/hub/status')
            ticket = json_request('/api/hub/control', {'action': 'prepareShutdown', 'instanceId': current['instanceId'], 'revision': current['revision']})
            json_request('/api/hub/control', {'action': 'commitShutdown', 'instanceId': current['instanceId'], 'nonce': ticket['nonce']})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    json_request('/api/health')
                except (OSError, urllib.error.URLError):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Owned portable service did not exit cooperatively')
    (scratch / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle, args.scratch), ensure_ascii=False))
