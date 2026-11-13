"""Deterministic keyword suggestions from a free-text domain description (no model, no network).

Order: quoted phrases (as written), then frequent 2-3-word phrases without stopwords, then single words.
Same input always gives the same output; ties break by first position in the text.
"""

from __future__ import annotations

import re
from collections import Counter

# Cyrillic on purpose: the suggester also handles Russian domain descriptions, so Russian stop-words are functional.
STOPWORDS = frozenset(
    {
        # English
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for", "from", "has", "have",
        "how", "i", "if", "in", "into", "is", "it", "its", "like", "more", "most", "not", "of", "on", "or",
        "other", "our", "over", "so", "some", "such", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "to", "up", "use", "used", "using", "via", "we", "what", "when",
        "where", "which", "who", "will", "with", "you", "your", "all", "any", "also", "about", "only",
        # Russian
        "а", "без", "бы", "был", "была", "были", "быть", "в", "во", "вы", "где", "да", "для", "до", "его",
        "ее", "её", "если", "есть", "еще", "ещё", "же", "за", "и", "из", "или", "им", "их", "к", "как", "ко",
        "когда", "кто", "ли", "мы", "на", "над", "не", "нет", "но", "о", "об", "он", "она", "они", "оно",
        "от", "по", "под", "при", "с", "со", "так", "также", "те", "то", "только", "у", "что", "это", "эти",
        "этот", "я",
    }
)  # fmt: skip

_TOKEN = re.compile(r"[^\W_]+(?:[-+#.][^\W_]+)*")
_QUOTED = re.compile(r'"([^"\n]{2,80})"|«([^»\n]{2,80})»|`([^`\n]{2,80})`')
_BAD_IN_TERM = re.compile(r'[,;"«»`]')
_MIN_TOKEN = 2


def _clean_term(text: str) -> str:
    """Lowercase, collapse spaces, drop characters that would break the `a, b; c` answer format."""
    return " ".join(_BAD_IN_TERM.sub(" ", text).lower().split())


def _quoted_phrases(description: str) -> list[str]:
    found: list[str] = []
    for match in _QUOTED.finditer(description):
        phrase = _clean_term(next(g for g in match.groups() if g is not None))
        if phrase and phrase not in found:
            found.append(phrase)
    return found


def _runs(description: str) -> list[list[str]]:
    """Maximal runs of non-stopword tokens; a stopword ends a run so phrases never span one."""
    runs: list[list[str]] = [[]]
    for token in _TOKEN.findall(description.lower()):
        if token in STOPWORDS or len(token) < _MIN_TOKEN or token.isdigit():
            if runs[-1]:
                runs.append([])
        else:
            runs[-1].append(token)
    return [run for run in runs if run]


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    size = len(shorter)
    return any(longer[i : i + size] == shorter for i in range(len(longer) - size + 1))


def suggest_keywords(description: str, max_groups: int = 6) -> list[list[str]]:
    """Keyword groups (each a one-term list) suggested from the description, best first."""
    groups: list[tuple[str, ...]] = [tuple(phrase.split()) for phrase in _quoted_phrases(description)]
    runs = _runs(description)

    # Phrases of 2-3 words, counted over the runs; ties go to longer phrases, then to earlier ones.
    phrase_count: Counter[tuple[str, ...]] = Counter()
    phrase_first: dict[tuple[str, ...], int] = {}
    position = 0
    for run in runs:
        for size in (2, 3):
            for start in range(len(run) - size + 1):
                phrase = tuple(run[start : start + size])
                phrase_count[phrase] += 1
                phrase_first.setdefault(phrase, position + start)
        position += len(run)
    repeated = [p for p in phrase_count if phrase_count[p] >= 2]
    repeated.sort(key=lambda p: (-phrase_count[p], -len(p), phrase_first[p]))

    # Single words: most frequent first, then first appearance.
    word_count: Counter[str] = Counter(token for run in runs for token in run)
    word_first: dict[str, int] = {}
    position = 0
    for run in runs:
        for offset, token in enumerate(run):
            word_first.setdefault(token, position + offset)
        position += len(run)
    words = sorted(word_count, key=lambda w: (-word_count[w], word_first[w]))

    for candidate in [*repeated, *((w,) for w in words)]:
        if len(groups) >= max_groups:
            break
        if not any(_contains(existing, candidate) for existing in groups):
            groups.append(candidate)
    return [[" ".join(group)] for group in groups[:max_groups]]
