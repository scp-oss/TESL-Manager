# ==================== release_manager.py ====================
"""
Release Manager — система версий для publisher (Uploder).

Изменения v2:
  • Компонентный релиз: Skyrim / MO2p / MO2ext как независимые компоненты.
  • Манифест хранит секцию "components" — лаунчер знает что скачивать.
  • Если компонент не включён (included: false) — клиент оставляет старые файлы.
  • Фикс # в именах файлов: NextcloudDAV.url() кодирует сегменты пути через
    urllib.parse.quote(seg, safe="") — символ # → %23.
"""

import json
import hashlib
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from PyQt6.QtCore import QObject, pyqtSignal

from depot_sync_manager import NextcloudDAV, DepotSyncManager, TIMEOUT_META


# ── Константы ─────────────────────────────────────────────────────────────────

DEPOT_JSON      = "depot.json"
MANIFESTS_DIR   = "manifests"
FILES_DIR       = "files"          # корень файлов сборки
COMPAT_MANIFEST = "manifest.json"  # копия для обратной совместимости

# Имена компонентов (совпадают с подпапками в files/)
COMPONENT_NAMES = ["Skyrim", "MO2p", "MO2ext"]


# ── Release ───────────────────────────────────────────────────────────────────

class Release:
    """Один релиз (версия) депо."""

    def __init__(
        self,
        label:        str,
        build_id:     str,
        build_number: int,
        channel:      str,
        created_at:   str,
        file_count:   int,
        total_size:   int,
        manifest_path: str,
        notes:        str = "",
        components:   Optional[Dict] = None,  # {"Skyrim": {"included": True, ...}}
    ):
        self.label         = label
        self.build_id      = build_id
        self.build_number  = build_number
        self.channel       = channel
        self.created_at    = created_at
        self.file_count    = file_count
        self.total_size    = total_size
        self.manifest_path = manifest_path
        self.notes         = notes
        self.components    = components or {}

    @property
    def version_key(self) -> str:
        return f"v{self.build_number}_{self.build_id}"

    def to_dict(self) -> dict:
        d = {
            "label":        self.label,
            "build_id":     self.build_id,
            "build_number": self.build_number,
            "channel":      self.channel,
            "created_at":   self.created_at,
            "file_count":   self.file_count,
            "total_size":   self.total_size,
            "manifest":     self.manifest_path,
            "notes":        self.notes,
        }
        if self.components:
            d["components"] = self.components
        return d

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "Release":
        return cls(
            label         = d.get("label", key),
            build_id      = d.get("build_id", ""),
            build_number  = d.get("build_number", 1),
            channel       = d.get("channel", "stable"),
            created_at    = d.get("created_at", ""),
            file_count    = d.get("file_count", 0),
            total_size    = d.get("total_size", 0),
            manifest_path = d.get("manifest", ""),
            notes         = d.get("notes", ""),
            components    = d.get("components", {}),
        )

    def human_size(self) -> str:
        s = self.total_size
        for u in ("B", "KB", "MB", "GB", "TB"):
            if s < 1024:
                return f"{s:.1f} {u}"
            s /= 1024
        return f"{s:.1f} TB"

    def __repr__(self):
        return f"<Release {self.version_key} [{self.channel}] {self.human_size()}>"


# ── DepotIndex ────────────────────────────────────────────────────────────────

