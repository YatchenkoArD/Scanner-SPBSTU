"""Точка входа: оркестрация сбора и объединения реестров.

Пайплайн:
    1. Загрузка конфигурации.
    2. Для каждого источника: загрузка -> очистка/mapping (с прогресс-баром).
    3. Объединение всех источников в одну таблицу.
    4. Нормализация значений (даты, телефоны, адреса, ID).
    5. Дедупликация по ключевым полям.
    6. Проверка качества данных.
    7. Экспорт в xlsx / csv / sqlite.

Запуск:
    python main.py --config config.yaml
"""
from __future__ import annotations

import argparse
import sys
from typing import List

import pandas as pd
from tqdm import tqdm

from config import AppConfig, load_config
from exporters import export_all
from loaders import get_loader
from notice import write_notice
from scanner.matcher import classify_kind, normalize as _normalize_name
from transformers import (
    check_quality,
    clean_dataframe,
    combine,
    deduplicate,
    finalize_columns,
    normalize_dataframe,
)
from utils.logger import setup_logger


def collect_sources(cfg: AppConfig) -> List[pd.DataFrame]:
    """Загрузить и очистить все источники, показывая прогресс.

    Ошибка в одном источнике не прерывает весь процесс — она логируется,
    а скрипт продолжает работу с остальными реестрами.
    """
    log = setup_logger(cfg.logging.directory, cfg.logging.level)
    frames: List[pd.DataFrame] = []

    for source in tqdm(cfg.sources, desc="Источники", unit="реестр"):
        try:
            loader = get_loader(source)
            raw = loader.load()
            cleaned = clean_dataframe(raw, source)
            # HEALTH-CHECK источника: подозрительно мало записей обычно означает
            # сломанный парсер (сайт сменил вёрстку). Такие данные НЕ берём,
            # чтобы не «разбавить» перечень мусором.
            min_rows = int(source.options.get("min_rows", 0))
            if len(cleaned) < min_rows:
                log.error(
                    "[%s] ПРОВАЛ health-check: %d записей < ожидаемого минимума %d "
                    "— возможно сломался парсер, источник ИСКЛЮЧЁН",
                    source.name, len(cleaned), min_rows,
                )
                continue
            frames.append(cleaned)
            log.info("[%s] успешно: %d строк", source.name, len(cleaned))
        except Exception:  # noqa: BLE001 - устойчивость к сбоям источников
            log.exception("[%s] ошибка загрузки/очистки — пропуск", source.name)

    return frames


def drop_person_rows(merged, categories, log) -> "pd.DataFrame":
    """Удалить строки-ФИЗЛИЦА в указанных категориях (по подстроке).

    Организации этих категорий и записи прочих категорий (иностранные агенты,
    в т.ч. физлица-иноагенты) остаются. Тип записи (физлицо/организация)
    определяется эвристикой из scanner.matcher.classify_kind.
    """
    if not categories or "category" not in merged.columns or "full_name" not in merged.columns:
        return merged
    before = len(merged)
    cats = [c.lower() for c in categories]
    cat_l = merged["category"].astype(str).str.lower()
    in_cat = cat_l.apply(lambda c: any(s in c for s in cats))
    is_person = merged["full_name"].apply(
        lambda n: classify_kind(_normalize_name(str(n)), str(n)) == "person"
    )
    result = merged[~(in_cat & is_person)].reset_index(drop=True)
    log.info(
        "Исключено физлиц из категорий %s: %d (осталось %d)",
        categories, before - len(result), len(result),
    )
    return result


def run(config_path: str) -> int:
    """Выполнить полный пайплайн. Возвращает код выхода процесса."""
    cfg = load_config(config_path)
    log = setup_logger(cfg.logging.directory, cfg.logging.level)
    log.info("Старт обработки. Источников в конфиге: %d", len(cfg.sources))

    # 1-2. Загрузка и очистка всех источников.
    frames = collect_sources(cfg)
    if not frames:
        log.error("Не удалось загрузить ни одного источника. Останов.")
        return 1

    # 3. Объединение.
    merged = combine(frames)

    # 4. Нормализация значений.
    merged = normalize_dataframe(merged, cfg.normalization)

    # 4b. Исключаем ФИЗЛИЦ из категорий-«шумелок» (террористы/экстремисты):
    #     на сайтах они дают только ложные срабатывания по однофамильцам.
    #     Делаем это ДО дедупликации — если человек есть и в реестре иноагентов,
    #     его запись-иноагент уцелеет (категория «Иностранный агент» не в списке).
    merged = drop_person_rows(merged, cfg.quality.drop_person_categories, log)

    # 5. Проверка качества ДО дедупликации: убираем пустые и записи со
    #    «стоп-полем» (напр. с заполненной датой исключения из реестра),
    #    чтобы исключённая запись не «вытеснила» активную при дедупликации.
    merged, report = check_quality(merged, cfg.quality)

    # 6. Дедупликация уже очищенных данных.
    merged, dup_removed = deduplicate(merged, cfg.merge)
    report.duplicates_removed = dup_removed
    report.total_rows = len(merged)
    log.info("\n%s", report.as_text())

    # 7. ГЛОБАЛЬНЫЙ health-check: если итоговых записей подозрительно мало,
    #    НЕ перезаписываем прежние выходные файлы (вероятно, отвалился крупный
    #    источник или сломался парсер) — лучше сохранить старый валидный перечень.
    if len(merged) < cfg.quality.min_total_rows:
        log.error(
            "ПРОВАЛ глобального health-check: итоговых записей %d < минимума %d. "
            "Экспорт ОТМЕНЁН, прежние файлы сохранены. Проверьте источники.",
            len(merged), cfg.quality.min_total_rows,
        )
        return 2

    # 8. Упорядочивание колонок и экспорт.
    merged = finalize_columns(merged, cfg.merge)
    created = export_all(merged, cfg.output)

    # 9. Правовая оговорка + происхождение данных рядом с результатами.
    notice_path = write_notice(cfg.output.directory)
    log.info("Правовая оговорка: %s", notice_path)

    log.info("Готово. Создано файлов: %d", len(created))
    for path in created:
        log.info("   -> %s", path)
    return 0


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Сбор и объединение данных из нескольких реестров."
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Путь к файлу конфигурации (по умолчанию: config.yaml).",
    )
    return parser.parse_args(argv)


def main() -> None:
    """CLI-обёртка с глобальной обработкой ошибок."""
    args = parse_args()
    try:
        exit_code = run(args.config)
    except Exception:  # noqa: BLE001 - последний рубеж: логируем и падаем с кодом 1
        # Логгер может быть ещё не настроен, поэтому подстраховываемся.
        log = setup_logger()
        log.exception("Критическая ошибка выполнения")
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
