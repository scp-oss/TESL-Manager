# ==================== depot_tab.py ====================
"""
Вкладка "Депо" для main_window — публикация через chunk-протокол
(chunks/<xx>/<id> или, с 2026-09-23, упакованные pack-файлы, см.
pack_writer.py) — независимо от старого компонентного протокола
(ReleaseTab), свой backend (WebDAV ИЛИ TESL-Panel, см. panel_client.py)
и своя локальная папка (НЕ переиспользует components из ReleaseTab —
это другой протокол публикации, привязывать их друг к другу смысла
нет: можно публиковать одну и ту же папку и старым, и новым способом
независимо).

Подключается в main_window.py:
    self.depot_tab = DepotTab(self)
    self.depot_tab.log_message.connect(self.log_message)
    self.tabs.addTab(self.depot_tab, "📦 Депо (chunks)")
"""
import json
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QLineEdit, QSpinBox, QFormLayout,
    QProgressBar, QTextEdit, QCheckBox, QSizePolicy, QFrame,
    QMessageBox, QFileDialog, QStackedWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from chunk_manager import DEFAULT_CHUNK_SIZE, DepotManifest, DepotDelta
from depot_sync_manager import DepotBuildWorker, DepotSyncManager
from pack_writer import DEFAULT_PACK_SIZE
from depot_confirm_dialog import DepotConfirmDialog


def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


CHUNK_SIZES = [
    ("512 KB  — максимальный delta",   512 * 1024),
    ("1 MB",                           1 * 1024 * 1024),
    ("4 MB   — рекомендуется",         4 * 1024 * 1024),
    ("8 MB",                           8 * 1024 * 1024),
    ("16 MB",                         16 * 1024 * 1024),
    ("32 MB  — крупные файлы",        32 * 1024 * 1024),
    ("64 MB  — минимальный overhead", 64 * 1024 * 1024),
]

CHANNELS = ["stable", "beta", "dev"]

# "depot" — отдельный ключ в конфиге (независим от "webdav"/"components",
# которыми пользуется ReleaseTab) — своя локальная папка, свой backend.
CFG_KEY = "depot_tab"


