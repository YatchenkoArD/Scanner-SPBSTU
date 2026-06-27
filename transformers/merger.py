"""Объединение реестров, дедупликация и проверка качества данных."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd

from config import MergeConfig, QualityConfig
from utils.logger import get_logger

log = get_logger()


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
        """Человекочитаемое представление отчёта для логов/консоли."""
        lines = [
            "===== Отчёт о качестве данных =====",
            f"Итоговых строк:           {self.total_rows}",
            f"Удалено дубликатов:       {self.duplicates_removed}",
            f"Удалено пустых (по ключу): {self.dropped_empty}",
            f"Удалено по стоп-полю:      {self.dropped_filled}",
        ]
        if self.missing_required:
            lines.append("Пропуски в обязательных полях:")
            for col, cnt in self.missing_required.items():
                lines.append(f"   - {col}: {cnt}")
        if self.pattern_violations:
            lines.append("Нарушения шаблонов (regex):")
            for col, cnt in self.pattern_violations.items():
                lines.append(f"   - {col}: {cnt}")
        return "\n".join(lines)


def _join_unique(series: pd.Series, sep: str = "; ") -> str:
    """Склеить уникальные непустые значения группы в строку с разделителем.

    Порядок сохраняется по первому появлению значения в группе.
    """
    seen: List[str] = []
    for value in series:
        text = str(value).strip()
        if text and text.lower() != "nan" and text not in seen:
            seen.append(text)
    return sep.join(seen)


def combine(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """Склеить список DataFrame в один с общим набором колонок."""
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    log.info("Объединено источников: %d -> %d строк", len(frames), len(combined))
    return combined


def deduplicate(df: pd.DataFrame, cfg: MergeConfig) -> tuple[pd.DataFrame, int]:
    """Удалить дубликаты по ключевым колонкам.

    Сравнение ведётся по НОРМАЛИЗОВАННОМУ ключу (верхний регистр + схлопнутые
    пробелы), поэтому «Иванов Иван» и «ИВАНОВ  ИВАН» считаются одной записью.
    Сами отображаемые значения при этом не меняются — остаётся вариант из
    записи, выбранной по правилу ``keep`` (first/last).

    Returns:
        Кортеж (очищенный DataFrame, число удалённых строк).
    """
    keys = [c for c in cfg.deduplicate_on if c in df.columns]
    if not keys:
        return df, 0
    before = len(df)
    df = df.copy()

    # Строим временные нормализованные ключи для устойчивого сравнения.
    tmp_keys = []
    for col in keys:
        tmp = f"__key__{col}"
        df[tmp] = (
            df[col]
            .astype("string")
            .str.upper()
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
            .fillna("")  # пустой ключ (напр. дата рождения у организаций) -> ""
        )
        tmp_keys.append(tmp)

    # Для combine-колонок (напр. category) собираем ВСЕ значения группы дублей
    # в один список: одна сущность из нескольких реестров получает перечень
    # всех своих категорий, напр. «Иностранный агент; Террорист/экстремист».
    combine_cols = [c for c in cfg.combine_columns if c in df.columns]
    for col in combine_cols:
        df[col] = df.groupby(tmp_keys, dropna=False)[col].transform(_join_unique)

    result = (
        df.drop_duplicates(subset=tmp_keys, keep=cfg.keep)
        .drop(columns=tmp_keys)
        .reset_index(drop=True)
    )
    removed = before - len(result)
    log.info(
        "Дедупликация по %s (без учёта регистра): удалено %d строк; "
        "категории объединены для колонок %s",
        keys, removed, combine_cols or "—",
    )
    return result, removed


def check_quality(
    df: pd.DataFrame, cfg: QualityConfig
) -> tuple[pd.DataFrame, QualityReport]:
    """Проверить качество данных и удалить заведомо некорректные строки."""
    report = QualityReport()

    # 1. Удаляем строки с пустыми ключевыми полями.
    before = len(df)
    for col in cfg.drop_if_empty:
        if col in df.columns:
            df = df[df[col].notna() & (df[col].astype(str).str.strip() != "")]
    df = df.reset_index(drop=True)
    report.dropped_empty = before - len(df)

    # 1b. Удаляем строки, где ЗАПОЛНЕНО «стоп-поле» (напр. дата исключения
    #     из реестра) — такие записи в итоговую таблицу не попадают.
    before_filled = len(df)
    for col in cfg.drop_if_filled:
        if col in df.columns:
            df = df[~(df[col].notna() & (df[col].astype(str).str.strip() != ""))]
    df = df.reset_index(drop=True)

    # 1c. Удаляем строки-заглушки по текстовому шаблону (regex), напр.
    #     «Организация исключена в связи с ликвидацией ...».
    for col, pattern in cfg.drop_if_matches.items():
        if col in df.columns:
            mask = df[col].astype(str).str.contains(pattern, case=False, regex=True)
            df = df[~mask]
    df = df.reset_index(drop=True)
    report.dropped_filled = before_filled - len(df)

    # 2. Считаем пропуски в обязательных полях (не удаляем, только фиксируем).
    for col in cfg.required_fields:
        if col in df.columns:
            missing = int(df[col].isna().sum())
            if missing:
                report.missing_required[col] = missing

    # 3. Проверяем значения на соответствие регулярным выражениям.
    for col, pattern in cfg.patterns.items():
        if col in df.columns:
            regex = re.compile(pattern)
            mask = df[col].notna() & ~df[col].astype(str).str.match(regex)
            violations = int(mask.sum())
            if violations:
                report.pattern_violations[col] = violations

    report.total_rows = len(df)
    return df, report


def finalize_columns(df: pd.DataFrame, cfg: MergeConfig) -> pd.DataFrame:
    """Сформировать финальную таблицу: порядок, отбор, нумерация, заголовки.

    Шаги:
        1. отбросить служебные колонки (drop_columns);
        2. упорядочить по output_columns (strict_columns — оставить только их);
        3. добавить сквозную нумерацию (add_row_number) первой колонкой;
        4. переименовать колонки в человекочитаемые заголовки (column_titles).
    """
    # 1. Отбрасываем служебные колонки (например, технический 'num', 'raw').
    drop = [c for c in cfg.drop_columns if c in df.columns]
    if drop:
        df = df.drop(columns=drop)

    # 2. Упорядочиваем колонки.
    if cfg.output_columns:
        ordered = [c for c in cfg.output_columns if c in df.columns]
        if cfg.strict_columns:
            df = df[ordered]  # только заданные колонки, без «хвоста»
        else:
            rest = [c for c in df.columns if c not in ordered]
            df = df[ordered + rest]

    # 3. Сквозная нумерация 1..N первой колонкой.
    if cfg.add_row_number:
        df = df.copy()
        df.insert(0, cfg.add_row_number, range(1, len(df) + 1))

    # 4. Человекочитаемые заголовки колонок.
    if cfg.column_titles:
        df = df.rename(columns=cfg.column_titles)

    return df
