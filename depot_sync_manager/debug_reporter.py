# ==================== debug_reporter.py ====================
"""
Отправка отладочного лога/крашей TESL-Manager в TESL-Panel — прямой
запрос пользователя (2026-09-23): "добавь дебаг режим чтобы при нём лог
приложения отправлялся в панель и добавь отправку крашей".

Использует уже существующий на панели эндпоинт `PUT /api/reports/
<report_type>/<username>/<timestamp>/<filename>` (Bearer, тот же токен,
что и публикация чанков — см. TESL-Panel/panel/reports_storage.py) —
типы "manager_log"/"manager_crash", НЕ "crash"/"debug_log" (те
зарезервированы под реальные крэши игроков Skyrim/лог самого лаунчера,
TESL, который в этом заходе по-прежнему не трогаем — "TESL (лаунчер) —
пока не трогаем его" в CLAUDE.md; TESL-Manager — другой клиент,
оператор/дев-инструмент, смешивать их отчёты в одном разделе панели
было бы неверно).

Только для backend="panel" — у WebDAV нет эквивалентного эндпоинта, и
не планируется (WebDAV — просто файловое хранилище, эта функция
специфична для панели). Best-effort везде: никогда не бросает
исключение наружу — отправка лога/краша не должна сама стать
источником ошибки, особенно критично для краш-репорта, вызываемого из
sys.excepthook в момент, когда приложение и так уже в нестабильном
состоянии.
"""
import getpass

import requests


def current_username() -> str:
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def upload_report(
    panel_cfg: dict,
    report_type: str,
    session_ts: str,
    filename: str,
    data: bytes,
    timeout: float = 10.0,
) -> bool:
    """True — успешно отправлено (панель ответила 2xx), False — что угодно
    пошло не так (панель не настроена, сеть недоступна, ошибка сервера)
    — вызывающий код не должен считать это фатальной ошибкой, лог/краш
    и так уже на диске (см. main_window.py::log_message()/main.py::
    _install_crash_handler())."""
    panel_cfg = panel_cfg or {}
    base_url = (panel_cfg.get("base_url") or "").rstrip("/")
    token    = panel_cfg.get("token") or ""
    if not base_url or not token:
        return False

    url = f"{base_url}/api/reports/{report_type}/{current_username()}/{session_ts}/{filename}"
    try:
        r = requests.put(
            url,
            data=data,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            verify=panel_cfg.get("verify_ssl", True),
        )
        return 200 <= r.status_code < 300
    except Exception:
        return False
