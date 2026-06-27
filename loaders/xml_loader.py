"""Загрузчик XML-файлов."""
from __future__ import annotations

import pandas as pd

from loaders.base import BaseLoader


class XmlLoader(BaseLoader):
    """Читает XML, где каждая запись — повторяющийся тег ``record_tag``."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение XML: %s", self.source.path)
        # pandas.read_xml использует lxml и сам разворачивает дочерние теги
        # повторяющегося элемента в колонки DataFrame.
        df = pd.read_xml(
            self.source.path,
            xpath=opts.get("xpath", f".//{opts.get('record_tag', 'record')}"),
            dtype=str,
        )
        return df
