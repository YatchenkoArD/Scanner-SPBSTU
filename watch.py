"""Мониторинг источников: нужно ли обновлять перечень (+ ежедневный запуск).

Для каждого источника вычисляется «подпись» из доступного ПРИЗНАКА АКТУАЛЬНОСТИ:
    • ФСБ                — HTTP-заголовки ETag / Last-Modified;
    • Минюст (API)       — поле lastModified реестра + size;
    • Минюст-экстремисты, Росфинмониторинг — число записей + хеш содержимого.

Подпись сравнивается с сохранённым эталоном (состояние на момент последней
сборки перечня). Эталон НЕ двигается сам — только ``--update-baseline`` (после
пересборки), чтобы флаг «нужно обновить» держался, пока список не пересобран.

Запуск:
    python watch.py --config config.yaml                 # проверить
    python watch.py --config config.yaml --update-baseline  # принять за эталон

Коды возврата: 0 — всё актуально; 1 — есть изменения/ошибки (для cron-алертов).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
import requests
import urllib3

from config import SourceConfig, load_config
from loaders import fetch, get_loader
from utils.logger import get_logger, setup_logger

log = get_logger()

# UUID реестра в URL вида .../rest/registry/<uuid>/values
_REG_ID = re.compile(r"/rest/registry/([0-9a-f-]{8,})/", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#                       Вычисление подписи источника                          #
# --------------------------------------------------------------------------- #
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
    return hashlib.sha1("\n".join(sorted(rows)).encode("utf-8")).hexdigest()[:16]


def _http_flags(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """HTTP-флаги актуальности: ETag, Last-Modified, Content-Length."""
    resp = fetch(url, options=opts)
    return {
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "content_length": resp.headers.get("Content-Length"),
    }


def _api_registry_flags(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """API Минюста: lastModified реестра + size — самый чистый флаг."""
    m = _REG_ID.search(url)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    headers = {"Accept": "application/json",
               "Content-Type": "application/json;charset=UTF-8",
               "Referer": "https://minjust.gov.ru/"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    verify = opts.get("verify_ssl", True)
    flags: Dict[str, Any] = {}
    reg_id = m.group(1) if m else None

    lst = requests.post(
        f"{base}/rest/registry/all", headers=headers,
        json={"offset": 0, "limit": 200, "search": "", "facets": {}, "sort": []},
        verify=verify, timeout=opts.get("timeout", 30)).json()
    for v in lst.get("values", []):
        if v.get("id") == reg_id:
            flags["registry_last_modified"] = v.get("lastModified")
            flags["registry_title"] = v.get("title")
            break
    if reg_id:
        vals = requests.post(
            f"{base}/rest/registry/{reg_id}/values", headers=headers,
            json={"offset": 0, "limit": 1, "search": "", "facets": {}, "sort": []},
            verify=verify, timeout=opts.get("timeout", 30)).json()
        flags["size"] = vals.get("size")
    return flags


def _content_flags(source: SourceConfig) -> Dict[str, Any]:
    """Признак по содержимому: число записей + хеш (через штатный loader)."""
    df = get_loader(source).load()
    return {"count": int(len(df)), "hash": _hash_df(df)}


def _pick_method(source: SourceConfig) -> str:
    """Метод проверки (один запрос). Переопределяется ``options.watch_method``."""
    explicit = source.options.get("watch_method")
    if explicit:
        return explicit
    if source.type == "api" and "reestrs.minjust.gov.ru" in source.path:
        return "api_registry"
    if source.type == "web":
        return "http_headers"
    return "content"


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
        return "content", _content_flags(source)
    return "content", _content_flags(source)


def _changed(method: str, current: Dict[str, Any], base: Dict[str, Any]) -> bool:
    """Изменилась ли подпись (по значимым для метода полям)."""
    keys = {"http_headers": ("etag", "last_modified", "content_length"),
            "api_registry": ("registry_last_modified", "size")}.get(method, ("count", "hash"))
    return any(current.get(k) != base.get(k) for k in keys)


def check_source(source: SourceConfig, baseline: Optional[Dict[str, Any]]) -> SourceStatus:
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
    return SourceStatus(source.name, method, "changed" if changed else "unchanged",
                        signature=sig, baseline=baseline.get("signature", {}))


# --------------------------------------------------------------------------- #
#                              Точка входа                                     #
# --------------------------------------------------------------------------- #
_ICON = {"changed": "🔴 ОБНОВИТЬ", "unchanged": "🟢 актуально",
         "new": "🆕 эталон задан", "error": "⚠️ ошибка"}


def _load_state(path: Path) -> Dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_state(path: Path, statuses: List[SourceStatus]) -> None:
    state = {s.name: {"method": s.method, "signature": s.signature,
                      "updated_at": datetime.now().isoformat(timespec="seconds")}
             for s in statuses if s.status != "error"}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run(config_path: str, state_file: str, update_baseline: bool) -> int:
    cfg = load_config(config_path)
    log_ = setup_logger(cfg.logging.directory, cfg.logging.level)
    state_path = Path(state_file)
    baseline = _load_state(state_path)

    log_.info("=== Проверка источников (%s) ===", datetime.now().strftime("%Y-%m-%d %H:%M"))
    statuses: List[SourceStatus] = []
    for i, source in enumerate(cfg.sources):
        if i:
            time.sleep(3)  # вежливая пауза между источниками (защита от 403/лимитов)
        st = check_source(source, baseline.get(source.name))
        statuses.append(st)
        detail = {"content": f"записей={st.signature.get('count')}",
                  "api_registry": f"size={st.signature.get('size')}",
                  "http_headers": f"etag={st.signature.get('etag')}"}.get(st.method, "")
        log_.info("  %-18s %-40s %s", _ICON.get(st.status, st.status), source.name, detail)

    changed = [s for s in statuses if s.status == "changed"]
    errors = [s for s in statuses if s.status == "error"]

    out = Path(cfg.database.directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / "source_watch_report.json").write_text(json.dumps({
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "need_update": [s.name for s in changed],
        "errors": [s.name for s in errors],
        "statuses": [asdict(s) for s in statuses],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if update_baseline or not baseline:
        _save_state(state_path, statuses)
        log_.info("Эталон сохранён: %s", state_path)

    if changed:
        log_.info("ИТОГ: требуется обновление перечня — %s", ", ".join(s.name for s in changed))
    elif not baseline:
        log_.info("ИТОГ: эталон установлен, при следующих запусках будет сравнение.")
    else:
        log_.info("ИТОГ: все источники актуальны, обновление не требуется.")
    return 1 if (changed or errors) else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Мониторинг источников перечня.")
    p.add_argument("--config", "-c", default="config.yaml")
    p.add_argument("--state", default="output/source_state.json",
                   help="файл эталонного состояния")
    p.add_argument("--update-baseline", action="store_true",
                   help="принять текущее состояние за эталон (после пересборки)")
    args = p.parse_args()
    try:
        sys.exit(run(args.config, args.state, args.update_baseline))
    except Exception:  # noqa: BLE001
        setup_logger().exception("Критическая ошибка watcher")
        sys.exit(1)


if __name__ == "__main__":
    main()
