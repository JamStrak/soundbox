"""Locally protected provider credentials; never serialized into API responses."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    'freesound_key': '', 'freesound_client_id': '', 'freesound_access_token': '',
    'freesound_refresh_token': '', 'freesound_expires_at': 0,
    'elevenlabs_key': '', 'elevenlabs_license': 'unknown',
    'text_api_key': '', 'text_api_base': '', 'text_model': '', 'ai_translation': False,
}


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(payload: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        return payload
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'Soundbox local settings', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise OSError('无法读取或保存本机加密设置，请使用创建设置的 Windows 账户运行。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class Settings:
    def __init__(self, directory: Path):
        self.path = directory / 'settings.vault'
        self.lock = threading.RLock()
        self.values = DEFAULTS.copy()
        if self.path.exists():
            saved = json.loads(protect(base64.b64decode(self.path.read_bytes()), decrypt=True))
            self.values.update({k: v for k, v in saved.items() if k in DEFAULTS})

    def get(self) -> dict:
        with self.lock:
            return self.values.copy()

    def update(self, changes: dict):
        with self.lock:
            values = self.values | {k: v for k, v in changes.items() if k in DEFAULTS}
            tmp = self.path.with_suffix('.tmp')
            tmp.write_bytes(base64.b64encode(protect(json.dumps(values).encode())))
            if os.name != 'nt':
                tmp.chmod(0o600)
            tmp.replace(self.path)
            self.values = values

    def public(self) -> dict:
        values = self.get()
        return {
            'freesound_key_set': bool(values['freesound_key']),
            'freesound_client_id': values['freesound_client_id'],
            'freesound_oauth_connected': bool(values['freesound_refresh_token']),
            'elevenlabs_key_set': bool(values['elevenlabs_key']),
            'elevenlabs_license': values['elevenlabs_license'],
            'text_api_key_set': bool(values['text_api_key']),
            'text_api_base': values['text_api_base'], 'text_model': values['text_model'],
            'ai_translation': values['ai_translation'], 'data_dir': str(self.path.parent),
        }
