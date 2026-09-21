# ==================== build_manifest_db.py ====================
"""
Одноразовый конвертор: depot.json + versions/<key>.json (уже скачанные
локально) → компактный `manifest.db` (SQLite) — файл, предназначенный для
ПУБЛИКАЦИИ на WebDAV и чтения ЛАУНЧЕРОМ (TESL), а не операторский индекс
(тот — отдельно, см. `db.py`/`ingest_local.py`, живёт в `%APPDATA%\\Uploder`
и никуда не публикуется).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ ОТ `db.py`/`uploder.db`:
- `uploder.db` — операторский, может хранить несколько версий, сырой JSON
  каждой (`raw_json`) и журнал `recovery_runs` — раздувать это до размера
  реального 77МБ-манифеста и публиковать пользователям смысла нет.
- `manifest.db` — публичный, ОДНА версия на файл (как сегодня
  `manifests/<version_key>.json`), только то, что реально нужно клиенту:
  список файлов (путь/компонент/размер/хэш) и чанков (id/offset/size) для
  их сборки. SQLite вместо JSON — потому что лаунчеру не нужно парсить и
  держать в памяти 77МБ JSON на каждый запуск: `sqlite3` (stdlib и в
  Python, и потенциально в PyInstaller-сборке без доп. зависимостей)
  позволяет делать точечные запросы по файлу/чанку без полной загрузки.

ПЛАН ПОСЛЕ ЭТОГО КОНВЕРТОРА (см. TESL/CLAUDE.md "Chunk-протокол на стороне
лаунчера"): TESL публикует/скачивает `manifest.db` вместо/вместе с
`manifests/<key>.json`, качает нужные чанки из `chunks/<xx>/<id>` (уже
лежат на сервере — ничего пересобирать и перезаливать не нужно) и собирает
файлы сам, тем же chunk-fetch-assemble-verify циклом, что уже есть в
`launcher_client.py`/`recover_from_chunks.py`.

СХЕМА `manifest.db`:
  meta    — одна строка: version_key/app_id/build_number/build_id/channel/
            created_at/file_count/total_size/chunk_count
  files   — path, component, size, file_hash (PRIMARY KEY component+path)
  chunks  — component, path, chunk_id, offset, size (по чанку на строку;
            один и тот же chunk_id может повторяться для разных файлов —
            дедуп на стороне лаунчера при скачивании, не здесь)

ИСПОЛЬЗОВАНИЕ:
    python build_manifest_db.py \\
        --depot-json depot.json \\
        --version-json v1_20eb01df.json \\
        --out manifest_v1_20eb01df.db

    # ключ версии, если не задан явно, берётся из depot.json (та же логика,
    # что в ingest_local.py/recover_from_chunks.py::resolve_version()) или
    # из имени файла версии.

Дальше `manifest_v1_20eb01df.db` заливается на WebDAV вручную (или через
`server_files_tab.py`/будущую автоматизацию) рядом с `chunks/`/`versions/` —
это ПОСЛЕДНИЙ ручной шаг, дальше лаунчер сам находит и читает этот файл
(см. TESL::core/depot_client.py, новый метод).
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recover_from_chunks import parse_version_manifest


MANIFEST_DB_SCHEMA = """
CREATE TABLE meta (
    version_key   TEXT PRIMARY KEY,
    app_id        TEXT,
    build_number  INTEGER,
    build_id      TEXT,
    channel       TEXT,
    created_at    TEXT,
    generated_at  TEXT NOT NULL,
    file_count    INTEGER NOT NULL,
    total_size    INTEGER NOT NULL,
    chunk_count   INTEGER
);

CREATE TABLE files (
    component     TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    file_hash     TEXT NOT NULL,
    PRIMARY KEY (component, path)
);

