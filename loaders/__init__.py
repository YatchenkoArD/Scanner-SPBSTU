"""Пакет загрузчиков данных и фабрика их создания.

Фабрика :func:`get_loader` по значению ``source.type`` подбирает нужный
класс-загрузчик. Чтобы добавить новый тип источника, достаточно написать
класс-наследник :class:`BaseLoader` и зарегистрировать его в ``_REGISTRY``.
"""
from __future__ import annotations

from typing import Dict, Type

from config import SourceConfig
from loaders.base import BaseLoader
from loaders.api_loader import ApiLoader
from loaders.csv_loader import CsvLoader
from loaders.excel_loader import ExcelLoader
from loaders.html_list_loader import HtmlListLoader
from loaders.json_loader import JsonLoader
from loaders.web_loader import WebLoader
from loaders.xml_loader import XmlLoader

# Сопоставление "тип источника -> класс загрузчика".
_REGISTRY: Dict[str, Type[BaseLoader]] = {
    "excel": ExcelLoader,
    "csv": CsvLoader,
    "json": JsonLoader,
    "xml": XmlLoader,
    "api": ApiLoader,
    "web": WebLoader,            # HTML-таблицы (<table>)
    "html_list": HtmlListLoader,  # нумерованные текстовые списки
}


def get_loader(source: SourceConfig) -> BaseLoader:
    """Вернуть экземпляр загрузчика, подходящий под тип источника.

    Raises:
        ValueError: если тип источника не поддерживается.
    """
    loader_cls = _REGISTRY.get(source.type)
    if loader_cls is None:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Неизвестный тип источника '{source.type}'. Поддерживаются: {supported}"
        )
    return loader_cls(source)


__all__ = ["BaseLoader", "get_loader"]
