"""Загрузка данных из источников всех форматов в один модуль.

Содержит:
  • HTTP-слой (``fetch``) с учётом особенностей госсайтов (SSL НУЦ РФ,
    браузерный User-Agent, таймауты, ретраи);
  • базовый класс ``BaseLoader`` и реализации для форматов:
    csv · json · xml · excel · api (REST/пагинация) · web (HTML-таблица) ·
    html_list (нумерованные текстовые списки);
  • фабрику ``get_loader`` (тип источника -> класс загрузчика).

Чтобы добавить новый тип источника — напишите класс-наследник ``BaseLoader``
и зарегистрируйте его в ``_REGISTRY``.
"""
from __future__ import annotations

import copy
import io
import json
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup
from tqdm import tqdm

from config import SourceConfig
from utils.logger import get_logger

log = get_logger()

# --------------------------------------------------------------------------- #
#                                HTTP-слой                                     #
# --------------------------------------------------------------------------- #
# «Браузерный» User-Agent по умолчанию — многие госсайты без него таймаутят.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch(
    url: str, *, method: str = "GET", options: Optional[Dict[str, Any]] = None
) -> requests.Response:
    """HTTP-запрос с учётом опций источника (SSL, заголовки, таймаут, ретраи).

    Ключи ``options``: verify_ssl, user_agent, headers, params, body, timeout,
    retries, retry_backoff. Возвращает ответ с проверенным статусом.

    ВНИМАНИЕ: ``verify_ssl: false`` снижает защиту соединения — допустимо
    только для доверенных официальных доменов из конфигурации.
    """
    opts = options or {}
    verify_ssl = bool(opts.get("verify_ssl", True))
    timeout = opts.get("timeout", 60)
    retries = int(opts.get("retries", 3))
    backoff = float(opts.get("retry_backoff", 2.0))

    headers = {"User-Agent": opts.get("user_agent") or DEFAULT_USER_AGENT}
    headers.update(opts.get("headers") or {})

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method=method.upper(), url=url, headers=headers,
                params=opts.get("params"), json=opts.get("body"),
                timeout=timeout, verify=verify_ssl,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            log.warning("HTTP попытка %d/%d не удалась (%s): %s",
                        attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}") from last_error


def dig(data: Any, path: Optional[str]) -> Any:
    """Пройти по JSON вглубь через путь вида ``data.items`` (или вернуть как есть)."""
    if not path:
        return data
    current = data
    for key in path.split("."):
        current = current[key]
    return current


# --------------------------------------------------------------------------- #
#                              Базовый загрузчик                               #
# --------------------------------------------------------------------------- #
class BaseLoader(ABC):
    """Абстрактный загрузчик одного источника -> «сырой» DataFrame."""

    def __init__(self, source: SourceConfig) -> None:
        self.source = source
        self.log = get_logger()

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Загрузить данные источника и вернуть сырой DataFrame."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#                          Файловые форматы                                    #
# --------------------------------------------------------------------------- #
class CsvLoader(BaseLoader):
    """CSV с настраиваемым разделителем и кодировкой."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение CSV: %s", self.source.path)
        return pd.read_csv(
            self.source.path, sep=opts.get("sep", ","),
            encoding=opts.get("encoding", "utf-8"),
            dtype=str, keep_default_na=False,
        )


class JsonLoader(BaseLoader):
    """Локальный JSON: массив записей -> DataFrame."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение JSON: %s", self.source.path)
        raw = json.loads(Path(self.source.path).read_text(encoding="utf-8"))
        return pd.json_normalize(dig(raw, opts.get("records_path")))


