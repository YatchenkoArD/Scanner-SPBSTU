"""Выгрузка итогового перечня в PostgreSQL (три таблицы).

По требованию руководителя выгрузка ведётся ТОЛЬКО в базу данных (без CSV/XLSX)
и разбивается на три таблицы:
    • registry_persons — только физические лица;
    • registry_orgs    — только организации;
    • registry_all     — совмещённый список.

Тип субъекта (физлицо/организация) определяется эвристикой
``scanner.matcher.classify_kind`` по наименованию.
"""
from __future__ import annotations

from typing import List

import pandas as pd
from sqlalchemy import create_engine

from config import DatabaseConfig
from scanner.matcher import classify_kind, normalize
from utils.logger import get_logger

log = get_logger()

# Внутреннее имя поля -> колонка в БД (читаемая, без пробелов для удобства SQL).
_COLUMNS = {
    "full_name": "naimenovanie_fio",
    "kind": "tip_subekta",
    "category": "kategoriya",
    "birth_date": "data_rozhdeniya",
    "inn": "inn",
    "reg_date": "data_vklyucheniya",
    "address": "adres",
    "source": "istochnik",
}
_KIND_RU = {"person": "Физическое лицо", "org": "Организация"}


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Добавить тип субъекта и отобрать/переименовать колонки для БД."""
    out = df.copy()
    out["kind"] = out["full_name"].map(
        lambda n: _KIND_RU[classify_kind(normalize(str(n)), str(n))]
    )
    cols = [c for c in _COLUMNS if c in out.columns]
    out = out[cols].rename(columns={c: _COLUMNS[c] for c in cols})
    # Сквозная нумерация (id) первой колонкой.
    out.insert(0, "id", range(1, len(out) + 1))
    return out


def export_to_db(df: pd.DataFrame, cfg: DatabaseConfig) -> List[str]:
    """Записать перечень в три таблицы PostgreSQL. Возвращает имена таблиц."""
    prepared = _prepare(df)
    persons = prepared[prepared["tip_subekta"] == _KIND_RU["person"]].reset_index(drop=True)
    orgs = prepared[prepared["tip_subekta"] == _KIND_RU["org"]].reset_index(drop=True)
    # Перенумеровываем id внутри подтаблиц.
    for sub in (persons, orgs):
        sub["id"] = range(1, len(sub) + 1)

    engine = create_engine(cfg.url)
    written: List[str] = []
    try:
        for frame, table in (
            (prepared, cfg.table_all),
            (persons, cfg.table_persons),
            (orgs, cfg.table_orgs),
        ):
            frame.to_sql(table, engine, if_exists="replace", index=False)
            log.info("Таблица %s: записано %d строк", table, len(frame))
            written.append(table)
    finally:
        engine.dispose()
    return written
