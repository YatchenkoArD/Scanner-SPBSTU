"""Загрузчик данных из REST API (JSON-ответ), с поддержкой пагинации.

Поддерживает два режима:
1. Одиночный запрос — как раньше (GET/POST, разворот JSON в DataFrame).
2. Постраничная выгрузка (``paginate``) — для API, отдающих данные кусками
   через offset/limit. Используется, например, реестром иностранных агентов
   Минюста: POST /rest/registry/{id}/values с телом {offset, limit, ...}.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

import pandas as pd
from tqdm import tqdm

from loaders.base import BaseLoader
from loaders.http import fetch
from loaders.json_loader import dig


class ApiLoader(BaseLoader):
    """Делает HTTP-запрос(ы) и разворачивает JSON-ответ в DataFrame."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        if opts.get("paginate"):
            records = self._load_paginated(opts)
        else:
            records = self._load_single(opts)
        return pd.json_normalize(records)

    def _load_single(self, opts: Dict[str, Any]) -> List[dict]:
        """Один запрос: вернуть список записей по records_path."""
        method = str(opts.get("method", "GET")).upper()
        self.log.info("Запрос к API [%s]: %s", method, self.source.path)
        response = fetch(self.source.path, method=method, options=opts)
        payload = response.json()
        records = dig(payload, opts.get("records_path"))
        return records if isinstance(records, list) else [records]

    def _load_paginated(self, opts: Dict[str, Any]) -> List[dict]:
        """Постраничная выгрузка по offset/limit до получения всех записей.

        Параметры ``paginate``:
            offset_field (str) — ключ смещения в теле запроса (по умолч. offset).
            limit_field (str)  — ключ размера страницы (по умолч. limit).
            page_size (int)    — размер страницы (по умолч. из body[limit] или 200).
            total_path (str)   — путь до общего числа записей в ответе (напр. size).
            records_path (str) — путь до массива записей (напр. values).
            max_pages (int)    — предохранитель от бесконечного цикла.
        """
        pg = opts["paginate"]
        method = str(opts.get("method", "POST")).upper()
        offset_field = pg.get("offset_field", "offset")
        limit_field = pg.get("limit_field", "limit")
        records_path = opts.get("records_path", "values")
        total_path = pg.get("total_path", "size")
        max_pages = int(pg.get("max_pages", 1000))

        base_body = copy.deepcopy(opts.get("body") or {})
        page_size = int(pg.get("page_size", base_body.get(limit_field, 200)))

        all_records: List[dict] = []
        offset = 0
        total: int | None = None
        progress = tqdm(desc=f"  {self.source.name}", unit="зап.", leave=False)

        for _ in range(max_pages):
            body = copy.deepcopy(base_body)
            body[offset_field] = offset
            body[limit_field] = page_size

            # Тело меняется на каждой странице → передаём его в опциях fetch.
            page_opts = {**opts, "body": body}
            response = fetch(self.source.path, method=method, options=page_opts)
            payload = response.json()

            if total is None:
                total = dig(payload, total_path) if total_path else None
                if isinstance(total, int):
                    progress.total = total
                    self.log.info(
                        "[%s] всего записей по API: %d", self.source.name, total
                    )

            batch = dig(payload, records_path) or []
            if not batch:
                break

            all_records.extend(batch)
            progress.update(len(batch))
            offset += page_size

            if total is not None and len(all_records) >= total:
                break

        progress.close()
        self.log.info("[%s] выгружено записей: %d", self.source.name, len(all_records))
        return all_records
