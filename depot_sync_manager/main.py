# ==================== main.py ====================
import sys
import traceback
from datetime import datetime
from PyQt6.QtWidgets import QApplication, QMessageBox
from main_window import MainWindow
from themes import ThemeManager
from config import LOG_FILE


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
    depot_tab.py::_on_scan_done."""
    def _hook(exc_type, exc_value, exc_tb):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{ts}] ❌ НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:\n{text}\n")
        except Exception:
            pass  # диск недоступен — хотя бы попробовать показать диалог ниже
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
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()