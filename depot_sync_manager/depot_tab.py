# ==================== depot_tab.py ====================
"""
Вкладка "Депо" для main_window.
Заменяет/дополняет стандартный flow синхронизации.

Подключается к MainWindow:
    self.depot_tab = DepotTab(self)
    self.tab_widget.addTab(self.depot_tab, "📦 Депо")
"""
import json
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QLineEdit, QSpinBox, QFormLayout,
    QProgressBar, QTextEdit, QCheckBox, QSizePolicy, QFrame,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from chunk_manager import DEFAULT_CHUNK_SIZE, DepotManifest, DepotDelta
from depot_sync_manager import DepotBuildWorker, DepotSyncManager
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


class DepotTab(QWidget):
    """Вкладка управления depot-системой"""

    log_message = pyqtSignal(str)   # пробрасываем в основной лог

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window   # ссылка на MainWindow для доступа к config
        self._worker: DepotBuildWorker = None
        self._thread: QThread = None
        # Хранение результата scan для последующей передачи в upload-worker
        self._pending_manifest: DepotManifest = None
        self._pending_delta:    DepotDelta = None
        self._init_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

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

        self.btn_save_settings = QPushButton("💾 Сохранить настройки депо")
        self.btn_save_settings.clicked.connect(self._save_depot_settings)
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

        # Загружаем сохранённые настройки
        self._load_depot_settings()

    # ── Settings persistence ──────────────────────────────────────────────────

    def _get_config(self) -> dict:
        return self.mw.file_selector.config

    def _load_depot_settings(self):
        cfg = self._get_config().get("depot", {})
        self.app_id_edit.setText(cfg.get("app_id", "my_app"))
        self.depot_id_spin.setValue(cfg.get("depot_id", 1001))
        ch = cfg.get("channel", "stable")
        idx = self.channel_combo.findText(ch)
        if idx >= 0:
            self.channel_combo.setCurrentIndex(idx)
        # Выбираем размер чанка
        target_size = cfg.get("chunk_size", DEFAULT_CHUNK_SIZE)
        for i, (_, size) in enumerate(CHUNK_SIZES):
            if size == target_size:
                self.chunk_combo.setCurrentIndex(i)
                break

    def _save_depot_settings(self):
        cfg = self._get_config()
        cfg["depot"] = {
            "app_id":     self.app_id_edit.text().strip() or "my_app",
            "depot_id":   self.depot_id_spin.value(),
            "channel":    self.channel_combo.currentText(),
            "chunk_size": CHUNK_SIZES[self.chunk_combo.currentIndex()][1],
        }
        self.mw.file_selector.save_config()
        self._log("✅ Настройки депо сохранены")

    # ── Connection test ───────────────────────────────────────────────────────

    def _test_connection(self):
        self._log("🔌 Проверяем соединение...")
        cfg = self._get_config()
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
        self._log("📥 Запрашиваем инфо с сервера...")
        cfg = self._get_config()
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
        cfg = self._get_config()
        if not cfg.get("local_dir"):
            QMessageBox.warning(self, "Ошибка", "Выберите локальную папку!")
            return
        if not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Настройте WebDAV сервер!")
            return

        # Сохраняем depot settings перед запуском
        self._save_depot_settings()

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
        # Останавливаем индикатор
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)

        if delta.is_empty:
            self._log("✅ Файлы актуальны — обновление не требуется")
            self._on_finished(True, "Депо актуально")
            return

        # Сохраняем для upload-воркера
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
        cfg = self._get_config()

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
        # Пробрасываем в основной лог MainWindow
        self.log_message.emit(msg)