CREATE TABLE chunks (
    component     TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL,
    chunk_id      TEXT NOT NULL,
    offset        INTEGER NOT NULL,
    size          INTEGER NOT NULL
);
CREATE INDEX idx_chunks_file ON chunks(component, path);
CREATE INDEX idx_chunks_chunk_id ON chunks(chunk_id);
"""


def _pick_version(depot: dict, version_key: Optional[str]):
    versions = depot.get("versions")
    if not isinstance(versions, dict) or not versions:
        return version_key, None
    if version_key:
        return version_key, versions.get(version_key)
    current = depot.get("current") or {}
    if isinstance(current, dict) and current:
        key = next(iter(current.values()))
        return key, versions.get(key)
    if len(versions) == 1:
        key = next(iter(versions.keys()))
        print(f"ℹ️ 'current' в depot.json пуст — взята единственная версия: {key}")
        return key, versions[key]
    print(f"❌ В depot.json несколько версий без активного 'current' ({list(versions.keys())}) — укажи --version-key явно")
    sys.exit(1)


def build_manifest_db(depot_json_path: Optional[str], version_json_path: str,
                       out_path: str, version_key: Optional[str] = None) -> None:
    version_data = json.loads(Path(version_json_path).read_text(encoding="utf-8"))

    depot = None
    version_meta = None
    if depot_json_path:
        depot = json.loads(Path(depot_json_path).read_text(encoding="utf-8"))
        version_key, version_meta = _pick_version(depot, version_key)

    if not version_key:
        version_key = Path(version_json_path).stem
        print(f"ℹ️ Ключ версии взят из имени файла версии: {version_key}")

    entries = parse_version_manifest(version_data)
    if not entries:
        print("❌ В манифесте версии не нашлось ни одного файла — нечего конвертировать")
        sys.exit(1)

    meta = version_meta or {}
    total_size = sum(e.size for e in entries)

    out = Path(out_path)
    if out.exists():
        out.unlink()  # sqlite3.connect не пересоздаёт файл с нуля сам

    conn = sqlite3.connect(str(out))
    try:
        conn.executescript(MANIFEST_DB_SCHEMA)
        conn.execute(
            """
            INSERT INTO meta (version_key, app_id, build_number, build_id, channel,
                               created_at, generated_at, file_count, total_size, chunk_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_key,
                (depot or {}).get("app_id"),
                meta.get("build_number"),
                meta.get("build_id"),
                meta.get("channel"),
                meta.get("created_at"),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                len(entries),
                total_size,
                meta.get("chunk_count"),
            ),
        )

        conn.executemany(
            "INSERT INTO files (component, path, size, file_hash) VALUES (?, ?, ?, ?)",
            [(e.component or "", e.path, e.size, e.file_hash) for e in entries],
        )

        chunk_rows = [
            (e.component or "", e.path, c.chunk_id, c.offset, c.size)
            for e in entries
            for c in e.chunks
        ]
        conn.executemany(
            "INSERT INTO chunks (component, path, chunk_id, offset, size) VALUES (?, ?, ?, ?, ?)",
            chunk_rows,
        )
        conn.commit()
    finally:
        conn.close()

    unique_chunks = len({c.chunk_id for e in entries for c in e.chunks})
    db_size = out.stat().st_size
    print(
        f"✅ {out_path}: {len(entries)} файлов, {total_size:,} байт исходной сборки, "
        f"{len(chunk_rows)} строк чанков ({unique_chunks} уникальных), "
        f"сам файл БД — {db_size:,} байт"
    )
    if version_meta:
        declared_files = version_meta.get("file_count")
        if declared_files is not None and declared_files != len(entries):
            print(
                f"⚠️ depot.json заявляет {declared_files} файлов, в версии-манифесте "
                f"разобрано {len(entries)} — проверь файл версии на полноту перед публикацией"
            )
        declared_chunks = version_meta.get("chunk_count")
        if declared_chunks is not None and declared_chunks != unique_chunks:
            print(
                f"⚠️ depot.json заявляет {declared_chunks} уникальных чанков, "
                f"в манифесте — {unique_chunks} — проверь файл версии на полноту перед публикацией"
            )


def main():
    parser = argparse.ArgumentParser(
        description="Одноразовый конвертор depot.json+versions/<key>.json -> компактный manifest.db для лаунчера.",
    )
    parser.add_argument("--depot-json", default=None, help="Путь к локальной копии depot.json (для build_number/build_id/channel/app_id)")
    parser.add_argument("--version-json", required=True, help="Путь к локальной копии versions/<key>.json")
    parser.add_argument("--version-key", default=None, help="Ключ версии, если не задан/не найден в depot.json")
    parser.add_argument("--out", required=True, help="Куда писать результат (manifest_<key>.db)")
    args = parser.parse_args()

    build_manifest_db(args.depot_json, args.version_json, args.out, args.version_key)


if __name__ == "__main__":
    main()
