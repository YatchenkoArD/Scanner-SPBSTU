"""Загрузка и валидация конфигурации проекта.

Модуль читает YAML- (или JSON-) файл конфигурации и превращает его в набор
типизированных датаклассов. Это даёт автодополнение в IDE, проверку структуры
на старте и единую точку доступа к настройкам во всём проекте.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class SourceConfig:
    """Описание одного источника данных (реестра)."""

    name: str
    type: str  # web | api | excel | csv | xml | json
    path: str  # путь к файлу или URL
    mapping: Dict[str, str] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizationConfig:
    """Правила нормализации значений."""

    date_fields: List[str] = field(default_factory=list)
    phone_fields: List[str] = field(default_factory=list)
    default_phone_region: str = "RU"
    id_fields: List[str] = field(default_factory=list)
    text_fields: List[str] = field(default_factory=list)
    uppercase_fields: List[str] = field(default_factory=list)


@dataclass
class MergeConfig:
    """Параметры объединения и дедупликации."""

    deduplicate_on: List[str] = field(default_factory=list)
    keep: str = "first"
    # Колонки, значения которых при схлопывании дублей ОБЪЕДИНЯЮТСЯ в перечень
    # (через "; "). Напр. category: сущность из нескольких реестров получает
    # список всех своих категорий.
    combine_columns: List[str] = field(default_factory=list)


@dataclass
class QualityConfig:
    """Параметры проверки качества данных."""

    required_fields: List[str] = field(default_factory=list)
    drop_if_empty: List[str] = field(default_factory=list)
    # Колонки, при ЗАПОЛНЕННОМ значении которых строка удаляется
    # (например, дата исключения из реестра).
    drop_if_filled: List[str] = field(default_factory=list)
    # {колонка: regex} — удалить строки, где значение соответствует шаблону
    # (например, заглушки «Организация исключена ...»).
    drop_if_matches: Dict[str, str] = field(default_factory=dict)
    patterns: Dict[str, str] = field(default_factory=dict)
    # HEALTH-CHECK: если итоговых записей меньше — выгрузка отменяется (вероятно,
    # сломался парсер источника), прежние данные в БД НЕ перезаписываются.
    min_total_rows: int = 0


@dataclass
class DatabaseConfig:
    """Параметры выгрузки в базу данных (PostgreSQL)."""

    url: str = "postgresql+psycopg2://localhost/registry"
    table_all: str = "registry_all"          # совмещённый список
    table_persons: str = "registry_persons"  # только физлица
    table_orgs: str = "registry_orgs"        # только организации
    directory: str = "output"                # для DISCLAIMER.txt


@dataclass
class LoggingConfig:
    """Параметры логирования."""

    directory: str = "logs"
    level: str = "INFO"


@dataclass
class AppConfig:
    """Корневая конфигурация всего приложения."""

    sources: List[SourceConfig]
    normalization: NormalizationConfig
    merge: MergeConfig
    quality: QualityConfig
    database: DatabaseConfig
    logging: LoggingConfig


def _read_raw(path: Path) -> Dict[str, Any]:
    """Прочитать YAML или JSON в зависимости от расширения файла."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text) or {}
    if path.suffix.lower() == ".json":
        return json.loads(text)
    raise ValueError(f"Неподдерживаемое расширение конфига: {path.suffix}")


def load_config(path: str | Path) -> AppConfig:
    """Загрузить конфиг из файла и собрать датаклассы.

    Args:
        path: путь к config.yaml / config.json.

    Returns:
        Полностью заполненный :class:`AppConfig`.

    Raises:
        FileNotFoundError: если файл не найден.
        ValueError: если структура конфигурации некорректна.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {cfg_path}")

    raw = _read_raw(cfg_path)

    raw_sources = raw.get("sources") or []
    if not raw_sources:
        raise ValueError("В конфигурации не задан ни один источник (sources).")

    sources = [
        SourceConfig(
            name=s["name"],
            type=str(s["type"]).lower(),
            path=s["path"],
            mapping=s.get("mapping", {}),
            options=s.get("options", {}),
        )
        for s in raw_sources
    ]

    return AppConfig(
        sources=sources,
        normalization=NormalizationConfig(**(raw.get("normalization") or {})),
        merge=MergeConfig(**(raw.get("merge") or {})),
        quality=QualityConfig(**(raw.get("quality") or {})),
        database=DatabaseConfig(**(raw.get("database") or {})),
        logging=LoggingConfig(**(raw.get("logging") or {})),
    )
