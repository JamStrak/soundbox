"""SQLite-backed personal library. Audio paths never come from a browser."""
from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Library:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.media = self.directory / 'media'
        self.media.mkdir(exist_ok=True)
        self.path = self.directory / 'library.sqlite3'
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sounds (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, media_path TEXT
                );
                CREATE TABLE IF NOT EXISTS folders (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    parent_id TEXT REFERENCES folders(id)
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self) -> list[dict]:
        with self.connection() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM sounds')]

    def get(self, sound_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT data FROM sounds WHERE id=?', (sound_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def file(self, sound_id: str) -> Path | None:
        with self.connection() as db:
            row = db.execute('SELECT media_path FROM sounds WHERE id=?', (sound_id,)).fetchone()
        if not row or not row['media_path']:
            return None
        path = (self.media / row['media_path']).resolve()
        return path if path.is_relative_to(self.media) and path.is_file() else None

    def put(self, sound: dict, file: Path | None = None) -> dict:
        with self.lock, self.connection() as db:
            previous = db.execute('SELECT media_path FROM sounds WHERE id=?', (sound['id'],)).fetchone()
            relative = str(file.resolve().relative_to(self.media)) if file else (previous['media_path'] if previous else None)
            db.execute('INSERT OR REPLACE INTO sounds(id,data,media_path) VALUES (?,?,?)',
                       (sound['id'], json.dumps(sound, ensure_ascii=False), relative))
        return sound

    def update(self, sound_id: str, changes: dict) -> dict:
        with self.lock:
            sound = self.get(sound_id)
            if sound is None:
                raise ValueError('这条音效已不在素材库中。请刷新列表。')
            sound.update(changes)
            return self.put(sound)

    def remove(self, sound_id: str):
        with self.lock, self.connection() as db:
            db.execute('DELETE FROM sounds WHERE id=?', (sound_id,))
        # The cached media is intentionally retained for recovery, never touch user's original file.

    def folders(self) -> list[dict]:
        sounds = self.all()
        with self.connection() as db:
            return [dict(row) | {'count': sum(s.get('folder_id') == row['id'] for s in sounds)}
                    for row in db.execute('SELECT * FROM folders ORDER BY rowid')]

    def check_folder(self, folder_id):
        if folder_id is not None and folder_id not in {f['id'] for f in self.folders()}:
            raise ValueError('目标文件夹不存在，请重新选择。')

    def add_folder(self, name: str, parent_id=None) -> dict:
        with self.lock, self.connection() as db:
            self.check_folder(parent_id)
            if any(f['name'].casefold() == name.casefold() and f['parent_id'] == parent_id for f in self.folders()):
                raise ValueError('同一层级已存在这个文件夹名称。')
            folder = {'id': uuid.uuid4().hex, 'name': name, 'parent_id': parent_id}
            db.execute('INSERT INTO folders(id,name,parent_id) VALUES (:id,:name,:parent_id)', folder)
        return folder | {'count': 0}

    def update_folder(self, folder_id: str, changes: dict) -> dict:
        with self.lock:
            folders = {f['id']: f for f in self.folders()}
            if folder_id not in folders:
                raise ValueError('文件夹不存在，请刷新。')
            folder = folders[folder_id] | changes
            self.check_folder(folder['parent_id'])
            ancestor = folder['parent_id']
            while ancestor:
                if ancestor == folder_id:
                    raise ValueError('不能把文件夹放进自己或自己的子文件夹。')
                ancestor = folders[ancestor]['parent_id']
            if any(f['id'] != folder_id and f['name'].casefold() == folder['name'].casefold()
                   and f['parent_id'] == folder['parent_id'] for f in folders.values()):
                raise ValueError('同一层级已存在这个文件夹名称。')
            with self.connection() as db:
                db.execute('UPDATE folders SET name=?,parent_id=? WHERE id=?',
                           (folder['name'], folder['parent_id'], folder_id))
            return folder

    def remove_folder(self, folder_id: str):
        with self.lock:
            folders = self.folders()
            if folder_id not in {f['id'] for f in folders}:
                raise ValueError('文件夹不存在。')
            ids = {folder_id}
            while True:
                expanded = ids | {f['id'] for f in folders if f['parent_id'] in ids}
                if expanded == ids:
                    break
                ids = expanded
            with self.connection() as db:
                for sound in self.all():
                    if sound.get('folder_id') in ids:
                        sound['folder_id'] = None
                        db.execute('UPDATE sounds SET data=? WHERE id=?',
                                   (json.dumps(sound, ensure_ascii=False), sound['id']))
                # Break links in one transaction before deleting the subtree.
                db.executemany('UPDATE folders SET parent_id=NULL WHERE id=?', [(i,) for i in ids])
                db.executemany('DELETE FROM folders WHERE id=?', [(i,) for i in ids])

    def seeded(self) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM meta WHERE key='seed-v1'").fetchone() is not None

    def mark_seeded(self):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('seed-v1','1')")
