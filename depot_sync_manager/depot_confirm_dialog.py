# ==================== depot_confirm_dialog.py ====================
"""
Диалог подтверждения сборки депо.
Показывает delta: новые чанки, изменённые/новые файлы, удалённые файлы.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QListWidget, QTabWidget, QWidget, QGroupBox,
    QTreeWidget, QTreeWidgetItem, QSplitter, QFrame, QTextEdit
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor

from chunk_manager import DepotManifest, DepotDelta


def _fmt_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


class DepotConfirmDialog(QDialog):
    """Диалог подтверждения публикации депо"""

    def __init__(
        self,
        parent=None,
        new_manifest: DepotManifest = None,
        delta: DepotDelta = None,
        prev_manifest: DepotManifest = None,
    ):
        super().__init__(parent)
        self.new_manifest  = new_manifest
        self.delta         = delta
        self.prev_manifest = prev_manifest

        self.setWindowTitle("📦 Подтверждение публикации депо")
        self.resize(900, 650)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Заголовок ────────────────────────────────────────────────────────
        title = QLabel("📦 Публикация обновления депо")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # ── Карточки с инфо ──────────────────────────────────────────────────
        cards_layout = QHBoxLayout()

        # Предыдущая версия
        prev_box = QGroupBox("Текущая версия на сервере")
        prev_inner = QVBoxLayout(prev_box)
        if self.prev_manifest:
            prev_inner.addWidget(QLabel(
                f"Build #{self.prev_manifest.build_number}"
                f"  ({self.prev_manifest.build_id})\n"
                f"Файлов: {len(self.prev_manifest.files)}\n"
                f"Размер: {self.prev_manifest.human_size()}"
            ))
        else:
            prev_inner.addWidget(QLabel("(первая публикация)"))
        cards_layout.addWidget(prev_box)

        # Стрелка
        arrow = QLabel("→")
        arrow.setFont(QFont("Segoe UI", 20))
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cards_layout.addWidget(arrow)

        # Новая версия
        new_box = QGroupBox("Новая версия")
        new_inner = QVBoxLayout(new_box)
        nm = self.new_manifest
        new_inner.addWidget(QLabel(
            f"Build #{nm.build_number}  ({nm.build_id})\n"
            f"Файлов: {len(nm.files)}\n"
            f"Размер: {nm.human_size()}\n"
            f"Канал: {nm.channel}"
        ))
        cards_layout.addWidget(new_box)

        layout.addLayout(cards_layout)

        # ── Сводка delta ─────────────────────────────────────────────────────
        d = self.delta
        summary_text = (
            f"<b>📋 План публикации:</b><br>"
            f"🆕 Новых файлов: <b>{len(d.files_new)}</b><br>"
            f"✏️  Изменённых файлов: <b>{len(d.files_changed)}</b><br>"
            f"✅ Без изменений: <b>{len(d.files_unchanged)}</b><br>"
            f"🗑️  Удалить с сервера: <b>{len(d.files_to_delete)}</b><br>"
            f"📦 Новых чанков к загрузке: <b>{len(d.chunks_to_upload)}</b>"
        )

        # Считаем размер новых чанков
        new_bytes = 0
        for path in d.files_new + d.files_changed:
            entry = nm.files.get(path)
            if entry:
                for chunk in entry.chunks:
                    if chunk.chunk_id in d.chunks_to_upload:
                        new_bytes += chunk.size

        if new_bytes:
            summary_text += f"<br>💾 Объём загрузки: <b>{_fmt_size(new_bytes)}</b>"

        summary_label = QLabel(summary_text)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        # ── Описание сборки ──────────────────────────────────────────────────
        # Прямой запрос пользователя (2026-09-23): "добавь описание к
        # сборке в момент опубликовать" — вводится здесь, а не заранее на
        # странице "Релизы", т.к. к этому моменту уже видна реальная delta
        # (что именно меняется) — писать описание изменений разумнее,
        # когда уже понятно, что публикуется. Необязательное поле,
        # уходит в DepotManifest.description → versions/<id>.json и
        # depot.json (см. chunk_manager.py/depot_sync_manager.py).
        desc_box = QGroupBox("Описание сборки (необязательно)")
        desc_layout = QVBoxLayout(desc_box)
        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText("Что изменилось в этой публикации…")
        self.description_edit.setMaximumHeight(70)
        desc_layout.addWidget(self.description_edit)
        layout.addWidget(desc_box)

        # ── Вкладки с деталями ───────────────────────────────────────────────
        tabs = QTabWidget()

        # Новые файлы
        if d.files_new:
            tabs.addTab(
                self._make_file_tab(d.files_new, nm, "🆕 Новые"),
                f"🆕 Новые ({len(d.files_new)})"
            )

        # Изменённые файлы
        if d.files_changed:
            tabs.addTab(
                self._make_changed_tab(d.files_changed, nm, self.prev_manifest),
                f"✏️ Изменённые ({len(d.files_changed)})"
            )

        # Удаляемые
        if d.files_to_delete:
            tabs.addTab(
                self._make_delete_tab(d.files_to_delete),
                f"🗑️ Удалить ({len(d.files_to_delete)})"
            )

        # Чанки
        if d.chunks_to_upload:
            tabs.addTab(
                self._make_chunks_tab(d.chunks_to_upload, nm),
                f"📦 Чанки ({len(d.chunks_to_upload)})"
            )

        if tabs.count() > 0:
            layout.addWidget(tabs)

        # ── Кнопки ───────────────────────────────────────────────────────────
        btn_layout = QHBoxLayout()

        if d.is_empty:
            info = QLabel("✅ Файлы актуальны — загрузка не требуется")
            btn_layout.addWidget(info)
        else:
            btn_publish = QPushButton("🚀 Опубликовать")
            btn_publish.setMinimumHeight(40)
            btn_publish.setDefault(True)
            btn_publish.clicked.connect(self.accept)
            btn_layout.addWidget(btn_publish)

        btn_cancel = QPushButton("❌ Отмена")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

    def get_description(self) -> str:
        return self.description_edit.toPlainText().strip()

    def _make_file_tab(self, paths: list, manifest: DepotManifest, label: str) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Файл", "Размер", "Чанков"])
        tree.setColumnWidth(0, 500)

        for p in paths[:500]:
            entry = manifest.files.get(p)
            size_str   = _fmt_size(entry.size) if entry else "—"
            chunks_str = str(len(entry.chunks)) if entry else "—"
            tree.addTopLevelItem(QTreeWidgetItem([p, size_str, chunks_str]))

        if len(paths) > 500:
            tree.addTopLevelItem(QTreeWidgetItem([f"... и ещё {len(paths) - 500} файлов", "", ""]))

        vl.addWidget(tree)
        return w

    def _make_changed_tab(self, paths: list, new_m: DepotManifest, prev_m) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Файл", "Было", "Стало", "Новых чанков"])
        tree.setColumnWidth(0, 400)

        for p in paths[:300]:
            new_e  = new_m.files.get(p)
            prev_e = prev_m.files.get(p) if prev_m else None

            old_size = _fmt_size(prev_e.size) if prev_e else "—"
            new_size = _fmt_size(new_e.size) if new_e else "—"

            if new_e and prev_e:
                old_ids = set(prev_e.chunk_ids())
                new_chunk_count = sum(1 for c in new_e.chunks if c.chunk_id not in old_ids)
            else:
                new_chunk_count = len(new_e.chunks) if new_e else 0

            tree.addTopLevelItem(QTreeWidgetItem([p, old_size, new_size, str(new_chunk_count)]))

        vl.addWidget(tree)
        return w

    def _make_delete_tab(self, paths: list) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        warn = QLabel("⚠️ Эти файлы будут удалены из манифеста (чанки сохраняются на сервере):")
        warn.setWordWrap(True)
        vl.addWidget(warn)

        lst = QListWidget()
        for p in paths[:300]:
            lst.addItem(p)
        if len(paths) > 300:
            lst.addItem(f"... и ещё {len(paths) - 300}")
        vl.addWidget(lst)
        return w

    def _make_chunks_tab(self, chunk_ids: set, manifest: DepotManifest) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        note = QLabel(
            f"Content-addressed хранилище: {len(chunk_ids)} новых блоков.\n"
            "Каждый чанк хранится один раз, используется любым количеством файлов."
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        lst = QListWidget()
        for cid in list(chunk_ids)[:200]:
            lst.addItem(cid)
        if len(chunk_ids) > 200:
            lst.addItem(f"... и ещё {len(chunk_ids) - 200}")
        vl.addWidget(lst)
        return w
