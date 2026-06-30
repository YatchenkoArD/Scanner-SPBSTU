"""Преобразование данных: очистка, нормализация, объединение, контроль качества.

Конвейер по одному источнику: ``clean_dataframe`` (mapping + очистка ячеек +
константы + source). По объединённому набору: ``normalize_dataframe`` (даты,
телефоны, ИНН, адреса), ``combine``, ``deduplicate`` (схлопывание дублей без
учёта регистра + объединение категорий), ``check_quality`` (фильтры + отчёт).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np
import pandas as pd
from dateutil import parser as date_parser

try:  # phonenumbers — опциональная зависимость.
    import phonenumbers
    _HAS_PHONENUMBERS = True
except ImportError:  # pragma: no cover
    _HAS_PHONENUMBERS = False

from config import MergeConfig, NormalizationConfig, QualityConfig, SourceConfig
from utils.logger import get_logger

log = get_logger()

# Строки, которые трактуем как отсутствие значения.
_NA_TOKENS = {"", "nan", "none", "null", "n/a", "na", "-", "—", "нет данных"}


# --------------------------------------------------------------------------- #
#                     Очистка одного источника                                 #
# --------------------------------------------------------------------------- #
def _clean_cell(value: object) -> object:
    """Очистить ячейку: убрать лишние пробелы и пустые маркеры."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    text = re.sub(r"\s+", " ", str(value).strip())
    return np.nan if text.lower() in _NA_TOKENS else text


def apply_mapping(df: pd.DataFrame, source: SourceConfig) -> pd.DataFrame:
    """Переименовать колонки по mapping, оставив только нужные (отсутствующие — пустыми)."""
    mapping = source.mapping
    if not mapping:
        return df.copy()
    present = {src: dst for src, dst in mapping.items() if src in df.columns}
    missing = set(mapping) - set(present)
    if missing:
        log.warning("[%s] нет колонок в источнике: %s", source.name, missing)
    result = df[list(present)].rename(columns=present)
    for dst in mapping.values():
        if dst not in result.columns:
            result[dst] = np.nan
    return result


def clean_dataframe(df: pd.DataFrame, source: SourceConfig) -> pd.DataFrame:
    """Полная очистка источника: mapping + очистка ячеек + константы + source."""
    log.info("[%s] очистка данных (%d строк)", source.name, len(df))
    result = apply_mapping(df, source)
    for column in result.columns:
        result[column] = result[column].map(_clean_cell)
    result = result.dropna(how="all").reset_index(drop=True)
    # Статические колонки (категория, тип субъекта и пр.).
    for column, value in (source.options.get("constants") or {}).items():
        result[column] = value
    result["source"] = source.name
    return result


# --------------------------------------------------------------------------- #
#                            Нормализация значений                            #
# --------------------------------------------------------------------------- #
_ISO_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")


def normalize_date(value: object) -> object:
    """Дата -> ISO ``YYYY-MM-DD``. dayfirst только для НЕ-ISO (русских дд.мм.гггг)."""
    if pd.isna(value):
        return value
    text = str(value).strip()
    try:
        dt = date_parser.parse(text, dayfirst=not bool(_ISO_DATE.match(text)))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        log.debug("Не удалось разобрать дату: %r", value)
        return value


def normalize_phone(value: object, region: str = "RU") -> object:
    """Телефон -> E.164 (``+7XXXXXXXXXX``)."""
    if pd.isna(value):
        return value
    raw = str(value)
    if _HAS_PHONENUMBERS:
        try:
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return value


def normalize_id(value: object) -> object:
    """Идентификатор (ИНН/ОГРН) -> только цифры."""
    if pd.isna(value):
        return value
    digits = re.sub(r"\D", "", str(value))
    return digits or value


def normalize_address(value: object) -> object:
    """Базовая нормализация адреса: единый вид сокращений и пробелы."""
    if pd.isna(value):
        return value
    text = re.sub(r"\s+", " ", str(value)).strip().strip(",")
    for pattern, repl in {
        r"\bг\.\s*": "г. ", r"\bул\.\s*": "ул. ",
        r"\bд\.\s*": "д. ", r"\bкв\.\s*": "кв. ",
    }.items():
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def normalize_dataframe(df: pd.DataFrame, cfg: NormalizationConfig) -> pd.DataFrame:
    """Применить все правила нормализации к объединённому DataFrame."""
    log.info("Нормализация значений (%d строк)", len(df))
    result = df.copy()
    for col in cfg.date_fields:
        if col in result.columns:
            result[col] = result[col].map(normalize_date)
    for col in cfg.phone_fields:
        if col in result.columns:
            result[col] = result[col].map(
                lambda v: normalize_phone(v, cfg.default_phone_region))
    for col in cfg.id_fields:
        if col in result.columns:
            result[col] = result[col].map(normalize_id)
    for col in cfg.text_fields:
        if col in result.columns and col not in cfg.id_fields:
            result[col] = result[col].map(normalize_address)
    for col in cfg.uppercase_fields:
        if col in result.columns:
            result[col] = result[col].str.upper()
    return result


