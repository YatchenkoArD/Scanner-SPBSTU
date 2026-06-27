"""Обнаружение подразделений и их поддоменов из официальной структуры вуза.

Парсим страницу(ы) структуры СПбПУ и собираем все хосты в пределах заданного
домена (``spbstu.ru`` и его поддомены): институты, высшие школы, лаборатории,
центры, кафедры, подразделения и сервисы.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from loaders.http import fetch
from utils.logger import get_logger

log = get_logger()


def _host_in_scope(host: str, suffix: str) -> bool:
    """Принадлежит ли хост целевому домену (сам домен или его поддомен)."""
    return host == suffix or host.endswith("." + suffix)


def discover_unit_hosts(
    structure_urls: List[str],
    domain_suffix: str = "spbstu.ru",
    http_options: Optional[Dict] = None,
) -> List[str]:
    """Собрать список хостов подразделений из страниц структуры.

    Args:
        structure_urls: адреса страниц официальной структуры.
        domain_suffix: целевой домен (хосты вне него игнорируются).
        http_options: сетевые опции.

    Returns:
        Отсортированный список уникальных хостов (включая основной домен).
    """
    hosts: Set[str] = set()
    for url in structure_urls:
        try:
            resp = fetch(url, options=http_options or {})
        except Exception as exc:  # noqa: BLE001 - страница структуры недоступна
            log.warning("Структура недоступна %s: %s", url, str(exc)[:70])
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(url, anchor["href"])
            host = urlparse(absolute).netloc.lower()
            if host and _host_in_scope(host, domain_suffix):
                hosts.add(host)

    result = sorted(hosts)
    log.info(
        "Обнаружено хостов в структуре (%s): %d", domain_suffix, len(result)
    )
    return result
