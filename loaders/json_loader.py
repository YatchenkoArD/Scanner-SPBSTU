"""Загрузчик локальных JSON-файлов."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from loaders.base import BaseLoader


def dig(data: Any, path: str | None) -> Any:
    """Пройти по JSON вглубь через путь вида "data.items".

    Если ``path`` не задан — вернуть данные как есть.
    """
    if not path:
        return data
    current = data
    for key in path.split("."):
        current = current[key]
    return current


class JsonLoader(BaseLoader):
    """Читает JSON-файл и разворачивает массив записей в DataFrame."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение JSON: %s", self.source.path)
        raw = json.loads(Path(self.source.path).read_text(encoding="utf-8"))
        records = dig(raw, opts.get("records_path"))
        # json_normalize "разворачивает" вложенные объекты в плоские колонки.
        return pd.json_normalize(records)
