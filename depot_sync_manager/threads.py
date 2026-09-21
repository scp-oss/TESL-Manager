# ==================== threads.py ====================
import threading
from PyQt6.QtCore import QObject, pyqtSignal

class ThreadSafeWorker(QObject):
    """Базовый класс для потокобезопасных работников"""
    finished = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()
        self._is_paused = False
        self._resume_event = threading.Event()
        self._resume_event.set()
    
    def stop(self):
        self._stop_event.set()
        self._resume_event.set()
    
    def pause(self):
        if not self._is_paused:
            self._resume_event.clear()
            self._is_paused = True
    
    def resume(self):
        if self._is_paused:
            self._resume_event.set()
            self._is_paused = False
    
    def _wait_if_paused(self):
        self._resume_event.wait()
    
    def _should_stop(self):
        return self._stop_event.is_set()

class UIUpdater(QObject):
    """Класс для потокобезопасного обновления UI"""
    update_log_signal = pyqtSignal(str)
    update_progress_signal = pyqtSignal(int, int, str)
    
    def __init__(self, parent=None):
        super().__init__(parent)