class DepotIndex:
    def __init__(self, app_id: str = ""):
        self.app_id   = app_id
        self.current: Dict[str, str] = {}
        self.versions: Dict[str, Release] = {}

    def set_current(self, channel: str, version_key: str):
        self.current[channel] = version_key

    def add_release(self, release: Release):
        self.versions[release.version_key] = release

    def get_current(self, channel: str = "stable") -> Optional[Release]:
        key = self.current.get(channel)
        return self.versions.get(key) if key else None

    def get_all_sorted(self) -> List[Release]:
        return sorted(self.versions.values(), key=lambda r: r.build_number, reverse=True)

    def get_by_channel(self, channel: str) -> List[Release]:
        return [r for r in self.get_all_sorted() if r.channel == channel]

    def to_dict(self) -> dict:
        return {
            "app_id":   self.app_id,
            "current":  self.current,
            "versions": {k: v.to_dict() for k, v in self.versions.items()},
            "updated_at": datetime.now().isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "DepotIndex":
        idx = cls(app_id=d.get("app_id", ""))
        idx.current = d.get("current", {})
        for key, v in d.get("versions", {}).items():
            idx.versions[key] = Release.from_dict(key, v)
        return idx

    @classmethod
    def from_json(cls, text: str) -> "DepotIndex":
        return cls.from_dict(json.loads(text))

    @property
    def next_build_number(self) -> int:
        if not self.versions:
            return 1
        return max(r.build_number for r in self.versions.values()) + 1


# ── ReleaseBuilder ────────────────────────────────────────────────────────────

class ReleaseBuilder(QObject):
    """
    Строит компонентный манифест из трёх локальных папок.

    Формат манифеста:
    {
      "version":      "v2.1.0",
      "build_id":     "e5f6a7b8",
      "build_number": 2,
      "created_at":   "...",
      "channel":      "stable",
      "notes":        "...",
      "components": {
        "Skyrim": {
          "included": true,
          "file_count": 1234,
          "total_size": 12884901888,
          "files": { "SkyrimSE.exe": "sha256:...", ... }
        },
        "MO2p": {
          "included": false,
          "file_count": null,
          "total_size": null,
          "files": null
        },
        "MO2ext": {
          "included": true,
          "file_count": 890,
          "total_size": 4294967296,
          "files": { "mods/ModName/plugin.esp": "sha256:...", ... }
        }
      },
      "files": { ... }   <- плоский список всех файлов для обратной совместимости
    }
    """

    progress = pyqtSignal(int, int, str)
    log      = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._stop = False

    def stop(self):
        self._stop = True

    def build_manifest(
        self,
        components_cfg: Dict,   # {"Skyrim": {"local_dir": "...", "included": True, "excludes": [...]}, ...}
        label:          str,
        build_number:   int,
        channel:        str,
        notes:          str = "",
    ) -> Tuple[Optional[dict], Optional[Release]]:
        """
        Сканирует все включённые компоненты и строит манифест.
        Возвращает (manifest_dict, Release).
        """
        self._stop = False
        build_id   = uuid.uuid4().hex[:8]
        created_at = datetime.now().isoformat()

        components_manifest = {}
        all_files_flat: Dict[str, str] = {}   # path → sha256 (плоский, с префиксом компонента)
        total_files = 0
        total_size  = 0

        # Предварительный подсчёт файлов для прогресса
        total_to_scan = 0
        for comp_name in COMPONENT_NAMES:
            comp = components_cfg.get(comp_name, {})
            if comp.get("included", True) and comp.get("local_dir"):
                local_dir = Path(comp["local_dir"])
                if local_dir.exists():
                    for _ in local_dir.rglob("*"):
                        if _.is_file():
                            total_to_scan += 1

        scanned = 0
        self.log.emit(f"📁 Файлов для сканирования: {total_to_scan}")

        for comp_name in COMPONENT_NAMES:
            comp = components_cfg.get(comp_name, {})
            included   = comp.get("included", True)
            local_dir  = comp.get("local_dir", "")
            excludes   = comp.get("excludes", [])

            if not included:
                self.log.emit(f"⏭ {comp_name}: пропущен (не включён в релиз)")
                components_manifest[comp_name] = {
                    "included":   False,
                    "file_count": None,
                    "total_size": None,
                    "files":      None,
                }
                continue

            if not local_dir:
                self.log.emit(f"⚠️ {comp_name}: папка не задана — пропускаем")
                components_manifest[comp_name] = {
                    "included":   False,
                    "file_count": None,
                    "total_size": None,
                    "files":      None,
                }
                continue

            local_path = Path(local_dir)
            if not local_path.exists():
                self.log.emit(f"❌ {comp_name}: папка не существует: {local_dir}")
                components_manifest[comp_name] = {
                    "included":   False,
                    "file_count": None,
                    "total_size": None,
                    "files":      None,
                }
                continue

            self.log.emit(f"🔍 {comp_name}: сканируем {local_dir}")
            if excludes:
                self.log.emit(f"   Исключений: {len(excludes)}: {', '.join(excludes[:5])}"
                              + (f" и ещё {len(excludes)-5}" if len(excludes) > 5 else ""))

            # Собираем файлы компонента
            comp_files: List[Path] = []
            for root, dirs, files in os.walk(local_path):
                dirs[:] = [d for d in dirs
                           if not self._should_exclude(Path(root) / d, excludes)]
                for f in files:
                    fp = Path(root) / f
                    if not self._should_exclude(fp, excludes):
                        comp_files.append(fp)

            comp_files_dict: Dict[str, str] = {}
            comp_size = 0

            for fp in comp_files:
                if self._stop:
                    self.log.emit("⏹ Прервано")
                    return None, None

                rel = str(fp.relative_to(local_path)).replace("\\", "/")
                # Путь в манифесте: CompName/rel/path
                manifest_path = f"{comp_name}/{rel}"

                scanned += 1
                self.progress.emit(scanned, total_to_scan, f"{comp_name}: {rel}")

                try:
                    file_hash = self._hash_file(fp)
                    size      = fp.stat().st_size
                    comp_files_dict[rel] = f"sha256:{file_hash}"
                    all_files_flat[manifest_path] = f"sha256:{file_hash}"
                    comp_size  += size
                    total_size += size
                except Exception as e:
                    self.log.emit(f"⚠️ {comp_name}/{rel}: {e}")

            total_files += len(comp_files_dict)
            self.log.emit(
                f"✅ {comp_name}: {len(comp_files_dict)} файлов, "
                f"{_human_size(comp_size)}"
            )

            components_manifest[comp_name] = {
                "included":   True,
                "file_count": len(comp_files_dict),
                "total_size": comp_size,
                "files":      comp_files_dict,
            }

        manifest = {
            "version":      label,
            "build_id":     build_id,
            "build_number": build_number,
            "created_at":   created_at,
            "channel":      channel,
            "notes":        notes,
            "components":   components_manifest,
            "files":        all_files_flat,   # плоский список для совместимости
        }

        version_key = f"v{build_number}_{build_id}"
        release = Release(
            label         = label,
            build_id      = build_id,
            build_number  = build_number,
            channel       = channel,
            created_at    = created_at,
            file_count    = total_files,
            total_size    = total_size,
            manifest_path = f"{MANIFESTS_DIR}/{version_key}.json",
            notes         = notes,
            components    = {
                name: {"included": components_manifest[name]["included"]}
                for name in COMPONENT_NAMES
                if name in components_manifest
            },
        )

        self.log.emit(
            f"✅ Манифест построен: {total_files} файлов, "
            f"{_human_size(total_size)}, build_id={build_id}"
        )
        return manifest, release

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _should_exclude(path: Path, excludes: List[str]) -> bool:
        import fnmatch
        name     = path.name
        path_str = str(path)
        for pattern in excludes:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern.endswith(("/", "\\")):
                p = pattern.rstrip("/\\").lower()
                if p in [part.lower() for part in path.parts]:
                    return True
            else:
                if fnmatch.fnmatch(name.lower(), pattern.lower()):
                    return True
        return False


# ── ReleaseManager ────────────────────────────────────────────────────────────

class ReleaseManager(QObject):
    """
    Управляет версиями депо на WebDAV сервере.

    Структура файлов на сервере:
      <remote_path>/
        depot.json
        manifests/
          v2_e5f6a7b8.json
        files/
          Skyrim/      ← компонент Skyrim
            SkyrimSE.exe
            Data/...
          MO2p/        ← портативный MO2
            ModOrganizer.exe
          MO2ext/      ← моды и прочее
            mods/...
            profiles/...
        manifest.json  ← обратная совместимость
    """

    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config      = config
        self.webdav_cfg  = config.get("webdav", {})
        self.remote_path = self.webdav_cfg.get("remote_path", "").strip("/")
        self._dav: Optional[NextcloudDAV] = None

    def _get_dav(self) -> NextcloudDAV:
        if self._dav is None:
            cfg = self.webdav_cfg
            self._dav = NextcloudDAV(
                server_url = cfg.get("server_url", ""),
                username   = cfg.get("username", ""),
                password   = cfg.get("password", ""),
                verify_ssl = cfg.get("verify_ssl", True),
            )
        return self._dav

    def _rp(self, *parts: str) -> str:
        segs = [self.remote_path] + [p.strip("/") for p in parts if p]
        return "/".join(s for s in segs if s)

    # ── Структура сервера ─────────────────────────────────────────────────────

    def ensure_structure(self) -> bool:
        dav = self._get_dav()
        dirs_needed = [
            self.remote_path,
            self._rp(MANIFESTS_DIR),
            self._rp(FILES_DIR),
        ] + [self._rp(FILES_DIR, name) for name in COMPONENT_NAMES]

        for d in dirs_needed:
            if not d:
                continue
            if not dav.exists(d):
                self.log.emit(f"📁 Создаём: {d}")
                if not dav.mkcol(d):
                    self.log.emit(f"❌ Не удалось создать: {d}")
                    return False
        return True

    # ── depot.json ────────────────────────────────────────────────────────────

    def fetch_index(self) -> Optional[DepotIndex]:
        data = self._get_dav().get_bytes(self._rp(DEPOT_JSON))
        if data is None:
            return None
        try:
            return DepotIndex.from_json(data.decode("utf-8"))
        except Exception as e:
            self.log.emit(f"⚠️ Ошибка парсинга depot.json: {e}")
            return None

    def _save_index(self, index: DepotIndex) -> bool:
        ok = self._get_dav().put(
            self._rp(DEPOT_JSON),
            index.to_json().encode("utf-8"),
            content_type="application/json",
        )
        if not ok:
            self.log.emit("❌ Не удалось сохранить depot.json")
        return ok

    # ── Publish ───────────────────────────────────────────────────────────────

    def publish_release(
        self,
        manifest:      dict,
        release:       Release,
        components_cfg: Dict,   # {"Skyrim": {"local_dir": "...", "included": True, ...}}
        prev_index:    Optional[DepotIndex],
        stop_fn=None,
        pause_fn=None,
    ) -> Tuple[bool, str]:
        """
        Публикует новый релиз:
          1. Загружает манифест версии
          2. Синхронизирует файлы по компонентам (только included=True)
          3. Обновляет depot.json
          4. Копирует manifest.json (совместимость)
        """
        dav = self._get_dav()

        # ── 1. Манифест версии ────────────────────────────────────────────────
        manifest_remote = self._rp(release.manifest_path)
        self.log.emit(f"📤 Загружаем манифест: {release.manifest_path}")
        ok = dav.put(
            manifest_remote,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
        )
        if not ok:
            return False, f"Не удалось загрузить манифест {release.manifest_path}"

        # ── 2. Файлы по компонентам ───────────────────────────────────────────
        failed: List[str] = []
        components_in_manifest = manifest.get("components", {})

        # Старый манифест для delta
        prev_files_flat: Dict[str, str] = {}
        if prev_index:
            prev_release = prev_index.get_current(release.channel)
            if prev_release:
                prev_manifest = self._fetch_release_manifest(prev_release)
                if prev_manifest:
                    prev_files_flat = prev_manifest.get("files", {})

        for comp_name in COMPONENT_NAMES:
            comp_info = components_in_manifest.get(comp_name, {})
            if not comp_info.get("included", False):
                self.log.emit(f"⏭ {comp_name}: пропущен")
                continue

            comp_files = comp_info.get("files", {})  # rel → sha256
            if not comp_files:
                self.log.emit(f"⚠️ {comp_name}: файлов нет")
                continue

            comp_cfg  = components_cfg.get(comp_name, {})
            local_dir = Path(comp_cfg.get("local_dir", ""))

            self.log.emit(f"📁 {comp_name}: загружаем файлы ({len(comp_files)} шт.)…")

            # Определяем delta для компонента
            to_upload: List[str] = []
            for rel_path, new_hash in comp_files.items():
                manifest_key = f"{comp_name}/{rel_path}"
                prev_hash = prev_files_flat.get(manifest_key)
                if prev_hash != new_hash:
                    to_upload.append(rel_path)

            self.log.emit(
                f"  Delta: {len(to_upload)} к загрузке, "
                f"{len(comp_files) - len(to_upload)} без изменений"
            )

            comp_failed = self._upload_component_files(
                comp_name  = comp_name,
                local_dir  = local_dir,
                to_upload  = to_upload,
                dav        = dav,
                stop_fn    = stop_fn,
                pause_fn   = pause_fn,
            )
            failed.extend(comp_failed)

        # ── 3. depot.json ─────────────────────────────────────────────────────
        if failed:
            self.log.emit(f"⚠️ Не загружено {len(failed)} файлов")
            release.notes = (
                (release.notes + "\n" if release.notes else "") +
                f"[incomplete: {len(failed)} файлов не загружены]"
            )
            manifest["_failed_files"] = failed
            dav.put(
                manifest_remote,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json",
            )

        index = prev_index or DepotIndex(
            app_id=self.config.get("depot", {}).get("app_id", "")
        )
        index.add_release(release)

        if not self._save_index(index):
            return False, "Файлы загружены, но depot.json не обновлён"

        # ── 4. manifest.json (обратная совместимость) ─────────────────────────
        self.log.emit("📋 Обновляем manifest.json…")
        compat = {
            "version":    release.label,
            "build_id":   release.build_id,
            "created_at": release.created_at,
            "channel":    release.channel,
            "components": {
                name: {"included": info.get("included", False)}
                for name, info in components_in_manifest.items()
            },
            "files": manifest.get("files", {}),
        }
        compat_bytes = json.dumps(compat, ensure_ascii=False, indent=2).encode("utf-8")
        dav.put(self._rp(COMPAT_MANIFEST), compat_bytes, content_type="application/json")

        try:
            from config import APPDATA_DIR
            (APPDATA_DIR / "manifest.json").write_bytes(compat_bytes)
        except Exception as e:
            self.log.emit(f"⚠️ manifest.json локально: {e}")

        if failed:
            return True, (
                f"⚠️ Релиз {release.label} опубликован частично: "
                f"{len(failed)} файлов не загружено. Используйте '🔁 Дозалить'."
            )

        return True, (
            f"✅ Релиз {release.label} ({release.version_key}) опубликован! "
            f"Файлов: {release.file_count}, размер: {release.human_size()}. "
            f"Используйте 'Сделать активным' чтобы переключить канал."
        )

    def _upload_component_files(
        self,
        comp_name:  str,
        local_dir:  Path,
        to_upload:  List[str],
        dav:        NextcloudDAV,
        stop_fn=None,
        pause_fn=None,
    ) -> List[str]:
        """Загружает файлы одного компонента. Возвращает список незагруженных."""
        import time as _time
        failed: List[str] = []

        for rel_path in to_upload:
            if stop_fn and stop_fn():
                return failed
            if pause_fn:
                pause_fn()

            local_file = local_dir / Path(rel_path.replace("/", os.sep))
            if not local_file.exists():
                self.log.emit(f"  ⚠️ Нет локально: {comp_name}/{rel_path}")
                failed.append(f"{comp_name}/{rel_path}")
                continue

            # Создаём подпапки на сервере
            parts = rel_path.split("/")
            if len(parts) > 1:
                subdir = self._rp(FILES_DIR, comp_name, "/".join(parts[:-1]))
                if not dav.exists(subdir):
                    dav.mkcol(subdir)

            remote_file = self._rp(FILES_DIR, comp_name, rel_path)
            uploaded = False
            for attempt in range(3):
                try:
                    with open(local_file, "rb") as f:
                        data = f.read()
                    if dav.put(remote_file, data):
                        self.log.emit(
                            f"  ✅ {comp_name}/{rel_path} "
                            f"({len(data) // 1024} KB)"
                        )
                        uploaded = True
                        break
                    else:
                        if attempt < 2:
                            _time.sleep(2 ** attempt)
                except Exception as e:
                    self.log.emit(f"  ⚠️ {comp_name}/{rel_path} попытка {attempt+1}: {e}")
                    if attempt < 2:
                        _time.sleep(2 ** attempt)

            if not uploaded:
                self.log.emit(f"  ❌ {comp_name}/{rel_path}")
                failed.append(f"{comp_name}/{rel_path}")

        return failed

    # ── Set current ───────────────────────────────────────────────────────────

    def set_current(self, version_key: str, channel: str) -> Tuple[bool, str]:
        index = self.fetch_index()
        if index is None:
            return False, "depot.json не найден на сервере"
        if version_key not in index.versions:
            return False, f"Версия {version_key} не найдена в depot.json"
        release = index.versions[version_key]
        index.set_current(channel, version_key)
        if not self._save_index(index):
            return False, "Не удалось обновить depot.json"
        if channel == "stable":
            manifest = self._fetch_release_manifest(release)
            if manifest:
                compat = {
                    "version":    release.label,
                    "build_id":   release.build_id,
                    "created_at": release.created_at,
                    "files":      manifest.get("files", {}),
                    "components": manifest.get("components", {}),
                }
                self._get_dav().put(
                    self._rp(COMPAT_MANIFEST),
                    json.dumps(compat, ensure_ascii=False, indent=2).encode("utf-8"),
                    content_type="application/json",
                )
        return True, f"Канал '{channel}' → {release.label} ({version_key})"

    # ── Rollback ──────────────────────────────────────────────────────────────

    def rollback(self, channel: str = "stable") -> Tuple[bool, str, Optional[str]]:
        index = self.fetch_index()
        if index is None:
            return False, "depot.json не найден", None
        channel_versions = index.get_by_channel(channel)
        if len(channel_versions) < 2:
            return False, f"Нет предыдущей версии для отката в канале '{channel}'", None
        current_key = index.current.get(channel)
        if current_key is None:
            return False, f"Канал '{channel}' не имеет активной версии", None
        sorted_versions = sorted(channel_versions, key=lambda r: r.build_number, reverse=True)
        current_idx = next(
            (i for i, r in enumerate(sorted_versions) if r.version_key == current_key), None
        )
        if current_idx is None or current_idx >= len(sorted_versions) - 1:
            return False, "Нет более старой версии для отката", None
        prev_release = sorted_versions[current_idx + 1]
        ok, msg = self.set_current(prev_release.version_key, channel)
        return ok, msg, prev_release.version_key if ok else None

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_release(self, version_key: str) -> Tuple[bool, str]:
        index = self.fetch_index()
        if index is None:
            return False, "depot.json не найден"
        if version_key not in index.versions:
            return False, f"Версия {version_key} не найдена"
        for ch, key in index.current.items():
            if key == version_key:
                return False, (
                    f"Нельзя удалить активную версию канала '{ch}'. "
                    f"Сначала переключите канал."
                )
        release = index.versions.pop(version_key)
        self._get_dav().delete(self._rp(release.manifest_path))
        self.log.emit(f"🗑 Удалён манифест: {release.manifest_path}")
        if not self._save_index(index):
            return False, "depot.json не обновлён после удаления"
        return True, f"Версия {version_key} удалена из индекса"

    # ── Resume ────────────────────────────────────────────────────────────────

    def resume_failed_release(
        self,
        version_key:    str,
        components_cfg: Dict,
        stop_fn=None,
        pause_fn=None,
    ) -> Tuple[bool, str]:
        dav = self._get_dav()
        index = self.fetch_index()
        if index is None:
            return False, "depot.json не найден"
        if version_key not in index.versions:
            return False, f"Версия {version_key} не найдена"
        release      = index.versions[version_key]
        manifest_data = self._fetch_release_manifest(release)
        if manifest_data is None:
            return False, f"Манифест {version_key} не найден"

        known_failed: List[str] = manifest_data.get("_failed_files", [])
        all_files_flat = manifest_data.get("files", {})
        to_check = known_failed if known_failed else list(all_files_flat.keys())

        self.log.emit(f"🔍 Проверяем {len(to_check)} файлов…")
        missing: List[str] = []
        for full_path in to_check:
            if not dav.exists(self._rp(FILES_DIR, full_path)):
                missing.append(full_path)

        if not missing:
            self._cleanup_incomplete_marker(release, manifest_data, dav)
            self._save_index(index)
            return True, f"Релиз {version_key} полный — depot.json обновлён"

        self.log.emit(f"📤 Загружаем {len(missing)} файлов…")
        total  = len(missing)
        done   = 0
        failed: List[str] = []

        for full_path in missing:
            if stop_fn and stop_fn():
                return False, "Остановлено"
            if pause_fn:
                pause_fn()

            done += 1
            self.progress.emit(done, total, f"Дозаливаем: {full_path}")

            # full_path вида "CompName/rel/path"
            parts_path = full_path.split("/", 1)
            if len(parts_path) != 2:
                failed.append(full_path)
                continue

            comp_name, rel_path = parts_path
            comp_cfg  = components_cfg.get(comp_name, {})
            local_dir = Path(comp_cfg.get("local_dir", ""))

            local_file = local_dir / Path(rel_path.replace("/", os.sep))
            if not local_file.exists():
                self.log.emit(f"⚠️ Нет локально: {full_path}")
                failed.append(full_path)
                continue

            parts = rel_path.split("/")
            if len(parts) > 1:
                subdir = self._rp(FILES_DIR, comp_name, "/".join(parts[:-1]))
                if not dav.exists(subdir):
                    dav.mkcol(subdir)

            remote_file = self._rp(FILES_DIR, full_path)
            uploaded = False
            import time as _t
            for attempt in range(3):
                try:
                    with open(local_file, "rb") as f:
                        data = f.read()
                    if dav.put(remote_file, data):
                        self.log.emit(f"  ✅ {full_path} ({len(data) // 1024} KB)")
                        uploaded = True
                        break
                    if attempt < 2:
                        _t.sleep(2 ** attempt)
                except Exception as e:
                    self.log.emit(f"  ⚠️ {full_path} попытка {attempt+1}: {e}")
                    if attempt < 2:
                        _t.sleep(2 ** attempt)

            if not uploaded:
                failed.append(full_path)

        if failed:
            manifest_data["_failed_files"] = failed
            dav.put(
                self._rp(release.manifest_path),
                json.dumps(manifest_data, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json",
            )
            return False, (
                f"Частично: {total - len(failed)}/{total} загружено. "
                f"Осталось {len(failed)}. Повторите 'Дозалить'."
            )

        self._cleanup_incomplete_marker(release, manifest_data, dav)
        import re as _re
        release.notes = _re.sub(r"\n?\[incomplete:[^\]]+\]", "", release.notes or "").strip()
        index.versions[version_key] = release
        self._save_index(index)
        return True, f"✅ Дозаливка завершена! {total} файлов. Релиз {version_key} полный."

    def _cleanup_incomplete_marker(self, release: Release, manifest_data: dict, dav):
        if "_failed_files" in manifest_data:
            manifest_data.pop("_failed_files")
            dav.put(
                self._rp(release.manifest_path),
                json.dumps(manifest_data, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json",
            )

    def _fetch_release_manifest(self, release: Release) -> Optional[dict]:
        data = self._get_dav().get_bytes(self._rp(release.manifest_path))
        if data is None:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def close(self):
        if self._dav:
            self._dav.close()
            self._dav = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(s: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if s < 1024:
            return f"{s:.1f} {u}"
        s /= 1024
    return f"{s:.1f} TB"
