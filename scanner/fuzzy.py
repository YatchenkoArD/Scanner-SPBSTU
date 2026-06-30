"""Этап 4 (search.md): нечёткий поиск похожих названий (Fuzzy Matching).

Aho–Corasick находит только ТОЧНЫЕ совпадения после нормализации. Чтобы ловить
опечатки и неизвестные варианты написания («стэнфорд» → «стэнфордд»), после
точного поиска применяется нечёткое сравнение (RapidFuzz, Damerau–Levenshtein).

Подход: из нормализованного текста страницы формируются окна по 1..N слов;
каждое окно сравнивается со словарём названий; совпадения с оценкой не ниже
порога фиксируются как нечёткие. Стоимость заметна, поэтому этап включается
опцией (для массового обхода может быть выключен).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from utils.logger import get_logger

log = get_logger()

try:
    from rapidfuzz import fuzz, process

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False


@dataclass
class FuzzyMatch:
    """Нечёткое совпадение: фрагмент текста ≈ запись словаря."""

    payload: object       # Pattern (исходная запись словаря)
    fragment: str         # найденный фрагмент текста
    score: float          # степень похожести 0..100


class FuzzyMatcher:
    """Нечёткий поиск названий из словаря в тексте (после точного этапа)."""

    def __init__(
        self,
        patterns: List,
        *,
        threshold: float = 90.0,
        min_len: int = 8,
        max_words: int = 5,
    ) -> None:
        # Берём только достаточно длинные шаблоны: на коротких fuzzy шумит.
        self.entries = [p for p in patterns if len(p.norm) >= min_len]
        self.choices = [p.norm for p in self.entries]
        self.threshold = threshold
        self.min_len = min_len
        # Сколько слов максимум в шаблонах (ограничивает размер окна).
        self.max_words = min(
            max_words, max((p.norm.count(" ") + 1 for p in self.entries), default=1)
        )
        if not _HAS_RAPIDFUZZ:
            log.warning("rapidfuzz не установлен — нечёткий поиск отключён")

    def find(self, norm_text: str, exclude_norms: set[str]) -> List[FuzzyMatch]:
        """Найти похожие фрагменты, отсутствующие среди точных совпадений.

        Args:
            norm_text: нормализованный текст страницы.
            exclude_norms: набор norm уже найденных точных совпадений (пропустить).
        """
        if not _HAS_RAPIDFUZZ or not self.choices:
            return []
        tokens = norm_text.split(" ")
        seen: Dict[str, FuzzyMatch] = {}

        # Скользящие окна по 1..max_words слов.
        for size in range(1, self.max_words + 1):
            for i in range(len(tokens) - size + 1):
                window = " ".join(tokens[i:i + size])
                if len(window) < self.min_len:
                    continue
                # Лучшее соответствие словаря для окна.
                best = process.extractOne(
                    window, self.choices,
                    scorer=fuzz.ratio, score_cutoff=self.threshold,
                )
                if not best:
                    continue
                _, score, idx = best
                pat = self.entries[idx]
                if pat.norm in exclude_norms:
                    continue  # это уже точное совпадение
                if pat.norm == window:
                    continue  # точное (не нечёткое) — пропускаем
                prev = seen.get(pat.norm)
                if prev is None or score > prev.score:
                    seen[pat.norm] = FuzzyMatch(pat, window, round(score, 1))
        return list(seen.values())
