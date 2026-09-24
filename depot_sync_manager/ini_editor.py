# ==================== ini_editor.py ====================
"""
Структурный редактор .ini-файлов (checkbox'ы для булевых значений,
текстовые поля для остального, сгруппировано по секциям) — прямой
запрос пользователя ("прикрути гу редактор с чекбоксами и тд к ини
файлу скайрима"), вместо голого текстового поля (`FileEditDialog` в
`depot_files_tab.py`, которое остаётся как есть для всех остальных
файлов — этот редактор открывается ТОЛЬКО для `.ini` внутри
`documents/`, см. `documents_tab.py`).

Реальные .ini Skyrim'а (Skyrim.ini/SkyrimPrefs.ini и т.п.) — не
стандартный Python `configparser`-формат: там бывают дублирующиеся
секции/ключи, инлайн-комментарии после `;`, и людям важно не терять
форматирование при правке через GUI. `configparser.write()` полностью
переформатирует файл и теряет комментарии — использовать его для
round-trip запись было бы разрушительно для реального чужого конфига.
Вместо этого — построчный парсер, который:

  - распознаёт `[Section]`-заголовки и `key=value`-строки (с
    сохранением отступа/пробелов вокруг `=`/инлайн-комментария после
    `;`/`#`);
  - ВСЁ остальное (пустые строки, отдельностоящие комментарии, строки,
    не похожие на `key=value`) сохраняет как есть, посимвольно;
  - при сохранении переписывает ТОЛЬКО те `key=value`-строки, чьё
    значение реально изменили в GUI — форматирование строки вокруг
    (отступ/пробелы/инлайн-комментарий) остаётся тем же, меняется
    только сама подстрока значения.

Булево-подобные значения (`0`/`1`, `true`/`false`, `yes`/`no` —
регистронезависимо, ровно то и только то, что Skyrim/Bethesda-движок
сам трактует как булево в .ini) рендерятся чекбоксом; при сохранении
чекбокса результат пишется в ТОМ ЖЕ стиле, что было в файле изначально
(если было `1` — пишем `0`/`1`, если было `true` — пишем `true`/`false`
той же регистровой формы), а не жёстко перезаписывается одним
каноничным видом. Всё остальное — обычное текстовое поле.
"""
import re
from typing import List, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget, QGroupBox,
    QFormLayout, QLabel, QLineEdit, QCheckBox, QDialogButtonBox,
    QMessageBox, QToolButton,
)

_SECTION_RE = re.compile(r"^(?P<pre>\s*)\[(?P<name>[^\]]+)\](?P<post>.*)$")
_KV_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[^=\[\];#\s][^=]*?)"
    r"(?P<sep>\s*=\s*)"
    r"(?P<value>[^;#]*?)"
    r"(?P<trail>\s*(?:[;#].*)?)$"
)

# Ровно то, что Skyrim/Creation-движок принимает как булево значение в .ini —
# не расширяем этот список произвольно (см. докстринг модуля).
_BOOL_STYLES = {
    "0": ("0", "1"), "1": ("0", "1"),
    "true": ("false", "true"), "false": ("false", "true"),
    "yes": ("no", "yes"), "no": ("no", "yes"),
}


class _KVLine:
    __slots__ = ("indent", "key", "sep", "value", "trail", "section")

    def __init__(self, indent, key, sep, value, trail, section):
        self.indent  = indent
        self.key     = key
        self.sep     = sep
        self.value   = value
        self.trail   = trail
        self.section = section

    def render(self, new_value: Optional[str] = None) -> str:
        v = self.value if new_value is None else new_value
        return f"{self.indent}{self.key}{self.sep}{v}{self.trail}"


def parse_ini(text: str) -> List:
    """Список строк модели — каждая либо _KVLine (редактируемая), либо
    голая строка (всё остальное, включая заголовки секций и
    комментарии — сохраняется без изменений при сборке обратно)."""
    entries = []
    section = ""
    # splitlines(keepends=True) — сохраняем реальные окончания строк файла
    # (\n/\r\n), чтобы не менять line-ending стиль всего файла ради правки
    # одного значения.
    for raw in text.splitlines(keepends=True):
        line_no_end = raw.rstrip("\r\n")
        m_sec = _SECTION_RE.match(line_no_end)
        if m_sec:
            section = m_sec.group("name").strip()
            entries.append(raw)
            continue
        m_kv = _KV_RE.match(line_no_end)
        if m_kv and m_kv.group("key").strip():
            ending = raw[len(line_no_end):]
            entries.append(_KVLine(
                indent  = m_kv.group("indent"),
                key     = m_kv.group("key"),
                sep     = m_kv.group("sep"),
                value   = m_kv.group("value"),
                trail   = m_kv.group("trail") + ending,
                section = section,
            ))
            continue
        entries.append(raw)
    return entries


def render_ini(entries: List) -> str:
    out = []
    for e in entries:
        out.append(e.render() if isinstance(e, _KVLine) else e)
    return "".join(out)


def is_bool_like(value: str) -> bool:
    return value.strip().lower() in _BOOL_STYLES


class IniEditorDialog(QDialog):
    """rel_path — только для заголовка окна. text — исходное содержимое
    файла. get_text() после успешного exec() (Accepted) возвращает
    пересобранный текст с применёнными правками."""

    def __init__(self, parent, rel_path: str, text: str):
        super().__init__(parent)
        self.rel_path = rel_path
        self.setWindowTitle(f"Редактор настроек — {rel_path}")
        self.resize(720, 640)

        try:
            self._entries = parse_ini(text)
        except Exception as e:
            self._entries = None
            self._parse_error = str(e)
        else:
            self._parse_error = None

        self._fields = []  # [(_KVLine, QCheckBox|QLineEdit, orig_style_low_high)]
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        if self._entries is None:
            layout.addWidget(QLabel(f"⚠️ Не удалось разобрать файл: {self._parse_error}"))
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
            btns.rejected.connect(self.reject)
            layout.addWidget(btns)
            return

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)

        sections: "dict[str, QFormLayout]" = {}

        def _section_form(name: str) -> QFormLayout:
            if name not in sections:
                box = QGroupBox(name or "(без секции)")
                form = QFormLayout(box)
                inner_layout.addWidget(box)
                sections[name] = form
            return sections[name]

        n_fields = 0
        for entry in self._entries:
            if not isinstance(entry, _KVLine):
                continue
            form = _section_form(entry.section)
            value = entry.value.strip()
            if is_bool_like(value):
                low, high = _BOOL_STYLES[value.lower()]
                cb = QCheckBox()
                cb.setChecked(value.lower() == high)
                form.addRow(entry.key.strip(), cb)
                self._fields.append((entry, cb, (low, high)))
            else:
                edit = QLineEdit(entry.value)
                form.addRow(entry.key.strip(), edit)
                self._fields.append((entry, edit, None))
            n_fields += 1

        if n_fields == 0:
            inner_layout.addWidget(QLabel(
                "В файле не найдено ни одной строки вида key=value — "
                "показывать в форме нечего, редактируйте как обычный "
                "текстовый файл через «Просмотреть/редактировать»."
            ))

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        hint = QLabel(
            "Отступы/комментарии в строках, которые вы не трогали, "
            "сохраняются как в исходном файле — меняется только "
            "значение справа от «=»."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(hint)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_save(self):
        for entry, widget, bool_styles in self._fields:
            if bool_styles is not None:
                low, high = bool_styles
                entry.value = high if widget.isChecked() else low
            else:
                entry.value = widget.text()
        self.accept()

    def get_text(self) -> str:
        return render_ini(self._entries)