# --------------------------------------------------------------------------- #
#                  Объединение, дедупликация, качество                         #
# --------------------------------------------------------------------------- #
@dataclass
class QualityReport:
    """Сводка по качеству итоговых данных."""

    total_rows: int = 0
    duplicates_removed: int = 0
    dropped_empty: int = 0
    dropped_filled: int = 0
    missing_required: Dict[str, int] = field(default_factory=dict)
    pattern_violations: Dict[str, int] = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [
            "===== Отчёт о качестве данных =====",
            f"Итоговых строк:           {self.total_rows}",
            f"Удалено дубликатов:       {self.duplicates_removed}",
            f"Удалено пустых (по ключу): {self.dropped_empty}",
            f"Удалено по стоп-полю:      {self.dropped_filled}",
        ]
        for title, data in (("Пропуски в обязательных полях:", self.missing_required),
                            ("Нарушения шаблонов (regex):", self.pattern_violations)):
            if data:
                lines.append(title)
                lines += [f"   - {col}: {cnt}" for col, cnt in data.items()]
        return "\n".join(lines)


def _join_unique(series: pd.Series, sep: str = "; ") -> str:
    """Уникальные непустые значения группы -> строка (порядок появления)."""
    seen: List[str] = []
    for value in series:
        text = str(value).strip()
        if text and text.lower() != "nan" and text not in seen:
            seen.append(text)
    return sep.join(seen)


def combine(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """Склеить список DataFrame в один."""
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    log.info("Объединено источников: %d -> %d строк", len(frames), len(combined))
    return combined


def deduplicate(df: pd.DataFrame, cfg: MergeConfig) -> tuple[pd.DataFrame, int]:
    """Удалить дубликаты по нормализованному ключу (регистр/пробелы не важны).

    Для ``combine_columns`` (напр. category) значения группы дублей
    объединяются: сущность из нескольких реестров получает перечень всех
    своих категорий. Returns: (df, число удалённых).
    """
    keys = [c for c in cfg.deduplicate_on if c in df.columns]
    if not keys:
        return df, 0
    before = len(df)
    df = df.copy()
    tmp_keys = []
    for col in keys:
        tmp = f"__key__{col}"
        df[tmp] = (df[col].astype("string").str.upper()
                   .str.replace(r"\s+", " ", regex=True).str.strip().fillna(""))
        tmp_keys.append(tmp)
    combine_cols = [c for c in cfg.combine_columns if c in df.columns]
    for col in combine_cols:
        df[col] = df.groupby(tmp_keys, dropna=False)[col].transform(_join_unique)
    result = (df.drop_duplicates(subset=tmp_keys, keep=cfg.keep)
              .drop(columns=tmp_keys).reset_index(drop=True))
    removed = before - len(result)
    log.info("Дедупликация по %s (без учёта регистра): удалено %d строк; "
             "категории объединены для %s", keys, removed, combine_cols or "—")
    return result, removed


def check_quality(df: pd.DataFrame, cfg: QualityConfig) -> tuple[pd.DataFrame, QualityReport]:
    """Фильтры качества (пустые/стоп-поле/заглушки) + отчёт."""
    report = QualityReport()
    before = len(df)
    for col in cfg.drop_if_empty:
        if col in df.columns:
            df = df[df[col].notna() & (df[col].astype(str).str.strip() != "")]
    df = df.reset_index(drop=True)
    report.dropped_empty = before - len(df)

    before_filled = len(df)
    for col in cfg.drop_if_filled:
        if col in df.columns:
            df = df[~(df[col].notna() & (df[col].astype(str).str.strip() != ""))]
    df = df.reset_index(drop=True)
    for col, pattern in cfg.drop_if_matches.items():
        if col in df.columns:
            df = df[~df[col].astype(str).str.contains(pattern, case=False, regex=True)]
    df = df.reset_index(drop=True)
    report.dropped_filled = before_filled - len(df)

    for col in cfg.required_fields:
        if col in df.columns:
            missing = int(df[col].isna().sum())
            if missing:
                report.missing_required[col] = missing
    for col, pattern in cfg.patterns.items():
        if col in df.columns:
            regex = re.compile(pattern)
            mask = df[col].notna() & ~df[col].astype(str).str.match(regex)
            if int(mask.sum()):
                report.pattern_violations[col] = int(mask.sum())

    report.total_rows = len(df)
    return df, report
