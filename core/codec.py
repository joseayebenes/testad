"""Codec de referencia dirigido por el modelo: encode/decode de mensajes.

Empaqueta y desempaqueta mensajes interpretando directamente el modelo
(`core/model.py`), sin código generado. Es la implementación de referencia
("oráculo") contra la que se verificará el código autogenerado (Ada/Python),
y la que usa el panel interactivo de la web.

CONVENCIÓN DE BITS (supuesta, pendiente de confirmar con un mensaje real)
-------------------------------------------------------------------------
* El bit 0 es el PRIMER bit del mensaje; dentro de cada byte, el bit 0 es el
  más significativo (MSB-first, orden de red).
* Un campo de longitud L con ``max_position = P`` ocupa los bits
  ``[P - L + 1 .. P]`` (P es el último bit ocupado). Es coherente con los
  ICD reales observados (maxPosition="49" con w16="4" w16_b="1"...).
* Si ``max_position`` es 0, se usa el fallback ``w16*16 + w16_b`` (o
  ``w12*12 + w12_b`` si solo hay palabras de 12 bits) como bit inicial.
* Los valores multibit se almacenan MSB primero a lo largo de su rango.

LIMITACIONES DOCUMENTADAS
-------------------------
* Arrays variables: el contador y los elementos se serializan secuencialmente
  desde la posición del campo; no se admite otro campo situado DESPUÉS de un
  array variable dentro del mismo registro (longitud dinámica).
* Codificaciones soportadas: twoComplement (con signo), sin codificación o
  'unsigned' (natural), IEEE754 (32/64 bits), BCD, y ASCII para TextType.
* Variantes: para codificar se indica el caso con la clave ``"_case"`` y el
  contenido bajo ``"value"``; para decodificar, si el discriminador no es un
  campo común del propio tipo, hay que pasar ``case=...``.

API principal
-------------
    encode_message(msg, values, engineering=False) -> bytes
    decode_message(msg, data, engineering=False, case=None) -> dict
    to_engineering(scalar, raw)  /  from_engineering(scalar, value)
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

from core.model import (
    ArrayType, CompositeType, EnumScaling, Field, LinearScaling, LUTScaling,
    Message, RecordType, ScalarType, Scaling, TextType, TypeDef, VariantType,
)


class CodecError(Exception):
    """Error de codificación/decodificación o de definición insuficiente."""


# ---------------------------------------------------------------------- #
# Buffer de bits (bit 0 = MSB del byte 0)
# ---------------------------------------------------------------------- #
class BitBuffer:
    def __init__(self, nbits: int) -> None:
        self.nbits = nbits
        self.data = bytearray((nbits + 7) // 8)

    def set_bits(self, start: int, length: int, value: int) -> None:
        if start < 0 or start + length > self.nbits:
            raise CodecError(f"rango de bits fuera del mensaje: [{start}, {start + length})")
        for i in range(length):
            bit = (value >> (length - 1 - i)) & 1
            pos = start + i
            if bit:
                self.data[pos >> 3] |= 1 << (7 - (pos & 7))
            else:
                self.data[pos >> 3] &= ~(1 << (7 - (pos & 7))) & 0xFF

    def get_bits(self, start: int, length: int) -> int:
        if start < 0 or start + length > len(self.data) * 8:
            raise CodecError(f"rango de bits fuera de los datos: [{start}, {start + length})")
        value = 0
        for i in range(length):
            pos = start + i
            bit = (self.data[pos >> 3] >> (7 - (pos & 7))) & 1
            value = (value << 1) | bit
        return value

    @classmethod
    def from_bytes(cls, data: bytes) -> "BitBuffer":
        buf = cls(len(data) * 8)
        buf.data = bytearray(data)
        return buf


# ---------------------------------------------------------------------- #
# Posiciones y tamaños
# ---------------------------------------------------------------------- #
def type_bit_length(dt: Optional[TypeDef]) -> int:
    """Longitud fija en bits de un tipo (los arrays variables no la tienen)."""
    if isinstance(dt, ScalarType):
        if dt.bit_length <= 0:
            raise CodecError(f"señal '{dt.name}' sin longitud en bits")
        return dt.bit_length
    if isinstance(dt, TextType):
        if dt.max_chars <= 0:
            raise CodecError(f"texto '{dt.name}' sin longitud")
        return dt.max_chars * 8
    if isinstance(dt, ArrayType):
        raise CodecError(f"'{dt.name}' es un array variable: longitud dinámica")
    if isinstance(dt, VariantType):
        common = [f for f in dt.fields if not f.is_conditional]
        end = max((field_span(f)[0] + field_span(f)[1] for f in common), default=0)
        case_end = max(
            (field_span(f)[0] + _field_bits(f) for f in dt.cases), default=0)
        return max(end, case_end)
    if isinstance(dt, RecordType):
        if dt.bit_length > 0:
            return dt.bit_length
        end = 0
        for f in dt.fields:
            start, length = field_span(f)
            end = max(end, start + length)
        return end
    raise CodecError(f"tipo no codificable: {type(dt).__name__} '{getattr(dt, 'name', '')}'")


def _field_bits(f: Field) -> int:
    dt = f.datatype
    if dt is None:
        target = f.ref.href if f.ref else "(vacío)"
        raise CodecError(f"campo '{f.name}' sin tipo resuelto -> {target}")
    return type_bit_length(dt)


def field_span(f: Field) -> Tuple[int, int]:
    """(bit_inicial, longitud) de un campo según la convención del módulo."""
    length = _field_bits(f)
    p = f.position
    if p.max_position > 0:
        start = p.max_position - length + 1
        if start < 0:
            raise CodecError(
                f"campo '{f.name}': max_position={p.max_position} < longitud {length}")
        return start, length
    if p.word16 or p.bit16:
        return p.word16 * 16 + p.bit16, length
    if p.word12 or p.bit12:
        return p.word12 * 12 + p.bit12, length
    return 0, length  # primer campo, posición 0


# ---------------------------------------------------------------------- #
# Escalado: crudo <-> ingeniería
# ---------------------------------------------------------------------- #
def to_engineering(scalar: ScalarType, raw: int) -> Any:
    s = scalar.scaling
    if isinstance(s, LinearScaling):
        return raw * s.lsb + s.offset
    if isinstance(s, EnumScaling):
        label = s.labels.get(str(raw))
        if label is None:
            raise CodecError(f"'{scalar.name}': valor {raw} sin estado enum definido")
        return label
    if isinstance(s, LUTScaling):
        for r in s.ranges:
            if r.begin <= raw <= r.end:
                return raw * r.lsb + r.offset
        raise CodecError(f"'{scalar.name}': valor {raw} fuera de los tramos de la LUT")
    return raw  # sin escalado (o solo unidades)


def from_engineering(scalar: ScalarType, value: Any) -> int:
    s = scalar.scaling
    if isinstance(s, LinearScaling):
        if s.lsb == 0:
            raise CodecError(f"'{scalar.name}': lsb=0, escalado no invertible")
        return int(round((float(value) - s.offset) / s.lsb))
    if isinstance(s, EnumScaling):
        if isinstance(value, str):
            for raw_str, label in s.labels.items():
                if label == value:
                    return int(raw_str)
            raise CodecError(f"'{scalar.name}': estado '{value}' no definido en el enum")
        return int(value)
    if isinstance(s, LUTScaling):
        for r in s.ranges:
            if r.lsb == 0:
                continue
            raw = (float(value) - r.offset) / r.lsb
            if r.begin <= raw <= r.end:
                return int(round(raw))
        raise CodecError(f"'{scalar.name}': valor {value} no invertible con la LUT")
    return int(value)


# ---------------------------------------------------------------------- #
# Escalares y texto
# ---------------------------------------------------------------------- #
def _encode_scalar(dt: ScalarType, value: Any, engineering: bool) -> int:
    if engineering:
        value = from_engineering(dt, value)
    n = dt.bit_length
    coding = (dt.encoding or "").lower()

    if coding == "twocomplement":
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
        v = int(value)
        if not lo <= v <= hi:
            raise CodecError(f"'{dt.name}': {v} fuera de rango [{lo}, {hi}]")
        return v & ((1 << n) - 1)
    if coding == "ieee754":
        if n == 32:
            return int.from_bytes(struct.pack(">f", float(value)), "big")
        if n == 64:
            return int.from_bytes(struct.pack(">d", float(value)), "big")
        raise CodecError(f"'{dt.name}': IEEE754 requiere 32 o 64 bits, no {n}")
    if coding == "bcd":
        digits = str(int(value))
        if len(digits) * 4 > n:
            raise CodecError(f"'{dt.name}': {value} no cabe en BCD de {n} bits")
        out = 0
        for d in digits:
            out = (out << 4) | int(d)
        return out
    if coding in ("", "unsigned", "ascii"):
        v = int(value)
        if not 0 <= v < (1 << n):
            raise CodecError(f"'{dt.name}': {v} fuera de rango [0, {(1 << n) - 1}]")
        return v
    raise CodecError(f"'{dt.name}': codificación no soportada '{dt.encoding}'")


def _decode_scalar(dt: ScalarType, raw: int, engineering: bool) -> Any:
    n = dt.bit_length
    coding = (dt.encoding or "").lower()

    if coding == "twocomplement":
        value: Any = raw - (1 << n) if raw >= (1 << (n - 1)) else raw
    elif coding == "ieee754":
        nbytes = n // 8
        value = struct.unpack(">f" if n == 32 else ">d", raw.to_bytes(nbytes, "big"))[0]
    elif coding == "bcd":
        digits = []
        for shift in range(n - 4, -1, -4):
            digits.append((raw >> shift) & 0xF)
        value = int("".join(str(d) for d in digits))
    else:
        value = raw

    if engineering and isinstance(value, int):
        return to_engineering(dt, value)
    return value


def _default_scalar(dt: ScalarType) -> int:
    try:
        return int(dt.default_value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------- #
# Compuestos
# ---------------------------------------------------------------------- #
def _composite_bits(comp: CompositeType, values: Optional[Dict[str, Any]]) -> int:
    """Longitud total en bits, resolviendo arrays variables con los valores."""
    dynamic_extra = 0
    static_end = 0
    for f in comp.fields:
        dt = f.datatype
        if isinstance(dt, ArrayType):
            start = _array_start(f)
            n_items = len(_field_value(values, f) or []) if values is not None else 0
            dynamic_extra = max(dynamic_extra, start + _array_bits(dt, n_items))
            continue
        start, length = field_span(f)
        static_end = max(static_end, start + length)
    declared = comp.bit_length if isinstance(comp, RecordType) else 0
    return max(declared, static_end, dynamic_extra)


def _array_start(f: Field) -> int:
    p = f.position
    if p.max_position > 0:
        return p.max_position  # convención: para arrays, posición inicial explícita
    if p.word16 or p.bit16:
        return p.word16 * 16 + p.bit16
    return 0


def _array_bits(dt: ArrayType, n_items: int) -> int:
    counter_bits = dt.counter_bits or 8
    return counter_bits + n_items * _array_element_bits(dt)


def _array_element_bits(dt: ArrayType) -> int:
    end = 0
    for f in dt.fields:
        start, length = field_span(f)
        end = max(end, start + length)
    if end == 0:
        raise CodecError(f"array '{dt.name}' sin definición del elemento")
    return end


def _field_value(values: Optional[Dict[str, Any]], f: Field) -> Any:
    if values is None:
        return None
    return values.get(f.name) if f.name else None


def _check_no_field_after_array(comp: CompositeType) -> None:
    array_starts = [
        _array_start(f) for f in comp.fields if isinstance(f.datatype, ArrayType)]
    if not array_starts:
        return
    first_array = min(array_starts)
    for f in comp.fields:
        if isinstance(f.datatype, ArrayType):
            continue
        start, _ = field_span(f)
        if start > first_array:
            raise CodecError(
                f"campo '{f.name}' situado después de un array variable: no soportado")


def _encode_composite(comp: CompositeType, values: Dict[str, Any],
                      buf: BitBuffer, base: int, engineering: bool,
                      case: Optional[str] = None) -> None:
    if isinstance(comp, VariantType):
        self_case = values.get("_case", case)
        if self_case is None:
            raise CodecError(f"variante '{comp.name}': falta la clave '_case'")
        common = [f for f in comp.fields if not f.is_conditional]
        selected = [f for f in comp.cases if f.condition == str(self_case)]
        if not selected:
            raise CodecError(f"variante '{comp.name}': caso '{self_case}' no definido")
        for f in common:
            _encode_field(f, values, buf, base, engineering)
        _encode_field(selected[0], {"__case_value__": values.get("value")},
                      buf, base, engineering, forced_key="__case_value__")
        return

    _check_no_field_after_array(comp)
    for f in comp.fields:
        _encode_field(f, values, buf, base, engineering)


def _encode_field(f: Field, values: Dict[str, Any], buf: BitBuffer, base: int,
                  engineering: bool, forced_key: Optional[str] = None) -> None:
    dt = f.datatype
    if dt is None:
        raise CodecError(f"campo '{f.name}' sin tipo resuelto")
    value = values.get(forced_key) if forced_key else _field_value(values, f)

    if isinstance(dt, ArrayType):
        items = value or []
        start = base + _array_start(f)
        counter_bits = dt.counter_bits or 8
        buf.set_bits(start, counter_bits, len(items))
        elem_bits = _array_element_bits(dt)
        for i, item in enumerate(items):
            item_base = start + counter_bits + i * elem_bits
            _encode_composite(dt, item if isinstance(item, dict) else {}, buf,
                              item_base, engineering)
        return

    start, length = field_span(f)
    start += base
    if isinstance(dt, ScalarType):
        raw = _encode_scalar(dt, value if value is not None else _default_scalar(dt),
                             engineering)
        buf.set_bits(start, length, raw)
    elif isinstance(dt, TextType):
        text = (value or "")
        if len(text) > dt.max_chars:
            raise CodecError(f"texto '{f.name}': '{text}' excede {dt.max_chars} chars")
        padded = text.ljust(dt.max_chars, " ")
        buf.set_bits(start, length, int.from_bytes(padded.encode("ascii"), "big"))
    elif isinstance(dt, CompositeType):
        _encode_composite(dt, value if isinstance(value, dict) else {}, buf,
                          start, engineering)
    else:
        raise CodecError(f"campo '{f.name}': tipo no codificable {type(dt).__name__}")


def _decode_composite(comp: CompositeType, buf: BitBuffer, base: int,
                      engineering: bool, case: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(comp, VariantType):
        common = [f for f in comp.fields if not f.is_conditional]
        for f in common:
            out[f.name or f"campo_{id(f)}"] = _decode_field(f, buf, base, engineering)
        the_case = case
        if the_case is None and comp.discriminator:
            disc = next((f for f in common if f.name == comp.discriminator), None)
            if disc is not None:
                the_case = str(out[disc.name])
        if the_case is None:
            raise CodecError(
                f"variante '{comp.name}': no se puede determinar el caso "
                "(pasa case=... o define el discriminador como campo común)")
        selected = [f for f in comp.cases if f.condition == str(the_case)]
        if not selected:
            raise CodecError(f"variante '{comp.name}': caso '{the_case}' no definido")
        out["_case"] = str(the_case)
        out["value"] = _decode_field(selected[0], buf, base, engineering)
        return out

    _check_no_field_after_array(comp)
    for f in comp.fields:
        out[f.name or f"campo_{id(f)}"] = _decode_field(f, buf, base, engineering)
    return out


def _decode_field(f: Field, buf: BitBuffer, base: int, engineering: bool) -> Any:
    dt = f.datatype
    if dt is None:
        raise CodecError(f"campo '{f.name}' sin tipo resuelto")

    if isinstance(dt, ArrayType):
        start = base + _array_start(f)
        counter_bits = dt.counter_bits or 8
        n_items = buf.get_bits(start, counter_bits)
        if dt.max_count and n_items > dt.max_count:
            raise CodecError(
                f"array '{f.name}': contador {n_items} > máximo {dt.max_count}")
        elem_bits = _array_element_bits(dt)
        items = []
        for i in range(n_items):
            item_base = start + counter_bits + i * elem_bits
            items.append(_decode_composite(dt, buf, item_base, engineering))
        return items

    start, length = field_span(f)
    start += base
    if isinstance(dt, ScalarType):
        return _decode_scalar(dt, buf.get_bits(start, length), engineering)
    if isinstance(dt, TextType):
        raw = buf.get_bits(start, length)
        text = raw.to_bytes(dt.max_chars, "big").decode("ascii", errors="replace")
        return text.rstrip(" \x00")
    if isinstance(dt, CompositeType):
        return _decode_composite(dt, buf, start, engineering)
    raise CodecError(f"campo '{f.name}': tipo no decodificable {type(dt).__name__}")


# ---------------------------------------------------------------------- #
# API pública
# ---------------------------------------------------------------------- #
def _message_structure(msg: Message) -> CompositeType:
    st = msg.structure
    if st is None:
        raise CodecError(f"mensaje '{msg.name}' sin payload resuelto")
    if not isinstance(st, CompositeType):
        raise CodecError(f"mensaje '{msg.name}': payload no compuesto")
    return st


def encode_message(msg: Message, values: Dict[str, Any], *,
                   engineering: bool = False) -> bytes:
    """Codifica un mensaje a bytes desde un dict {campo: valor} (anidado)."""
    st = _message_structure(msg)
    total = _composite_bits(st, values)
    buf = BitBuffer(((total + 7) // 8) * 8)   # redondeado a byte
    _encode_composite(st, values, buf, 0, engineering)
    return bytes(buf.data)


def decode_message(msg: Message, data: bytes, *, engineering: bool = False,
                   case: Optional[str] = None) -> Dict[str, Any]:
    """Decodifica bytes a un dict {campo: valor} (anidado)."""
    st = _message_structure(msg)
    buf = BitBuffer.from_bytes(data)
    return _decode_composite(st, buf, 0, engineering, case=case)


def encode_type(comp: CompositeType, values: Dict[str, Any], *,
                engineering: bool = False, case: Optional[str] = None) -> bytes:
    """Codifica un tipo compuesto suelto (sin mensaje)."""
    total = _composite_bits(comp, values)
    buf = BitBuffer(((total + 7) // 8) * 8)
    _encode_composite(comp, values, buf, 0, engineering, case=case)
    return bytes(buf.data)


def decode_type(comp: CompositeType, data: bytes, *, engineering: bool = False,
                case: Optional[str] = None) -> Dict[str, Any]:
    buf = BitBuffer.from_bytes(data)
    return _decode_composite(comp, buf, 0, engineering, case=case)
