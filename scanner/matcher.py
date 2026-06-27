"""Многошаблонный поиск имён из перечня в тексте (оптимизированный).

Идея оптимизации: вместо того чтобы для каждой страницы перебирать все
~24 000 имён по отдельности (O(страница × имена)), мы один раз строим
автомат Ахо-Корасик по всем именам и затем находим вхождения ВСЕХ имён за
один линейный проход по тексту страницы (O(длина_текста + число_совпадений)).

Нормализация и обе стороны (имена и текст страницы) приводятся к одному виду:
верхний регистр, латиница/кириллица/цифры сохраняются, любые иные символы
(кавычки «», -, ., *, скобки) заменяются на пробел, пробелы схлопываются.
Так различия в оформлении (кавычки, дефисы, регистр) не мешают совпадению.

Используется C-расширение ``pyahocorasick`` при наличии, иначе — встроенная
чистая Python-реализация Ахо-Корасик (медленнее на сборке, но без зависимостей).
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from utils.logger import get_logger

log = get_logger()

# Символы, которые сохраняем; всё остальное -> пробел.
_KEEP = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")


def normalize(text: str) -> str:
    """Привести строку к каноническому виду для сопоставления."""
    if not text:
        return ""
    return _KEEP.sub(" ", text).upper().strip()


# Ключевые слова, по которым запись считаем ОРГАНИЗАЦИЕЙ (а не физлицом).
_ORG_KEYWORDS = (
    "ОРГАНИЗАЦ", "ДВИЖЕНИ", "ПАРТИ", "ЦЕНТР", "ФОНД", "ОБЪЕДИНЕНИ", "ОБЩИНА",
    "ГРУПП", "АССОЦИАЦ", "СОЮЗ", "ЦЕРКОВ", "РЕЛИГИОЗН", "БРАТСТВО", "ДЖАМААТ",
    "БАТАЛЬОН", "ПОЛК", "КОНГРЕСС", "ФЕДЕРАЦ", "ИМПЕРИ", "СООБЩЕСТВ", "ФРОНТ",
    "АРМИЯ", "СЕТЬ", "МЕДИА", "УЧРЕЖДЕН", "КОМПАНИ", "ОБЩЕСТВО", "ПРОЕКТ",
    "FOUNDATION", "CENTER", "PROJECT", "MEDIA", "NETWORK",
)

# Основы самых частых русских фамилий (мужская форма; startswith ловит и
# женскую на «-а», и косвенные падежи). Такие ФИО дают коллизии-«тёзки».
_COMMON_SURNAMES = frozenset({
    "ИВАНОВ", "СМИРНОВ", "КУЗНЕЦОВ", "ПОПОВ", "ВАСИЛЬЕВ", "ПЕТРОВ", "СОКОЛОВ",
    "МИХАЙЛОВ", "НОВИКОВ", "ФЕДОРОВ", "МОРОЗОВ", "ВОЛКОВ", "АЛЕКСЕЕВ",
    "ЛЕБЕДЕВ", "СЕМЕНОВ", "ЕГОРОВ", "ПАВЛОВ", "КОЗЛОВ", "СТЕПАНОВ", "НИКОЛАЕВ",
    "ОРЛОВ", "АНДРЕЕВ", "МАКАРОВ", "НИКИТИН", "ЗАХАРОВ", "ЗАЙЦЕВ", "СОЛОВЬЕВ",
    "БОРИСОВ", "ЯКОВЛЕВ", "ГРИГОРЬЕВ", "РОМАНОВ", "ВОРОБЬЕВ", "СЕРГЕЕВ",
    "КУЗЬМИН", "ФРОЛОВ", "АЛЕКСАНДРОВ", "ДМИТРИЕВ", "КОРОЛЕВ", "ГУСЕВ",
    "КИСЕЛЕВ", "ИЛЬИН", "МАКСИМОВ", "ПОЛЯКОВ", "СОРОКИН", "ВИНОГРАДОВ",
    "КОВАЛЕВ", "БЕЛОВ", "МЕДВЕДЕВ", "АНТОНОВ", "ТАРАСОВ", "ЖУКОВ", "БАРАНОВ",
    "ФИЛИППОВ", "КОМАРОВ", "ДАВЫДОВ", "БЕЛЯЕВ", "ГЕРАСИМОВ", "БОГДАНОВ",
    "ОСИПОВ", "СИДОРОВ", "МАТВЕЕВ", "ТИТОВ", "МАРКОВ", "МИРОНОВ", "КРЫЛОВ",
    "КУЛИКОВ", "КАРПОВ", "ВЛАСОВ", "МЕЛЬНИКОВ", "ДЕНИСОВ", "ГАВРИЛОВ",
    "ТИХОНОВ", "КАЗАКОВ", "АФАНАСЬЕВ", "ДАНИЛОВ", "ЕФИМОВ", "КИРИЛЛОВ",
})


def classify_kind(norm: str, original: str) -> str:
    """Грубо определить тип записи: ``org`` (организация) или ``person`` (ФИО)."""
    if any(k in norm for k in _ORG_KEYWORDS):
        return "org"
    if "«" in original or '"' in original or "(" in original:
        return "org"
    tokens = norm.split(" ")
    # Физлицо: 2-4 слова, все кириллические (без латиницы/цифр).
    if 2 <= len(tokens) <= 4 and all(
        t.isalpha() and "А" <= t[0] <= "Я" or "Ё" == t[0] for t in tokens
    ):
        return "person"
    return "org"


def assess_confidence(norm: str, original: str) -> str:
    """Оценить достоверность совпадения (для приоритизации ручной проверки)."""
    kind = classify_kind(norm, original)
    tokens = norm.split(" ")
    if kind == "person":
        surname = tokens[0]
        if any(surname.startswith(s) for s in _COMMON_SURNAMES):
            return "низкая (возможен тёзка)"
        return "средняя"
    # Организация: длинное отличительное имя -> высокая, иначе средняя.
    if len(norm) >= 15 or len(tokens) >= 3 or "«" in original:
        return "высокая"
    return "средняя"


@dataclass
class Pattern:
    """Один поисковый шаблон (имя из перечня)."""

    norm: str          # нормализованная форма (по ней ищем)
    original: str      # как записано в перечне (для отчёта)
    category: str      # тип/категория записи
    kind: str = ""     # org | person
    confidence: str = ""  # высокая | средняя | низкая (возможен тёзка)


# --------------------------------------------------------------------------- #
#                       Чистая Python-реализация Aho-Corasick                  #
# --------------------------------------------------------------------------- #
class _PyAhoCorasick:
    """Минимальный автомат Ахо-Корасик (fallback без внешних зависимостей)."""

    def __init__(self) -> None:
        self._goto: List[Dict[str, int]] = [{}]
        self._fail: List[int] = [0]
        self._out: List[List[int]] = [[]]
        self._words: List[Tuple[str, object]] = []

    def add_word(self, word: str, payload: object) -> None:
        node = 0
        for ch in word:
            nxt = self._goto[node].get(ch)
            if nxt is None:
                nxt = len(self._goto)
                self._goto.append({})
                self._fail.append(0)
                self._out.append([])
                self._goto[node][ch] = nxt
            node = nxt
        self._out[node].append(len(self._words))
        self._words.append((word, payload))

    def make_automaton(self) -> None:
        queue: deque[int] = deque()
        for ch, nxt in self._goto[0].items():
            self._fail[nxt] = 0
            queue.append(nxt)
        while queue:
            node = queue.popleft()
            for ch, nxt in self._goto[node].items():
                queue.append(nxt)
                f = self._fail[node]
                while f and ch not in self._goto[f]:
                    f = self._fail[f]
                self._fail[nxt] = self._goto[f].get(ch, 0) if f or ch in self._goto[0] else 0
                self._out[nxt] += self._out[self._fail[nxt]]

    def iter(self, text: str) -> Iterable[Tuple[int, Tuple[str, object]]]:
        node = 0
        for i, ch in enumerate(text):
            while node and ch not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(ch, 0)
            for widx in self._out[node]:
                yield i, self._words[widx]


def _build_automaton(patterns: Iterable[Pattern]):
    """Собрать автомат: pyahocorasick при наличии, иначе чистый Python."""
    try:
        import ahocorasick  # type: ignore

        automaton = ahocorasick.Automaton()
        engine = "pyahocorasick (C)"
        for pat in patterns:
            automaton.add_word(pat.norm, pat)
        automaton.make_automaton()
    except ImportError:
        automaton = _PyAhoCorasick()
        engine = "pure-python"
        for pat in patterns:
            automaton.add_word(pat.norm, pat)
        automaton.make_automaton()
    log.info("Движок поиска: %s", engine)
    return automaton


@dataclass
class Match:
    """Найденное вхождение шаблона в тексте."""

    pattern: Pattern
    start: int
    end: int  # включительно (индекс последнего символа)


class NameMatcher:
    """Строит автомат по перечню имён и ищет их вхождения в тексте."""

    def __init__(self, patterns: List[Pattern]) -> None:
        self.patterns = patterns
        log.info("Построение автомата по %d именам...", len(patterns))
        self._automaton = _build_automaton(patterns)

    def find(self, text: str) -> List[Match]:
        """Найти вхождения имён в УЖЕ нормализованном тексте.

        Проверяются границы слов: совпадение должно быть отделено пробелом
        (или краем строки) с обеих сторон — чтобы «ЯШИН» не находилось внутри
        «КУЗЯШИН».
        """
        matches: List[Match] = []
        n = len(text)
        for end_index, pat in self._automaton.iter(text):
            start_index = end_index - len(pat.norm) + 1
            before_ok = start_index == 0 or text[start_index - 1] == " "
            after_ok = end_index == n - 1 or text[end_index + 1] == " "
            if before_ok and after_ok:
                matches.append(Match(pat, start_index, end_index))
        return matches


# Одиночные общеупотребительные слова: как самостоятельный шаблон дают шум
# (напр. дефектная запись «ОБЪЕДИНЕНИЕ» совпадала со словом в обычном тексте:
# «студенческое объединение», «объединение институтов»). В составных именах
# эти слова остаются — отбрасываются только КАК ОТДЕЛЬНЫЙ шаблон.
_DEFAULT_STOPWORDS = frozenset({
    "ОБЪЕДИНЕНИЕ", "ОРГАНИЗАЦИЯ", "ГРУППА", "ЦЕНТР", "ФОНД", "ДВИЖЕНИЕ",
    "СОЮЗ", "АССОЦИАЦИЯ", "ОБЩИНА", "СЕТЬ", "ПРОЕКТ", "АРМИЯ", "СИРЕНА",
    "БРАТСТВО", "СООБЩЕСТВО", "ПАРТИЯ", "КОНГРЕСС", "ФРОНТ", "ИМПЕРИЯ",
    "ПОЛК", "БАТАЛЬОН", "ФЕДЕРАЦИЯ", "ОБЩЕСТВО", "КОМПАНИЯ", "МЕДИА",
    "ЦЕРКОВЬ", "СОВЕТ", "КОМИТЕТ", "ШТАБ", "БЛОК", "ФОРУМ",
})


def _matches_rule(pat: Pattern, rule: dict) -> bool:
    """Подходит ли шаблон под правило фильтра (kind и/или category_contains)."""
    if "kind" in rule and pat.kind != rule["kind"]:
        return False
    subs = rule.get("category_contains")
    if subs and not any(s.lower() in pat.category.lower() for s in subs):
        return False
    return True


def load_patterns(
    rows: Iterable[Tuple[str, str]],
    min_length: int = 6,
    stopwords: Iterable[str] | None = None,
    drop: List[dict] | None = None,
    keep_only: List[dict] | None = None,
) -> List[Pattern]:
    """Построить список шаблонов из пар (имя, категория).

    Отбрасываются:
      • имена короче ``min_length`` (после нормализации);
      • одиночные общеупотребительные слова (стоп-слова);
      • шаблоны, подходящие под любое правило ``drop`` (напр. физлица в
        категории «террорист/экстремист» — дают шум по тёзкам);
      • если задан ``keep_only`` — остаются ТОЛЬКО подходящие под него
        (напр. оставить лишь физлиц-террористов для точечной проверки).
    Правило: ``{"kind": "person"|"org", "category_contains": ["...", ...]}``.
    Дубликаты нормализованных форм схлопываются.
    """
    stop = {s.upper() for s in (stopwords or _DEFAULT_STOPWORDS)}
    seen: Dict[str, Pattern] = {}
    skipped_short = skipped_stop = skipped_filter = 0
    for original, category in rows:
        norm = normalize(original)
        if len(norm) < min_length:
            skipped_short += 1
            continue
        if " " not in norm and norm in stop:
            skipped_stop += 1
            continue
        if norm in seen:
            continue
        pat = Pattern(
            norm=norm,
            original=original,
            category=category,
            kind=classify_kind(norm, original),
            confidence=assess_confidence(norm, original),
        )
        # Фильтры по типу/категории.
        if drop and any(_matches_rule(pat, r) for r in drop):
            skipped_filter += 1
            continue
        if keep_only and not any(_matches_rule(pat, r) for r in keep_only):
            skipped_filter += 1
            continue
        seen[norm] = pat
    log.info(
        "Шаблонов готово: %d (коротких: %d, стоп-слов: %d, по фильтру: %d)",
        len(seen), skipped_short, skipped_stop, skipped_filter,
    )
    return list(seen.values())


def patterns_from_aliases(aliases: Iterable[dict]) -> List[Pattern]:
    """Построить шаблоны из списка алиасов (разные написания одной сущности).

    Нужно для сущностей, которые на сайтах пишут по-разному — на латинице и
    кириллице, с опечатками и сокращениями. Например, Meta/Instagram:
    ``Instagram``, ``Инстаграм``, ``Facebook``, ``Фейсбук`` и т.п. указывают
    на одну запись перечня. Алиасы курируются вручную и НЕ фильтруются
    стоп-словами.

    Каждый элемент ``aliases``:
        entity (str)     — как показывать в отчёте (каноническое имя).
        category (str)   — категория.
        variants (list)  — список написаний, по которым ищем.
        confidence (str) — достоверность (по умолчанию «высокая»).
    """
    patterns: List[Pattern] = []
    for alias in aliases:
        entity = alias["entity"]
        category = alias.get("category", "")
        confidence = alias.get("confidence", "высокая")
        for variant in alias.get("variants", []):
            norm = normalize(variant)
            if len(norm) < 3:
                continue
            patterns.append(
                Pattern(
                    norm=norm,
                    original=entity,
                    category=category,
                    kind="org",
                    confidence=confidence,
                )
            )
    log.info("Шаблонов из алиасов: %d", len(patterns))
    return patterns
