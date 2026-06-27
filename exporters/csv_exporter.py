"""Экспорт DataFrame в CSV."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from utils.logger import get_logger

log = get_logger()


def to_csv(df: pd.DataFrame, path: Path) -> Path:
    """Сохранить таблицу в CSV (UTF-8 с BOM для совместимости с Excel)."""
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info("Сохранён CSV: %s", path)
    return path
