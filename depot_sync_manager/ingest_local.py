# ==================== ingest_local.py ====================
"""
Заносит данные о сборке в локальный индекс (`db.py`) СРАЗУ из уже имеющихся
на диске `depot.json` + `versions/<key>.json` — БЕЗ WebDAV, без пароля, без
скачивания единого чанка. Если оба файла уже где-то лежат (выгружены вручную
через root SSH/`cat`, через Google Drive и т.п.), это самый быстрый способ
получить данные о сборке в БД для запросов — не обязательно дожидаться
полного `recover_from_chunks.py` (который реально качает чанки и собирает
файлы на диск).

ВАЖНО: это НЕ восстановление файлов. Ничего не скачивается, никакие
Data/*.esm и т.п. на диск не пишутся — только метаданные (какие файлы есть,
их sha256, какие чанки к ним относятся) идут в БД. Для реальной сборки
файлов из чанков — `recover_from_chunks.py`.

РЕАЛЬНАЯ схема depot.json (подтверждено на живом сервере 2026-09-21):
    {
      "versions": {
        "<version_key>": {
          "build_number": int, "build_id": str, "channel": str,
          "file_count": int, "total_size": int, "chunk_count": int,
          "manifest": "versions/<version_key>.json",
          "components": {"Skyrim": {"included": bool}, "MO2p": {...}, "MO2ext": {...}}
        }
      },
      "current": {channel: version_key} | {}   # пусто, пока версия не активирована
    }
Схема `versions/<key>.json` — см. `recover_from_chunks.py::parse_version_manifest()`
(тот же парсер переиспользуется здесь, не продублирован).

ИСПОЛЬЗОВАНИЕ:

    # Есть ОБА файла локально — полный sync (files+chunks+components):
    python ingest_local.py --depot-json depot.json --version-json v1_20eb01df.json

    # Есть ТОЛЬКО depot.json (большой файл версии ещё не скачан/не готов) —
    # заносим что знаем (агрегаты: build_number/build_id/channel/file_count/
    # total_size/chunk_count/компоненты) без файлов/чанков. Позже, когда
    # появится версия-файл, обычный вызов выше ДОПОЛНИТ эту запись полными
    # данными, не потеряв то, что уже было известно.
    python ingest_local.py --depot-json depot.json

Ключ версии, если не задан явно `--version-key`, берётся из `depot.json`
(единственная запись в `versions`, или через `current`, если он уже
непустой) — та же логика, что в `recover_from_chunks.py::resolve_version()`,
только без сети.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as _db
from recover_from_chunks import parse_version_manifest


def _pick_version(depot: dict, version_key: Optional[str]):
    """Возвращает (version_key, version_meta_или_None) из depot.json."""
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


def main():
    parser = argparse.ArgumentParser(
        description="Заносит данные о сборке в локальный индекс (db.py) из уже скачанных файлов, без сети.",
    )
    parser.add_argument("--depot-json", default=None, help="Путь к локальной копии depot.json")
    parser.add_argument("--version-json", default=None, help="Путь к локальной копии versions/<key>.json")
    parser.add_argument("--version-key", default=None, help="Ключ версии, если не удаётся/не нужно брать из depot.json")
    parser.add_argument("--remote-path", default=None, help="Путь сборки на WebDAV — только для справки в записи, не используется для сети")
    parser.add_argument("--db-path", default=None, help="Свой путь к файлу SQLite вместо %%APPDATA%%/Uploder/uploder.db")
    args = parser.parse_args()

    if not args.depot_json and not args.version_json:
        parser.error("нужен хотя бы один из --depot-json / --version-json")

    depot = None
    if args.depot_json:
        depot = json.loads(Path(args.depot_json).read_text(encoding="utf-8"))

    version_key = args.version_key
    version_meta = None
    if depot is not None:
        version_key, version_meta = _pick_version(depot, version_key)

    if not version_key and args.version_json:
        version_key = Path(args.version_json).stem
        print(f"ℹ️ Ключ версии взят из имени файла версии: {version_key}")

    if not version_key:
        print("❌ Не удалось определить ключ версии — укажи --version-key явно")
        sys.exit(1)

    conn = _db.init_db(args.db_path)
    try:
        if args.version_json:
            version_data = json.loads(Path(args.version_json).read_text(encoding="utf-8"))
            try:
                entries = parse_version_manifest(version_data)
            except ValueError as e:
                print(f"❌ {e}")
                sys.exit(1)

            protocol = "hybrid" if any(e.component for e in entries) else "chunk"
            meta = version_meta or {}
            _db.sync_release(
                conn, version_key=version_key, protocol=protocol, entries=entries,
                raw_manifest=version_data, remote_path=args.remote_path,
                build_number=meta.get("build_number"),
                build_id=meta.get("build_id"),
                channel=meta.get("channel"),
                declared_chunk_count=meta.get("chunk_count"),
            )

            rel = _db.get_release(conn, version_key)
            print(f"✅ Полный sync: {rel['file_count']} файлов, {rel['total_size']:,} байт, версия {version_key}")

            if version_meta:
                declared_files = version_meta.get("file_count")
                if declared_files is not None and declared_files != rel["file_count"]:
                    print(
                        f"⚠️ depot.json заявляет {declared_files} файлов, в версии-манифесте "
                        f"разобрано {rel['file_count']} — расхождение, проверь файл версии на полноту"
                    )
                declared_chunks = version_meta.get("chunk_count")
                if declared_chunks is not None:
                    actual_unique_chunks = conn.execute(
                        "SELECT COUNT(DISTINCT chunk_id) AS c FROM chunks WHERE version_key = ?",
                        (version_key,),
                    ).fetchone()["c"]
                    if declared_chunks != actual_unique_chunks:
                        print(
                            f"⚠️ depot.json заявляет {declared_chunks} уникальных чанков, "
                            f"в манифесте — {actual_unique_chunks} — расхождение, проверь файл версии на полноту"
                        )

        elif depot is not None and version_meta is not None:
            # Только depot.json — заносим агрегаты без files/chunks (см.
            # докстринг sync_release про files_synced=0 vs 1).
            components_meta = {
                name: bool(info.get("included", True))
                for name, info in (version_meta.get("components") or {}).items()
            }
            protocol = "hybrid" if components_meta else "chunk"
            _db.sync_release(
                conn, version_key=version_key, protocol=protocol, entries=None,
                remote_path=args.remote_path,
                build_number=version_meta.get("build_number"),
                build_id=version_meta.get("build_id"),
                channel=version_meta.get("channel"),
                declared_file_count=version_meta.get("file_count"),
                declared_total_size=version_meta.get("total_size"),
                declared_chunk_count=version_meta.get("chunk_count"),
                components_meta=components_meta or None,
            )
            rel = _db.get_release(conn, version_key)
            print(
                f"✅ Занесены агрегаты из depot.json (без файлов/чанков — версия-манифест не передан): "
                f"{rel['file_count']} файлов, {rel['total_size']:,} байт, версия {version_key}"
            )
            print(
                "ℹ️ Когда появится versions/<key>.json — перезапусти с --version-json, "
                "это дополнит запись полными files/chunks, не потеряв уже занесённое."
            )
        else:
            print("❌ Нечего заносить — задан только --depot-json, но версия в нём не найдена")
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
