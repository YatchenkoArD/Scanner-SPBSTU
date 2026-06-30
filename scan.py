"""Точка входа СКРИНЕРА: поиск имён из перечня на сайтах СПбПУ.

Режимы (``scan.mode``):
    single    — один сайт/хост (BFS-обход по ссылкам).
    structure — вся структура вуза: обнаруживаем подразделения и их поддомены
                из официальной страницы структуры и обходим ВСЕ их страницы,
                перечисленные в sitemap.xml (для хостов без sitemap — BFS).

Масштаб структуры СПбПУ — десятки тысяч страниц, поэтому предусмотрены:
    • лимиты (max_pages_total / max_pages_per_host; 0 = без предела = «все»);
    • round-robin по хостам — ограниченный прогон охватывает много подразделений;
    • возобновляемость (resume): уже просканированные URL пропускаются;
    • периодическое сохранение отчёта и состояния.

Запуск:
    python scan.py --config scan_config.yaml
"""
from __future__ import annotations

import argparse
import sys
import time
from itertools import zip_longest
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd
import yaml
from tqdm import tqdm

from scanner.crawler import (
    RobotsCache,
    SiteCrawler,
    collect_sitemap_urls,
    discover_unit_hosts,
    fetch_page,
    load_hosts_file,
)
from scanner.matcher import (
    FuzzyMatcher,
    NameMatcher,
    load_patterns,
    normalize,
    patterns_from_aliases,
)
from scanner.report import Finding, extract_context, write_findings
from utils.logger import get_logger, setup_logger

log = get_logger()


def _load_registry(cfg: Dict) -> List[Tuple[str, str]]:
    """Считать (имя, категория) перечня из БД (PostgreSQL) или CSV.

    Приоритет — база данных (matching.registry_db). Для совместимости
    поддерживается и CSV (matching.registry_csv).
    """
    db = cfg.get("registry_db")
    if db:
        from sqlalchemy import create_engine
        engine = create_engine(db["url"])
        try:
            df = pd.read_sql(f'SELECT * FROM {db["table"]}', engine).fillna("")
        finally:
            engine.dispose()
        name = db.get("name_column", "naimenovanie_fio")
        category = db.get("category_column", "kategoriya")
        return list(zip(df[name].astype(str), df[category].astype(str)))
    # запасной путь — CSV
    df = pd.read_csv(cfg["registry_csv"], dtype=str).fillna("")
    name = df.columns[cfg.get("name_column_index", 1)]
    category = df.columns[cfg.get("category_column_index", 2)]
    return list(zip(df[name], df[category]))


def _resolve_hosts(scfg: Dict, http_options: Dict) -> List[str]:
    """Определить список хостов для обхода.

    Приоритет — явный файл целевых доменов (``hosts_file`` от руководителя);
    если он не задан/не найден — авто-обнаружение по странице структуры.
    """
    suffix = scfg.get("domain_suffix", "spbstu.ru")
    hosts_file = scfg.get("hosts_file")
    if hosts_file and Path(hosts_file).exists():
        hosts = load_hosts_file(hosts_file, suffix)
    else:
        if hosts_file:
            log.warning("Файл доменов '%s' не найден — авто-обнаружение по структуре",
                        hosts_file)
        hosts = discover_unit_hosts(scfg.get("structure_urls", []), suffix, http_options)
    main = f"www.{suffix}"
    ordered = [main] + [h for h in hosts if h != main]
    for extra in scfg.get("extra_hosts", []):
        if extra not in ordered:
            ordered.append(extra)
    return ordered


def _round_robin(batches: List[List[str]]) -> List[str]:
    """Чередовать URL по хостам: [h1u1, h2u1, h3u1, h1u2, ...]."""
    out: List[str] = []
    for group in zip_longest(*batches):
        out.extend(u for u in group if u)
    return out


