# ==================== publish_packed.py ====================
"""
CLI-обёртка для публикации через TESL-Panel с упаковкой чанков
(DepotSyncManager.execute_sync_packed) — без GUI, поскольку вкладка
этого протокола (depot_tab.py) ещё не подключена к MainWindow.tabs (см.
CLAUDE.md "Chunk-based протокол"). Прямой способ проверить упаковку +
Range-GET на реальном сервере, не дожидаясь GUI-интеграции.

Использование:
  python publish_packed.py ^
      --local-dir "P:\путь\к\папке" ^
      --panel-url https://tesl-panel.neth.de5.net ^
      --project TESVAE ^
      --token <upload-токен из вывода infra/deploy.sh> ^
      --app-id tesvae --channel stable

Повторный запуск с теми же аргументами — инкрементальная публикация
(только новые/изменённые файлы, delta считается как обычно). Требует
PyQt6+requests (уже есть, если TESL-Manager запускается из исходников —
requirements.txt этого репозитория).
"""
import argparse
import sys
from pathlib import Path

from chunk_manager import ChunkManager, DepotManifest, DEFAULT_CHUNK_SIZE
from depot_sync_manager import DepotSyncManager


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local-dir", required=True, help="Локальная папка для публикации")
    ap.add_argument("--panel-url", required=True, help="Например https://tesl-panel.neth.de5.net")
    ap.add_argument("--project", required=True, help="Имя сборки на панели (см. /admin) — создаётся, если ещё не существует")
    ap.add_argument("--token", required=True, help="Upload-токен панели")
    ap.add_argument("--app-id", default="app")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--depot-id", type=int, default=1)
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    ap.add_argument("--pack-size", type=int, default=None, help="Байт на один pack-файл (по умолчанию 256MB)")
    ap.add_argument("--exclude", action="append", default=[], help="Можно указать несколько раз")
    ap.add_argument("--no-verify-ssl", action="store_true")
    args = ap.parse_args()

    local_dir = Path(args.local_dir)
    if not local_dir.is_dir():
        print(f"❌ Не найдена папка: {local_dir}", file=sys.stderr)
        return 1

    depot_cfg = {
        "app_id": args.app_id, "depot_id": args.depot_id,
        "channel": args.channel, "chunk_size": args.chunk_size,
    }
    if args.pack_size:
        depot_cfg["pack_size"] = args.pack_size

    # Панель адресует сборки по id (UUID), не по имени, см. TESL-Panel's
    # builds_db.py — --project здесь остаётся именем ради удобства CLI,
    # резолвится/создаётся через /api/builds перед публикацией.
    from panel_client import PanelHTTP
    lookup = PanelHTTP(
        base_url=args.panel_url, build_id="", token=args.token,
        verify_ssl=not args.no_verify_ssl,
    )
    build = next((b for b in lookup.list_builds() if b["name"] == args.project), None)
    if build is None:
        print(f"ℹ️  Сборка «{args.project}» не найдена на панели — создаю…")
        build, err = lookup.create_build(args.project)
        if build is None:
            print(f"❌ Не удалось создать сборку: {err}", file=sys.stderr)
            return 1
    lookup.close()
    build_id = build["id"]
    print(f"📦 Сборка: {args.project} ({build_id[:8]})")

    config = {
        "backend": "panel",
        "panel": {
            "base_url": args.panel_url,
            "build_id": build_id,
            "token": args.token,
            "verify_ssl": not args.no_verify_ssl,
        },
        "use_packs": True,
        "depot": depot_cfg,
        "local_dir": str(local_dir),
        "excludes": args.exclude,
    }

    sync = DepotSyncManager(config)
    sync.log.connect(print)
    sync.progress.connect(lambda done, total, msg: print(f"[{done}/{total}] {msg}"))

    print("🔌 Проверяем соединение с панелью…")
    ok, msg = sync.test_connection()
    if not ok:
        print(f"❌ {msg}", file=sys.stderr)
        return 1
    print(f"✅ {msg}")

    if not sync.ensure_depot_structure():
        print("❌ Не удалось создать структуру депо", file=sys.stderr)
        return 1

    print("📥 Загружаем текущий манифест с сервера…")
    prev_manifest = sync.fetch_remote_manifest()
    if prev_manifest:
        print(
            f"📋 Сервер: build #{prev_manifest.build_number} ({prev_manifest.build_id}), "
            f"файлов: {len(prev_manifest.files)}, размер: {prev_manifest.human_size()}"
        )
    else:
        print("ℹ️  Манифест не найден на панели — первая публикация в этот проект")

    remote_chunks = sync.fetch_remote_chunk_ids(prev_manifest)

    cm = ChunkManager(chunk_size=args.chunk_size)
    cm.log.connect(print)
    # Прогресс сканирования печатаем не на каждый файл (может быть их
    # тысячи) — только каждые 200-й и последний, тот же принцип, что и у
    # остальных длинных операций в этом репозитории (не заспамить stdout).
    cm.progress.connect(
        lambda cur, total, m: print(f"[{cur}/{total}] {m}") if cur % 200 == 0 or cur == total else None
    )

    print(f"📁 Сканируем: {local_dir}")
    entries, stats = cm.scan_directory(local_dir, excludes=args.exclude, prev_manifest=prev_manifest)
    print(f"📊 {stats}")
    if not entries:
        print("Нет файлов для загрузки")
        return 0

    manifest = DepotManifest(
        app_id=args.app_id, depot_id=args.depot_id, channel=args.channel,
        chunk_size=args.chunk_size,
        build_number=(prev_manifest.build_number + 1) if prev_manifest else 1,
    )
    manifest.files = entries
    manifest.excludes = args.exclude
    manifest.local_dir = str(local_dir)

    prev_entries = prev_manifest.files if prev_manifest else {}
    delta = cm.compute_delta(entries, prev_entries, remote_chunks)
    print(f"📋 Delta: {delta.summary()}")

    ok, msg = sync.execute_sync_packed(manifest, delta, str(local_dir))
    print(msg)
    sync.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
