# ==================== main.py ====================
import sys
import traceback
from datetime import datetime
from PyQt6.QtWidgets import QApplication, QMessageBox
from main_window import MainWindow
from themes import ThemeManager
from config import LOG_FILE


# Ссылка на живое окно — выставляется в main() сразу после конструирования
# MainWindow, чтобы _hook() (может сработать в любой момент, включая ДО
# того, как окно вообще создано) мог достать актуальный cfg["panel"] для
# отправки краш-репорта. None до этого момента — краш-репорт просто не
# отправляется (лог на диск и messagebox всё равно срабатывают).
_current_window = None


def _install_crash_handler():
    """Живой случай (2026-09-23): публикация большой сборки (Skyrim,
    хэширование ~170К файлов) уронила приложение без единого следа — до
    этой правки не было ни глобального excepthook, ни файлового лога
    (см. main_window.py::log_message), а .exe собран с --noconsole
    --windowed (build-release.yml), так что необработанное исключение
    просто гасило процесс молча, некуда было даже посмотреть stderr.

    Ловит исключения, которые долетели до самого верха — т.е. НЕ те,
    что уже пойманы своими try/except внутри QThread-воркеров
    (depot_sync_manager.py::DepotBuildWorker.run() и т.п., те и раньше
    логировались штатно через log.emit()) — а то, что вылетело из кода
    на ГЛАВНОМ потоке (слоты, диалоги, обработчики сигналов) и раньше
    просто убивало процесс. PyQt6 по умолчанию действительно прокидывает
    исключения из слотов в sys.excepthook (в отличие от некоторых старых
    связок PyQt5, где это могло привести к abort()) — но полагаться на
    это одно рискованно без Windows под рукой для проверки, поэтому
    самые новые/непроверенные места (сборка диалога подтверждения
    публикации) дополнительно обёрнуты локальным try/except — см.
    depot_tab.py::_on_scan_done.

    Плюс отправка краш-репорта в панель (2026-09-23, прямой запрос
    "добавь отправку крашей") — ВСЕГДА, если панель подключена, не
    зависит от галочки "режим отладки" в настройках (та отвечает только
    за периодическую отправку ОБЫЧНОГО лога, см. main_window.py::
    _upload_debug_log()) — краш сам по себе всегда достаточно ценное
    событие, чтобы его отправлять безусловно. Синхронно, с коротким
    таймаутом (в отличие от периодической отправки лога, которая уходит
    в фоновый поток) — на случай, если процесс скоро действительно
    завершится, у фонового потока может не быть шанса доработать."""
    def _hook(exc_type, exc_value, exc_tb):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] ❌ НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:\n{text}\n")
        except Exception:
            pass  # диск недоступен — хотя бы попробовать показать диалог ниже
        try:
            if _current_window is not None:
                from debug_reporter import upload_report
                panel_cfg = _current_window.config.get("panel", {})
                crash_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    log_tail = "\n".join(_current_window._log_lines[-200:])
                except Exception:
                    log_tail = ""
                payload = f"{text}\n\n--- последние строки лога ---\n{log_tail}".encode("utf-8")
                upload_report(panel_cfg, "manager_crash", crash_ts, "crash.txt", payload, timeout=5.0)
        except Exception:
            pass  # best-effort — отправка краша не должна сама что-то сломать
        try:
            QMessageBox.critical(
                None,
                "Неожиданная ошибка",
                "Приложение столкнулось с необработанной ошибкой и может "
                "закрыться.\n\nПодробности записаны в лог:\n"
                f"{LOG_FILE}\n\n{exc_type.__name__}: {exc_value}",
            )
        except Exception:
            pass  # если сам QApplication уже в нерабочем состоянии
        # Не подавляем стандартное поведение целиком — полезно при запуске
        # через `python main.py` (dev), где stderr реально виден.
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main():
    global _current_window
    _install_crash_handler()
    app = QApplication(sys.argv)
    app.setApplicationName("Uploder")
    app.setOrganizationName("Uploder")

    # Инициализация менеджера тем
    theme_manager = ThemeManager()

    # Применяем тему ко всему приложению
    theme_manager.apply_theme(app)

    # Устанавливаем стиль Fusion для лучшей поддержки тем
    app.setStyle("Fusion")

    window = MainWindow()
    _current_window = window
    window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()