def _gather_urls(hosts: List[str], scfg: Dict, http_options: Dict) -> Tuple[List[str], List[str]]:
    """Собрать URL страниц из sitemap каждого хоста.

    Returns:
        (urls — чередованный список из sitemap; no_sitemap — хосты без карты).
    """
    per_host = scfg.get("max_pages_per_host", 0)
    batches: List[List[str]] = []
    no_sitemap: List[str] = []
    for host in tqdm(hosts, desc="Сбор sitemap", unit="хост"):
        urls = collect_sitemap_urls(
            f"https://{host}/", http_options, max_urls=per_host
        )
        if urls:
            batches.append(urls)
        else:
            no_sitemap.append(host)
    return _round_robin(batches), no_sitemap


def _load_state(path: Path, resume: bool) -> Set[str]:
    """Загрузить множество уже просканированных URL (для возобновления)."""
    if resume and path.exists():
        done = set(path.read_text(encoding="utf-8").splitlines())
        if done:
            return done
    return set()


def run(config_path: str) -> int:
    """Выполнить скрининг по конфигурации."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    log = setup_logger(
        cfg.get("logging", {}).get("directory", "logs"),
        cfg.get("logging", {}).get("level", "INFO"),
    )

    # 1. Перечень + алиасы -> шаблоны -> автомат.
    mcfg = cfg["matching"]
    patterns = load_patterns(
        _load_registry(mcfg),
        min_length=mcfg.get("min_pattern_length", 6),
        drop=mcfg.get("drop"),
        keep_only=mcfg.get("keep_only"),
    )
    patterns += patterns_from_aliases(mcfg.get("aliases", []))
    matcher = NameMatcher(patterns)

    # Этап 4 (search.md): нечёткий поиск (опционально, для опечаток/вариантов).
    fcfg = mcfg.get("fuzzy") or {}
    fuzzy = None
    if fcfg.get("enabled"):
        fuzzy = FuzzyMatcher(
            patterns,
            threshold=fcfg.get("threshold", 90),
            min_len=fcfg.get("min_len", 8),
            max_words=fcfg.get("max_words", 5),
        )
        log.info("Нечёткий поиск включён (порог %s)", fcfg.get("threshold", 90))

    scfg = cfg["scan"]
    rcfg = cfg["report"]
    http_options = {
        "verify_ssl": scfg.get("verify_ssl", True),
        "timeout": scfg.get("timeout", 25),
        "retries": scfg.get("retries", 2),
        "user_agent": scfg.get("user_agent"),
    }
    max_total = scfg.get("max_pages_total", 0)
    delay = scfg.get("delay_seconds", 0.5)
    flush_every = scfg.get("flush_every", 200)
    state_path = Path(rcfg.get("state_file", "output/scan_state.txt"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Куда писать находки: та же БД, что и перечень; таблица — db_table.
    db_url = mcfg["registry_db"]["url"]
    findings_table = rcfg.get("db_table", "scan_findings")

    robots = RobotsCache(http_options, scfg.get("respect_robots", True))
    done = _load_state(state_path, scfg.get("resume", True))
    findings: List[Finding] = []
    scanned = 0

    def scan_page(page) -> None:
        """Найти совпадения на странице и добавить в отчёт."""
        norm_text = normalize(page.text)
        seen: Set[str] = set()
        exact_norms: Set[str] = set()
        # Этап 3: точный поиск (Aho–Corasick).
        for m in matcher.find(norm_text):
            exact_norms.add(m.pattern.norm)
            # Одну сущность на странице показываем один раз, даже если совпало
            # несколько её написаний (напр. «Facebook» и «facebook.com»).
            if m.pattern.original in seen:
                continue
            seen.add(m.pattern.original)
            findings.append(
                Finding(
                    entity=m.pattern.original,
                    category=m.pattern.category,
                    confidence=m.pattern.confidence,
                    page_url=page.url,
                    page_title=page.title,
                    context=extract_context(page.text, m.pattern.norm),
                )
            )
        # Этап 4: нечёткий поиск (опечатки/варианты), если включён.
        if fuzzy is not None:
            for fm in fuzzy.find(norm_text, exact_norms):
                if fm.payload.original in seen:
                    continue
                seen.add(fm.payload.original)
                findings.append(
                    Finding(
                        entity=fm.payload.original,
                        category=fm.payload.category,
                        confidence=f"нечёткое совпадение ({fm.score}%): «{fm.fragment}»",
                        page_url=page.url,
                        page_title=page.title,
                        context=extract_context(page.text, fm.fragment),
                    )
                )

    # 2. Определяем источник URL: структура или одиночный сайт.
    mode = scfg.get("mode", "single")
    state_fh = state_path.open("a", encoding="utf-8")

    try:
        if mode == "structure":
            hosts = _resolve_hosts(scfg, http_options)
            log.info("Хостов к обходу: %d", len(hosts))
            urls, no_sitemap = _gather_urls(hosts, scfg, http_options)
            log.info(
                "URL из sitemap: %d; хостов без sitemap: %d",
                len(urls), len(no_sitemap),
            )

            # 2a. Обход страниц из sitemap (вежливо, с учётом robots и лимита).
            for url in tqdm(urls, desc="Скрининг", unit="стр."):
                if max_total and scanned >= max_total:
                    break
                if url in done or not robots.allowed(url):
                    continue
                page = fetch_page(url, http_options)
                done.add(url)
                state_fh.write(url + "\n")
                if page is None:
                    continue
                scan_page(page)
                scanned += 1
                if scanned % flush_every == 0:
                    state_fh.flush()
                    write_findings(findings, db_url, findings_table)
                time.sleep(delay)

            # 2b. Хосты без sitemap — BFS-обход (в пределах оставшегося лимита).
            if scfg.get("crawl_fallback", True):
                for host in no_sitemap:
                    if max_total and scanned >= max_total:
                        break
                    crawler = SiteCrawler(
                        f"https://{host}/",
                        max_pages=scfg.get("max_pages_per_host", 30) or 30,
                        max_depth=scfg.get("max_depth", 2),
                        delay=delay,
                        respect_robots=scfg.get("respect_robots", True),
                        same_host_only=True,
                        http_options=http_options,
                    )
                    for page in crawler.crawl():
                        if page.url in done:
                            continue
                        done.add(page.url)
                        state_fh.write(page.url + "\n")
                        scan_page(page)
                        scanned += 1
                        if max_total and scanned >= max_total:
                            break
        else:
            # Одиночный сайт: BFS-обход.
            crawler = SiteCrawler(
                scfg["start_url"],
                max_pages=scfg.get("max_pages", 30),
                max_depth=scfg.get("max_depth", 2),
                delay=delay,
                respect_robots=scfg.get("respect_robots", True),
                same_host_only=scfg.get("same_host_only", True),
                http_options=http_options,
            )
            for page in tqdm(crawler.crawl(), desc="Скрининг", unit="стр."):
                scan_page(page)
                scanned += 1
    finally:
        state_fh.close()

    # 3. Итоговая выгрузка находок в БД + сводка.
    write_findings(findings, db_url, findings_table)
    by_conf: Dict[str, int] = {}
    for f in findings:
        by_conf[f.confidence] = by_conf.get(f.confidence, 0) + 1
    log.info("===== ИТОГ СКРИНИНГА =====")
    log.info("Просканировано страниц:   %d", scanned)
    log.info("Всего совпадений:         %d", len(findings))
    log.info("Уникальных сущностей:     %d", len({f.entity for f in findings}))
    for conf, cnt in sorted(by_conf.items()):
        log.info("   достоверность '%s': %d", conf, cnt)
    log.info("Находки в таблице БД: %s", findings_table)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Скрининг сайтов СПбПУ по перечню.")
    parser.add_argument("--config", "-c", default="config.yaml")
    args = parser.parse_args()
    try:
        sys.exit(run(args.config))
    except Exception:  # noqa: BLE001
        setup_logger().exception("Критическая ошибка скрининга")
        sys.exit(1)


if __name__ == "__main__":
    main()
