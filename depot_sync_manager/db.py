# ==================== db.py ====================
"""
Локальный операторский индекс релизов/версий/чанков — SQLite,
`%APPDATA%\\Uploder\\uploder.db` (см. `config.DB_FILE`).

ЧТО ЭТО И ЧТО ЭТО НЕ ТАКОЕ (важно, не перепутать при доработках):
- Это ИНДЕКС на машине оператора, не источник истины и не сервер. Публикация
  на WebDAV (`depot.json`/`manifests/*.json`/`files/**` — компонентный
  протокол, `versions/*.json`/`chunks/**` — chunk-протокол) устроена ровно
  так же, как раньше — эта БД ничего не меняет в том, что реально лежит на
  сервере и что оттуда читает TESL. TESL (клиент у пользователей) с этой БД
  вообще не взаимодействует и не знает о её существовании — она никуда не
  публикуется и не должна.
- Задача этой БД — дать операторским инструментам (в первую очередь
  `recover_from_chunks.py`, дальше — возможно `release_manager.py`, когда
  дойдут руки на мультисборочность) структурированный, запрашиваемый локальный
  слепок того, что видели на сервере: какие версии/релизы существуют, какие
  файлы/чанки в них входят, какие чанки переиспользуются между версиями,
  и (для recover_from_chunks.py конкретно) что было восстановлено и когда.
  Без этой БД та же информация раньше жила только россыпью JSON на WebDAV,
  без возможности что-либо запросить без похода на сервер за каждым файлом.
- Только stdlib (`sqlite3`) — без Qt-зависимости, чтобы `recover_from_chunks.py`
  (CLI без GUI) мог использовать модуль напрямую.

Схема (все таблицы создаются идемпотентно через `init_db()`):
  releases    — одна строка на версию (и компонентного, и chunk-протокола)
  components  — компоненты внутри релиза (Skyrim/MO2p/MO2ext), только для
                компонентного/гибридного протокола; для чистого chunk-формата
                без components — не заполняется
  files       — плоский список файлов (path, size, hash) на версию+компонент
  chunks      — чанки, из которых состоит каждый файл (chunk_id, offset, size);
                один и тот же chunk_id может повторяться в разных
                версиях/файлах — это и есть переиспользование, которое эта
                таблица делает видимым через `chunk_reuse_stats()`
  recovery_runs — журнал запусков recover_from_chunks.py (dry-run и реальных)
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union


SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    version_key   TEXT PRIMARY KEY,
    protocol      TEXT NOT NULL,               -- 'component' | 'chunk' | 'hybrid'
    build_number  INTEGER,
    build_id      TEXT,
    channel       TEXT,
    remote_path   TEXT,
    file_count    INTEGER,
    total_size    INTEGER,
    raw_json      TEXT,                        -- сырой манифест как пришёл — для отладки/аудита
    first_synced_at TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    version_key   TEXT NOT NULL REFERENCES releases(version_key) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    included      INTEGER NOT NULL,
    file_count    INTEGER,
    total_size    INTEGER,
    PRIMARY KEY (version_key, name)
);

CREATE TABLE IF NOT EXISTS files (
    version_key   TEXT NOT NULL REFERENCES releases(version_key) ON DELETE CASCADE,
    component     TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    file_hash     TEXT NOT NULL,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_key, component, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    version_key   TEXT NOT NULL REFERENCES releases(version_key) ON DELETE CASCADE,
    component     TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL,
    chunk_id      TEXT NOT NULL,
    offset        INTEGER NOT NULL,
    size          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_id ON chunks(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version_file ON chunks(version_key, component, path);

CREATE TABLE IF NOT EXISTS recovery_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_key     TEXT,
    remote_path     TEXT,
    mode            TEXT NOT NULL,              -- 'dry-run' | 'recover'
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    files_total     INTEGER,
    files_ok        INTEGER,
    files_failed    INTEGER,
    files_skipped   INTEGER,
    chunks_total    INTEGER,
    chunks_missing  INTEGER,
    out_dir         TEXT,
    notes           TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(path: Union[str, Path, None] = None) -> sqlite3.Connection:
    """Открывает (создавая при необходимости) БД и применяет схему."""
    if path is None:
        from config import DB_FILE
        path = DB_FILE
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Ingest: заносим распарсенный манифест версии в индекс ─────────────────────

def sync_release(
    conn: sqlite3.Connection,
    version_key: str,
    protocol: str,
    entries: List,          # List[FileEntry] — см. recover_from_chunks.py::FileEntry
    raw_manifest: Optional[dict] = None,
    build_number: Optional[int] = None,
    build_id: Optional[str] = None,
    channel: Optional[str] = None,
    remote_path: Optional[str] = None,
) -> None:
    """
    Заносит/обновляет одну версию и все её файлы/чанки. Идемпотентно —
    повторный вызов с теми же данными просто перезаписывает файлы/чанки этой
    версии (DELETE + INSERT), чтобы не плодить дубликаты при повторных `sync`.
    """
    now = _now()
    total_size = sum(e.size for e in entries)

    by_component: Dict[str, List] = {}
    for e in entries:
        by_component.setdefault(e.component or "", []).append(e)

    with conn:
        existing = conn.execute(
            "SELECT first_synced_at FROM releases WHERE version_key = ?", (version_key,)
        ).fetchone()
        first_synced_at = existing["first_synced_at"] if existing else now

        conn.execute(
            """
            INSERT INTO releases
                (version_key, protocol, build_number, build_id, channel, remote_path,
                 file_count, total_size, raw_json, first_synced_at, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_key) DO UPDATE SET
                protocol = excluded.protocol,
                build_number = excluded.build_number,
                build_id = excluded.build_id,
                channel = excluded.channel,
                remote_path = excluded.remote_path,
                file_count = excluded.file_count,
                total_size = excluded.total_size,
                raw_json = excluded.raw_json,
                last_synced_at = excluded.last_synced_at
            """,
            (
                version_key, protocol, build_number, build_id, channel, remote_path,
                len(entries), total_size,
                json.dumps(raw_manifest, ensure_ascii=False) if raw_manifest is not None else None,
                first_synced_at, now,
            ),
        )

        conn.execute("DELETE FROM components WHERE version_key = ?", (version_key,))
        for comp_name, comp_entries in by_component.items():
            if not comp_name:
                continue
            conn.execute(
                """
                INSERT INTO components (version_key, name, included, file_count, total_size)
                VALUES (?, ?, 1, ?, ?)
                """,
                (version_key, comp_name, len(comp_entries), sum(e.size for e in comp_entries)),
            )

        conn.execute("DELETE FROM files WHERE version_key = ?", (version_key,))
        conn.execute("DELETE FROM chunks WHERE version_key = ?", (version_key,))
        for e in entries:
            conn.execute(
                """
                INSERT INTO files (version_key, component, path, size, file_hash, chunk_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (version_key, e.component or "", e.path, e.size, e.file_hash, len(e.chunks)),
            )
            if e.chunks:
                conn.executemany(
                    """
                    INSERT INTO chunks (version_key, component, path, chunk_id, offset, size)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (version_key, e.component or "", e.path, c.chunk_id, c.offset, c.size)
                        for c in e.chunks
                    ],
                )


# ── Recovery run logging ───────────────────────────────────────────────────────

@dataclass
class RecoveryRunResult:
    version_key:    Optional[str] = None
    remote_path:    Optional[str] = None
    mode:           str = "dry-run"   # 'dry-run' | 'recover'
    files_total:    int = 0
    files_ok:       int = 0
    files_failed:   int = 0
    files_skipped:  int = 0
    chunks_total:   int = 0
    chunks_missing: int = 0
    out_dir:        Optional[str] = None
    notes:          str = ""


def start_recovery_run(conn: sqlite3.Connection, version_key: Optional[str], remote_path: Optional[str], mode: str) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO recovery_runs (version_key, remote_path, mode, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (version_key, remote_path, mode, _now()),
        )
        return cur.lastrowid


def finish_recovery_run(conn: sqlite3.Connection, run_id: int, result: RecoveryRunResult) -> None:
    with conn:
        conn.execute(
            """
            UPDATE recovery_runs SET
                finished_at = ?, files_total = ?, files_ok = ?, files_failed = ?,
                files_skipped = ?, chunks_total = ?, chunks_missing = ?,
                out_dir = ?, notes = ?
            WHERE id = ?
            """,
            (
                _now(), result.files_total, result.files_ok, result.files_failed,
                result.files_skipped, result.chunks_total, result.chunks_missing,
                result.out_dir, result.notes, run_id,
            ),
        )


# ── Queries ─────────────────────────────────────────────────────────────────────

def list_releases(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM releases ORDER BY last_synced_at DESC"
    ).fetchall()


def get_release(conn: sqlite3.Connection, version_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM releases WHERE version_key = ?", (version_key,)
    ).fetchone()


def list_files_for_version(conn: sqlite3.Connection, version_key: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files WHERE version_key = ? ORDER BY component, path", (version_key,)
    ).fetchall()


def list_recovery_runs(conn: sqlite3.Connection, version_key: Optional[str] = None) -> List[sqlite3.Row]:
    if version_key:
        return conn.execute(
            "SELECT * FROM recovery_runs WHERE version_key = ? ORDER BY started_at DESC",
            (version_key,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM recovery_runs ORDER BY started_at DESC"
    ).fetchall()


def chunk_reuse_stats(conn: sqlite3.Connection, min_versions: int = 2) -> List[sqlite3.Row]:
    """
    Чанки, встречающиеся в НЕСКОЛЬКИХ разных версиях — переиспользование
    content-addressed хранилища между релизами. Полезно на будущее для
    оценки, сколько реально нового трафика/места требует новая публикация
    поверх уже имеющихся на сервере чанков (мультисборочность, см. CLAUDE.md).
    """
    return conn.execute(
        """
        SELECT chunk_id, COUNT(DISTINCT version_key) AS version_count, MAX(size) AS size
        FROM chunks
        GROUP BY chunk_id
        HAVING version_count >= ?
        ORDER BY version_count DESC
        """,
        (min_versions,),
    ).fetchall()
