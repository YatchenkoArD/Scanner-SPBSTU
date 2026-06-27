"""Пакет экспортеров итоговой таблицы в разные форматы.

:func:`export_all` сохраняет DataFrame во все форматы, перечисленные в
конфигурации (xlsx, csv, sqlite), и возвращает список созданных файлов.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import pandas as pd

from config import OutputConfig
from exporters.csv_exporter import to_csv
from exporters.excel_exporter import to_excel
from exporters.sqlite_exporter import to_sqlite
from utils.logger import get_logger

log = get_logger()


def export_all(df: pd.DataFrame, cfg: OutputConfig) -> List[Path]:
    """Сохранить DataFrame во все форматы из конфигурации."""
    out_dir = Path(cfg.directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / cfg.basename

    created: List[Path] = []
    for fmt in cfg.formats:
        fmt = fmt.lower()
        try:
            if fmt == "xlsx":
                created.append(to_excel(df, base.with_suffix(".xlsx")))
            elif fmt == "csv":
                created.append(to_csv(df, base.with_suffix(".csv")))
            elif fmt == "sqlite":
                created.append(
                    to_sqlite(df, base.with_suffix(".db"), cfg.sqlite_table)
                )
            else:
                log.warning("Неизвестный формат экспорта: %s", fmt)
        except Exception:  # noqa: BLE001 - логируем и продолжаем с остальными
            log.exception("Ошибка экспорта в формат %s", fmt)
    return created


__all__ = ["export_all", "to_csv", "to_excel", "to_sqlite"]
