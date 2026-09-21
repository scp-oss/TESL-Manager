# ==================== release_tab.py ====================
"""
Вкладка «Релизы» — полностью переработана под компонентный подход.

Три компонента: Skyrim / MO2p / MO2ext
  • Каждый компонент: чекбокс включить/пропустить, поле пути, кнопка 📁
  • Кнопка «Исключения» открывает диалог с папка-пикером
  • Все пути и исключения сохраняются в config["components"] отдельно
    для каждого компонента
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QLineEdit, QTextEdit, QFormLayout,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QSplitter, QAbstractItemView, QSizePolicy, QFrame,
    QCheckBox, QDialog, QListWidget, QFileDialog, QDialogButtonBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QColor

from release_manager import (
    ReleaseManager, ReleaseBuilder, DepotIndex, Release, COMPONENT_NAMES,
)
from config import DEFAULT_COMPONENTS_CONFIG

CHANNELS = ["stable", "beta", "dev"]
CHANNEL_COLORS = {
    "stable": "#1a9b5c",
    "beta":   "#c07a00",
    "dev":    "#666666",
}


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16] if iso else "—"


def _human_size(s: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if s < 1024:
            return f"{s:.1f} {u}"
        s /= 1024
    return f"{s:.1f} TB"


# ── Диалог исключений компонента ─────────────────────────────────────────────

class ExcludesDialog(QDialog):
    """
    Диалог управления исключениями для одного компонента.
    Поддерживает ручной ввод и выбор папки через пикер
    (путь добавляется как относительный от local_dir).
    """

    def __init__(self, parent, comp_name: str, excludes: List[str], local_dir: str = ""):
        super().__init__(parent)
        self.comp_name = comp_name
        self.local_dir = local_dir
        self.setWindowTitle(f"Исключения — {comp_name}")
        self.resize(560, 420)
        self._init_ui(excludes)

    def _init_ui(self, excludes: List[str]):
        layout = QVBoxLayout(self)

        info = QLabel(
            f"Файлы и папки из <b>{self.comp_name}</b> которые не попадут в релиз.<br>"
            "Можно вводить вручную или выбирать папку через 📁."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.lst = QListWidget()
        self.lst.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.lst.addItems(excludes)
        self.lst.setMinimumHeight(180)
        layout.addWidget(self.lst)

        # Добавление
        add_row = QHBoxLayout()
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("*.txt  или  mods/UnwantedMod/  или  downloads/")
        self.add_edit.returnPressed.connect(self._add_manual)
        add_row.addWidget(self.add_edit)

        btn_add = QPushButton("➕")
        btn_add.setFixedWidth(36)
        btn_add.setToolTip("Добавить шаблон вручную")
        btn_add.clicked.connect(self._add_manual)
        add_row.addWidget(btn_add)

        btn_folder = QPushButton("📁")
        btn_folder.setFixedWidth(36)
        btn_folder.setToolTip(
            "Выбрать папку — добавится как относительный путь от корня компонента"
        )
        btn_folder.clicked.connect(self._pick_folder)
        add_row.addWidget(btn_folder)

        layout.addLayout(add_row)

        # Удаление
        btn_del = QPushButton("🗑 Удалить выбранные")
        btn_del.clicked.connect(self._remove_selected)
        layout.addWidget(btn_del)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _add_manual(self):
        text = self.add_edit.text().strip()
        if not text:
            return
        existing = [self.lst.item(i).text() for i in range(self.lst.count())]
        if text not in existing:
            self.lst.addItem(text)
        self.add_edit.clear()

    def _pick_folder(self):
        start = self.local_dir or ""
        folder = QFileDialog.getExistingDirectory(
            self, f"Выберите папку для исключения из {self.comp_name}", start
        )
        if not folder:
            return

        # Вычисляем относительный путь
        rel = folder
        if self.local_dir:
            try:
                rel = str(Path(folder).relative_to(Path(self.local_dir)))
                rel = rel.replace("\\", "/") + "/"
            except ValueError:
                rel = folder  # не внутри local_dir — добавляем как есть

        existing = [self.lst.item(i).text() for i in range(self.lst.count())]
        if rel not in existing:
            self.lst.addItem(rel)

    def _remove_selected(self):
        for item in self.lst.selectedItems():
            self.lst.takeItem(self.lst.row(item))

    def get_excludes(self) -> List[str]:
        return [self.lst.item(i).text() for i in range(self.lst.count())]


# ── Workers ───────────────────────────────────────────────────────────────────

class PublishWorker(QObject):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config, components_cfg, label, channel, notes, auto_activate, prev_index):
        super().__init__()
        self.config         = config
        self.components_cfg = components_cfg
        self.label          = label
        self.channel        = channel
        self.notes          = notes
        self.auto_activate  = auto_activate
        self.prev_index     = prev_index
        self._stop          = False

    def stop(self): self._stop = True

    def run(self):
        try:
            cfg     = self.config
            builder = ReleaseBuilder()
            builder.log.connect(self.log)
            builder.progress.connect(self.progress)

            rm = ReleaseManager(cfg)
            rm.log.connect(self.log)
            rm.progress.connect(self.progress)

            build_number = self.prev_index.next_build_number if self.prev_index else 1

            self.log.emit("🔨 Индексируем компоненты…")
            manifest, release = builder.build_manifest(
                components_cfg = self.components_cfg,
                label          = self.label,
                build_number   = build_number,
                channel        = self.channel,
                notes          = self.notes,
            )

            if manifest is None or self._stop:
                self.finished.emit(False, "Прервано" if self._stop else "Ошибка индексирования")
                return

            if not rm.ensure_structure():
                self.finished.emit(False, "Не удалось создать структуру на сервере")
                return

            ok, msg = rm.publish_release(
                manifest       = manifest,
                release        = release,
                components_cfg = self.components_cfg,
                prev_index     = self.prev_index,
                stop_fn        = lambda: self._stop,
            )

            if ok and self.auto_activate:
                self.log.emit(f"🔀 Активируем на канале '{self.channel}'…")
                ok2, msg2 = rm.set_current(release.version_key, self.channel)
                self.log.emit(f"  {'✅' if ok2 else '❌'} {msg2}")

            rm.close()
            self.finished.emit(ok, msg)
        except Exception as e:
            import traceback
            self.log.emit(f"❌ {e}\n{traceback.format_exc()}")
            self.finished.emit(False, str(e))


class VerifyWorker(QObject):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config, manifest_manager):
        super().__init__()
        self.config           = config
        self.manifest_manager = manifest_manager
        self._stop            = False

    def stop(self): self._stop = True

    def run(self):
        try:
            from config import APPDATA_DIR
            local_mf = APPDATA_DIR / "manifest.json"

            if not local_mf.exists():
                self.log.emit("📥 Скачиваем manifest.json с сервера…")
                try:
                    from depot_sync_manager import NextcloudDAV
                    from release_manager import COMPAT_MANIFEST
                    cfg = self.config.get("webdav", {})
                    rp  = cfg.get("remote_path", "").strip("/")
                    dav = NextcloudDAV(
                        cfg.get("server_url", ""),
                        cfg.get("username", ""),
                        cfg.get("password", ""),
                        cfg.get("verify_ssl", True),
                    )
                    path = f"{rp}/{COMPAT_MANIFEST}" if rp else COMPAT_MANIFEST
                    data = dav.get_bytes(path)
                    dav.close()
                    if data:
                        local_mf.write_bytes(data)
                    else:
                        self.finished.emit(False, "manifest.json не найден ни локально ни на сервере")
                        return
                except Exception as e:
                    self.finished.emit(False, f"Не удалось получить manifest.json: {e}")
                    return

            self.log.emit("🔍 Проверяем файлы…")
            ok, result = self.manifest_manager.verify_local_files()

            if not ok:
                self.finished.emit(False, str(result))
                return

            if isinstance(result, dict):
                v    = result["valid_files"]
                t    = result["total_files"]
                miss = len(result["missing_files"])
                corr = len(result["corrupted_files"])
                if result["status"] == "perfect":
                    self.finished.emit(True, f"Все {t} файлов в порядке")
                else:
                    self.finished.emit(False, f"Проверено: {v}/{t} | Отсутствует: {miss} | Повреждено: {corr}")
            else:
                self.finished.emit(True, str(result))
        except Exception as e:
            self.finished.emit(False, str(e))


class ResumeWorker(QObject):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, config, version_key, components_cfg):
        super().__init__()
        self.config         = config
        self.version_key    = version_key
        self.components_cfg = components_cfg
        self._stop          = False

    def stop(self): self._stop = True

    def run(self):
        try:
            rm = ReleaseManager(self.config)
            rm.log.connect(self.log)
            rm.progress.connect(self.progress)
            ok, msg = rm.resume_failed_release(
                version_key    = self.version_key,
                components_cfg = self.components_cfg,
                stop_fn        = lambda: self._stop,
            )
            rm.close()
            self.finished.emit(ok, msg)
        except Exception as e:
            import traceback
            self.log.emit(f"❌ {e}\n{traceback.format_exc()}")
            self.finished.emit(False, str(e))


# ── ComponentRow ──────────────────────────────────────────────────────────────

class ComponentRow(QWidget):
    """
    Строка одного компонента в UI публикации:
      [✓] Skyrim    [D:\path\to\Skyrim\     📁]   [Исключения (0)]
    """

    changed = pyqtSignal()

    def __init__(self, comp_name: str, comp_cfg: dict, parent=None):
        super().__init__(parent)
        self.comp_name = comp_name
        self._excludes: List[str] = list(comp_cfg.get("excludes", []))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)

        # Чекбокс
        self.check = QCheckBox(f"<b>{comp_name}</b>")
        self.check.setChecked(comp_cfg.get("included", True))
        self.check.setFixedWidth(90)
        self.check.stateChanged.connect(self._on_toggle)
        layout.addWidget(self.check)

        # Поле пути
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(f"Путь к папке {comp_name}/")
        self.path_edit.setText(comp_cfg.get("local_dir", ""))
        self.path_edit.setMinimumWidth(220)
        self.path_edit.textChanged.connect(self.changed)
        layout.addWidget(self.path_edit, stretch=1)

        # Кнопка выбора папки
        btn_pick = QPushButton("📁")
        btn_pick.setFixedWidth(34)
        btn_pick.setToolTip(f"Выбрать папку {comp_name}")
        btn_pick.clicked.connect(self._pick_folder)
        layout.addWidget(btn_pick)

        # Кнопка исключений
        self.btn_excl = QPushButton(f"Исключения ({len(self._excludes)})")
        self.btn_excl.setFixedWidth(140)
        self.btn_excl.setToolTip("Файлы и папки, не попадающие в релиз")
        self.btn_excl.clicked.connect(self._open_excludes)
        layout.addWidget(self.btn_excl)

        self._on_toggle()

    def _on_toggle(self):
        enabled = self.check.isChecked()
        self.path_edit.setEnabled(enabled)
        self.btn_excl.setEnabled(enabled)
        self.changed.emit()

    def _pick_folder(self):
        start = self.path_edit.text().strip() or ""
        folder = QFileDialog.getExistingDirectory(
            self, f"Выберите папку для компонента {self.comp_name}", start
        )
        if folder:
            self.path_edit.setText(os.path.normpath(folder))
            self.changed.emit()

    def _open_excludes(self):
        dlg = ExcludesDialog(
            self,
            comp_name  = self.comp_name,
            excludes   = self._excludes,
            local_dir  = self.path_edit.text().strip(),
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._excludes = dlg.get_excludes()
            self.btn_excl.setText(f"Исключения ({len(self._excludes)})")
            self.changed.emit()

    def get_config(self) -> dict:
        return {
            "local_dir": self.path_edit.text().strip(),
            "included":  self.check.isChecked(),
            "excludes":  list(self._excludes),
        }


# ── ReleaseTab ────────────────────────────────────────────────────────────────

class ReleaseTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw             = main_window
        self._index:        Optional[DepotIndex] = None
        self._worker        = None
        self._thread        = None
        self._selected_key: Optional[str] = None
        self._comp_rows:    Dict[str, ComponentRow] = {}
        self._init_ui()
        QTimer.singleShot(300, self._refresh_index)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        # ══════════════════════════════════════════════════════════════════════
        # ВЕРХ: Новый релиз
        # ══════════════════════════════════════════════════════════════════════
        top = QWidget()
        top_v = QVBoxLayout(top)
        top_v.setContentsMargins(0, 0, 0, 0)
        top_v.setSpacing(6)

        publish_box = QGroupBox("Публикация релиза")
        pub_layout  = QVBoxLayout(publish_box)
        pub_layout.setSpacing(8)

        # ── Метка, канал, режим ───────────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Версия:"))
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("v2.1.0")
        self.label_edit.setMaximumWidth(120)
        row1.addWidget(self.label_edit)

        row1.addSpacing(16)
        row1.addWidget(QLabel("Канал:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(CHANNELS)
        self.channel_combo.setMaximumWidth(100)
        row1.addWidget(self.channel_combo)

        row1.addSpacing(16)
        self.auto_activate_check = QComboBox()
        self.auto_activate_check.addItems([
            "Опубликовать (без активации)",
            "Опубликовать и активировать",
        ])
        self.auto_activate_check.setMinimumWidth(220)
        row1.addWidget(self.auto_activate_check)
        row1.addStretch()
        pub_layout.addLayout(row1)

        # ── Заметки ───────────────────────────────────────────────────────────
        notes_row = QHBoxLayout()
        notes_row.addWidget(QLabel("Заметки:"))
        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("Что изменилось в этой версии…")
        notes_row.addWidget(self.notes_edit)
        pub_layout.addLayout(notes_row)

        # ── Компоненты ────────────────────────────────────────────────────────
        comp_box = QGroupBox("Компоненты сборки")
        comp_layout = QVBoxLayout(comp_box)
        comp_layout.setSpacing(4)

        comp_hint = QLabel(
            "Включите компоненты которые обновились в этом релизе.\n"
            "Невключённые компоненты клиент возьмёт из предыдущей версии."
        )
        comp_hint.setStyleSheet("font-size: 9pt; color: #888;")
        comp_layout.addWidget(comp_hint)

        cfg = self._get_config()
        saved_components = cfg.get("components", {})

        for comp_name in COMPONENT_NAMES:
            comp_cfg = saved_components.get(comp_name, DEFAULT_COMPONENTS_CONFIG.get(comp_name, {}))
            row = ComponentRow(comp_name, comp_cfg)
            row.changed.connect(self._on_component_changed)
            self._comp_rows[comp_name] = row
            comp_layout.addWidget(row)

        pub_layout.addWidget(comp_box)

        # ── Кнопки действий ───────────────────────────────────────────────────
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)

        self.btn_publish = QPushButton("🚀 Создать релиз")
        self.btn_publish.setMinimumHeight(38)
        self.btn_publish.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.btn_publish.clicked.connect(self._on_publish)
        actions_row.addWidget(self.btn_publish)

        self.btn_verify = QPushButton("🔍 Проверить файлы")
        self.btn_verify.setMinimumHeight(38)
        self.btn_verify.clicked.connect(self._on_verify)
        actions_row.addWidget(self.btn_verify)

        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_stop.setMinimumHeight(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        actions_row.addWidget(self.btn_stop)

        actions_row.addStretch()
        pub_layout.addLayout(actions_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        pub_layout.addWidget(self.progress_bar)

        top_v.addWidget(publish_box)
        splitter.addWidget(top)

        # ══════════════════════════════════════════════════════════════════════
        # НИЗ: Версии на сервере
        # ══════════════════════════════════════════════════════════════════════
        bottom = QWidget()
        bot_v  = QVBoxLayout(bottom)
        bot_v.setContentsMargins(0, 0, 0, 0)
        bot_v.setSpacing(6)

        versions_box = QGroupBox("Версии на сервере")
        vl = QVBoxLayout(versions_box)
        vl.setSpacing(6)

        ver_toolbar = QHBoxLayout()
        ver_toolbar.setSpacing(4)
        BTN_H = 32

        self.btn_refresh = QPushButton("🔄 Обновить")
        self.btn_refresh.setFixedHeight(BTN_H)
        self.btn_refresh.setMinimumWidth(100)
        self.btn_refresh.clicked.connect(self._refresh_index)
        ver_toolbar.addWidget(self.btn_refresh)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        ver_toolbar.addSpacing(6)
        ver_toolbar.addWidget(sep)
        ver_toolbar.addSpacing(6)

        self.btn_activate = QPushButton("✅ Активировать на канале")
        self.btn_activate.setEnabled(False)
        self.btn_activate.setFixedHeight(BTN_H)
        self.btn_activate.clicked.connect(self._on_activate)
        ver_toolbar.addWidget(self.btn_activate)

        self.activate_channel_combo = QComboBox()
        self.activate_channel_combo.addItems(CHANNELS)
        self.activate_channel_combo.setFixedWidth(80)
        self.activate_channel_combo.setFixedHeight(BTN_H)
        ver_toolbar.addWidget(self.activate_channel_combo)

        ver_toolbar.addSpacing(6)
        self.btn_rollback = QPushButton("↩️ Откат")
        self.btn_rollback.setFixedHeight(BTN_H)
        self.btn_rollback.setMinimumWidth(90)
        self.btn_rollback.clicked.connect(self._on_rollback)
        ver_toolbar.addWidget(self.btn_rollback)

        self.rollback_channel_combo = QComboBox()
        self.rollback_channel_combo.addItems(CHANNELS)
        self.rollback_channel_combo.setFixedWidth(80)
        self.rollback_channel_combo.setFixedHeight(BTN_H)
        ver_toolbar.addWidget(self.rollback_channel_combo)

        ver_toolbar.addSpacing(6)
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFixedWidth(1)
        ver_toolbar.addWidget(sep2)
        ver_toolbar.addSpacing(6)

        self.btn_delete = QPushButton("🗑 Удалить")
        self.btn_delete.setEnabled(False)
        self.btn_delete.setFixedHeight(BTN_H)
        self.btn_delete.setMinimumWidth(90)
        self.btn_delete.clicked.connect(self._on_delete)
        ver_toolbar.addWidget(self.btn_delete)

        ver_toolbar.addSpacing(6)
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setFixedWidth(1)
        ver_toolbar.addWidget(sep3)
        ver_toolbar.addSpacing(6)

        self.btn_resume = QPushButton("🔁 Дозалить")
        self.btn_resume.setEnabled(False)
        self.btn_resume.setFixedHeight(BTN_H)
        self.btn_resume.setMinimumWidth(100)
        self.btn_resume.clicked.connect(self._on_resume)
        ver_toolbar.addWidget(self.btn_resume)

        ver_toolbar.addStretch()
        vl.addLayout(ver_toolbar)

        self.channel_status = QLabel("Каналы: —")
        self.channel_status.setStyleSheet("font-size: 11px; padding: 2px 0;")
        vl.addWidget(self.channel_status)

        # Таблица
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Версия", "Build ID", "Канал", "Компоненты", "Файлов", "Размер", "Дата"]
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 7):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(140)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        vl.addWidget(self.table)

        self.notes_label = QLabel()
        self.notes_label.setWordWrap(True)
        self.notes_label.setStyleSheet("font-style: italic; font-size: 10px; padding: 2px 0;")
        vl.addWidget(self.notes_label)

        bot_v.addWidget(versions_box)

        self.op_log = QTextEdit()
        self.op_log.setReadOnly(True)
        self.op_log.setFont(QFont("Consolas", 8))
        self.op_log.setMaximumHeight(100)
        self.op_log.setPlaceholderText("Лог операций…")
        bot_v.addWidget(self.op_log)

        splitter.addWidget(bottom)
        splitter.setSizes([320, 480])
        root.addWidget(splitter)

    # ── Component config helpers ──────────────────────────────────────────────

    def _on_component_changed(self):
        """Сохраняем состояние компонентов в config при любом изменении."""
        self._save_components_to_config()

    def _save_components_to_config(self):
        cfg = self._get_config()
        cfg["components"] = {
            name: row.get_config()
            for name, row in self._comp_rows.items()
        }
        self.mw.file_selector.save_config()

    def _get_components_cfg(self) -> Dict:
        return {
            name: row.get_config()
            for name, row in self._comp_rows.items()
        }

    # ── Publish ───────────────────────────────────────────────────────────────

    def _on_publish(self):
        cfg = self._get_config()
        if not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Настройте WebDAV в настройках!")
            self.mw.tabs.setCurrentIndex(3)  # вкладка настроек
            return

        label = self.label_edit.text().strip()
        if not label:
            QMessageBox.warning(self, "Ошибка", "Введите метку версии (например v2.1.0)!")
            self.label_edit.setFocus()
            return

        components_cfg = self._get_components_cfg()

        # Проверяем что хотя бы один компонент включён
        included = [n for n, c in components_cfg.items() if c.get("included")]
        if not included:
            QMessageBox.warning(self, "Ошибка", "Включите хотя бы один компонент!")
            return

        # Проверяем что у включённых компонентов заданы пути
        missing = [n for n in included if not components_cfg[n].get("local_dir")]
        if missing:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не задан путь для компонентов: {', '.join(missing)}"
            )
            return

        channel       = self.channel_combo.currentText()
        notes         = self.notes_edit.text().strip()
        auto_activate = self.auto_activate_check.currentIndex() == 1

        # Формируем сводку для подтверждения
        comp_lines = []
        for name in COMPONENT_NAMES:
            c = components_cfg[name]
            if c.get("included"):
                excl = len(c.get("excludes", []))
                comp_lines.append(
                    f"  ✅ {name}: {c['local_dir']}"
                    + (f"  ({excl} исключ.)" if excl else "")
                )
            else:
                comp_lines.append(f"  ⏭ {name}: пропущен")

        confirm_text = (
            f"Опубликовать <b>{label}</b> на канале <b>{channel}</b>?<br><br>"
            + "<br>".join(comp_lines)
        )
        if auto_activate:
            confirm_text += f"<br><br><i>Лаунчеры начнут скачивать эту версию немедленно.</i>"

        ans = QMessageBox.question(
            self, "Подтверждение",
            confirm_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        # Сохраняем компоненты перед запуском
        self._save_components_to_config()

        self._set_busy(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()

        self._worker = PublishWorker(
            cfg, components_cfg, label, channel, notes, auto_activate, self._index
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    # ── Verify ────────────────────────────────────────────────────────────────

    def _on_verify(self):
        self._set_busy(True)
        self._log("🔍 Запускаем проверку файлов…")
        self._worker = VerifyWorker(self._get_config(), self.mw.manifest_manager)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.log.connect(self._log)
        self._worker.finished.connect(self._on_verify_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_verify_finished(self, ok: bool, msg: str):
        self._cleanup_thread()
        self._set_busy(False)
        self.progress_bar.hide()
        if ok:
            self._log(f"✅ {msg}")
            QMessageBox.information(self, "Проверка", msg)
        else:
            self._log(f"⚠️ {msg}")
            QMessageBox.warning(self, "Проблемы с файлами", msg)

    # ── Activate ──────────────────────────────────────────────────────────────

    def _on_activate(self):
        if not self._selected_key or self._index is None:
            return
        release = self._index.versions.get(self._selected_key)
        if release is None:
            return
        channel = self.activate_channel_combo.currentText()
        ans = QMessageBox.question(
            self, "Активация",
            f"Сделать <b>{release.label}</b> активной версией канала <b>{channel}</b>?<br>"
            f"Лаунчеры начнут скачивать эту версию.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        rm = ReleaseManager(self._get_config())
        rm.log.connect(self._log)
        ok, msg = rm.set_current(self._selected_key, channel)
        rm.close()
        self._log(f"{'✅' if ok else '❌'} {msg}")
        if ok:
            self._refresh_index()
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    # ── Rollback ──────────────────────────────────────────────────────────────

    def _on_rollback(self):
        if self._index is None:
            QMessageBox.warning(self, "Ошибка", "Загрузите список версий")
            return
        channel  = self.rollback_channel_combo.currentText()
        versions = self._index.get_by_channel(channel)
        if len(versions) < 2:
            QMessageBox.information(self, "Откат", f"Нет предыдущей версии в канале '{channel}'")
            return
        sorted_v    = sorted(versions, key=lambda r: r.build_number, reverse=True)
        current_key = self._index.current.get(channel, "")
        cur_idx     = next((i for i, r in enumerate(sorted_v) if r.version_key == current_key), 0)
        if cur_idx + 1 >= len(sorted_v):
            QMessageBox.information(self, "Откат", "Нет более старой версии")
            return
        current = sorted_v[cur_idx]
        target  = sorted_v[cur_idx + 1]
        ans = QMessageBox.question(
            self, "Откат",
            f"Откатить канал <b>{channel}</b>:<br>"
            f"<b>{current.label}</b> → <b>{target.label}</b> ({target.version_key})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        rm = ReleaseManager(self._get_config())
        rm.log.connect(self._log)
        ok, msg, _ = rm.rollback(channel)
        rm.close()
        self._log(f"{'↩️' if ok else '❌'} {msg}")
        if ok:
            self._refresh_index()
            QMessageBox.information(self, "Откат", msg)
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    # ── Delete ────────────────────────────────────────────────────────────────

    def _on_delete(self):
        if not self._selected_key:
            return
        ans = QMessageBox.question(
            self, "Удаление",
            f"Удалить версию <b>{self._selected_key}</b> из индекса?<br>"
            f"Файлы на сервере сохранятся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        rm = ReleaseManager(self._get_config())
        rm.log.connect(self._log)
        ok, msg = rm.delete_release(self._selected_key)
        rm.close()
        self._log(f"{'✅' if ok else '❌'} {msg}")
        if ok:
            self._refresh_index()
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    # ── Resume ────────────────────────────────────────────────────────────────

    def _on_resume(self):
        if not self._selected_key:
            return
        ans = QMessageBox.question(
            self, "Дозаливка",
            f"Дозалить недостающие файлы для <b>{self._selected_key}</b>?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self._log(f"🔁 Дозаливка {self._selected_key}…")

        self._worker = ResumeWorker(
            self._get_config(), self._selected_key, self._get_components_cfg()
        )
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

    # ── Index refresh ─────────────────────────────────────────────────────────

    def _refresh_index(self):
        self._log("🔄 Загружаем depot.json…")
        cfg = self._get_config()
        if not cfg.get("webdav", {}).get("server_url"):
            self._log("⚠️ WebDAV не настроен")
            return
        rm    = ReleaseManager(cfg)
        rm.log.connect(self._log)
        index = rm.fetch_index()
        rm.close()
        if index is None:
            self._log("ℹ️ depot.json не найден — первый релиз")
            self._index = None
            self._update_table(None)
            self.channel_status.setText("Сервер пуст (первый релиз)")
            return
        self._index = index
        self._update_table(index)
        parts = []
        for ch in CHANNELS:
            cur = index.get_current(ch)
            if cur:
                color = CHANNEL_COLORS.get(ch, "#333")
                parts.append(f'<span style="color:{color}"><b>{ch}</b>: {cur.label}</span>')
        self.channel_status.setText("  |  ".join(parts) if parts else "Нет активных версий")
        self._log(f"✅ Версий: {len(index.versions)}")

    def _update_table(self, index: Optional[DepotIndex]):
        self.table.setRowCount(0)
        if index is None:
            return
        active_keys = set(index.current.values())
        for r in index.get_all_sorted():
            row = self.table.rowCount()
            self.table.insertRow(row)

            label_item = QTableWidgetItem(
                f"★ {r.label}" if r.version_key in active_keys else r.label
            )
            label_item.setData(Qt.ItemDataRole.UserRole, r.version_key)
            if r.version_key in active_keys:
                label_item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            self.table.setItem(row, 0, label_item)
            self.table.setItem(row, 1, QTableWidgetItem(r.build_id))

            ch_item = QTableWidgetItem(r.channel)
            ch_item.setForeground(QColor(CHANNEL_COLORS.get(r.channel, "#333")))
            self.table.setItem(row, 2, ch_item)

            # Колонка компонентов
            comp_info = r.components
            comp_str = "  ".join(
                f"{'✅' if comp_info.get(n, {}).get('included', True) else '⏭'} {n}"
                for n in COMPONENT_NAMES
                if n in comp_info
            ) if comp_info else "—"
            self.table.setItem(row, 3, QTableWidgetItem(comp_str))
            self.table.setItem(row, 4, QTableWidgetItem(str(r.file_count)))
            self.table.setItem(row, 5, QTableWidgetItem(r.human_size()))
            self.table.setItem(row, 6, QTableWidgetItem(_fmt_dt(r.created_at)))

    # ── Selection ─────────────────────────────────────────────────────────────

    def _on_selection(self):
        row  = self.table.currentRow()
        item = self.table.item(row, 0)
        if item and self._index:
            self._selected_key = item.data(Qt.ItemDataRole.UserRole)
            release = self._index.versions.get(self._selected_key)
            self.btn_activate.setEnabled(True)
            self.btn_delete.setEnabled(True)
            self.btn_resume.setEnabled(True)
            is_incomplete = release and "[incomplete:" in (release.notes or "")
            self.btn_resume.setText("🔁 Дозалить ⚠️" if is_incomplete else "🔁 Дозалить")
            import re as _re
            notes_text = release.notes or "" if release else ""
            notes_display = _re.sub(r'\n?\[incomplete:[^\]]+\]', "", notes_text).strip()
            self.notes_label.setText(f"📝 {notes_display}" if notes_display else "")
        else:
            self._selected_key = None
            self.btn_activate.setEnabled(False)
            self.btn_delete.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.notes_label.setText("")

    # ── Worker utils ──────────────────────────────────────────────────────────

    def _on_progress(self, cur, total, msg):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(cur)
            self.progress_bar.setFormat(f"{msg}  [{cur}/{total}]")

    def _on_finished(self, ok: bool, msg: str):
        self._cleanup_thread()
        self._set_busy(False)
        self.progress_bar.hide()
        self._log(f"{'✅' if ok else '❌'} {msg}")
        if ok:
            QMessageBox.information(self, "Готово", msg)
            self._refresh_index()
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    def _cleanup_thread(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool):
        self.btn_publish.setEnabled(not busy)
        self.btn_verify.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.btn_refresh.setEnabled(not busy)
        self.btn_activate.setEnabled(not busy and bool(self._selected_key))
        self.btn_rollback.setEnabled(not busy)
        self.btn_delete.setEnabled(not busy and bool(self._selected_key))
        self.btn_resume.setEnabled(not busy and bool(self._selected_key))
        for row in self._comp_rows.values():
            row.setEnabled(not busy)

    def _get_config(self) -> dict:
        return self.mw.file_selector.config

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.op_log.append(f"[{ts}] {msg}")
        self.op_log.verticalScrollBar().setValue(self.op_log.verticalScrollBar().maximum())
        self.log_message.emit(msg)