class DepotTab(QWidget):
    """Вкладка управления chunk-based depot-системой."""

    log_message = pyqtSignal(str)   # пробрасываем в основной лог

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window   # ссылка на MainWindow для доступа к config
        self._worker: DepotBuildWorker = None
        self._thread: QThread = None
        self._btn_pause_state = False
        self._pending_manifest: DepotManifest = None
        self._pending_delta:    DepotDelta = None
        self._init_ui()
        self._load_settings()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Локальная папка ──────────────────────────────────────────────────
        dir_box = QGroupBox("Локальная папка")
        dir_row = QHBoxLayout(dir_box)
        self.local_dir_edit = QLineEdit()
        self.local_dir_edit.setPlaceholderText(r"P:\Games\skyrim\Skyrim")
        dir_row.addWidget(self.local_dir_edit)
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._browse_folder)
        dir_row.addWidget(btn_browse)
        root.addWidget(dir_box)

        # ── Куда публикуем ────────────────────────────────────────────────────
        backend_box = QGroupBox("Куда публикуем")
        backend_layout = QVBoxLayout(backend_box)

        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["TESL-Panel (напрямую, минуя Nextcloud)", "WebDAV (Nextcloud)"])
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        backend_layout.addWidget(self.backend_combo)

        self.backend_stack = QStackedWidget()

        # -- Panel fields --
        panel_page = QWidget()
        panel_form = QFormLayout(panel_page)
        self.panel_url_edit = QLineEdit()
        self.panel_url_edit.setPlaceholderText("https://tesl-panel.neth.de5.net")
        self.panel_project_edit = QLineEdit()
        self.panel_project_edit.setPlaceholderText("TESVAE")
        self.panel_token_edit = QLineEdit()
        self.panel_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        panel_form.addRow("URL панели:", self.panel_url_edit)
        panel_form.addRow("Проект:", self.panel_project_edit)
        panel_form.addRow("Upload-токен:", self.panel_token_edit)
        self.backend_stack.addWidget(panel_page)

        # -- WebDAV fields (независимы от тех, что в "⚙️ Настройки" —
        #    та вкладка настраивает WebDAV для СТАРОГО компонентного
        #    протокола ReleaseTab, здесь свой, специально под этот путь
        #    публикации; можно указать тот же URL, можно другой) --
        webdav_page = QWidget()
        webdav_form = QFormLayout(webdav_page)
        self.webdav_url_edit = QLineEdit()
        self.webdav_url_edit.setPlaceholderText("https://host/cloud/remote.php/dav/files/user")
        self.webdav_username_edit = QLineEdit()
        self.webdav_password_edit = QLineEdit()
        self.webdav_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.webdav_path_edit = QLineEdit()
        self.webdav_path_edit.setPlaceholderText("1TB/TESS/Instances/TESVAE")
        self.webdav_ssl_check = QCheckBox("Проверять SSL сертификат")
        self.webdav_ssl_check.setChecked(True)
        webdav_form.addRow("URL сервера:", self.webdav_url_edit)
        webdav_form.addRow("Пользователь:", self.webdav_username_edit)
        webdav_form.addRow("Пароль:", self.webdav_password_edit)
        webdav_form.addRow("Путь на сервере:", self.webdav_path_edit)
        webdav_form.addRow("", self.webdav_ssl_check)
        self.backend_stack.addWidget(webdav_page)

        backend_layout.addWidget(self.backend_stack)
        root.addWidget(backend_box)

        # ── Настройки депо ────────────────────────────────────────────────────
        settings_box = QGroupBox("Настройки депо")
        form = QFormLayout(settings_box)

        self.app_id_edit = QLineEdit()
        self.app_id_edit.setPlaceholderText("skyrim-001")
        form.addRow("App ID:", self.app_id_edit)

        self.depot_id_spin = QSpinBox()
        self.depot_id_spin.setRange(1, 99999)
        self.depot_id_spin.setValue(1001)
        form.addRow("Depot ID:", self.depot_id_spin)

        self.channel_combo = QComboBox()
        self.channel_combo.addItems(CHANNELS)
        form.addRow("Канал:", self.channel_combo)

        self.chunk_combo = QComboBox()
        default_idx = 2  # 4 MB
        for i, (label, _) in enumerate(CHUNK_SIZES):
            self.chunk_combo.addItem(label)
        self.chunk_combo.setCurrentIndex(default_idx)
        form.addRow("Размер чанка:", self.chunk_combo)

        self.use_packs_check = QCheckBox(
            "Упаковывать чанки в pack-файлы вместо chunks/<xx>/<id> "
            "(меньше отдельных файлов на диске сервера)"
        )
        self.use_packs_check.setChecked(True)
        self.use_packs_check.stateChanged.connect(self._on_use_packs_changed)
        form.addRow("", self.use_packs_check)

        self.pack_size_spin = QSpinBox()
        self.pack_size_spin.setRange(16, 4096)
        self.pack_size_spin.setSuffix(" MB")
        self.pack_size_spin.setValue(DEFAULT_PACK_SIZE // 1024 // 1024)
        form.addRow("Размер pack-файла:", self.pack_size_spin)

        self.btn_save_settings = QPushButton("💾 Сохранить настройки")
        self.btn_save_settings.clicked.connect(self._save_settings)
        form.addRow("", self.btn_save_settings)

        root.addWidget(settings_box)

        # ── Статус сервера ────────────────────────────────────────────────────
        server_box = QGroupBox("Статус сервера")
        server_inner = QVBoxLayout(server_box)

        self.server_status_label = QLabel("Нет данных")
        self.server_status_label.setWordWrap(True)
        server_inner.addWidget(self.server_status_label)

        btn_row = QHBoxLayout()
        self.btn_test_conn = QPushButton("🔌 Проверить соединение")
        self.btn_test_conn.clicked.connect(self._test_connection)
        btn_row.addWidget(self.btn_test_conn)
        self.btn_fetch_info = QPushButton("📥 Инфо с сервера")
        self.btn_fetch_info.clicked.connect(self._fetch_server_info)
        btn_row.addWidget(self.btn_fetch_info)
        btn_row.addStretch()
        server_inner.addLayout(btn_row)

        root.addWidget(server_box)

        # ── Действия ─────────────────────────────────────────────────────────
        actions_box = QGroupBox("Публикация")
        actions_inner = QVBoxLayout(actions_box)

        desc = QLabel(
            "Сканирует локальную папку, вычисляет delta относительно сервера,\n"
            "загружает только изменившиеся чанки. Чанки дедуплицируются."
        )
        desc.setWordWrap(True)
        actions_inner.addWidget(desc)

        btn_pub_row = QHBoxLayout()

        self.btn_publish = QPushButton("📦 Собрать и опубликовать")
        self.btn_publish.setMinimumHeight(48)
        self.btn_publish.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_publish.clicked.connect(self._start_build)
        btn_pub_row.addWidget(self.btn_publish)

        self.btn_pause = QPushButton("⏸ Пауза")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_pub_row.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_pub_row.addWidget(self.btn_stop)

        actions_inner.addLayout(btn_pub_row)
        root.addWidget(actions_box)

        # ── Прогресс ─────────────────────────────────────────────────────────
        self.progress_label = QLabel("")
        root.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # ── Лог ──────────────────────────────────────────────────────────────
        log_box = QGroupBox("Лог")
        log_inner = QVBoxLayout(log_box)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.log_edit.setMaximumHeight(220)
        log_inner.addWidget(self.log_edit)

        btn_clear = QPushButton("🗑 Очистить")
        btn_clear.clicked.connect(self.log_edit.clear)
        log_inner.addWidget(btn_clear)
        root.addWidget(log_box)

        root.addStretch()

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку для публикации")
        if folder:
            self.local_dir_edit.setText(folder)

    def _on_backend_changed(self, idx: int):
        self.backend_stack.setCurrentIndex(idx)

    def _on_use_packs_changed(self, state):
        self.pack_size_spin.setEnabled(self.use_packs_check.isChecked())

    # ── Settings persistence ──────────────────────────────────────────────────
    # Отдельная секция конфига (CFG_KEY) — независима от "webdav"/
    # "components", которыми пользуется ReleaseTab (старый протокол).

    def _get_config(self) -> dict:
        return self.mw.file_selector.config

    def _load_settings(self):
        cfg = self._get_config().get(CFG_KEY, {})

        self.local_dir_edit.setText(cfg.get("local_dir", ""))
        self.backend_combo.setCurrentIndex(0 if cfg.get("backend", "panel") == "panel" else 1)
        self.backend_stack.setCurrentIndex(self.backend_combo.currentIndex())

        panel = cfg.get("panel", {})
        self.panel_url_edit.setText(panel.get("base_url", ""))
        self.panel_project_edit.setText(panel.get("project", ""))
        self.panel_token_edit.setText(panel.get("token", ""))

        webdav = cfg.get("webdav", {})
        self.webdav_url_edit.setText(webdav.get("server_url", ""))
        self.webdav_username_edit.setText(webdav.get("username", ""))
        self.webdav_password_edit.setText(webdav.get("password", ""))
        self.webdav_path_edit.setText(webdav.get("remote_path", ""))
        self.webdav_ssl_check.setChecked(webdav.get("verify_ssl", True))

        depot = cfg.get("depot", {})
        self.app_id_edit.setText(depot.get("app_id", "my_app"))
        self.depot_id_spin.setValue(depot.get("depot_id", 1001))
        ch = depot.get("channel", "stable")
        idx = self.channel_combo.findText(ch)
        if idx >= 0:
            self.channel_combo.setCurrentIndex(idx)
        target_size = depot.get("chunk_size", DEFAULT_CHUNK_SIZE)
        for i, (_, size) in enumerate(CHUNK_SIZES):
            if size == target_size:
                self.chunk_combo.setCurrentIndex(i)
                break

        self.use_packs_check.setChecked(cfg.get("use_packs", True))
        self.pack_size_spin.setValue(depot.get("pack_size", DEFAULT_PACK_SIZE) // 1024 // 1024)
        self.pack_size_spin.setEnabled(self.use_packs_check.isChecked())

    def _build_runtime_config(self) -> dict:
        """Собирает config-словарь ровно в форме, которую ожидает
        DepotSyncManager (backend/panel/webdav/use_packs/depot/local_dir/
        excludes) — та же схема, что publish_packed.py собирал вручную."""
        backend = "panel" if self.backend_combo.currentIndex() == 0 else "webdav"
        return {
            "backend": backend,
            "use_packs": self.use_packs_check.isChecked(),
            "panel": {
                "base_url": self.panel_url_edit.text().strip().rstrip("/"),
                "project":  self.panel_project_edit.text().strip(),
                "token":    self.panel_token_edit.text(),
                "verify_ssl": True,
            },
            "webdav": {
                "server_url":  self.webdav_url_edit.text().strip().rstrip("/"),
                "username":    self.webdav_username_edit.text().strip(),
                "password":    self.webdav_password_edit.text(),
                "remote_path": self.webdav_path_edit.text().strip().strip("/"),
                "verify_ssl":  self.webdav_ssl_check.isChecked(),
            },
            "depot": {
                "app_id":     self.app_id_edit.text().strip() or "my_app",
                "depot_id":   self.depot_id_spin.value(),
                "channel":    self.channel_combo.currentText(),
                "chunk_size": CHUNK_SIZES[self.chunk_combo.currentIndex()][1],
                "pack_size":  self.pack_size_spin.value() * 1024 * 1024,
            },
            "local_dir": self.local_dir_edit.text().strip(),
            "excludes": [],
        }

    def _save_settings(self):
        cfg = self._get_config()
        runtime = self._build_runtime_config()
        cfg[CFG_KEY] = runtime
        self.mw.file_selector.save_config()
        self._log("✅ Настройки депо сохранены")

    # ── Connection test ───────────────────────────────────────────────────────

    def _validate_backend(self, cfg: dict) -> str:
        """Возвращает текст ошибки или "" если конфиг backend'а достаточен
        для сетевого запроса."""
        if cfg["backend"] == "panel":
            if not cfg["panel"]["base_url"] or not cfg["panel"]["project"]:
                return "Заполните URL панели и имя проекта!"
        else:
            if not cfg["webdav"]["server_url"]:
                return "Заполните URL WebDAV сервера!"
        return ""

    def _test_connection(self):
        cfg = self._build_runtime_config()
        err = self._validate_backend(cfg)
        if err:
            QMessageBox.warning(self, "Ошибка", err)
            return
        self._log("🔌 Проверяем соединение...")
        sync = DepotSyncManager(cfg)
        ok, msg = sync.test_connection()
        sync.close()
        if ok:
            self.server_status_label.setText(f"✅ {msg}")
            self._log(f"✅ Соединение: {msg}")
        else:
            self.server_status_label.setText(f"❌ {msg}")
            self._log(f"❌ {msg}")

    def _fetch_server_info(self):
        cfg = self._build_runtime_config()
        err = self._validate_backend(cfg)
        if err:
            QMessageBox.warning(self, "Ошибка", err)
            return
        self._log("📥 Запрашиваем инфо с сервера...")
        sync = DepotSyncManager(cfg)
        manifest = sync.fetch_remote_manifest()
        sync.close()
        if manifest:
            info = (
                f"✅ Текущая версия на сервере:\n"
                f"  App: {manifest.app_id}\n"
                f"  Build: #{manifest.build_number} ({manifest.build_id})\n"
                f"  Канал: {manifest.channel}\n"
                f"  Файлов: {len(manifest.files)}\n"
                f"  Размер: {manifest.human_size()}\n"
                f"  Создан: {manifest.created_at[:19]}"
            )
            self.server_status_label.setText(info)
            self._log(f"✅ Манифест получен: {manifest.version_label()}")
        else:
            self.server_status_label.setText("⚠️ Манифест не найден (первая публикация?)")
            self._log("⚠️ Манифест на сервере не найден")

    # ── Build & publish ───────────────────────────────────────────────────────

    def _start_build(self):
        """Запуск фазы сканирования. После — показ диалога подтверждения."""
        cfg = self._build_runtime_config()
        if not cfg["local_dir"]:
            QMessageBox.warning(self, "Ошибка", "Выберите локальную папку!")
            return
        if not Path(cfg["local_dir"]).is_dir():
            QMessageBox.warning(self, "Ошибка", f"Папка не найдена: {cfg['local_dir']}")
            return
        err = self._validate_backend(cfg)
        if err:
            QMessageBox.warning(self, "Ошибка", err)
            return

        self._save_settings()

        self._set_ui_busy(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self._log("🚀 Запуск сканирования...")

        self._worker = DepotBuildWorker(cfg)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.scan_done.connect(self._on_scan_done)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_scan_done(self, new_manifest: DepotManifest, delta: DepotDelta, prev_manifest):
        """Сканирование завершено — показываем диалог подтверждения"""
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)

        if delta.is_empty:
            self._log("✅ Файлы актуальны — обновление не требуется")
            self._on_finished(True, "Депо актуально")
            return

        self._pending_manifest = new_manifest
        self._pending_delta    = delta

        dlg = DepotConfirmDialog(
            parent        = self,
            new_manifest  = new_manifest,
            delta         = delta,
            prev_manifest = prev_manifest,
        )

        if dlg.exec() == dlg.DialogCode.Accepted:
            self._start_upload(new_manifest, delta)
        else:
            self._log("❌ Публикация отменена")
            self._on_finished(False, "Отменено пользователем")

    def _start_upload(self, manifest: DepotManifest, delta: DepotDelta):
        """Запуск фазы загрузки чанков"""
        cfg = self._build_runtime_config()

        self.progress_bar.setRange(0, 0)
        self._log("📤 Начинаем загрузку чанков...")

        self._worker = DepotBuildWorker(
            config             = cfg,
            confirmed_delta    = delta,
            confirmed_manifest = manifest,
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    # ── Controls ──────────────────────────────────────────────────────────────

    def _toggle_pause(self):
        if not self._worker:
            return
        if self._btn_pause_state:
            self._worker.resume()
            self.btn_pause.setText("⏸ Пауза")
            self._btn_pause_state = False
        else:
            self._worker.pause()
            self.btn_pause.setText("▶ Продолжить")
            self._btn_pause_state = True

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._log("⏹ Остановка...")

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        self.progress_label.setText(f"{msg}  [{current}/{total}]")

    def _on_finished(self, ok: bool, msg: str):
        self._cleanup_thread()
        self._set_ui_busy(False)
        self.progress_bar.hide()
        self.progress_label.setText("")

        if ok:
            self._log(f"✅ {msg}")
            QMessageBox.information(self, "Успешно", msg)
        else:
            self._log(f"❌ {msg}")
            if msg and msg != "Отменено пользователем":
                QMessageBox.warning(self, "Ошибка", msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_ui_busy(self, busy: bool):
        self.btn_publish.setEnabled(not busy)
        self.btn_pause.setEnabled(busy)
        self.btn_stop.setEnabled(busy)
        self._btn_pause_state = False
        if busy:
            self.btn_publish.setText("⏳ Работает...")
        else:
            self.btn_publish.setText("📦 Собрать и опубликовать")

    def _cleanup_thread(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        self._worker = None
        self._thread = None

    def _log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_edit.append(f"[{ts}] {msg}")
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )
        self.log_message.emit(msg)
