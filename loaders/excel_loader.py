"""Загрузчик Excel-файлов (.xlsx, .xls) — с локального диска или по URL.

Поддерживает три сценария:
1. Локальный путь — читается напрямую.
2. Прямой URL на .xlsx/.xls — файл скачивается (с учётом SSL/headers) в память.
3. URL HTML-страницы с кнопкой «Скачать XLS» (опция ``find_link_on_page``) —
   на странице ищется ссылка на .xlsx/.xls и затем скачивается. Это нужно
   для реестра иностранных агентов Минюста, где имя файла содержит
   меняющийся хеш.
"""
from __future__ import annotations

import io
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from loaders.base import BaseLoader
from loaders.http import fetch


class ExcelLoader(BaseLoader):
    """Читает таблицу из листа Excel-файла (файл/URL/страница со ссылкой)."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        source_path = self.source.path

        if str(source_path).lower().startswith(("http://", "https://")):
            buffer = self._download_excel(source_path, opts)
            read_from: object = buffer
        else:
            self.log.info("Чтение Excel с диска: %s", source_path)
            read_from = source_path

        # dtype=str — читаем всё как текст, чтобы не терять ведущие нули в ИНН/ОГРН.
        df = pd.read_excel(
            read_from,
            sheet_name=opts.get("sheet_name", 0),
            header=opts.get("header", 0),
            dtype=str,
            engine=opts.get("engine"),  # None -> автоопределение (openpyxl/xlrd)
        )
        return df

    def _download_excel(self, url: str, opts: dict) -> io.BytesIO:
        """Скачать .xls(x) по прямому URL или найти ссылку на HTML-странице."""
        target_url = url

        if opts.get("find_link_on_page"):
            target_url = self._resolve_download_link(url, opts)

        self.log.info("Скачивание Excel: %s", target_url)
        response = fetch(target_url, options=opts)
        return io.BytesIO(response.content)

    def _resolve_download_link(self, page_url: str, opts: dict) -> str:
        """Найти на HTML-странице ссылку на файл .xlsx/.xls и вернуть абсолютный URL."""
        self.log.info("Поиск ссылки на Excel на странице: %s", page_url)
        response = fetch(page_url, options=opts)
        soup = BeautifulSoup(response.text, "lxml")

        # Допустимые расширения ссылки (по умолчанию xlsx/xls).
        extensions = tuple(opts.get("link_extensions", (".xlsx", ".xls")))
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.lower().split("?")[0].endswith(extensions):
                absolute = urljoin(page_url, href)
                self.log.info("Найдена ссылка на файл: %s", absolute)
                return absolute

        raise ValueError(
            f"На странице {page_url} не найдена ссылка на файл {extensions}"
        )
