"""Настройка единого логгера для всего приложения.

Логи пишутся одновременно в консоль и в файл с отметкой времени запуска,
чтобы каждый прогон скрипта имел собственный журнал в каталоге ``logs/``.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_LOGGER_NAME = "registry_merger"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logger(log_dir: str | Path = "logs", level: str = "INFO") -> logging.Logger:
    """Создать (или вернуть уже настроенный) логгер приложения.

    Args:
        log_dir: каталог для файлов логов; будет создан при отсутствии.
        level: уровень логирования в виде строки (DEBUG/INFO/...).

    Returns:
        Готовый к использованию :class:`logging.Logger`.
    """
    logger = logging.getLogger(_LOGGER_NAME)

    # Повторная настройка не нужна, если обработчики уже добавлены.
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT)

    # --- Вывод в консоль ---
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # --- Вывод в файл ---
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(
        log_path / f"run_{timestamp}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    """Вернуть уже сконфигурированный логгер приложения."""
    return logging.getLogger(_LOGGER_NAME)
