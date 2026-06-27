"""Экспорт DataFrame в SQLite-базу через SQLAlchemy."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from utils.logger import get_logger

log = get_logger()


def to_sqlite(df: pd.DataFrame, path: Path, table: str = "registry") -> Path:
    """Записать DataFrame в таблицу SQLite (перезаписывая существующую)."""
    # SQLAlchemy-движок поверх файла SQLite.
    engine = create_engine(f"sqlite:///{path}")
    try:
        df.to_sql(table, engine, if_exists="replace", index=False)
    finally:
        engine.dispose()
    log.info("Сохранена SQLite-база: %s (таблица '%s')", path, table)
    return path
