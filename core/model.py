"""Modelo de dominio para describir cualquier mensaje de comunicación.

Diseñado desde cero, sin relación con la estructura del XML de origen: el
parser (core/parser.py) traduce los XMI de Eclipse EMF a este modelo en un
solo sentido (los XML no se reescriben; la persistencia futura será JSON).

Sistema de tipos (qué se transmite)
-----------------------------------
    TypeDef                     definición de tipo reutilizable
    ├── ScalarType              entero/real codificado (longitud en bits,
    │                           codificación, escalado físico)
    ├── TextType                cadenas de longitud fija o variable
    └── CompositeType           tipos con campos
        ├── RecordType          registro: secuencia de campos posicionados
        ├── ArrayType           lista de longitud variable regida por contador
        └── VariantType         campos condicionales: payloads alternativos
                                seleccionados por un discriminador

    Field                       hueco dentro de un composite: posición física
                                + tipo (referencia o inline) + condición

Escalado físico (cómo se interpreta el valor crudo)
---------------------------------------------------
    LinearScaling               v = raw * lsb + offset
    EnumScaling                 valor -> etiqueta de estado
    LUTScaling                  calibración por tramos

Transmisión (cuándo/dónde se transmite)
---------------------------------------
    Message                     payload + características temporales
    Network / Port / Bus /      arquitectura de comunicaciones
    MessageSlot                 ranura de un mensaje en un bus

Organización
------------
    Module                      unidad bajo control de configuración
    Folder                      agrupación recursiva
    Reference                   enlace a otra entidad (local o entre archivos)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Type


# ---------------------------------------------------------------------- #
# Referencias y posición física
# ---------------------------------------------------------------------- #
@dataclass
class Reference:
    """Enlace hacia otra entidad, local ('_id') o externa ('Base.xmi#_id')."""

    target_id: str = ""
    file: str = ""                  # vacío = mismo archivo
    role: str = "with"              # origen del enlace (with, explicitNational_EC...)
    owner: Optional["Entity"] = field(default=None, repr=False, compare=False)
    target: Optional["Entity"] = field(default=None, repr=False, compare=False)

    @property
    def is_resolved(self) -> bool:
        return self.target is not None

    @property
    def href(self) -> str:
        return f"{self.file}#{self.target_id}" if self.file else self.target_id


@dataclass
class BitPosition:
    """Posición física de un campo (palabras de 16/12 bits + offset de bit)."""

    word16: int = 0
    bit16: int = 0
    word12: int = 0
    bit12: int = 0
    max_position: int = 0
    pre_padding: int = 0


# ---------------------------------------------------------------------- #
# Escalado físico
# ---------------------------------------------------------------------- #
@dataclass
class Scaling:
    """Base de las reglas de interpretación física del valor crudo."""

    units: str = ""


@dataclass
class LinearScaling(Scaling):
    """v_fisico = raw * lsb + offset."""

    lsb: float = 0.0
    offset: float = 0.0


@dataclass
class EnumScaling(Scaling):
    """Valor entero -> etiqueta de estado ('0' -> 'UP', '1' -> 'DOWN')."""

    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class LUTRange:
    begin: float = 0.0
    end: float = 0.0
    lsb: float = 0.0
    offset: float = 0.0


@dataclass
class LUTScaling(Scaling):
    """Calibración por tramos no solapados, cada uno con escalado lineal."""

    ranges: List[LUTRange] = field(default_factory=list)


# ---------------------------------------------------------------------- #
# Base común
# ---------------------------------------------------------------------- #
@dataclass
class Entity:
    id: str = ""
    name: str = ""
    security: str = ""
    remarks: str = ""
    parent: Optional["Entity"] = field(default=None, repr=False, compare=False)

    @property
    def children(self) -> List["Entity"]:
        return []

    def walk(self) -> Iterator["Entity"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, cls: Optional[Type] = None, name: Optional[str] = None) -> List["Entity"]:
        """Busca en el subárbol: find(ScalarType), find(name='NavMsg')."""
        return [
            e for e in self.walk()
            if (cls is None or isinstance(e, cls)) and (name is None or e.name == name)
        ]

    def find_one(self, cls: Optional[Type] = None, name: Optional[str] = None) -> Optional["Entity"]:
        matches = self.find(cls, name)
        return matches[0] if matches else None

    @property
    def path(self) -> str:
        parts: List[str] = []
        node: Optional[Entity] = self
        while node is not None:
            parts.append(node.name or node.id or type(node).__name__)
            node = node.parent
        return "/".join(reversed(parts))

    def references(self) -> List[Reference]:
        """Enlaces salientes de esta entidad (para el resolver del registro)."""
        return []

    def _label(self) -> str:
        label = type(self).__name__
        if self.name:
            label += f" '{self.name}'"
        refs = []
        for r in self.references():
            arrow = "->" if r.is_resolved else "-?>"
            refs.append(f"{r.role}{arrow}{r.target.name if r.target else r.href}")
        if refs:
            label += "  (" + ", ".join(refs) + ")"
        return label

    def pretty(self, indent: int = 0) -> str:
        lines = ["  " * indent + self._label()]
        for child in self.children:
            lines.append(child.pretty(indent + 1))
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} '{self.name}' id={self.id!r}>"


# ---------------------------------------------------------------------- #
# Sistema de tipos
# ---------------------------------------------------------------------- #
def _scaling_summary(s: Optional[Scaling]) -> str:
    if isinstance(s, LinearScaling):
        out = f"v=raw×{s.lsb:g}"
        if s.offset:
            out += f"{s.offset:+g}"
        return out + (f" {s.units}" if s.units else "")
    if isinstance(s, EnumScaling):
        items = " | ".join(f"{v}={t}" for v, t in list(s.labels.items())[:4])
        if len(s.labels) > 4:
            items += " | ..."
        return f"estados: {items}"
    if isinstance(s, LUTScaling):
        return f"LUT de {len(s.ranges)} tramos" + (f" {s.units}" if s.units else "")
    if s is not None and s.units:
        return s.units
    return ""


@dataclass
class TypeDef(Entity):
    """Definición de tipo reutilizable. Base concreta: los tipos del XML aún
    no modelados se instancian como TypeDef plano."""

    def summary(self) -> str:
        """Resumen de una línea de la definición ('16 bits, twoComplement...')."""
        return ""

    def describe(self, indent: int = 0, _seen: Optional[set] = None) -> str:
        """Descripción completa del tipo, siguiendo referencias resueltas."""
        _seen = _seen if _seen is not None else set()
        pad = "  " * indent
        head = f"{pad}{type(self).__name__} '{self.name}'"
        s = self.summary()
        if s:
            head += f" — {s}"
        lines = [head]
        if isinstance(self, CompositeType):
            if id(self) in _seen:
                lines.append(f"{pad}  ... (recursivo)")
            else:
                _seen.add(id(self))
                for f in self.fields:
                    lines.append(f.describe(indent + 1, _seen))
        return "\n".join(lines)


@dataclass
class ScalarType(TypeDef):
    """Valor numérico codificado sobre N bits."""

    bit_length: int = 0
    encoding: str = ""              # twoComplement, BCD, IEEE754, ASCII...
    default_value: str = ""
    scaling: Optional[Scaling] = None

    @property
    def units(self) -> str:
        return self.scaling.units if self.scaling else ""

    def summary(self) -> str:
        parts = [f"{self.bit_length} bits"]
        if self.encoding:
            parts.append(self.encoding)
        s = _scaling_summary(self.scaling)
        if s:
            parts.append(s)
        return ", ".join(parts)


@dataclass
class TextType(TypeDef):
    """Cadena de texto, de longitud fija o variable."""

    max_chars: int = 0
    length_mode: str = ""           # 'fixed' | 'variable'
    encoding: str = ""              # ASCII, UTF8...
    bit_endianness: str = ""

    def summary(self) -> str:
        parts = [f"texto {self.length_mode or '?'}", f"{self.max_chars} chars"]
        if self.encoding:
            parts.append(self.encoding)
        return ", ".join(parts)


@dataclass
class Field(Entity):
    """Hueco dentro de un tipo compuesto.

    El tipo que lo ocupa viene o por referencia a una definición compartida
    (``ref``) o definido inline aquí mismo (``inline``).
    ``condition`` lo usan las variantes: valor del discriminador que activa
    este campo (campo condicional).
    """

    position: BitPosition = field(default_factory=BitPosition)
    ref: Optional[Reference] = None
    inline: Optional[TypeDef] = None
    condition: str = ""

    @property
    def is_conditional(self) -> bool:
        return self.condition != ""

    @property
    def datatype(self) -> Optional[TypeDef]:
        """La definición de tipo de este campo, venga inline o por referencia."""
        if self.inline is not None:
            return self.inline
        if self.ref is not None and isinstance(self.ref.target, TypeDef):
            return self.ref.target
        return None

    @property
    def children(self) -> List[Entity]:
        return [self.inline] if self.inline is not None else []

    def references(self) -> List[Reference]:
        return [self.ref] if self.ref is not None else []

    def describe(self, indent: int = 0, _seen: Optional[set] = None) -> str:
        """Línea del campo: posición física + tipo, descendiendo si es compuesto."""
        _seen = _seen if _seen is not None else set()
        pad = "  " * indent
        pos = f"[w16 {self.position.word16}:{self.position.bit16}]"
        cond = f" (si ={self.condition})" if self.is_conditional else ""
        origin = " (inline)" if self.inline is not None else ""

        dt = self.datatype
        if dt is None:
            target = self.ref.href if self.ref else "?"
            return f"{pad}{pos} {self.name or '<campo>'}{cond}: SIN RESOLVER -> {target}"

        if isinstance(dt, CompositeType):
            head = f"{pad}{pos} {self.name or '<campo>'}{cond}{origin}:"
            return head + "\n" + dt.describe(indent + 1, _seen)

        summary = dt.summary()
        label = f"{dt.name}" + (f" — {summary}" if summary else "")
        return f"{pad}{pos} {self.name or '<campo>'}{cond}{origin}: {label}"


@dataclass
class CompositeType(TypeDef):
    """Tipo con campos."""

    fields: List[Field] = field(default_factory=list)

    @property
    def children(self) -> List[Entity]:
        return list(self.fields)


@dataclass
class RecordType(CompositeType):
    """Registro: secuencia de campos en posiciones fijas."""

    bit_length: int = 0

    def summary(self) -> str:
        out = f"{len(self.fields)} campos"
        if self.bit_length:
            out += f", {self.bit_length} bits"
        return out


@dataclass
class ArrayType(CompositeType):
    """Lista de longitud variable: el nº de elementos lo da un contador
    transmitido en el propio mensaje. Sus ``fields`` describen el elemento."""

    counter_type: str = ""
    counter_bits: int = 0
    max_count: int = 0

    def summary(self) -> str:
        counter = self.counter_type or (f"{self.counter_bits} bits" if self.counter_bits else "?")
        return f"array variable, contador {counter}, máx {self.max_count}"


@dataclass
class VariantType(CompositeType):
    """Payloads alternativos multiplexados por un campo discriminador.

    Cada campo con ``condition`` es una alternativa: se transmite cuando el
    discriminador vale ``condition``.
    """

    discriminator: str = ""

    @property
    def cases(self) -> List[Field]:
        return [f for f in self.fields if f.is_conditional]

    def summary(self) -> str:
        return f"variante por '{self.discriminator}', {len(self.cases)} casos"


# ---------------------------------------------------------------------- #
# Transmisión
# ---------------------------------------------------------------------- #
@dataclass
class Message(Entity):
    """Mensaje transmisible: payload + características temporales."""

    period: int = 0
    rate_mode: str = ""
    payload: Optional[Reference] = None     # tipo definido en otro sitio...
    body: Optional[RecordType] = None       # ...o registro definido inline

    @property
    def structure(self) -> Optional[TypeDef]:
        if self.body is not None:
            return self.body
        if self.payload is not None and isinstance(self.payload.target, TypeDef):
            return self.payload.target
        return None

    @property
    def children(self) -> List[Entity]:
        return [self.body] if self.body is not None else []

    def references(self) -> List[Reference]:
        return [self.payload] if self.payload is not None else []

    def describe(self) -> str:
        """Ficha completa del mensaje con su layout, para inspección rápida."""
        props = []
        if self.period:
            props.append(f"periodo={self.period}")
        if self.rate_mode:
            props.append(self.rate_mode)
        head = f"Message '{self.name}'" + (f" — {', '.join(props)}" if props else "")

        st = self.structure
        if st is not None:
            return head + "\n" + st.describe(indent=1)
        if self.payload is not None:
            return head + f"\n  payload SIN RESOLVER -> {self.payload.href}"
        return head + "\n  (sin payload)"


@dataclass
class Port(Entity):
    number: int = 0
    role: str = ""
    ip_address: str = ""
    send: str = ""
    receive: str = ""


@dataclass
class MessageSlot(Entity):
    """Ranura de transmisión de un mensaje concreto dentro de un bus."""

    period: int = 0
    rate_mode: str = ""
    multicast_ip: str = ""
    max_peak_rate: int = 0
    message: Optional[Reference] = None

    def references(self) -> List[Reference]:
        return [self.message] if self.message is not None else []


@dataclass
class Bus(Entity):
    coding: str = ""
    speed: str = ""
    slots: List[MessageSlot] = field(default_factory=list)

    @property
    def children(self) -> List[Entity]:
        return list(self.slots)


@dataclass
class Network(Entity):
    protocol: str = ""                      # "UDP" | "TCP"
    alias: str = ""
    ports: List[Port] = field(default_factory=list)
    buses: List[Bus] = field(default_factory=list)

    @property
    def children(self) -> List[Entity]:
        return [*self.ports, *self.buses]


# ---------------------------------------------------------------------- #
# Organización
# ---------------------------------------------------------------------- #
@dataclass
class Folder(Entity):
    folders: List["Folder"] = field(default_factory=list)
    types: List[TypeDef] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)

    @property
    def children(self) -> List[Entity]:
        return [*self.folders, *self.types, *self.messages]


@dataclass
class Module(Folder):
    """Raíz: unidad de información bajo control de configuración."""

    networks: List[Network] = field(default_factory=list)
    national_export_control: str = ""
    us_export_control: str = ""
    explicit_national_ec: Optional[Reference] = None
    explicit_us_ec: Optional[Reference] = None
    # Archivo del que se parseó: necesario para resolver referencias
    # entre archivos ('Base.xmi#_id') cuando hay varios módulos cargados.
    source_file: str = ""

    @property
    def children(self) -> List[Entity]:
        return [*self.folders, *self.types, *self.messages, *self.networks]

    def references(self) -> List[Reference]:
        return [r for r in (self.explicit_national_ec, self.explicit_us_ec) if r is not None]
