"""Находки скрининга: модель Finding, контекст и выгрузка в PostgreSQL.

CSV/XLSX в проекте не используются — результаты пишутся только в базу данных.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import List

import pandas as pd
from sqlalchemy import create_engine

from utils.logger import get_logger

log = get_logger()

# Для построения «гибкого» поиска контекста в исходном тексте.
_WORD = r"[0-9A-Za-zА-Яа-яЁё]"


@dataclass
class Finding:
    """Одно совпадение для отчёта."""

    entity: str        # имя/организация как в перечне
    category: str      # категория из перечня
    confidence: str    # достоверность (высокая/средняя/низкая/нечёткое)
    page_url: str      # где найдено
    page_title: str    # заголовок страницы
    context: str       # фрагмент текста вокруг совпадения


def extract_context(original_text: str, norm_pattern: str, width: int = 80) -> str:
    """Найти фрагмент исходного текста вокруг совпадения (±``width``)."""
    tokens = [re.escape(t) for t in norm_pattern.split(" ") if t]
    if not tokens:
        return ""
    flexible = r"[^0-9A-Za-zА-Яа-яЁё]+".join(tokens)
    pattern = rf"(?<!{_WORD})(?:{flexible})(?!{_WORD})"
    m = re.search(pattern, original_text, re.IGNORECASE)
    if not m:
        return ""
    start = max(0, m.start() - width)
    end = min(len(original_text), m.end() + width)
    snippet = re.sub(r"\s+", " ", original_text[start:end]).strip()
    return f"…{snippet}…"


# Внутреннее поле Finding -> колонка таблицы (translit, удобно для SQL).
_COLUMNS = {
    "entity": "sushchnost",        # имя/организация из перечня
    "category": "kategoriya",      # категория
    "confidence": "dostovernost",  # высокая/средняя/низкая/нечёткое
    "page_url": "stranica_url",    # где найдено
    "page_title": "zagolovok",     # заголовок страницы
    "context": "kontekst",         # фрагмент текста вокруг совпадения
}
# Порядок достоверности для сортировки (высокие — выше).
_CONF_ORDER = {"высокая": 0, "средняя": 1}


def write_findings(findings: List[Finding], db_url: str,
                   table: str = "scan_findings") -> int:
    """Записать находки скрининга в таблицу PostgreSQL (перезаписью).

    Вызывается на флешах и в конце скана — таблица всегда отражает прогресс.
    Возвращает число записанных строк.
    """
    df = pd.DataFrame([asdict(f) for f in findings], columns=list(_COLUMNS))
    df = df.rename(columns=_COLUMNS)
    if not df.empty:
        conf = _COLUMNS["confidence"]
        df["_o"] = df[conf].map(lambda c: _CONF_ORDER.get(str(c).split(" ")[0], 2))
        df = df.sort_values(["_o", _COLUMNS["category"]]).drop(columns="_o")
    df.insert(0, "id", range(1, len(df) + 1))

    engine = create_engine(db_url)
    try:
        df.to_sql(table, engine, if_exists="replace", index=False)
    finally:
        engine.dispose()
    log.info("Таблица %s: записано находок %d", table, len(df))
    return len(df)
