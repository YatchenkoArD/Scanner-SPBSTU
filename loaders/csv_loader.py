"""Загрузчик CSV-файлов."""
from __future__ import annotations

import pandas as pd

from loaders.base import BaseLoader


class CsvLoader(BaseLoader):
    """Читает CSV с настраиваемым разделителем и кодировкой."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение CSV: %s", self.source.path)
        df = pd.read_csv(
            self.source.path,
            sep=opts.get("sep", ","),
            encoding=opts.get("encoding", "utf-8"),
            dtype=str,             # сохраняем исходный текст (ведущие нули и т.п.)
            keep_default_na=False,  # пустые ячейки -> "", обработаем сами в cleaner
        )
        return df
