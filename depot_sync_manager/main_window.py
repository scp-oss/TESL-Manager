# ==================== main_window.py ====================
import os
import json
import threading
from pathlib import Path
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QFileDialog,
    QMessageBox, QGroupBox, QLineEdit, QCheckBox,
    QListWidget, QTabWidget, QFormLayout, QStatusBar,
    QFrame, QSizePolicy
)
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from config import APPDATA_DIR, CONFIG_FILE, DEFAULT_COMPONENTS_CONFIG, COMPONENT_NAMES
from file_selector import FileSelector
from manifest_manager import ManifestManager
from themes import ThemeManager
from theme_dialog import ThemeDialog
from release_tab import ReleaseTab
from server_files_tab import ServerFilesTab
from static_folders_tab import StaticFoldersTab


class StatusBar(QFrame):
    """Компактная статус-панель"""

    folder_change_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedHeight(58)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)

        # ── WebDAV ────────────────────────────────────────────────────────────
        dav_info = QVBoxLayout()
        dav_info.setSpacing(1)
        lbl_dt = QLabel("WebDAV")
        lbl_dt.setStyleSheet("font-size: 10px; color: #888;")
        self.lbl_dav = QLabel("не настроен")
        self.lbl_dav.setFont(QFont("Segoe UI", 9))
        self.lbl_dav.setMinimumWidth(260)
        dav_info.addWidget(lbl_dt)
        dav_info.addWidget(self.lbl_dav)
        layout.addLayout(dav_info)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFixedWidth(1)
        sep1.setStyleSheet("background: #444; margin: 8px 16px;")
        layout.addSpacing(16)
        layout.addWidget(sep1)
        layout.addSpacing(16)

        # ── Компоненты ────────────────────────────────────────────────────────
        comp_info = QVBoxLayout()
        comp_info.setSpacing(1)
        lbl_ct = QLabel("Компоненты")
        lbl_ct.setStyleSheet("font-size: 10px; color: #888;")
        self.lbl_components = QLabel("не настроены")
        self.lbl_components.setFont(QFont("Segoe UI", 9))
        comp_info.addWidget(lbl_ct)
        comp_info.addWidget(self.lbl_components)
        layout.addLayout(comp_info)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFixedWidth(1)
        sep2.setStyleSheet("background: #444; margin: 8px 16px;")
        layout.addSpacing(16)
        layout.addWidget(sep2)
        layout.addSpacing(16)

        # ── Манифест ──────────────────────────────────────────────────────────
        mf_info = QVBoxLayout()
        mf_info.setSpacing(1)
        lbl_mft = QLabel("Манифест")
        lbl_mft.setStyleSheet("font-size: 10px; color: #888;")
        self.lbl_manifest = QLabel("—")
        self.lbl_manifest.setFont(QFont("Segoe UI", 9))
        mf_info.addWidget(lbl_mft)
        mf_info.addWidget(self.lbl_manifest)
        layout.addLayout(mf_info)

        layout.addStretch()

    def set_webdav(self, url: str, remote_path: str):
        if url:
            from urllib.parse import urlparse
            host = urlparse(url).netloc
            self.lbl_dav.setText(f"{host} → {remote_path or '/'}")
            self.lbl_dav.setToolTip(url)
        else:
            self.lbl_dav.setText("не настроен")

    def set_components(self, components: dict):
        """Показать какие компоненты настроены."""
        parts = []
        for name in COMPONENT_NAMES:
            c = components.get(name, {})
            if c.get("local_dir"):
                parts.append(f"✅ {name}")
            else:
                parts.append(f"⚠️ {name}")
        self.lbl_components.setText("  ".join(parts) if parts else "не настроены")

    def set_manifest(self, version: str, file_count: int):
        if version:
            self.lbl_manifest.setText(f"{version}  ({file_count} файлов)")
        else:
            self.lbl_manifest.setText("—")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Uploder — Менеджер релизов")
        self.resize(1150, 820)
        self.setMinimumSize(900, 620)

        self.theme_manager    = ThemeManager()
        self.file_selector    = FileSelector(self)
        self.config           = self.file_selector.config
        self.manifest_manager = ManifestManager(self.config)

        self._create_ui()
        self.theme_manager.apply_theme(self)
        self._connect_signals()
        self._load_initial_data()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _create_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._create_menu()

        self.status_panel = StatusBar()
        root.addWidget(self.status_panel)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.release_tab = ReleaseTab(self)
        self.release_tab.log_message.connect(self.log_message)
        self.tabs.addTab(self.release_tab, "🏷️ Релизы")

        self.server_files_tab = ServerFilesTab(self)
        self.server_files_tab.log_message.connect(self.log_message)
        self.tabs.addTab(self.server_files_tab, "🗂️ Файлы сервера")

        self.static_tab = StaticFoldersTab(self)
        self.static_tab.log_message.connect(self.log_message)
        self.tabs.addTab(self.static_tab, "📦 Статик-папки")

        # Chunk-based депо (TESL-Panel/WebDAV, опционально с упаковкой чанков
        # в pack-файлы) — независимый от ReleaseTab протокол публикации, см.
        # depot_tab.py и CLAUDE.md "Упаковка чанков в pack-файлы".
        from depot_tab import DepotTab
        self.depot_tab = DepotTab(self)
        self.depot_tab.log_message.connect(self.log_message)
        self.tabs.addTab(self.depot_tab, "📦 Депо (chunks)")

        self.tabs.addTab(self._build_settings_tab(), "⚙️ Настройки")
        self.tabs.addTab(self._build_log_tab(), "📝 Лог")

        self.sb = QStatusBar()
        self.setStatusBar(self.sb)
        self.sb.showMessage("Готов")

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _create_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("📁 Файл")
        a2 = QAction("Выход", self)
        a2.setShortcut("Ctrl+Q")
        a2.triggered.connect(self.close)
        fm.addAction(a2)

        sm = mb.addMenu("🌐 Сервер")
        a3 = QAction("Проверить соединение", self)
        a3.triggered.connect(self._test_connection)
        sm.addAction(a3)
        a4 = QAction("Обновить список версий", self)
        a4.triggered.connect(lambda: self.release_tab._refresh_index())
        sm.addAction(a4)

        stm = mb.addMenu("⚙️ Настройки")
        a5 = QAction("Тема…", self)
        a5.triggered.connect(self.show_theme_dialog)
        stm.addAction(a5)

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        # WebDAV
        wbox = QGroupBox("Подключение WebDAV")
        form = QFormLayout(wbox)
        form.setSpacing(8)

        self.webdav_url_edit      = QLineEdit()
        self.webdav_url_edit.setPlaceholderText("https://host/cloud/remote.php/dav/files/user")
        self.webdav_username_edit = QLineEdit()
        self.webdav_password_edit = QLineEdit()
        self.webdav_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.webdav_path_edit     = QLineEdit()
        self.webdav_path_edit.setPlaceholderText("1TB/TESS/Instances/TESVAE")
        self.webdav_ssl_check     = QCheckBox("Проверять SSL сертификат")
        self.webdav_ssl_check.setChecked(True)

        form.addRow("URL сервера:",     self.webdav_url_edit)
        form.addRow("Пользователь:",    self.webdav_username_edit)
        form.addRow("Пароль:",          self.webdav_password_edit)
        form.addRow("Путь на сервере:", self.webdav_path_edit)
        form.addRow("",                 self.webdav_ssl_check)

        btn_row = QHBoxLayout()
        self.btn_save_webdav = QPushButton("💾 Сохранить")
        self.btn_save_webdav.setFixedHeight(36)
        self.btn_test_conn   = QPushButton("🔌 Проверить соединение")
        self.btn_test_conn.setFixedHeight(36)
        btn_row.addWidget(self.btn_save_webdav)
        btn_row.addWidget(self.btn_test_conn)
        btn_row.addStretch()
        form.addRow("", btn_row)

        layout.addWidget(wbox)

        # Hint
        hint = QLabel(
            "💡 Пути к компонентам (Skyrim, MO2p, MO2ext) и их исключения\n"
            "настраиваются на вкладке «Релизы» → блок «Компоненты сборки»."
        )
        hint.setStyleSheet("color: #888; font-size: 9pt; padding: 4px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        return tab

    # ── Log tab ───────────────────────────────────────────────────────────────

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("🗑 Очистить")
        btn_save  = QPushButton("💾 Сохранить")
        btn_clear.setFixedHeight(30)
        btn_save.setFixedHeight(30)
        btn_clear.clicked.connect(self.log_text.clear)
        btn_save.clicked.connect(self._save_log)
        btn_row.addWidget(btn_clear)
        btn_row.addWidget(btn_save)
        btn_row.addStretch()

        layout.addWidget(self.log_text)
        layout.addLayout(btn_row)
        return tab

    # ── Signals ───────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.btn_save_webdav.clicked.connect(self.save_webdav_settings)
        self.btn_test_conn.clicked.connect(self._test_connection)
        self.file_selector.config_updated.connect(self._on_config_updated)

    # ── Initial data ──────────────────────────────────────────────────────────

    def _load_initial_data(self):
        cfg = self.file_selector.config

        # Инициализируем компоненты в конфиге если их нет
        if "components" not in cfg:
            cfg["components"] = {}
        for name in COMPONENT_NAMES:
            if name not in cfg["components"]:
                cfg["components"][name] = dict(DEFAULT_COMPONENTS_CONFIG[name])

        webdav = self.file_selector.get_webdav_config()
        if webdav.get("server_url"):
            self.webdav_url_edit.setText(webdav.get("server_url", ""))
            self.webdav_username_edit.setText(webdav.get("username", ""))
            self.webdav_password_edit.setText(webdav.get("password", ""))
            self.webdav_path_edit.setText(webdav.get("remote_path", ""))
            self.webdav_ssl_check.setChecked(webdav.get("verify_ssl", True))
            self.status_panel.set_webdav(
                webdav.get("server_url", ""),
                webdav.get("remote_path", ""),
            )

        self.status_panel.set_components(cfg.get("components", {}))

        # Читаем manifest.json для статус-панели
        mf = APPDATA_DIR / "manifest.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                files   = data.get("files", {})
                version = data.get("version", data.get("build_id", "—"))
                self.status_panel.set_manifest(version, len(files))
                self.log_message(f"📄 Манифест: {version}, {len(files)} файлов")
            except Exception:
                pass

    # ── WebDAV ────────────────────────────────────────────────────────────────

    def save_webdav_settings(self):
        url      = self.webdav_url_edit.text().strip()
        username = self.webdav_username_edit.text().strip()
        password = self.webdav_password_edit.text()
        path     = self.webdav_path_edit.text().strip()
        ssl      = self.webdav_ssl_check.isChecked()

        if not url:
            QMessageBox.warning(self, "Ошибка", "Введите URL сервера!")
            return
        if not username:
            QMessageBox.warning(self, "Ошибка", "Введите имя пользователя!")
            return

        ok = self.file_selector.set_webdav_config(url, username, password, path, ssl)
        if ok:
            self.status_panel.set_webdav(url, path)
            self.log_message("✅ Настройки WebDAV сохранены")
            QMessageBox.information(self, "Сохранено", "Настройки WebDAV сохранены!")
        else:
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить настройки")

    def _test_connection(self):
        cfg = self.file_selector.config
        if not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Сначала настройте WebDAV!")
            self.tabs.setCurrentIndex(3)
            return
        self.log_message("🔌 Проверяем соединение…")
        self.sb.showMessage("Проверка соединения…")
        from depot_sync_manager import NextcloudDAV
        w = cfg.get("webdav", {})
        dav = NextcloudDAV(
            w.get("server_url", ""),
            w.get("username", ""),
            w.get("password", ""),
            w.get("verify_ssl", True),
        )
        ok, msg = dav.test_connection()
        dav.close()
        if ok:
            self.log_message(f"✅ {msg}")
            self.sb.showMessage(f"✅ {msg}")
            QMessageBox.information(self, "Соединение", f"✅ {msg}")
        else:
            self.log_message(f"❌ {msg}")
            self.sb.showMessage("❌ Ошибка соединения")
            QMessageBox.warning(self, "Ошибка соединения", msg)

    # ── Config updated ────────────────────────────────────────────────────────

    def _on_config_updated(self, config: dict):
        # Обновляем статус-панель компонентов
        self.status_panel.set_components(config.get("components", {}))

    # ── Log ───────────────────────────────────────────────────────────────────

    def log_message(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {msg}")
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить лог",
            str(Path.home() / "uploder_log.txt"),
            "Текстовые файлы (*.txt)"
        )
        if path:
            try:
                Path(path).write_text(self.log_text.toPlainText(), encoding="utf-8")
                self.log_message(f"✅ Лог → {path}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", str(e))

    # ── Theme ─────────────────────────────────────────────────────────────────

    def show_theme_dialog(self):
        dlg = ThemeDialog(self, self.theme_manager.get_current_theme())
        dlg.theme_changed.connect(self._change_theme)
        dlg.exec()

    def _change_theme(self, name: str):
        if self.theme_manager.set_theme(name):
            self.theme_manager.apply_theme(self)

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.file_selector.save_config()
        event.accept()
