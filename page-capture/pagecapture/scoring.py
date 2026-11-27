"""Heurística pura para escolher o controlo de "avançar" (testável sem browser)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

POSITIVE_TERMS = frozenset(
    {
        "next", "seguinte", "proximo", "proxima", "avancar", "continuar", "continue",
        "forward", "proceed", "siguiente", "suivant", "weiter",
    }
)
NEGATIVE_TERMS = frozenset(
    {
        "back", "anterior", "voltar", "previous", "prev", "retroceder", "exit", "sair",
        "close", "fechar", "menu", "home", "inicio", "restart", "reiniciar", "replay",
        "repetir", "glossary", "glossario", "help", "ajuda", "print", "imprimir", "logout",
    }
)
DIRECTION_ICON_TERMS = frozenset({"arrow", "chevron", "caret", "angle"})

MAX_LABEL_TEXT = 40
ARROW_ONLY_RE = re.compile(r"^[>›»→▶►❯⟩⇨➔➜➤\s]+$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
CLICKABLE_TAGS = frozenset({"button", "a", "input"})


@dataclass(frozen=True)
class Candidate:
    frame_index: int
    idx: int
    score: float
    description: str


def tokens(s: str | None) -> set[str]:
    """Palavras normalizadas: separa camelCase, remove acentos, lowercase."""
    if not s:
        return set()
    s = _CAMEL_RE.sub(" ", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return set(_TOKEN_RE.findall(s))


def _text(feat: dict) -> str:
    return (feat.get("text") or "").strip()


def _label_tokens(feat: dict) -> set[str]:
    parts = [feat.get("aria"), feat.get("title"), feat.get("alt")]
    text = _text(feat)
    if len(text) <= MAX_LABEL_TEXT:
        parts.append(text)
    return tokens(" ".join(p for p in parts if p))


def score(feat: dict) -> float | None:
    """Pontuação de um elemento. None = excluído (desativado, negativo ou seta para a esquerda)."""
    if feat.get("disabled"):
        return None
    labels = _label_tokens(feat)
    code = tokens(f"{feat.get('id') or ''} {feat.get('cls') or ''}")
    every = labels | code
    if every & NEGATIVE_TERMS:
        return None
    if "left" in every and every & DIRECTION_ICON_TERMS:
        return None

    s = 0.0
    if labels & POSITIVE_TERMS:
        s += 10
    text = _text(feat)
    if text and ARROW_ONLY_RE.match(text):
        s += 7
    if code & POSITIVE_TERMS:
        s += 6
    if "right" in every and every & DIRECTION_ICON_TERMS:
        s += 5
    if s <= 0:
        return 0.0

    vw, vh = feat.get("vw") or 0, feat.get("vh") or 0
    if vw and feat.get("x", 0) > 0.5 * vw:
        s += 1
    if vh and feat.get("y", 0) > 0.6 * vh:
        s += 1
    if feat.get("tag") in CLICKABLE_TAGS or feat.get("role") == "button":
        s += 0.5
    if vw and vh and feat.get("w", 0) * feat.get("h", 0) > 0.15 * vw * vh:
        s -= 15
    return s


def describe(feat: dict) -> str:
    """Descrição curta do elemento para o index.csv."""
    text = _text(feat)
    label = next(
        (v for v in (text if len(text) <= MAX_LABEL_TEXT else "", feat.get("aria"),
                     feat.get("title"), feat.get("alt")) if v),
        "",
    )
    out = feat.get("tag") or "?"
    if label:
        out += f' "{" ".join(label.split())}"'
    if feat.get("id"):
        out += f" #{feat['id']}"
    if feat.get("cls"):
        out += " ." + ".".join((feat["cls"] or "").split())[:60]
    return out[:120]


MIN_SCORE = 6


def is_blocked_next(feat: dict, min_score: float = MIN_SCORE) -> bool:
    """Controlo de avançar que existe mas está desativado (ex. slide Storyline a decorrer)."""
    if not feat.get("disabled"):
        return False
    s = score({**feat, "disabled": False})
    return s is not None and s >= min_score


def rank(frames_features: list[list[dict]], min_score: float = MIN_SCORE) -> list[Candidate]:
    """Candidatos com score >= min_score em todos os frames; melhor primeiro, DOM no empate."""
    found: list[Candidate] = []
    for fi, feats in enumerate(frames_features):
        for idx, feat in enumerate(feats):
            s = score(feat)
            if s is not None and s >= min_score:
                found.append(Candidate(fi, idx, s, describe(feat)))
    found.sort(key=lambda c: c.score, reverse=True)
    return found
