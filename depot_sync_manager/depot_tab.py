# ==================== depot_tab.py ====================
"""
Страница "🚀 Релизы" — публикация через chunk-протокол (chunks/<xx>/<id>
или упакованные pack-файлы, см. pack_writer.py). Прямой запрос
пользователя (2026-09-23, третий заход): "депо это часть релизов,
настраивается в настройках либо вебдав либо токен с панели хранилища" —
эта страница больше НЕ содержит выбор backend'а/URL/токена/WebDAV-полей
и НЕ содержит chunk_size/use_packs/pack_size — всё это теперь ЕДИНОЖДЫ
настраивается на странице "⚙️ Настройки" (cfg["backend"]/cfg["panel"]/
cfg["webdav"]/cfg["depot_publish"]) и просто читается отсюда при сборке
runtime-конфига. Здесь остаётся только то, что относится к КОНКРЕТНОЙ
публикации: какие компоненты собирать, в какой канал, и сама публикация.

Компоненты — три независимых локальных папки (Skyrim/MO2p/MO2ext),
каждая с чекбоксом "включить" и своими файловыми исключениями
(ComponentRow/ExcludesDialog из release_tab.py — общий виджет с прежней
вкладкой "Релизы", старый компонентный протокол которой эта страница
теперь заменяет в GUI целиком).

Подключается в main_window.py как страница бокового меню, не как вкладка
QTabWidget — см. MainWindow._create_ui().
"""
from pathlib import Path
from typing import Dict

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QProgressBar, QMessageBox, QInputDialog,
)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont

from chunk_manager import DEFAULT_CHUNK_SIZE, DepotManifest, DepotDelta
from depot_sync_manager import DepotBuildWorker, DepotSyncManager
from pack_writer import DEFAULT_PACK_SIZE
from depot_confirm_dialog import DepotConfirmDialog
from release_tab import ComponentRow
from config import COMPONENT_NAMES, DEFAULT_COMPONENTS_CONFIG

CHANNELS = ["stable", "beta", "dev"]

# "depot_tab" — ключ конфига под компоненты/канал этой страницы (имя
# оставлено историческим ради обратной совместимости с уже сохранённым
# config.json — снаружи эта страница называется "Релизы", но ключ внутри
# файла менять не обязательно, никто кроме кода его не видит).
CFG_KEY = "depot_tab"