class XmlLoader(BaseLoader):
    """XML, где каждая запись — повторяющийся тег ``record_tag``."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Чтение XML: %s", self.source.path)
        return pd.read_xml(
            self.source.path,
            xpath=opts.get("xpath", f".//{opts.get('record_tag', 'record')}"),
            dtype=str,
        )


class ExcelLoader(BaseLoader):
    """Excel (.xlsx/.xls) с диска, по прямому URL или со страницы со ссылкой."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        path = self.source.path
        if str(path).lower().startswith(("http://", "https://")):
            read_from: object = self._download(path, opts)
        else:
            self.log.info("Чтение Excel с диска: %s", path)
            read_from = path
        # dtype=str — не теряем ведущие нули в ИНН/ОГРН.
        return pd.read_excel(
            read_from, sheet_name=opts.get("sheet_name", 0),
            header=opts.get("header", 0), dtype=str, engine=opts.get("engine"),
        )

    def _download(self, url: str, opts: dict) -> io.BytesIO:
        target = self._resolve_link(url, opts) if opts.get("find_link_on_page") else url
        self.log.info("Скачивание Excel: %s", target)
        return io.BytesIO(fetch(target, options=opts).content)

    def _resolve_link(self, page_url: str, opts: dict) -> str:
        self.log.info("Поиск ссылки на Excel: %s", page_url)
        soup = BeautifulSoup(fetch(page_url, options=opts).text, "lxml")
        extensions = tuple(opts.get("link_extensions", (".xlsx", ".xls")))
        for anchor in soup.find_all("a", href=True):
            if anchor["href"].lower().split("?")[0].endswith(extensions):
                return urljoin(page_url, anchor["href"])
        raise ValueError(f"На странице {page_url} не найдена ссылка {extensions}")


