"""Мониторинг источников: определяет, нужно ли обновлять перечень.

Для каждого источника вычисляется «подпись» (signature) из доступных
ПРИЗНАКОВ АКТУАЛЬНОСТИ (флагов), которые сайт отдаёт сам:

    • ФСБ                — HTTP-заголовки ETag / Last-Modified;
    • Иностранные агенты — поле lastModified реестра в API Минюста + size;
    • Минюст (экстремисты), Росфинмониторинг — заголовков актуальности нет,
      поэтому признак = число записей + хеш содержимого (через штатный loader).

Подпись сравнивается с сохранённым «эталоном» (состоянием на момент последней
сборки перечня). Если подпись изменилась — источник помечается как
требующий обновления. Эталон НЕ двигается автоматически: он обновляется лишь
явно (``--update-baseline`` или после пересборки перечня), чтобы флаг
«нужно обновить» держался, пока вы реально не пересоберёте список.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import pandas as pd
import requests
import urllib3

from config import SourceConfig
from loaders import get_loader
from loaders.http import fetch
from utils.logger import get_logger

log = get_logger()

# UUID реестра в URL вида .../rest/registry/<uuid>/values
_REG_ID = re.compile(r"/rest/registry/([0-9a-f-]{8,})/", re.IGNORECASE)


@dataclass
class SourceStatus:
    """Результат проверки одного источника."""

    name: str
    method: str                       # http_headers | api_registry | content | error
    status: str                       # new | changed | unchanged | error
    signature: Dict[str, Any] = field(default_factory=dict)
    baseline: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _hash_df(df: pd.DataFrame) -> str:
    """Детерминированный короткий хеш содержимого таблицы (без учёта порядка)."""
    rows = df.astype(str).apply(lambda r: "|".join(r.values), axis=1)
    joined = "\n".join(sorted(rows))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _http_flags(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """Снять HTTP-флаги актуальности: ETag, Last-Modified, Content-Length."""
    resp = fetch(url, options=opts)
    return {
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "content_length": resp.headers.get("Content-Length"),
    }


def _api_registry_flags(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """Для API Минюста: lastModified реестра (+ size) — самый чистый флаг."""
    m = _REG_ID.search(url)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Referer": "https://minjust.gov.ru/",
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    verify = opts.get("verify_ssl", True)
    flags: Dict[str, Any] = {}

    # lastModified реестра из списка реестров.
    lst = requests.post(
        f"{base}/rest/registry/all", headers=headers,
        json={"offset": 0, "limit": 200, "search": "", "facets": {}, "sort": []},
        verify=verify, timeout=opts.get("timeout", 30),
    ).json()
    reg_id = m.group(1) if m else None
    for v in lst.get("values", []):
        if v.get("id") == reg_id:
            flags["registry_last_modified"] = v.get("lastModified")
            flags["registry_title"] = v.get("title")
            break

    # size (число записей) — лёгкий запрос на 1 запись.
    if reg_id:
        vals = requests.post(
            f"{base}/rest/registry/{reg_id}/values", headers=headers,
            json={"offset": 0, "limit": 1, "search": "", "facets": {}, "sort": []},
            verify=verify, timeout=opts.get("timeout", 30),
        ).json()
        flags["size"] = vals.get("size")
    return flags


def _content_flags(source: SourceConfig) -> Dict[str, Any]:
    """Признак по содержимому: число записей + хеш (через штатный loader)."""
    df = get_loader(source).load()
    return {"count": int(len(df)), "hash": _hash_df(df)}


def _pick_method(source: SourceConfig) -> str:
    """Выбрать метод проверки источника (один запрос, без лишних проб).

    Можно переопределить в конфиге через ``options.watch_method``:
    http_headers | api_registry | content.
    """
    explicit = source.options.get("watch_method")
    if explicit:
        return explicit
    if source.type == "api" and "reestrs.minjust.gov.ru" in source.path:
        return "api_registry"      # lastModified реестра (иноагенты)
    if source.type == "web":
        return "http_headers"      # ФСБ отдаёт ETag/Last-Modified
    return "content"               # Минюст экстр., Росфинмониторинг и пр.


def compute_signature(source: SourceConfig) -> tuple[str, Dict[str, Any]]:
    """Вычислить (метод, подпись) источника РОВНО одним обращением."""
    opts = {
        "verify_ssl": source.options.get("verify_ssl", True),
        "timeout": source.options.get("timeout", 30),
        "retries": source.options.get("retries", 3),
        "retry_backoff": source.options.get("retry_backoff", 4),
        "headers": source.options.get("headers"),
        "user_agent": source.options.get("user_agent"),
    }
    method = _pick_method(source)

    if method == "api_registry":
        return method, _api_registry_flags(source.path, opts)
    if method == "http_headers":
        flags = _http_flags(source.path, opts)
        if any(flags.values()):
            return method, flags
        # заголовков актуальности нет — переходим к признаку по содержимому.
        return "content", _content_flags(source)
    return "content", _content_flags(source)


def _changed(method: str, current: Dict[str, Any], base: Dict[str, Any]) -> bool:
    """Изменилась ли подпись (по значимым для метода полям)."""
    if method == "http_headers":
        keys = ("etag", "last_modified", "content_length")
    elif method == "api_registry":
        keys = ("registry_last_modified", "size")
    else:
        keys = ("count", "hash")
    return any(current.get(k) != base.get(k) for k in keys)


def check_source(
    source: SourceConfig, baseline: Optional[Dict[str, Any]]
) -> SourceStatus:
    """Проверить один источник относительно эталонной подписи."""
    try:
        method, sig = compute_signature(source)
    except Exception as exc:  # noqa: BLE001 - сбой источника не валит весь watcher
        log.warning("[%s] ошибка проверки: %s", source.name, str(exc)[:90])
        return SourceStatus(source.name, "error", "error", note=str(exc)[:120])

    if not baseline:
        return SourceStatus(source.name, method, "new", signature=sig,
                            note="эталон ещё не задан")
    changed = _changed(method, sig, baseline.get("signature", {}))
    return SourceStatus(
        source.name, method, "changed" if changed else "unchanged",
        signature=sig, baseline=baseline.get("signature", {}),
    )
