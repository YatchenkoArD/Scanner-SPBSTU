"""Загрузчик HTML-таблиц (<table>) с веб-страниц."""
from __future__ import annotations

import pandas as pd

from loaders.base import BaseLoader
from loaders.http import fetch


class WebLoader(BaseLoader):
    """Скачивает веб-страницу и извлекает из неё HTML-таблицу.

    Опции (``options``):
        table_index (int) — какую таблицу взять (0 — первая, по умолчанию).
        header (int|None) — строка-заголовок для read_html.
        match (str)       — regex для отбора нужной таблицы по содержимому.
        columns (list)    — позиционное ПЕРЕИМЕНОВАНИЕ колонок (надёжнее, чем
                            ловить длинные заголовки госсайтов по тексту).
        skiprows (int)    — сколько верхних строк отбросить (например, шапку).
    + сетевые опции из loaders.http.fetch (verify_ssl, headers, timeout, ...).

    Для нумерованных текстовых списков (а не <table>) используйте тип
    ``html_list``.
    """

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Загрузка веб-страницы (таблица): %s", self.source.path)

        response = fetch(self.source.path, options=opts)
        response.encoding = opts.get("encoding") or response.apparent_encoding

        tables = pd.read_html(
            response.text,
            match=opts.get("match", ".+"),
            header=opts.get("header"),
        )
        if not tables:
            raise ValueError(f"На странице не найдено таблиц: {self.source.path}")

        df = tables[opts.get("table_index", 0)]

        # Многоуровневый заголовок -> берём нижний уровень.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(-1)

        # Позиционное переименование колонок (если задано в конфиге).
        columns = opts.get("columns")
        if columns:
            if len(columns) != df.shape[1]:
                self.log.warning(
                    "[%s] ожидалось колонок %d, а в таблице %d — переименование "
                    "по минимуму",
                    self.source.name,
                    len(columns),
                    df.shape[1],
                )
            width = min(len(columns), df.shape[1])
            df = df.iloc[:, :width]
            df.columns = columns[:width]

        # Отбрасываем служебные верхние строки (например, продублированный заголовок).
        skiprows = int(opts.get("skiprows", 0))
        if skiprows:
            df = df.iloc[skiprows:].reset_index(drop=True)

        return df.astype(str)
