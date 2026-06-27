"""Очистка данных и приведение колонок к единому формату.

Этап выполняется сразу после загрузки источника:
1. применяется mapping (переименование и отбор нужных колонок);
2. значения очищаются от лишних пробелов и мусорных "пустых" маркеров;
3. добавляется служебная колонка ``source`` с именем реестра.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from config import SourceConfig
from utils.logger import get_logger

# Строки, которые трактуем как отсутствие значения.
_NA_TOKENS = {"", "nan", "none", "null", "n/a", "na", "-", "—", "нет данных"}

log = get_logger()


def _clean_cell(value: object) -> object:
    """Очистить одну ячейку: убрать лишние пробелы и пустые маркеры."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    text = str(value).strip()
    # Схлопываем повторяющиеся пробелы/переводы строк в один пробел.
    text = re.sub(r"\s+", " ", text)
    if text.lower() in _NA_TOKENS:
        return np.nan
    return text


def apply_mapping(df: pd.DataFrame, source: SourceConfig) -> pd.DataFrame:
    """Переименовать колонки по mapping и оставить только нужные.

    Колонки, отсутствующие в источнике, создаются пустыми, чтобы у всех
    реестров была одинаковая схема.
    """
    mapping = source.mapping
    if not mapping:
        # mapping не задан — оставляем колонки как есть.
        result = df.copy()
    else:
        present = {src: dst for src, dst in mapping.items() if src in df.columns}
        missing = set(mapping) - set(present)
        if missing:
            log.warning("[%s] нет колонок в источнике: %s", source.name, missing)
        result = df[list(present)].rename(columns=present)
        # Гарантируем наличие всех целевых колонок.
        for dst in mapping.values():
            if dst not in result.columns:
                result[dst] = np.nan
    return result


def clean_dataframe(df: pd.DataFrame, source: SourceConfig) -> pd.DataFrame:
    """Полная очистка одного источника: mapping + очистка ячеек + source."""
    log.info("[%s] очистка данных (%d строк)", source.name, len(df))
    result = apply_mapping(df, source)

    # Применяем очистку к каждой ячейке всех колонок-объектов.
    for column in result.columns:
        result[column] = result[column].map(_clean_cell)

    # Удаляем полностью пустые строки.
    result = result.dropna(how="all").reset_index(drop=True)

    # Статические колонки из options.constants (напр. категория, тип субъекта).
    # Удобно размечать «Экстремистская организация» / «Физическое лицо» и т.п.
    for column, value in (source.options.get("constants") or {}).items():
        result[column] = value

    # Добавляем источник как метаданные происхождения записи.
    result["source"] = source.name
    return result
