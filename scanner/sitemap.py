"""Перечисление страниц сайта через ``sitemap.xml``.

Sitemap — самый вежливый и полный способ узнать все URL сайта: вместо слепого
обхода по ссылкам мы читаем готовый список адресов, который сайт сам публикует.
Поддерживаются как карты-индексы (``<sitemapindex>`` -> вложенные карты), так и
обычные карты (``<urlset>``), а также gzip-сжатые карты (``*.xml.gz``).
"""
from __future__ import annotations

import gzip
import re
from typing import Dict, Iterator, List, Optional

from loaders.http import fetch
from utils.logger import get_logger

log = get_logger()

# Извлечение содержимого тегов <loc> без полноценного XML-парсинга
# (надёжно к «грязным» картам и быстро на больших файлах).
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _get_text(url: str, http_options: Optional[Dict]) -> str:
    """Скачать карту; при необходимости распаковать gzip."""
    resp = fetch(url, options=http_options or {})
    content = resp.content
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content.decode("utf-8", errors="replace")


def iter_sitemap_urls(
    sitemap_url: str,
    http_options: Optional[Dict] = None,
    *,
    max_urls: int = 0,
    _depth: int = 0,
) -> Iterator[str]:
    """Выдать URL страниц из карты сайта (рекурсивно по картам-индексам).

    Args:
        sitemap_url: адрес sitemap.xml (или sitemap-индекса).
        http_options: сетевые опции (verify_ssl, timeout, ...).
        max_urls: предел числа URL (0 — без предела).

    Yields:
        Адреса страниц (<loc> из <urlset>).
    """
    if _depth > 5:  # защита от циклов в картах-индексах
        return
    try:
        text = _get_text(sitemap_url, http_options)
    except Exception as exc:  # noqa: BLE001 - нет карты -> пустой результат
        log.debug("Карта недоступна %s: %s", sitemap_url, str(exc)[:60])
        return

    locs = _LOC.findall(text)
    is_index = "<sitemapindex" in text.lower()

    count = 0
    if is_index:
        # Это индекс: каждый <loc> — адрес вложенной карты.
        for child in locs:
            for url in iter_sitemap_urls(
                child, http_options, max_urls=max_urls, _depth=_depth + 1
            ):
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


def collect_sitemap_urls(
    host_url: str, http_options: Optional[Dict] = None, *, max_urls: int = 0
) -> List[str]:
    """Вернуть список URL страниц хоста по его /sitemap.xml (если есть)."""
    from urllib.parse import urljoin

    sitemap_url = urljoin(host_url, "/sitemap.xml")
    urls = list(iter_sitemap_urls(sitemap_url, http_options, max_urls=max_urls))
    log.info("sitemap %s: страниц %d", sitemap_url, len(urls))
    return urls
