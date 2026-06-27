"""Экспорт DataFrame в Excel (.xlsx) через openpyxl."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from utils.logger import get_logger

log = get_logger()


def to_excel(df: pd.DataFrame, path: Path) -> Path:
    """Сохранить таблицу в .xlsx и автоматически подобрать ширину колонок."""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="registry")
        worksheet = writer.sheets["registry"]
        # Автоширина: по самой длинной строке в каждой колонке (с лимитом).
        for idx, column in enumerate(df.columns, start=1):
            max_len = max(
                [len(str(column))]
                + [len(str(v)) for v in df[column].head(1000).tolist()]
            )
            worksheet.column_dimensions[
                worksheet.cell(row=1, column=idx).column_letter
            ].width = min(max_len + 2, 60)
    log.info("Сохранён Excel: %s", path)
    return path
