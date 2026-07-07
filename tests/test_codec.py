"""Tests del codec de referencia (encode/decode dirigido por el modelo).

Los bytes esperados están calculados a mano según la convención documentada
en core/codec.py (bit 0 = primer bit del mensaje, MSB-first; campo en
[max_position - length + 1 .. max_position]).

Ejecutar con:  python3 tests/test_codec.py   (o python -m pytest tests/)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.codec import (
    CodecError, decode_message, decode_type, encode_message, encode_type,
    from_engineering, to_engineering,
)
from core.model import (
    ArrayType, BitPosition, Field, Message, RecordType, ScalarType, TextType,
    VariantType,
)
from core.parser import ICDParser
from core.registry import ICDRegistry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load():
    reg = ICDRegistry()
    p = ICDParser(reg)
    fcs = p.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    p.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    nested = p.parse_file(os.path.join(DATA_DIR, "Nested.xmi"))
    reg.resolve_references()
    return fcs, nested


# ---------------------------------------------------------------------- #
# Mensaje completo: NavMsg = NavBlock (48 bits)
#   speedField  bits  0..15  (16 b, twoComplement)
#   altField    bits 16..39  (24 b, twoComplement)  <- resuelto entre archivos
#   counterField bits 40..47 ( 8 b, inline)
# ---------------------------------------------------------------------- #
def test_encode_message_raw():
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    data = encode_message(msg, {
        "speedField": 0x1234, "altField": 0x0ABCDE, "counterField": 0x5A,
    })
    assert data == bytes.fromhex("12340ABCDE5A")


def test_decode_message_raw_round_trip():
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    values = {"speedField": 0x1234, "altField": 0x0ABCDE, "counterField": 0x5A}
    assert decode_message(msg, encode_message(msg, values)) == values


def test_twocomplement_negative():
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    data = encode_message(msg, {"speedField": -1, "altField": -2, "counterField": 0})
    assert data == bytes.fromhex("FFFFFFFFFE00")
    out = decode_message(msg, data)
    assert out["speedField"] == -1 and out["altField"] == -2


def test_engineering_units():
    """speedField con LinearScaling lsb=0.0625 kt: 10.0 kt -> raw 160."""
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    data = encode_message(
        msg, {"speedField": 10.0, "altField": 1000, "counterField": 7},
        engineering=True)
    assert data[0:2] == (160).to_bytes(2, "big")
    out = decode_message(msg, data, engineering=True)
    assert out["speedField"] == 10.0
    assert out["altField"] == 1000       # Altitude solo tiene unidades (sin transform)
    assert out["counterField"] == 7


def test_default_value_used_when_missing():
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    out = decode_message(msg, encode_message(msg, {}))
    assert out == {"speedField": 0, "altField": 0, "counterField": 0}


# ---------------------------------------------------------------------- #
# Escalado
# ---------------------------------------------------------------------- #
def test_enum_scaling_conversions():
    fcs, _ = _load()
    gear = fcs.find_one(ScalarType, "GearStatus")
    assert to_engineering(gear, 1) == "DOWN"
    assert from_engineering(gear, "TRANSIT") == 2
    try:
        to_engineering(gear, 9)
        assert False, "valor sin estado debe fallar"
    except CodecError:
        pass


def test_lut_scaling_conversions():
    _, nested = _load()
    temp = nested.find_one(ScalarType, "Temperature")
    assert to_engineering(temp, 50) == 25.0          # tramo 1: *0.5
    assert to_engineering(temp, 150) == 87.5         # tramo 2: *0.25 + 50
    assert from_engineering(temp, 25.0) == 50


# ---------------------------------------------------------------------- #
# Estructuras anidadas y padding declarado
# ---------------------------------------------------------------------- #
def test_nested_structure_and_padding():
    """OuterMsg (48 bits declarados): status 0..7, subBlock.innerTemp 16..31."""
    _, nested = _load()
    msg = nested.find_one(Message, "OuterMsg")
    data = encode_message(
        msg, {"status": 1, "subBlock": {"innerTemp": 0x1EEF}})
    assert data == bytes.fromhex("01001EEF0000")     # bits 8..15 y 32..47 padding
    out = decode_message(msg, data)
    assert out == {"status": 1, "subBlock": {"innerTemp": 0x1EEF}}


def test_nested_engineering_enum_inline():
    """El enum inline (OFF/ON) del campo status, en unidades de ingeniería."""
    _, nested = _load()
    msg = nested.find_one(Message, "OuterMsg")
    data = encode_message(msg, {"status": "ON", "subBlock": {"innerTemp": 25.0}},
                          engineering=True)
    assert data[0] == 1
    # 25.0 degC -> raw 50 (tramo 1 de la LUT: /0.5)
    assert data[2:4] == (50).to_bytes(2, "big")
    out = decode_message(msg, data, engineering=True)
    assert out["status"] == "ON"
    assert out["subBlock"]["innerTemp"] == 25.0      # round-trip en ingeniería


# ---------------------------------------------------------------------- #
# Variantes (campos condicionales)
# ---------------------------------------------------------------------- #
def test_variant_encode_decode_with_case():
    fcs, _ = _load()
    header = fcs.find_one(VariantType, "NavHeader")
    values = {"_case": "1",
              "value": {"speedField": 5, "altField": 6, "counterField": 7}}
    data = encode_type(header, values)
    out = decode_type(header, data, case="1")
    assert out["_case"] == "1"
    assert out["value"] == {"speedField": 5, "altField": 6, "counterField": 7}


def test_variant_unknown_case_fails():
    fcs, _ = _load()
    header = fcs.find_one(VariantType, "NavHeader")
    try:
        encode_type(header, {"_case": "99", "value": {}})
        assert False
    except CodecError as exc:
        assert "99" in str(exc)


# ---------------------------------------------------------------------- #
# Arrays variables (con modelo sintético controlado)
# ---------------------------------------------------------------------- #
def _synthetic_record_with_array(fcs):
    """head (16 b, bits 0..15) + wps: array variable desde el bit 16."""
    waypoints = fcs.find_one(ArrayType, "Waypoints")
    head_t = ScalarType(name="HeadT", bit_length=16, encoding="twoComplement")
    rec = RecordType(name="R")
    f_head = Field(name="head", position=BitPosition(max_position=15), inline=head_t)
    f_arr = Field(name="wps", position=BitPosition(word16=1), inline=waypoints)
    rec.fields = [f_head, f_arr]
    return rec


def test_variable_array_encode_decode():
    fcs, _ = _load()
    rec = _synthetic_record_with_array(fcs)
    values = {"head": 0x0AAA,
              "wps": [{"waypoint": 0x1111}, {"waypoint": 0x2222}]}
    data = encode_type(rec, values)
    # 16 b head + 8 b contador (=2) + 2 elementos de 16 b = 56 bits = 7 bytes
    assert data == bytes.fromhex("0AAA0211112222")
    assert decode_type(rec, data) == values


def test_array_counter_over_max_fails():
    fcs, _ = _load()
    rec = _synthetic_record_with_array(fcs)
    data = bytearray(encode_type(rec, {"head": 0, "wps": []}))
    data[2] = 200                                    # contador > max_count (32)
    try:
        decode_type(rec, bytes(data))
        assert False
    except CodecError as exc:
        assert "contador" in str(exc)


# ---------------------------------------------------------------------- #
# Texto y codificaciones especiales
# ---------------------------------------------------------------------- #
def test_text_field():
    fcs, _ = _load()
    callsign = fcs.find_one(TextType, "CallSign")     # 8 chars ASCII
    rec = RecordType(name="T")
    rec.fields = [Field(name="cs", position=BitPosition(max_position=63),
                        inline=callsign)]
    data = encode_type(rec, {"cs": "IBE32"})
    assert data == b"IBE32   "
    assert decode_type(rec, data) == {"cs": "IBE32"}


def test_bcd_and_ieee754():
    rec = RecordType(name="B")
    bcd = ScalarType(name="bcd", bit_length=16, encoding="BCD")
    flt = ScalarType(name="flt", bit_length=32, encoding="IEEE754")
    rec.fields = [
        Field(name="n", position=BitPosition(max_position=15), inline=bcd),
        Field(name="x", position=BitPosition(max_position=47), inline=flt),
    ]
    data = encode_type(rec, {"n": 1234, "x": 1.5})
    assert data == bytes.fromhex("1234" "3FC00000")
    out = decode_type(rec, data)
    assert out["n"] == 1234 and out["x"] == 1.5


# ---------------------------------------------------------------------- #
# Errores de definición
# ---------------------------------------------------------------------- #
def test_out_of_range_fails():
    fcs, _ = _load()
    msg = fcs.find_one(Message, "NavMsg")
    try:
        encode_message(msg, {"speedField": 40000})   # > 32767 en 16 b c2
        assert False
    except CodecError as exc:
        assert "fuera de rango" in str(exc)


def test_field_after_variable_array_rejected():
    fcs, _ = _load()
    rec = _synthetic_record_with_array(fcs)
    tail_t = ScalarType(name="TailT", bit_length=8, encoding="twoComplement")
    rec.fields.append(Field(name="tail", position=BitPosition(max_position=63),
                            inline=tail_t))
    try:
        encode_type(rec, {"head": 0, "wps": [], "tail": 0})
        assert False
    except CodecError as exc:
        assert "array variable" in str(exc)


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("\nTodos los tests pasan." if not failures else f"\n{failures} tests fallan.")
    return failures


if __name__ == "__main__":
    sys.exit(_run_all())