# --------------------------------------------------------------------------- #
#                           Сетевые форматы                                    #
# --------------------------------------------------------------------------- #
class ApiLoader(BaseLoader):
    """REST API (JSON): одиночный запрос или постраничная выгрузка (paginate)."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        records = self._paginated(opts) if opts.get("paginate") else self._single(opts)
        return pd.json_normalize(records)

    def _single(self, opts: Dict[str, Any]) -> List[dict]:
        method = str(opts.get("method", "GET")).upper()
        self.log.info("Запрос к API [%s]: %s", method, self.source.path)
        payload = fetch(self.source.path, method=method, options=opts).json()
        records = dig(payload, opts.get("records_path"))
        return records if isinstance(records, list) else [records]

    def _paginated(self, opts: Dict[str, Any]) -> List[dict]:
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
        offset, total = 0, None
        progress = tqdm(desc=f"  {self.source.name}", unit="зап.", leave=False)
        for _ in range(max_pages):
            body = copy.deepcopy(base_body)
            body[offset_field], body[limit_field] = offset, page_size
            payload = fetch(self.source.path, method=method,
                            options={**opts, "body": body}).json()
            if total is None:
                total = dig(payload, total_path) if total_path else None
                if isinstance(total, int):
                    progress.total = total
                    self.log.info("[%s] всего записей по API: %d",
                                  self.source.name, total)
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


class WebLoader(BaseLoader):
    """HTML-таблица (<table>) со страницы. Опции: table_index, header, match,
    columns (позиционное переименование), skiprows + сетевые опции fetch."""

    def load(self) -> pd.DataFrame:
        opts = self.source.options
        self.log.info("Загрузка веб-страницы (таблица): %s", self.source.path)
        response = fetch(self.source.path, options=opts)
        response.encoding = opts.get("encoding") or response.apparent_encoding
        tables = pd.read_html(response.text, match=opts.get("match", ".+"),
                              header=opts.get("header"))
        if not tables:
            raise ValueError(f"На странице не найдено таблиц: {self.source.path}")
        df = tables[opts.get("table_index", 0)]
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(-1)
        columns = opts.get("columns")
        if columns:
            width = min(len(columns), df.shape[1])
            df = df.iloc[:, :width]
            df.columns = columns[:width]
        skiprows = int(opts.get("skiprows", 0))
        if skiprows:
            df = df.iloc[skiprows:].reset_index(drop=True)
        return df.astype(str)


# Разбиение нумерованного списка на записи: ведущая «N.» / «N)».
_ENTRY_SPLIT = re.compile(r"(?:^|\n)\s*(\d{1,4})[.)]\s+")


class HtmlListLoader(BaseLoader):
    """Нумерованный текстовый список (Минюст-экстремисты, ФСБ, Росфинмониторинг).

    Опции: container_selector, item_selector (напр. ``li``), basis_markers
    (ключевые слова основания — имя режется перед «(<слово>»), name_until
    (запасной regex-разделитель), extract ({колонка: regex}), min_len.
    """

    def load(self) -> pd.DataFrame:
        opts: Dict[str, Any] = self.source.options
        self.log.info("Загрузка HTML-списка: %s", self.source.path)
        response = fetch(self.source.path, options=opts)
        response.encoding = opts.get("encoding") or response.apparent_encoding
        soup = BeautifulSoup(response.text, "lxml")

        selector = opts.get("container_selector")
        root = soup.select_one(selector) if selector else soup
        if root is None:
            self.log.warning("[%s] селектор '%s' не найден, парсим всю страницу",
                             self.source.name, selector)
            root = soup

        if opts.get("item_selector"):
            items = [el.get_text(separator=" ", strip=True)
                     for el in root.select(opts["item_selector"])]
            records = self._parse_items(items, opts)
        else:
            records = self._split_entries(root.get_text(separator="\n"), opts)
        self.log.info("[%s] найдено записей: %d", self.source.name, len(records))
        return pd.DataFrame(records)

    def _parse_items(self, items: List[str], opts: Dict[str, Any]) -> List[dict]:
        lead = re.compile(r"^\s*(\d{1,6})[.)]\s+(.*)$", re.DOTALL)
        records: List[dict] = []
        for item in items:
            m = lead.match(item)
            if m:
                body = re.sub(r"\s+", " ", m.group(2)).strip()
                records.extend(self._build_record(m.group(1), body, opts))
        return records

    def _split_entries(self, text: str, opts: Dict[str, Any]) -> List[dict]:
        parts = _ENTRY_SPLIT.split(text)
        records: List[dict] = []
        for i in range(1, len(parts) - 1, 2):
            body = re.sub(r"\s+", " ", parts[i + 1]).strip().rstrip(".")
            records.extend(self._build_record(parts[i], body, opts))
        return records

    def _build_record(self, num: str, body: str, opts: Dict[str, Any]) -> List[dict]:
        if len(body) < int(opts.get("min_len", 5)):
            return []
        record: Dict[str, Any] = {"num": num, "raw": body,
                                  "name": self._extract_name(body, opts)}
        for col, pat in (opts.get("extract") or {}).items():
            m = re.search(pat, body)
            record[col] = (m.group(1) if m.groups() else m.group(0)) if m else None
        return [record]

    @staticmethod
    def _extract_name(body: str, opts: Dict[str, Any]) -> str:
        """Имя = текст до начала основания «(решение…)» или до name_until."""
        markers = opts.get("basis_markers")
        cut: Optional[int] = None
        if markers:
            m = re.search(r"[(\[]\s*(?:" + "|".join(markers) + r")", body, re.IGNORECASE)
            cut = m.start() if m else None
        if cut is None and opts.get("name_until"):
            m = re.search(opts["name_until"], body)
            cut = m.start() if m else None
        name = body if cut is None else body[:cut]
        name = re.sub(r"[\s,;.]+$", "", name.strip())
        return re.sub(r"\s*[(\[]\s*$", "", name).strip()


# --------------------------------------------------------------------------- #
#                                 Фабрика                                      #
# --------------------------------------------------------------------------- #
_REGISTRY: Dict[str, type[BaseLoader]] = {
    "csv": CsvLoader,
    "json": JsonLoader,
    "xml": XmlLoader,
    "excel": ExcelLoader,
    "api": ApiLoader,
    "web": WebLoader,            # HTML-таблицы (<table>)
    "html_list": HtmlListLoader,  # нумерованные текстовые списки
}


def get_loader(source: SourceConfig) -> BaseLoader:
    """Вернуть экземпляр загрузчика под тип источника."""
    cls = _REGISTRY.get(source.type)
    if cls is None:
        raise ValueError(
            f"Неизвестный тип источника '{source.type}'. "
            f"Поддерживаются: {', '.join(sorted(_REGISTRY))}"
        )
    return cls(source)
