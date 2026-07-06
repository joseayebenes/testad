"""Lógica de presentación de los visores de mensaje y de tipo.

Funciones puras que transforman el modelo en filas/estructuras listas para
pintar como tabla. Separadas de NiceGUI para poder testearlas sin navegador.

* ``message_rows(message)`` — aplana un mensaje completo (su estructura y las
  subestructuras anidadas) en una lista de filas, siguiendo las referencias
  resueltas. Es lo que muestra el "visor de mensaje".
* ``type_view(typedef)``   — describe cómo se decodifica un tipo: propiedades,
  escalado detallado (lineal/enum/LUT) y, si es compuesto, sus campos. Es lo
  que muestra el "visor de tipo".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.model import (
    ArrayType, CompositeType, EnumScaling, Field, LinearScaling, LUTScaling,
    Message, RecordType, Scaling, ScalarType, TextType, TypeDef,
)


@dataclass
class Row:
    """Una fila del visor: un campo del mensaje/tipo, aplanado."""
    level: int = 0
    name: str = ""
    position: str = ""
    bits: str = ""
    type_name: str = ""
    coding: str = ""
    scaling: str = ""
    condition: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # sangría visual por nivel (espacios duros para que la tabla los respete)
        d["name"] = ("   " * self.level) + (self.name or "—")
        return d


# ---------------------------------------------------------------------- #
# Helpers de formato
# ---------------------------------------------------------------------- #
def _position(f: Field) -> str:
    p = f.position
    if p.word12 or p.bit12:
        return f"w12 {p.word12}.{p.bit12}"
    return f"w16 {p.word16}.{p.bit16}"


def _bits(dt: Optional[TypeDef]) -> str:
    if isinstance(dt, ScalarType):
        return str(dt.bit_length)
    if isinstance(dt, TextType):
        return f"{dt.max_chars} ch"
    if isinstance(dt, ArrayType):
        return "variable"
    if isinstance(dt, RecordType):
        return str(dt.bit_length) if dt.bit_length else ""
    return ""


def _type_name(f: Field, dt: Optional[TypeDef]) -> str:
    if dt is None:
        if f.inline is None and f.ref is None:
            return "(vacío)"
        return "(sin resolver)"
    label = type(dt).__name__
    return f"{label} · {dt.name}" if dt.name else label


def scaling_detail(s: Optional[Scaling]) -> Dict[str, Any]:
    """Escalado en forma estructurada, para el visor de tipo."""
    if isinstance(s, LinearScaling):
        return {"kind": "linear", "units": s.units, "lsb": s.lsb, "offset": s.offset,
                "formula": f"valor = crudo × {s.lsb:g}"
                           + (f" + {s.offset:g}" if s.offset else "")
                           + (f"  [{s.units}]" if s.units else "")}
    if isinstance(s, EnumScaling):
        return {"kind": "enum", "units": s.units,
                "labels": [{"valor": v, "estado": t} for v, t in s.labels.items()]}
    if isinstance(s, LUTScaling):
        return {"kind": "lut", "units": s.units,
                "ranges": [{"desde": r.begin, "hasta": r.end, "lsb": r.lsb, "offset": r.offset}
                           for r in s.ranges]}
    if s is not None:
        return {"kind": "raw", "units": s.units}
    return {"kind": None}


def _scaling_summary(dt: Optional[TypeDef]) -> str:
    s = getattr(dt, "scaling", None)
    info = scaling_detail(s)
    kind = info.get("kind")
    if kind == "linear":
        return info["formula"]
    if kind == "enum":
        return f"{len(info['labels'])} estados"
    if kind == "lut":
        return f"LUT {len(info['ranges'])} tramos"
    if kind == "raw" and info.get("units"):
        return info["units"]
    return ""


def _note(f: Field) -> str:
    if f.inline is not None:
        return "inline"
    if f.ref is not None and not f.ref.is_resolved:
        return f"SIN RESOLVER → {f.ref.href}"
    if f.ref is not None:
        return f"→ {f.ref.href}"
    return ""


# ---------------------------------------------------------------------- #
# Aplanado
# ---------------------------------------------------------------------- #
def _field_row(f: Field, level: int) -> Row:
    dt = f.datatype
    return Row(
        level=level,
        name=f.name,
        position=_position(f),
        bits=_bits(dt),
        type_name=_type_name(f, dt),
        coding=getattr(dt, "encoding", "") or "",
        scaling=_scaling_summary(dt),
        condition=f.condition,
        note=_note(f),
    )


def flatten(composite: CompositeType, level: int = 0, _seen: Optional[set] = None) -> List[Row]:
    """Filas de un tipo compuesto, descendiendo por los campos compuestos."""
    _seen = _seen if _seen is not None else set()
    if id(composite) in _seen:
        return [Row(level=level, name="… (recursivo)")]
    _seen = _seen | {id(composite)}
    rows: List[Row] = []
    for f in composite.fields:
        rows.append(_field_row(f, level))
        dt = f.datatype
        if isinstance(dt, CompositeType):
            rows.extend(flatten(dt, level + 1, _seen))
    return rows


def message_rows(message: Message) -> List[Row]:
    """Aplana el mensaje completo (estructura + subestructuras) en filas."""
    st = message.structure
    if st is None:
        return []
    if isinstance(st, CompositeType):
        return flatten(st)
    # payload escalar directo (raro): una única fila
    return [Row(name=st.name, bits=_bits(st), type_name=type(st).__name__,
                coding=getattr(st, "encoding", ""), scaling=_scaling_summary(st))]


# ---------------------------------------------------------------------- #
# Visor de tipo
# ---------------------------------------------------------------------- #
def type_view(t: TypeDef) -> Dict[str, Any]:
    """Cómo se decodifica un tipo: propiedades + escalado + campos."""
    props: List[Dict[str, str]] = []

    def add(k: str, v: Any) -> None:
        if v not in ("", None):
            props.append({"prop": k, "valor": str(v)})

    view: Dict[str, Any] = {"kind": type(t).__name__, "name": t.name,
                            "props": props, "scaling": {"kind": None}, "fields": []}

    if isinstance(t, ScalarType):
        add("Longitud (bits)", t.bit_length)
        add("Codificación", t.encoding)
        add("Valor por defecto", t.default_value)
        add("Unidades", t.units)
        view["scaling"] = scaling_detail(t.scaling)
    elif isinstance(t, TextType):
        add("Máx. caracteres", t.max_chars)
        add("Modo de longitud", t.length_mode)
        add("Codificación", t.encoding)
        add("Endianness de bit", t.bit_endianness)
    elif isinstance(t, ArrayType):
        add("Tipo de contador", t.counter_type)
        add("Bits de contador", t.counter_bits)
        add("Máx. elementos", t.max_count)
        view["fields"] = [r.as_dict() for r in flatten(t)]
    elif isinstance(t, RecordType) and not isinstance(t, ArrayType):
        add("Longitud (bits)", t.bit_length)
        add("Nº de campos", len(t.fields))
        view["fields"] = [r.as_dict() for r in flatten(t)]
    else:  # VariantType u otros compuestos
        disc = getattr(t, "discriminator", "")
        add("Discriminador", disc)
        if isinstance(t, CompositeType):
            add("Nº de casos", len(getattr(t, "cases", t.fields)))
            view["fields"] = [r.as_dict() for r in flatten(t)]

    return view


MESSAGE_COLUMNS = [
    {"name": "name", "label": "Campo", "field": "name", "align": "left"},
    {"name": "position", "label": "Posición", "field": "position", "align": "left"},
    {"name": "bits", "label": "Bits", "field": "bits", "align": "right"},
    {"name": "type_name", "label": "Tipo", "field": "type_name", "align": "left"},
    {"name": "coding", "label": "Codificación", "field": "coding", "align": "left"},
    {"name": "scaling", "label": "Escalado", "field": "scaling", "align": "left"},
    {"name": "condition", "label": "Cond.", "field": "condition", "align": "left"},
    {"name": "note", "label": "Nota", "field": "note", "align": "left"},
]