class DepotTab(QWidget):
    """Страница "Релизы" — компоненты, канал, публикация."""

    log_message = pyqtSignal(str)   # пробрасываем в центральный лог

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker: DepotBuildWorker = None
        self._thread: QThread = None
        self._btn_pause_state = False
        self._pending_manifest: DepotManifest = None
        self._pending_delta:    DepotDelta = None
        self._comp_rows: Dict[str, ComponentRow] = {}
        self._init_ui()
        self._load_settings()

    def showEvent(self, event):
        super().showEvent(event)
        if self.build_combo.count() == 0 and self._backend_cfg()["panel"].get("base_url"):
            self._refresh_builds()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Сборка (проект на панели) ────────────────────────────────────────
        # Прямой запрос пользователя: выбрать существующую сборку или
        # добавить новую с именем — прямо здесь, на странице "Релизы", а
        # не только на "Настройках" (там остаётся код подключения к
        # панели самой — URL/токен, см. main_window.py). "Сборка" — то же
        # понятие, что TESL-Panel называет "проект"
        # (panel/templates/admin.html: заголовок "Сборки") — combo пишет
        # в тот же cfg["panel"]["project"], который читает
        # _build_runtime_config() ниже.
        build_box = QGroupBox("Сборка")
        build_row = QHBoxLayout(build_box)
        self.build_combo = QComboBox()
        self.build_combo.setMinimumWidth(200)
        self.build_combo.currentIndexChanged.connect(self._save_build_choice)
        build_row.addWidget(self.build_combo, stretch=1)
        btn_refresh_build = QPushButton("🔄")
        btn_refresh_build.setFixedWidth(32)
        btn_refresh_build.setToolTip("Обновить список сборок с сервера")
        btn_refresh_build.clicked.connect(self._refresh_builds)
        build_row.addWidget(btn_refresh_build)
        btn_new_build = QPushButton("➕ Новая сборка")
        btn_new_build.clicked.connect(self._create_new_build)
        build_row.addWidget(btn_new_build)
        root.addWidget(build_box)

        # ── Компоненты сборки (Skyrim / MO2p / MO2ext) ──────────────────────
        comp_box = QGroupBox("Компоненты сборки")
        comp_layout = QVBoxLayout(comp_box)
        comp_layout.setSpacing(6)

        comp_hint = QLabel(
            "Skyrim и MO2 обычно в одной папке — тогда достаточно заполнить\n"
            "«Skyrim» и оставить «MO2ext» пропущенным. Если MO2 и папка модов\n"
            "лежат раздельно — включите «MO2ext» и укажите её отдельно."
        )
        comp_hint.setStyleSheet("font-size: 9pt; color: #888;")
        comp_layout.addWidget(comp_hint)

        saved_components = self._get_config().get(CFG_KEY, {}).get("components", {})
        for comp_name in COMPONENT_NAMES:
            comp_cfg = saved_components.get(comp_name, DEFAULT_COMPONENTS_CONFIG.get(comp_name, {}))
            row = ComponentRow(comp_name, comp_cfg)
            row.setMinimumHeight(34)
            row.changed.connect(self._save_components)
            self._comp_rows[comp_name] = row
            comp_layout.addWidget(row)

        root.addWidget(comp_box)

        # ── Канал + публикация ──────────────────────────────────────────────
        actions_box = QGroupBox("Публикация")
        actions_inner = QVBoxLayout(actions_box)
        actions_inner.setSpacing(8)

        channel_row = QHBoxLayout()
        channel_row.addWidget(QLabel("Канал:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(CHANNELS)
        self.channel_combo.setMaximumWidth(120)
        self.channel_combo.currentIndexChanged.connect(self._save_components)
        channel_row.addWidget(self.channel_combo)
        channel_row.addStretch()
        actions_inner.addLayout(channel_row)

        desc = QLabel(
            "Сканирует включённые компоненты, вычисляет delta относительно сервера,\n"
            "загружает только изменившиеся чанки. Номер сборки на канале — автоматически.\n"
            "Куда публиковать (TESL-Panel/WebDAV) — настраивается на странице «Настройки»."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 9pt; color: #888;")
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
        root.addStretch()

    # ── Shared backend config (Settings-owned, см. main_window.py) ───────────

    def _get_config(self) -> dict:
        return self.mw.file_selector.config

    def _backend_cfg(self) -> dict:
        """Собирает всё, что нужно DepotSyncManager, из общих ключей,
        настроенных на странице «⚙️ Настройки» — эта страница сама их не
        редактирует и не хранит своей копии."""
        cfg = self._get_config()
        publish = cfg.get("depot_publish", {})
        return {
            "backend":   cfg.get("backend", "panel"),
            "use_packs": publish.get("use_packs", True),
            "panel":     cfg.get("panel", {}),
            "webdav":    cfg.get("webdav", {}),
            "publish":   publish,
        }

    # ── Components / channel persistence ─────────────────────────────────────

    def _get_components_cfg(self) -> Dict:
        return {name: row.get_config() for name, row in self._comp_rows.items()}

    def _load_settings(self):
        cfg = self._get_config().get(CFG_KEY, {})
        ch = cfg.get("channel", "stable")
        idx = self.channel_combo.findText(ch)
        if idx >= 0:
            self.channel_combo.setCurrentIndex(idx)

        saved_project = self._get_config().get("panel", {}).get("project", "")
        if saved_project:
            self.build_combo.blockSignals(True)
            self.build_combo.addItem(saved_project)
            self.build_combo.setCurrentIndex(0)
            self.build_combo.blockSignals(False)

    # ── Build (project) selection ─────────────────────────────────────────────

    def _save_build_choice(self, _idx: int = 0):
        cfg = self._get_config()
        panel = dict(cfg.get("panel", {}))
        panel["project"] = self.build_combo.currentText().strip()
        cfg["panel"] = panel
        self.mw.file_selector.save_config()
        if hasattr(self.mw, "status_panel"):
            self.mw.status_panel.set_backend(cfg)

    def _refresh_builds(self):
        panel = self._backend_cfg()["panel"]
        if not panel.get("base_url"):
            QMessageBox.warning(self, "Ошибка", "Сначала подключитесь к панели на странице «⚙️ Настройки»!")
            return
        from panel_client import PanelHTTP
        client = PanelHTTP(base_url=panel["base_url"], project="", token=panel.get("token", ""))
        names = client.list_projects()
        client.close()
        current = self.build_combo.currentText()
        self.build_combo.blockSignals(True)
        self.build_combo.clear()
        self.build_combo.addItems(names)
        if current:
            idx = self.build_combo.findText(current)
            if idx >= 0:
                self.build_combo.setCurrentIndex(idx)
        self.build_combo.blockSignals(False)
        self._log(f"📋 Сборок на панели: {len(names)}")

    def _create_new_build(self):
        panel = self._backend_cfg()["panel"]
        if not panel.get("base_url") or not panel.get("token"):
            QMessageBox.warning(self, "Ошибка", "Сначала подключитесь к панели на странице «⚙️ Настройки»!")
            return
        name, ok = QInputDialog.getText(self, "Новая сборка", "Имя сборки (буквы/цифры/_/-):")
        name = (name or "").strip()
        if not ok or not name:
            return
        from panel_client import PanelHTTP
        client = PanelHTTP(base_url=panel["base_url"], project="", token=panel.get("token", ""))
        created, msg = client.create_project(name)
        client.close()
        if created:
            self._log(f"✅ Сборка создана: {name}")
            self._refresh_builds()
            idx = self.build_combo.findText(name)
            if idx >= 0:
                self.build_combo.setCurrentIndex(idx)
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    def _save_components(self):
        cfg = self._get_config()
        prev = cfg.get(CFG_KEY, {})
        prev["components"] = self._get_components_cfg()
        prev["channel"]    = self.channel_combo.currentText()
        cfg[CFG_KEY] = prev
        self.mw.file_selector.save_config()

    def _build_runtime_config(self) -> dict:
        """Собирает config-словарь ровно в форме, которую ожидает
        DepotSyncManager/DepotBuildWorker — backend/panel/webdav/use_packs/
        depot/components, backend-часть берётся из общих настроек
        (см. _backend_cfg()), сюда добавляются только компоненты и канал."""
        backend_cfg = self._backend_cfg()
        publish = backend_cfg["publish"]
        project = backend_cfg["panel"].get("project", "")
        return {
            "backend":   backend_cfg["backend"],
            "use_packs": backend_cfg["use_packs"],
            "panel":     backend_cfg["panel"],
            "webdav":    backend_cfg["webdav"],
            "depot": {
                "app_id":     project or "app",
                "depot_id":   1,
                "channel":    self.channel_combo.currentText(),
                "chunk_size": publish.get("chunk_size", DEFAULT_CHUNK_SIZE),
                "pack_size":  publish.get("pack_size", DEFAULT_PACK_SIZE),
            },
            "components": self._get_components_cfg(),
        }

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_backend(self, cfg: dict) -> str:
        if cfg["backend"] == "panel":
            if not cfg["panel"].get("base_url") or not cfg["panel"].get("project"):
                return "Настройте URL панели и проект на странице «⚙️ Настройки»!"
        else:
            if not cfg["webdav"].get("server_url"):
                return "Настройте URL WebDAV сервера на странице «⚙️ Настройки»!"
        return ""

    def _validate_components(self, cfg: dict) -> str:
        included = [n for n, c in cfg["components"].items() if c.get("included")]
        if not included:
            return "Включите хотя бы один компонент (Skyrim/MO2p/MO2ext)!"
        missing = [n for n in included if not cfg["components"][n].get("local_dir")]
        if missing:
            return f"Не задан путь для компонентов: {', '.join(missing)}"
        for n in included:
            local_dir = cfg["components"][n]["local_dir"]
            if not Path(local_dir).is_dir():
                return f"Папка не найдена ({n}): {local_dir}"
        return ""

    # ── Connection test / server info ────────────────────────────────────────

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
                f"  Создан: {manifest.created_at[:19]}\n"
                f"  Следующая сборка получит номер #{manifest.build_number + 1}"
            )
            self.server_status_label.setText(info)
            self._log(f"✅ Манифест получен: {manifest.version_label()}")
        else:
            self.server_status_label.setText("⚠️ Манифест не найден (первая публикация?)")
            self._log("⚠️ Манифест на сервере не найден — следующая сборка получит номер #1")

    # ── Build & publish ───────────────────────────────────────────────────────

    def _start_build(self):
        """Запуск фазы сканирования. После — показ диалога подтверждения."""
        cfg = self._build_runtime_config()
        err = self._validate_components(cfg)
        if err:
            QMessageBox.warning(self, "Ошибка", err)
            return
        err = self._validate_backend(cfg)
        if err:
            QMessageBox.warning(self, "Ошибка", err)
            return

        self._save_components()

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
        for row in self._comp_rows.values():
            row.setEnabled(not busy)
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
        self.log_message.emit(msg)
