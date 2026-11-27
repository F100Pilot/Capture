"""Validação de parâmetros partilhada entre a CLI e a interface web."""

from __future__ import annotations

import argparse
import re
from urllib.parse import urlparse


def url_arg(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    if parsed.scheme == "file" and parsed.path:
        return value
    raise argparse.ArgumentTypeError("URL inválido: use http://, https:// ou file://")


def viewport_arg(value: str) -> dict[str, int]:
    m = re.fullmatch(r"(\d{2,5})[xX](\d{2,5})", value.strip())
    if not m:
        raise argparse.ArgumentTypeError("viewport inválido: use LARGURAxALTURA, ex. 1366x768")
    width, height = int(m[1]), int(m[2])
    if not (200 <= width <= 10000 and 200 <= height <= 10000):
        raise argparse.ArgumentTypeError("viewport fora dos limites (200-10000 px)")
    return {"width": width, "height": height}


def int_min1(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"inteiro inválido: {value}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("tem de ser >= 1")
    return n


def positive_float(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"número inválido: {value}") from None
    if not x > 0:
        raise argparse.ArgumentTypeError("tem de ser > 0")
    return x


def fraction_arg(value: str) -> float:
    x = positive_float(value)
    if x >= 1:
        raise argparse.ArgumentTypeError("tem de estar entre 0 e 1 (ex. 0.003)")
    return x
