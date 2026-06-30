"""Вежливый обход одного сайта и извлечение видимого текста страниц.

Особенности (важно для работы по инфраструктуре СПбПУ):
    • соблюдение robots.txt (можно отключить опцией);
    • обход только в пределах одного хоста;
    • лимиты на число страниц и глубину;
    • задержка между запросами (не «долбим» сервер);
    • пропуск бинарных ссылок (pdf, изображения, архивы).
"""
from __future__ import annotations

import gzip
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Set
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from loaders import DEFAULT_USER_AGENT, fetch
from utils.logger import get_logger

log = get_logger()

# Расширения, которые не качаем (не HTML).
_SKIP_EXT = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".avi", ".mov", ".css", ".js",
)


@dataclass
class Page:
    """Загруженная страница: адрес, заголовок и видимый текст."""

    url: str
    title: str
    text: str


def extract_visible(html: str) -> tuple[str, str, List[str]]:
    """Вернуть (title, текст_для_поиска, ссылки) из HTML-страницы.

    В «текст для поиска» помимо видимого текста добавляются:
      • адреса ссылок (href) — чтобы ловить соцсети-иконки без подписи,
        напр. ссылку на ``instagram.com`` в футере;
      • атрибуты alt/title у картинок и ссылок (часто там «Instagram» и т.п.).
    Это повышает полноту поиска брендов/организаций, оформленных иконками.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""

    links = [a["href"] for a in soup.find_all("a", href=True)]
    # alt/title у картинок и ссылок.
    attrs: List[str] = []
    for el in soup.find_all(["img", "a"]):
        for key in ("alt", "title", "aria-label"):
            val = el.get(key)
            if val:
                attrs.append(val)

    visible = soup.get_text(separator=" ")
    # Дописываем href и атрибуты к тексту, по которому ведём поиск.
    text = " ".join([visible, " ".join(links), " ".join(attrs)])
    return title, text, links


def fetch_page(url: str, http_options: Dict | None = None) -> Page | None:
    """Загрузить одну HTML-страницу и извлечь видимый текст (или None при сбое)."""
    if url.lower().split("?")[0].endswith(_SKIP_EXT):
        return None
    try:
        resp = fetch(url, options=http_options or {})
    except Exception as exc:  # noqa: BLE001 - битый URL не должен валить процесс
        log.warning("Не удалось загрузить %s: %s", url, str(exc)[:80])
        return None
    if "html" not in resp.headers.get("Content-Type", "").lower():
        return None
    resp.encoding = resp.apparent_encoding or resp.encoding
    title, text, _ = extract_visible(resp.text)
    return Page(url=url, title=title, text=text)


class RobotsCache:
    """Кэш robots.txt по хостам: один разбор на каждый поддомен."""

    def __init__(self, http_options: Dict | None = None, enabled: bool = True) -> None:
        self.http_options = http_options or {}
        self.enabled = enabled
        self._cache: Dict[str, RobotFileParser | None] = {}

    def _parser_for(self, host: str) -> RobotFileParser | None:
        if host in self._cache:
            return self._cache[host]
        rp: RobotFileParser | None = RobotFileParser()
        try:
            resp = fetch(f"https://{host}/robots.txt", options=self.http_options)
            rp.parse(resp.text.splitlines())
        except Exception:  # noqa: BLE001 - нет robots.txt -> разрешаем всё
            rp = None
        self._cache[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        """Разрешает ли robots.txt соответствующего хоста скачивать URL."""
        if not self.enabled:
            return True
        host = urlparse(url).netloc
        rp = self._parser_for(host)
        return True if rp is None else rp.can_fetch(DEFAULT_USER_AGENT, url)


class SiteCrawler:
    """Обходит сайт в ширину (BFS) в пределах одного хоста."""

    def __init__(
        self,
        start_url: str,
        *,
        max_pages: int = 30,
        max_depth: int = 2,
        delay: float = 1.0,
        respect_robots: bool = True,
        same_host_only: bool = True,
        http_options: Dict | None = None,
    ) -> None:
        self.start_url = start_url
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.delay = delay
        self.same_host_only = same_host_only
        self.http_options = http_options or {}
        self.host = urlparse(start_url).netloc

        self._robots = self._load_robots() if respect_robots else None

    def _load_robots(self) -> RobotFileParser | None:
        """Загрузить и разобрать robots.txt стартового хоста."""
        robots_url = urljoin(self.start_url, "/robots.txt")
        rp = RobotFileParser()
        try:
            resp = fetch(robots_url, options=self.http_options)
            rp.parse(resp.text.splitlines())
            log.info("robots.txt загружен: %s", robots_url)
            return rp
        except Exception:  # noqa: BLE001 - нет robots.txt -> обходим без него
            log.warning("robots.txt недоступен, продолжаем без него")
            return None

    def _allowed(self, url: str) -> bool:
        """Разрешено ли robots.txt скачивать данный URL нашим User-Agent."""
        if self._robots is None:
            return True
        return self._robots.can_fetch(DEFAULT_USER_AGENT, url)

    def _same_site(self, url: str) -> bool:
        return (not self.same_host_only) or urlparse(url).netloc == self.host

    def crawl(self) -> Iterator[Page]:
        """Генератор страниц сайта (с учётом лимитов и robots.txt)."""
        visited: Set[str] = set()
        queue: List[tuple[str, int]] = [(self.start_url, 0)]
        count = 0

        while queue and count < self.max_pages:
            url, depth = queue.pop(0)
            url = url.split("#")[0]  # отбрасываем якорь
            if url in visited or depth > self.max_depth:
                continue
            visited.add(url)

            if not self._allowed(url):
                log.debug("robots.txt запрещает: %s", url)
                continue
            if url.lower().split("?")[0].endswith(_SKIP_EXT):
                continue

            try:
                resp = fetch(url, options=self.http_options)
            except Exception as exc:  # noqa: BLE001 - битая ссылка не валит обход
                log.warning("Не удалось загрузить %s: %s", url, str(exc)[:80])
                continue

            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                continue

            resp.encoding = resp.apparent_encoding or resp.encoding
            # Единый разбор (как в structure-режиме): текст + href/alt ссылок,
            # чтобы ловить соцсети-иконки (напр. ссылку на facebook.com).
            title, text, links = extract_visible(resp.text)
            count += 1
            log.info("[%d/%d] %s", count, self.max_pages, url)
            yield Page(url=url, title=title, text=text)

            # Добавляем новые ссылки в очередь.
            if depth < self.max_depth:
                for href in links:
                    nxt = urljoin(url, href).split("#")[0]
                    if nxt.startswith(("http://", "https://")) and self._same_site(nxt) and nxt not in visited:
                        queue.append((nxt, depth + 1))

            time.sleep(self.delay)  # вежливая пауза между запросами


# =========================================================================== #
#         Перечисление страниц (sitemap) и обнаружение поддоменов             #
# =========================================================================== #
# Извлечение <loc> без полноценного XML-парсинга (быстро и устойчиво).
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _sitemap_text(url: str, http_options: Optional[Dict]) -> str:
    """Скачать карту сайта; при необходимости распаковать gzip."""
    content = fetch(url, options=http_options or {}).content
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content.decode("utf-8", errors="replace")


def iter_sitemap_urls(sitemap_url: str, http_options: Optional[Dict] = None, *,
                      max_urls: int = 0, _depth: int = 0) -> Iterator[str]:
    """Выдать URL страниц из карты сайта (рекурсивно по картам-индексам)."""
    if _depth > 5:  # защита от циклов в индексах
        return
    try:
        text = _sitemap_text(sitemap_url, http_options)
    except Exception as exc:  # noqa: BLE001 - нет карты -> пустой результат
        log.debug("Карта недоступна %s: %s", sitemap_url, str(exc)[:60])
        return
    locs = _LOC.findall(text)
    count = 0
    if "<sitemapindex" in text.lower():  # индекс: каждый <loc> — вложенная карта
        for child in locs:
            for url in iter_sitemap_urls(child, http_options,
                                         max_urls=max_urls, _depth=_depth + 1):
                yield url
                count += 1
                if max_urls and count >= max_urls:
                    return
    else:
        for url in locs:
            yield url
            count += 1
            if max_urls and count >= max_urls:
                return


def collect_sitemap_urls(host_url: str, http_options: Optional[Dict] = None, *,
                         max_urls: int = 0) -> List[str]:
    """Список URL страниц хоста по его /sitemap.xml (если есть)."""
    sitemap_url = urljoin(host_url, "/sitemap.xml")
    urls = list(iter_sitemap_urls(sitemap_url, http_options, max_urls=max_urls))
    log.info("sitemap %s: страниц %d", sitemap_url, len(urls))
    return urls


def _host_in_scope(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def discover_unit_hosts(structure_urls: List[str], domain_suffix: str = "spbstu.ru",
                        http_options: Optional[Dict] = None) -> List[str]:
    """Собрать хосты подразделений/поддоменов из страниц официальной структуры."""
    hosts: Set[str] = set()
    for url in structure_urls:
        try:
            resp = fetch(url, options=http_options or {})
        except Exception as exc:  # noqa: BLE001 - страница структуры недоступна
            log.warning("Структура недоступна %s: %s", url, str(exc)[:70])
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for anchor in soup.find_all("a", href=True):
            host = urlparse(urljoin(url, anchor["href"])).netloc.lower()
            if host and _host_in_scope(host, domain_suffix):
                hosts.add(host)
    result = sorted(hosts)
    log.info("Обнаружено хостов в структуре (%s): %d", domain_suffix, len(result))
    return result


def load_hosts_file(path: str, domain_suffix: Optional[str] = None) -> List[str]:
    """Прочитать список целевых доменов из файла (.xls/.xlsx/.csv/.txt).

    Берётся колонка «Домен» (или первая). Адреса приводятся к чистому хосту
    (без схемы/пути), приводятся к нижнему регистру и дедуплицируются; при
    заданном ``domain_suffix`` оставляются только хосты этого домена.
    """
    import pandas as pd

    low = str(path).lower()
    if low.endswith((".xls", ".xlsx")):
        df = pd.read_excel(path, dtype=str)
        col = next((c for c in df.columns
                    if any(k in str(c).lower() for k in ("домен", "domain", "host"))),
                   df.columns[0])
        raw = df[col].dropna().astype(str).tolist()
    elif low.endswith(".csv"):
        df = pd.read_csv(path, dtype=str)
        col = next((c for c in df.columns
                    if any(k in str(c).lower() for k in ("домен", "domain", "host"))),
                   df.columns[0])
        raw = df[col].dropna().astype(str).tolist()
    else:  # .txt — один хост на строку
        raw = Path(path).read_text(encoding="utf-8").splitlines()

    result, seen = [], set()
    for item in raw:
        host = item.strip().lower()
        host = re.sub(r"^https?://", "", host).split("/")[0].split("?")[0]
        if not host or host in seen:
            continue
        if domain_suffix and not (host == domain_suffix or host.endswith("." + domain_suffix)):
            continue
        seen.add(host)
        result.append(host)
    log.info("Загружено целевых доменов из файла %s: %d", path, len(result))
    return result
