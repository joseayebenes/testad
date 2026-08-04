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

# palabras reservadas de Ada 95 (RM 2.9) + nombres usados por el runtime
_ADA_RESERVED = {
    "abort", "abs", "abstract", "accept", "access", "aliased", "all", "and",
    "array", "at", "begin", "body", "case", "constant", "declare", "delay",
    "delta", "digits", "do", "else", "elsif", "end", "entry", "exception",
    "exit", "for", "function", "generic", "goto", "if", "in", "is", "limited",
    "loop", "mod", "new", "not", "null", "of", "or", "others", "out",
    "package", "pragma", "private", "procedure", "protected", "raise",
    "range", "record", "rem", "renames", "requeue", "return", "reverse",
    "select", "separate", "subtype", "tagged", "task", "terminate", "then",
    "type", "until", "use", "when", "while", "with", "xor",
    "base", "data", "item", "t",
}


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


def ada_ident(name: str) -> str:
    """Identificador Ada Pascal_Snake: 'speedField' -> 'Speed_Field'.

    Las reservadas de Ada reciben el sufijo '_Id' (Ada no permite
    identificadores con guion bajo final).
    """
    ident = "_".join(w[:1].upper() + w[1:].lower() for w in _words(name))
    if ident[0].isdigit():
        ident = "N" + ident
    if ident.lower() in _ADA_RESERVED:
        ident += "_Id"
    return ident


def ada_file(package_name: str) -> str:
    """Nombre de fichero GNAT: 'Fcs_Icd.Nav_Block' -> 'fcs_icd-nav_block'."""
    return package_name.lower().replace(".", "-")


class Namespace:
    """Asigna identificadores únicos dentro de un ámbito (colisiones -> _2)."""

    def __init__(self) -> None:
        self._taken: Set[str] = set()
        self._by_key: Dict[int, str] = {}
        self._keep: list = []

    def assign(self, key: object, base: str) -> str:
        """Identificador único y estable para 'key' (idempotente).

        key=None asigna sin memorizar (para elementos sin identidad estable;
        no usar objetos desechables como clave: id() puede reciclarse).
        """
        if key is not None and id(key) in self._by_key:
            return self._by_key[id(key)]
        ident, n = base, 1
        while ident in self._taken:
            n += 1
            ident = f"{base}_{n}"
        self._taken.add(ident)
        if key is not None:
            self._by_key[id(key)] = ident
            self._keep.append(key)   # evita reciclaje de id()
        return ident
