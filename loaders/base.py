"""Базовый интерфейс загрузчиков данных.

Каждый конкретный загрузчик (Excel, CSV, API, ...) наследуется от
:class:`BaseLoader` и реализует метод :meth:`load`, возвращающий
"сырой" :class:`pandas.DataFrame` ещё до применения mapping и очистки.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from config import SourceConfig
from utils.logger import get_logger


class BaseLoader(ABC):
    """Абстрактный загрузчик одного источника."""

    def __init__(self, source: SourceConfig) -> None:
        self.source = source
        self.log = get_logger()

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Загрузить данные источника и вернуть сырой DataFrame."""
        raise NotImplementedError
