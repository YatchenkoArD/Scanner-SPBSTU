"""Общий HTTP-слой для загрузчиков.

Российские государственные порталы (minjust, fsb, fedsfm) имеют две
особенности, которые ломают «наивные» запросы:

1. TLS-сертификаты, выпущенные Национальным удостоверяющим центром РФ,
   обычно отсутствуют в системном хранилище → ``SSLError``.
   Поэтому поддерживается опция ``verify_ssl: false``.
2. Часть сайтов отдаёт страницу только при «браузерном» User-Agent и
   медленно отвечает → задаём заголовки по умолчанию, таймаут и ретраи.

ВНИМАНИЕ: отключение проверки SSL снижает безопасность соединения.
Используйте только для доверенных официальных доменов из конфигурации.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests
import urllib3

from utils.logger import get_logger

log = get_logger()

# «Браузерный» User-Agent по умолчанию — многие госсайты без него таймаутят.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch(
    url: str,
    *,
    method: str = "GET",
    options: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    """Выполнить HTTP-запрос с учётом опций источника.

    Поддерживаемые ключи ``options``:
        verify_ssl (bool)  — проверять ли TLS-сертификат (по умолчанию True).
        user_agent (str)   — переопределить User-Agent.
        headers (dict)     — дополнительные заголовки.
        params (dict)      — query-параметры.
        body (dict)        — JSON-тело (для POST).
        timeout (int)      — таймаут одного запроса, сек (по умолчанию 60).
        retries (int)      — число повторных попыток (по умолчанию 3).
        retry_backoff (float) — базовая пауза между попытками, сек.

    Returns:
        Объект :class:`requests.Response` с проверенным статусом (raise_for_status).
    """
    opts = options or {}
    verify_ssl = bool(opts.get("verify_ssl", True))
    timeout = opts.get("timeout", 60)
    retries = int(opts.get("retries", 3))
    backoff = float(opts.get("retry_backoff", 2.0))

    headers = {"User-Agent": opts.get("user_agent", DEFAULT_USER_AGENT)}
    headers.update(opts.get("headers") or {})

    if not verify_ssl:
        # Глушим предупреждение об отключённой проверке сертификата.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=opts.get("params"),
                json=opts.get("body"),
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            log.warning(
                "HTTP попытка %d/%d не удалась (%s): %s",
                attempt,
                retries,
                url,
                exc,
            )
            if attempt < retries:
                time.sleep(backoff * attempt)

    # Все попытки исчерпаны — пробрасываем последнюю ошибку наверх.
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}") from last_error
