# ==================== static_folders_tab.py ====================
"""
Вкладка "Статик-папки" для Uploder.

Статик-папки не версионируются — они хранятся на сервере как есть.
Эта вкладка:
  1. Сканирует выбранную локальную папку
  2. Строит manifest.json (path → sha256:hex)
  3. Загружает manifest.json в корень папки на сервере
     (файлы сами уже должны быть там — грузились вручную)

Структура на сервере:
  1TB/TESS/Instances/TESVAE/staticfolders/Patcher/manifest.json
  1TB/TESS/Instances/TESVAE/staticfolders/MO2p/manifest.json

Лаунчер читает эти манифесты и скачивает файлы по ним.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QLineEdit, QTextEdit, QProgressBar, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QComboBox, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont


# ── Имена встроенных статик-папок (можно добавлять) ──────────────────────────
BUILTIN_STATIC_FOLDERS = [
    ("Patcher",  "1TB/TESS/Instances/TESVAE/staticfolders/Patcher"),
    ("MO2p",     "1TB/TESS/Instances/TESVAE/staticfolders/MO2p"),
    ("CRASH_Log","1TB/TESS/Staticfolders/CRASH_Log"),
    ("Skyrim",   "1TB/TESS/Instances/TESVAE/staticfolders/TESV1.6.1170.0"),
]


# ── Worker ────────────────────────────────────────────────────────────────────

class StaticManifestWorker(QObject):
    """
    Сканирует локальную папку, строит манифест, загружает его на WebDAV.
    Файлы при этом НЕ загружаются — только manifest.json.
    """
    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(
        self,
        config:      dict,
        local_dir:   str,
        remote_path: str,   # напр. "1TB/TESS/Instances/TESVAE/staticfolders/Patcher"
        folder_name: str,
        upload_files: bool = False,  # True = загружать и файлы тоже
    ):
        super().__init__()
        self.config       = config
        self.local_dir    = Path(local_dir)
        self.remote_path  = remote_path.strip("/")
        self.folder_name  = folder_name
        self.upload_files = upload_files
        self._stop        = False

    def stop(self): self._stop = True

    def run(self):
        try:
            from depot_sync_manager import NextcloudDAV

            webdav = self.config.get("webdav", {})
            dav = NextcloudDAV(
                server_url = webdav.get("server_url", ""),
                username   = webdav.get("username", ""),
                password   = webdav.get("password", ""),
                verify_ssl = webdav.get("verify_ssl", True),
            )

            # ── 1. Сканируем файлы ────────────────────────────────────────────
            self.log.emit(f"📁 Сканируем: {self.local_dir}")
            if not self.local_dir.exists():
                self.finished.emit(False, f"Папка не найдена: {self.local_dir}")
                return

            all_files = []
            for root, dirs, files in os.walk(self.local_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    fp = Path(root) / f
                    all_files.append(fp)

            total = len(all_files)
            if total == 0:
                self.finished.emit(False, "Папка пуста — нечего индексировать")
                return

            self.log.emit(f"📋 Файлов: {total}")

            # ── 2. Строим манифест ────────────────────────────────────────────
            files_dict: Dict[str, str] = {}
            total_size = 0

            for i, fp in enumerate(all_files):
                if self._stop:
                    self.finished.emit(False, "Остановлено")
                    return

                rel = str(fp.relative_to(self.local_dir)).replace("\\", "/")
                self.progress.emit(i + 1, total, f"Хэш: {rel}")

                try:
                    h = hashlib.sha256()
                    with open(fp, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    files_dict[rel] = f"sha256:{h.hexdigest()}"
                    total_size += fp.stat().st_size
                except Exception as e:
                    self.log.emit(f"⚠️ Ошибка {rel}: {e}")

            manifest = {
                "folder":     self.folder_name,
                "created_at": datetime.now().isoformat(),
                "file_count": len(files_dict),
                "total_size": total_size,
                "files":      files_dict,
            }

            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            self.log.emit(
                f"✅ Манифест построен: {len(files_dict)} файлов, "
                f"{_fmt_size(total_size)}"
            )

            # ── 3. Убеждаемся что папка на сервере существует ─────────────────
            if not dav.exists(self.remote_path):
                self.log.emit(f"📁 Создаём папку на сервере: {self.remote_path}")
                # Создаём рекурсивно
                parts = self.remote_path.split("/")
                cur = ""
                for part in parts:
                    cur = f"{cur}/{part}" if cur else part
                    if not dav.exists(cur):
                        dav.mkcol(cur)

            # ── 4. Загружаем manifest.json ────────────────────────────────────
            manifest_remote = f"{self.remote_path}/manifest.json"
            self.log.emit(f"📤 Загружаем manifest.json → {manifest_remote}")

            ok = dav.put(manifest_remote, manifest_bytes, content_type="application/json")
            if not ok:
                self.finished.emit(False, "Не удалось загрузить manifest.json на сервер")
                return

            self.log.emit("✅ manifest.json загружен!")

            # ── 5. Опционально — загружаем сами файлы ────────────────────────
            if self.upload_files:
                self.log.emit("📤 Загружаем файлы на сервер…")
                failed = []
                upload_total = len(all_files)
                done = 0

                for fp in all_files:
                    if self._stop:
                        self.finished.emit(False, "Остановлено")
                        return

                    rel = str(fp.relative_to(self.local_dir)).replace("\\", "/")
                    done += 1
                    self.progress.emit(done, upload_total, f"Загрузка: {rel}")

                    remote_file = f"{self.remote_path}/{rel}"

                    # Создаём подпапки
                    rel_parts = rel.split("/")
                    if len(rel_parts) > 1:
                        subdir = f"{self.remote_path}/{'/'.join(rel_parts[:-1])}"
                        if not dav.exists(subdir):
                            dav.mkcol(subdir)

                    uploaded = False
                    import time
                    for attempt in range(3):
                        try:
                            with open(fp, "rb") as f:
                                data = f.read()
                            if dav.put(remote_file, data):
                                uploaded = True
                                break
                            if attempt < 2:
                                time.sleep(2 ** attempt)
                        except Exception as e:
                            self.log.emit(f"  ⚠️ {rel} попытка {attempt+1}: {e}")
                            if attempt < 2:
                                time.sleep(2 ** attempt)

                    if not uploaded:
                        failed.append(rel)
                        self.log.emit(f"  ❌ {rel}")
                    else:
                        self.log.emit(f"  ✅ {rel} ({fp.stat().st_size // 1024} KB)")

                if failed:
                    dav.close()
                    self.finished.emit(
                        False,
                        f"Загружено {upload_total - len(failed)}/{upload_total} файлов. "
                        f"Не загружено: {len(failed)}."
                    )
                    return

            dav.close()
            action = "загружены файлы и" if self.upload_files else ""
            self.finished.emit(
                True,
                f"✅ Готово! {action} manifest.json для '{self.folder_name}' "
                f"({len(files_dict)} файлов, {_fmt_size(total_size)})."
            )

        except Exception as e:
            import traceback
            self.log.emit(f"❌ {e}\n{traceback.format_exc()}")
            self.finished.emit(False, str(e))


# ── UI ────────────────────────────────────────────────────────────────────────

class StaticFoldersTab(QWidget):
    """Вкладка управления статик-папками."""

    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw      = main_window
        self._worker = None
        self._thread = None
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(6, 6, 6, 6)

        # ── Инструкция ────────────────────────────────────────────────────────
        info = QLabel(
            "Статик-папки не версионируются. Здесь вы строите <b>manifest.json</b> "
            "для папок Patcher, MO2p и др. — лаунчер использует его для скачивания файлов.\n"
            "Файлы уже должны быть загружены вручную, либо выберите «Загрузить файлы и манифест»."
        )
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 10px; padding: 4px;")
        root.addWidget(info)

        # ── Быстрые кнопки встроенных папок ──────────────────────────────────
        quick_box = QGroupBox("Быстрое создание манифеста")
        ql = QVBoxLayout(quick_box)

        self.table = QTableWidget(len(BUILTIN_STATIC_FOLDERS), 4)
        self.table.setHorizontalHeaderLabels(["Папка", "Путь на сервере", "Локальная папка", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(140)

        self._local_path_edits = {}
        for row, (name, remote) in enumerate(BUILTIN_STATIC_FOLDERS):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(remote))

            edit = QLineEdit()
            edit.setPlaceholderText("Выберите локальную папку…")
            self.table.setCellWidget(row, 2, edit)
            self._local_path_edits[name] = (edit, remote)

            btn = QPushButton("📁")
            btn.setFixedWidth(34)
            btn.setToolTip("Выбрать папку")
            btn.clicked.connect(lambda _, n=name: self._browse_for_folder(n))
            self.table.setCellWidget(row, 3, btn)

        ql.addWidget(self.table)

        # Кнопки действий
        btn_row = QHBoxLayout()

        self.btn_manifest_only = QPushButton("📋 Создать манифест (файлы уже на сервере)")
        self.btn_manifest_only.setFixedHeight(36)
        self.btn_manifest_only.clicked.connect(lambda: self._start_selected(upload_files=False))
        btn_row.addWidget(self.btn_manifest_only)

        self.btn_upload_all = QPushButton("📤 Загрузить файлы и манифест")
        self.btn_upload_all.setFixedHeight(36)
        self.btn_upload_all.clicked.connect(lambda: self._start_selected(upload_files=True))
        btn_row.addWidget(self.btn_upload_all)

        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self.btn_stop)

        ql.addLayout(btn_row)
        root.addWidget(quick_box)

        # ── Произвольная папка ────────────────────────────────────────────────
        custom_box = QGroupBox("Произвольная статик-папка")
        cl = QVBoxLayout(custom_box)

        form_row1 = QHBoxLayout()
        form_row1.addWidget(QLabel("Название:"))
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setPlaceholderText("Patcher")
        self.custom_name_edit.setMaximumWidth(150)
        form_row1.addWidget(self.custom_name_edit)
        form_row1.addSpacing(16)
        form_row1.addWidget(QLabel("Локальная папка:"))
        self.custom_local_edit = QLineEdit()
        self.custom_local_edit.setPlaceholderText("D:\\MyMods\\Patcher")
        form_row1.addWidget(self.custom_local_edit)
        self.btn_custom_browse = QPushButton("📁")
        self.btn_custom_browse.setFixedWidth(34)
        self.btn_custom_browse.clicked.connect(self._browse_custom)
        form_row1.addWidget(self.btn_custom_browse)

        form_row2 = QHBoxLayout()
        form_row2.addWidget(QLabel("Путь на сервере:"))
        self.custom_remote_edit = QLineEdit()
        self.custom_remote_edit.setPlaceholderText("1TB/TESS/Instances/TESVAE/staticfolders/Patcher")
        form_row2.addWidget(self.custom_remote_edit)

        custom_btn_row = QHBoxLayout()
        self.btn_custom_manifest = QPushButton("📋 Только манифест")
        self.btn_custom_manifest.setFixedHeight(34)
        self.btn_custom_manifest.clicked.connect(lambda: self._start_custom(upload_files=False))
        self.btn_custom_upload = QPushButton("📤 Файлы + манифест")
        self.btn_custom_upload.setFixedHeight(34)
        self.btn_custom_upload.clicked.connect(lambda: self._start_custom(upload_files=True))
        custom_btn_row.addWidget(self.btn_custom_manifest)
        custom_btn_row.addWidget(self.btn_custom_upload)
        custom_btn_row.addStretch()

        cl.addLayout(form_row1)
        cl.addLayout(form_row2)
        cl.addLayout(custom_btn_row)
        root.addWidget(custom_box)

        # ── Прогресс + лог ────────────────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        log_box = QGroupBox("Лог")
        ll = QVBoxLayout(log_box)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 8))
        self.log_edit.setMaximumHeight(150)
        ll.addWidget(self.log_edit)
        root.addWidget(log_box)

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_for_folder(self, name: str):
        path = QFileDialog.getExistingDirectory(self, f"Папка для {name}")
        if path:
            edit, _ = self._local_path_edits[name]
            edit.setText(path)

    def _browse_custom(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку")
        if path:
            self.custom_local_edit.setText(path)
            if not self.custom_name_edit.text():
                self.custom_name_edit.setText(Path(path).name)

    # ── Start actions ─────────────────────────────────────────────────────────

    def _start_selected(self, upload_files: bool):
        """Запускаем для всех строк у которых заполнена локальная папка."""
        tasks = []
        for name, (edit, remote) in self._local_path_edits.items():
            local = edit.text().strip()
            if local:
                tasks.append((name, local, remote))

        if not tasks:
            QMessageBox.warning(
                self, "Нет папок",
                "Укажите локальные папки для одной или нескольких строк."
            )
            return

        # Запускаем последовательно через очередь
        self._task_queue = tasks
        self._upload_files_flag = upload_files
        self._run_next_task()

    def _run_next_task(self):
        if not self._task_queue:
            self._log("✅ Все задачи завершены")
            self._set_busy(False)
            return
        name, local, remote = self._task_queue.pop(0)
        self._log(f"\n{'='*40}")
        self._log(f"▶ Обрабатываем: {name}")
        self._start_worker(name, local, remote, self._upload_files_flag)

    def _start_custom(self, upload_files: bool):
        name   = self.custom_name_edit.text().strip() or "custom"
        local  = self.custom_local_edit.text().strip()
        remote = self.custom_remote_edit.text().strip()

        if not local:
            QMessageBox.warning(self, "Ошибка", "Укажите локальную папку!")
            return
        if not remote:
            QMessageBox.warning(self, "Ошибка", "Укажите путь на сервере!")
            return

        self._task_queue = []
        self._start_worker(name, local, remote, upload_files)

    def _start_worker(self, name: str, local: str, remote: str, upload_files: bool):
        cfg = self.mw.file_selector.config
        if not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Настройте WebDAV в настройках!")
            return

        self._set_busy(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()

        self._worker = StaticManifestWorker(cfg, local, remote, name, upload_files)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
        self._task_queue = []

    def _on_progress(self, cur, total, msg):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(cur)
        self.progress_bar.setFormat(f"{msg}  [{cur}/{total}]")

    def _on_finished(self, ok: bool, msg: str):
        self._cleanup_thread()
        self._log(f"{'✅' if ok else '❌'} {msg}")
        self.log_message.emit(msg)

        # Если есть ещё задачи в очереди — продолжаем
        if hasattr(self, '_task_queue') and self._task_queue:
            QTimer.singleShot(300, self._run_next_task)
        else:
            self._set_busy(False)
            self.progress_bar.hide()
            if ok:
                QMessageBox.information(self, "Готово", msg)
            else:
                QMessageBox.warning(self, "Ошибка", msg)

    def _cleanup_thread(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool):
        self.btn_manifest_only.setEnabled(not busy)
        self.btn_upload_all.setEnabled(not busy)
        self.btn_custom_manifest.setEnabled(not busy)
        self.btn_custom_upload.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.progress_bar.setVisible(busy)

    def _log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_edit.append(f"[{ts}] {msg}")
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"
