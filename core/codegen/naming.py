"""Saneado determinista de nombres ICD a identificadores de código.

Normas (spec 2.6): sustitución de caracteres inválidos, prefijo si empieza
por dígito, evitar palabras reservadas, y resolución de colisiones con
sufijo estable. El mismo nombre ICD produce siempre el mismo identificador.
"""

from __future__ import annotations

import keyword
import re
from typing import Dict, Set

_INVALID = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# reservadas de Python + nombres que usa el código generado
_RESERVED = set(keyword.kwlist) | {"bitio", "dataclass", "field", "self", "base", "buf"}


def _words(name: str) -> list:
    """Trocea un nombre ICD en palabras: camelCase, guiones, espacios..."""
    parts = [p for p in _INVALID.split(name or "") if p]
    words = []
    for part in parts:
        words.extend(w for w in _CAMEL.split(part) if w)
    return words or ["sin_nombre"]


def snake(name: str) -> str:
    """'speedField' -> 'speed_field'; 'DATA-SET-INFO' -> 'data_set_info'."""
    ident = "_".join(w.lower() for w in _words(name))
    if ident[0].isdigit():
        ident = "n_" + ident
    if ident in _RESERVED:
        ident += "_"
    return ident


def pascal(name: str) -> str:
    """'nav_block' -> 'NavBlock'; 'TTF_L16_INFO' -> 'TtfL16Info'."""
    ident = "".join(w[:1].upper() + w[1:].lower() for w in _words(name))
    if ident[0].isdigit():
        ident = "N" + ident
    return ident


def py_module(name: str) -> str:
    """Nombre de módulo/paquete Python: 'FCS_ICD' -> 'fcs_icd'."""
    return snake(name)


class Namespace:
    """Asigna identificadores únicos dentro de un ámbito (colisiones -> _2)."""

    def __init__(self) -> None:
        self._taken: Set[str] = set()
        self._by_key: Dict[int, str] = {}

    def assign(self, key: object, base: str) -> str:
        """Identificador único y estable para 'key' (idempotente)."""
        if id(key) in self._by_key:
            return self._by_key[id(key)]
        ident, n = base, 1
        while ident in self._taken:
            n += 1
            ident = f"{base}_{n}"
        self._taken.add(ident)
        self._by_key[id(key)] = ident
        return ident
