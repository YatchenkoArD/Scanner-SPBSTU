"""Ежедневная проверка источников: нужно ли обновлять перечень.

Запуск:
    python watch_sources.py --config config.yaml          # проверить и сообщить
    python watch_sources.py --config config.yaml --update-baseline
                                                          # принять текущее за эталон
                                                          # (после пересборки перечня)

Коды возврата (удобно для cron/алертов):
    0 — все источники без изменений (обновление не требуется);
    1 — есть изменения (нужно пересобрать перечень) либо ошибки проверки.

Состояние (эталон) хранится в JSON; история проверок дописывается в лог.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from config import load_config
from utils.logger import setup_logger
from watcher import SourceStatus, check_source

_ICON = {"changed": "🔴 ОБНОВИТЬ", "unchanged": "🟢 актуально",
         "new": "🆕 эталон задан", "error": "⚠️ ошибка"}


def _load_state(path: Path) -> Dict[str, dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(path: Path, statuses: List[SourceStatus]) -> None:
    """Сохранить текущие подписи как новый эталон."""
    state = {
        s.name: {"method": s.method, "signature": s.signature,
                 "updated_at": datetime.now().isoformat(timespec="seconds")}
        for s in statuses if s.status != "error"
    }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run(config_path: str, state_file: str, update_baseline: bool) -> int:
    cfg = load_config(config_path)
    log = setup_logger(cfg.logging.directory, cfg.logging.level)
    state_path = Path(state_file)
    baseline = _load_state(state_path)

    log.info("=== Проверка источников (%s) ===",
             datetime.now().strftime("%Y-%m-%d %H:%M"))
    statuses: List[SourceStatus] = []
    for i, source in enumerate(cfg.sources):
        if i:
            time.sleep(3)  # вежливая пауза между источниками (защита от 403/лимитов)
        st = check_source(source, baseline.get(source.name))
        statuses.append(st)
        detail = ""
        if st.method == "content":
            detail = f"записей={st.signature.get('count')}"
        elif st.method == "api_registry":
            detail = f"size={st.signature.get('size')}"
        elif st.method == "http_headers":
            detail = f"etag={st.signature.get('etag')}"
        log.info("  %-18s %-40s %s", _ICON.get(st.status, st.status),
                 source.name, detail)

    changed = [s for s in statuses if s.status == "changed"]
    errors = [s for s in statuses if s.status == "error"]
    new = [s for s in statuses if s.status == "new"]

    # Отчёт на диск (для истории/алертов).
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "need_update": [s.name for s in changed],
        "errors": [s.name for s in errors],
        "statuses": [asdict(s) for s in statuses],
    }
    out = Path(cfg.output.directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / "source_watch_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Эталон: задаём при первом запуске или по явному флагу.
    if update_baseline or not baseline:
        _save_state(state_path, statuses)
        log.info("Эталон сохранён: %s", state_path)

    if changed:
        log.info("ИТОГ: требуется обновление перечня — %s",
                 ", ".join(s.name for s in changed))
    elif not baseline:
        log.info("ИТОГ: эталон установлен, при следующих запусках будет сравнение.")
    else:
        log.info("ИТОГ: все источники актуальны, обновление не требуется.")

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
