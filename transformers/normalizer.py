"""Нормализация значений: даты, телефоны, адреса, идентификаторы.

Приводит "разношёрстные" данные из разных реестров к единому виду, чтобы
последующая дедупликация и сравнение работали корректно.
"""
from __future__ import annotations

import re

import pandas as pd
from dateutil import parser as date_parser

try:  # phonenumbers — опциональная, но желательная зависимость.
    import phonenumbers

    _HAS_PHONENUMBERS = True
except ImportError:  # pragma: no cover
    _HAS_PHONENUMBERS = False

from config import NormalizationConfig
from utils.logger import get_logger

log = get_logger()


# Уже-ISO дата/таймстамп: 2013-07-05 или 2013-07-05 00:00:00.
_ISO_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")


def normalize_date(value: object) -> object:
    """Привести дату к ISO-формату ``YYYY-MM-DD``.

    Поддерживает разные исходные форматы (``31.12.2023``, ``2023/12/31`` и т.п.).
    Если распознать не удалось — возвращает исходное значение без изменений.

    Важно: ``dayfirst=True`` применяется только к НЕ-ISO строкам (русские
    даты вида ``дд.мм.гггг``). Для уже-ISO значений (``гггг-мм-дд``) day-first
    отключается, иначе день и месяц меняются местами.
    """
    if pd.isna(value):
        return value
    text = str(value).strip()
    day_first = not bool(_ISO_DATE.match(text))
    try:
        dt = date_parser.parse(text, dayfirst=day_first)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        log.debug("Не удалось разобрать дату: %r", value)
        return value


def normalize_phone(value: object, region: str = "RU") -> object:
    """Привести телефон к формату E.164 (``+7XXXXXXXXXX``)."""
    if pd.isna(value):
        return value
    raw = str(value)

    if _HAS_PHONENUMBERS:
        try:
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except phonenumbers.NumberParseException:
            pass  # упадём в ручную логику ниже

    # Резервная логика без внешней библиотеки: оставляем только цифры.
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return value


def normalize_id(value: object) -> object:
    """Очистить идентификатор (ИНН/ОГРН): оставить только цифры."""
    if pd.isna(value):
        return value
    digits = re.sub(r"\D", "", str(value))
    return digits or value


def normalize_address(value: object) -> object:
    """Базовая нормализация адреса: единый регистр сокращений и пробелы."""
    if pd.isna(value):
        return value
    text = re.sub(r"\s+", " ", str(value)).strip().strip(",")
    # Унифицируем популярные сокращения.
    replacements = {
        r"\bг\.\s*": "г. ",
        r"\bул\.\s*": "ул. ",
        r"\bд\.\s*": "д. ",
        r"\bкв\.\s*": "кв. ",
    }
    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def normalize_dataframe(
    df: pd.DataFrame, cfg: NormalizationConfig
) -> pd.DataFrame:
    """Применить все правила нормализации к итоговому DataFrame."""
    log.info("Нормализация значений (%d строк)", len(df))
    result = df.copy()

    for col in cfg.date_fields:
        if col in result.columns:
            result[col] = result[col].map(normalize_date)

    for col in cfg.phone_fields:
        if col in result.columns:
            result[col] = result[col].map(
                lambda v: normalize_phone(v, cfg.default_phone_region)
            )

    for col in cfg.id_fields:
        if col in result.columns:
            result[col] = result[col].map(normalize_id)

    for col in cfg.text_fields:
        if col in result.columns and col not in cfg.id_fields:
            # Адресные/текстовые поля прогоняем через адресную нормализацию.
            result[col] = result[col].map(normalize_address)

    for col in cfg.uppercase_fields:
        if col in result.columns:
            result[col] = result[col].str.upper()

    return result
