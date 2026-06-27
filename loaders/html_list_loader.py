"""Загрузчик нумерованных текстовых списков из HTML-страниц.

Многие реестры (перечень экстремистских организаций Минюста, список
террористов ФСБ, перечень Росфинмониторинга) опубликованы НЕ таблицей,
а сплошным нумерованным текстом вида::

    1. Организация «N» (решение Верховного Суда от 14.02.2003 ...).
    2. ИВАНОВ ИВАН ИВАНОВИЧ, 01.01.1980 г.р., ...

Этот загрузчик:
1. скачивает страницу (с учётом SSL/headers/ретраев);
2. при необходимости сужает область до CSS-контейнера с основным текстом;
3. разбивает текст на записи по ведущей нумерации ``N.`` / ``N)``;
4. для каждой записи формирует колонки ``num`` и ``raw`` и, опционально,
   выделяет ``name`` и произвольные поля (например, дату рождения) по regex.

Параметры (``options``):
    container_selector (str) — CSS-селектор контейнера с текстом (опц.).
    item_selector (str)      — CSS-селектор элемента-записи (напр. ``li``).
    basis_markers (list)     — ключевые слова основания (решение, приговор...);
                               имя = текст до "(<ключевое слово>". Корректно
                               работает с названиями, содержащими свои скобки.
    name_until (str)         — запасной regex-разделитель (напр. "," для ФИО).
    extract (dict)           — {имя_колонки: regex}; первая группа -> значение.
    min_len (int)            — минимальная длина записи (отсев мусора).
+ все сетевые опции из loaders.http.fetch.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import pandas as pd
from bs4 import BeautifulSoup

from loaders.base import BaseLoader
from loaders.http import fetch

# Разбиение на записи: ведущая «N.» или «N)» в начале строки/после пробелов.
_ENTRY_SPLIT = re.compile(r"(?:^|\n)\s*(\d{1,4})[.)]\s+")


class HtmlListLoader(BaseLoader):
    """Парсит нумерованный список организаций/лиц со страницы реестра."""

    def load(self) -> pd.DataFrame:
        opts: Dict[str, Any] = self.source.options
        self.log.info("Загрузка HTML-списка: %s", self.source.path)

        response = fetch(self.source.path, options=opts)
        response.encoding = opts.get("encoding") or response.apparent_encoding
        soup = BeautifulSoup(response.text, "lxml")

        # Сужаем область поиска, если задан селектор основного контента.
        selector = opts.get("container_selector")
        root = soup.select_one(selector) if selector else soup
        if root is None:
            self.log.warning(
                "[%s] селектор '%s' не найден, парсим всю страницу",
                self.source.name,
                selector,
            )
            root = soup

        item_selector = opts.get("item_selector")
        if item_selector:
            # Каждая запись — отдельный элемент (например, <li>). Это надёжнее
            # «нарезки» сплошного текста, когда список разбит на разделы.
            raw_items = [
                el.get_text(separator=" ", strip=True)
                for el in root.select(item_selector)
            ]
            records = self._parse_items(raw_items, opts)
        else:
            # Текст с переводами строк — так ведущая нумерация остаётся видимой.
            text = root.get_text(separator="\n")
            records = self._split_entries(text, opts)

        self.log.info("[%s] найдено записей: %d", self.source.name, len(records))
        return pd.DataFrame(records)

    def _parse_items(
        self, items: List[str], opts: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Распарсить заранее выделенные элементы списка (по item_selector).

        Оставляем только элементы с ведущей нумерацией ``N.`` / ``N)`` —
        так отсекаются пункты меню и прочая разметка страницы.
        """
        lead = re.compile(r"^\s*(\d{1,6})[.)]\s+(.*)$", re.DOTALL)
        records: List[Dict[str, Any]] = []
        for item in items:
            m = lead.match(item)
            if not m:
                continue
            num, body = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
            records.extend(self._build_record(num, body, opts))
        return records

    def _split_entries(
        self, text: str, opts: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Разбить сплошной текст на записи и распарсить поля каждой."""
        # re.split с захватом номера: [мусор, num1, body1, num2, body2, ...].
        parts = _ENTRY_SPLIT.split(text)
        records: List[Dict[str, Any]] = []
        # Идём парами (номер, тело), пропуская нулевой «пре-текст».
        for i in range(1, len(parts) - 1, 2):
            num = parts[i]
            body = re.sub(r"\s+", " ", parts[i + 1]).strip().rstrip(".")
            records.extend(self._build_record(num, body, opts))
        return records

    def _build_record(
        self, num: str, body: str, opts: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Собрать словарь одной записи: num, raw, name + поля из extract.

        Возвращает список из одного элемента (или пустой, если запись короче
        ``min_len``) — удобно расширять результат через ``extend``.
        """
        if len(body) < int(opts.get("min_len", 5)):
            return []

        record: Dict[str, Any] = {"num": num, "raw": body}
        record["name"] = self._extract_name(body, opts)

        # Дополнительные поля по regex (например, дата рождения, ИНН).
        for col, pat in (opts.get("extract") or {}).items():
            m = re.search(pat, body)
            record[col] = (
                (m.group(1) if m.groups() else m.group(0)) if m else None
            )
        return [record]

    def _extract_name(self, body: str, opts: Dict[str, Any]) -> str:
        """Выделить наименование/ФИО из тела записи.

        Стратегия (в порядке приоритета):
        1. ``basis_markers`` — список ключевых слов основания (решение, приговор
           и т.п.). Имя = текст до открывающей скобки ``(``, за которой следует
           одно из этих слов. Это корректно обрабатывает названия, СОДЕРЖАЩИЕ
           собственные скобки и кавычки, напр.:
             «Общественное объединение (движение) «Омская организация ...»
              (решение Омского областного суда ...)»
           — разрыв произойдёт только перед «(решение ...)», а не перед
           «(движение)».
        2. ``name_until`` — простой regex-разделитель (напр. запятая для ФИО).
        3. Если ничего не сработало — берём всё тело целиком.
        Результат очищается от хвостовых разделителей и «висящих» открывающих
        скобок.
        """
        markers = opts.get("basis_markers")
        cut: int | None = None

        if markers:
            # Открывающая скобка (любого вида) + опц. пробелы + ключевое слово.
            marker_re = re.compile(
                r"[(\[]\s*(?:" + "|".join(markers) + r")", re.IGNORECASE
            )
            m = marker_re.search(body)
            cut = m.start() if m else None

        if cut is None and opts.get("name_until"):
            m = re.search(opts["name_until"], body)
            cut = m.start() if m else None

        name = body if cut is None else body[:cut]
        # Чистим хвост: пробелы, запятые/точки/точка-с-запятой, незакрытая "(".
        name = re.sub(r"[\s,;.]+$", "", name.strip())
        name = re.sub(r"\s*[(\[]\s*$", "", name).strip()
        return name
