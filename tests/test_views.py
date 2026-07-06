"""Tests de la lógica de los visores de mensaje y de tipo."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.model import ArrayType, Message, ScalarType, VariantType
from core.parser import ICDParser
from core.registry import ICDRegistry
from web.views import flatten, message_rows, scaling_detail, type_view

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load():
    reg = ICDRegistry()
    p = ICDParser(reg)
    main = p.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    p.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    reg.resolve_references()
    return main


# ---------------------------------------------------------------------- #
# Visor de mensaje
# ---------------------------------------------------------------------- #
def test_message_rows_flattens_structure():
    main = _load()
    rows = message_rows(main.find_one(Message, "NavMsg"))
    assert [r.name for r in rows] == ["speedField", "altField", "counterField"]
    speed, alt, counter = rows

    assert speed.position == "15"          # max_position
    assert speed.length == "16"            # longitud en bits
    assert "AirSpeed" in speed.type_name
    assert speed.coding == "twoComplement"
    assert "× 0.0625" in speed.scaling and "kt" in speed.scaling
    assert speed.ref_id == "_sig_speed"    # enlace a la señal referenciada

    # referencia entre archivos resuelta
    assert "Altitude" in alt.type_name
    assert alt.length == "24"
    assert alt.ref_id == "_sig_alt"

    # tipo definido inline (sin ref_id, va inline)
    assert "frameCounter" in counter.type_name
    assert counter.note == "inline"
    assert counter.ref_id == ""


def test_message_rows_recurses_into_nested_structures():
    """Un campo cuyo tipo es un registro debe expandir sus subcampos."""
    main = _load()
    # NavHeader es una variante cuyo caso apunta a NavBlock (compuesto)
    header = main.find_one(VariantType, "NavHeader")
    rows = flatten(header)
    # primera fila: el miembro de la variante; luego, sangrados, los 3 de NavBlock
    assert rows[0].level == 0
    assert rows[0].has_children          # el miembro apunta a un compuesto
    sub = [r for r in rows if r.level == 1]
    assert {r.name for r in sub} == {"speedField", "altField", "counterField"}
    assert rows[0].condition == "1"
    # las filas hijas cuelgan de la clave del padre
    assert all(r.parent_key == rows[0].key for r in sub)


# ---------------------------------------------------------------------- #
# Visor de tipo
# ---------------------------------------------------------------------- #
def test_type_view_scalar_linear():
    main = _load()
    view = type_view(main.find_one(ScalarType, "AirSpeed"))
    assert view["kind"] == "ScalarType"
    props = {p["prop"]: p["valor"] for p in view["props"]}
    assert props["Longitud (bits)"] == "16"
    assert props["Codificación"] == "twoComplement"
    assert view["scaling"]["kind"] == "linear"
    assert view["scaling"]["lsb"] == 0.0625


def test_type_view_scalar_enum():
    main = _load()
    view = type_view(main.find_one(ScalarType, "GearStatus"))
    sc = view["scaling"]
    assert sc["kind"] == "enum"
    labels = {r["valor"]: r["estado"] for r in sc["labels"]}
    assert labels == {"0": "UP", "1": "DOWN", "2": "TRANSIT"}


def test_type_view_array_shows_counter_and_fields():
    main = _load()
    view = type_view(main.find_one(ArrayType, "Waypoints"))
    props = {p["prop"]: p["valor"] for p in view["props"]}
    assert props["Tipo de contador"] == "uint8"
    assert props["Máx. elementos"] == "32"
    assert len(view["fields"]) == 1        # el elemento del array


def test_scaling_detail_lut():
    from core.model import LUTRange, LUTScaling
    s = LUTScaling(units="deg", ranges=[LUTRange(0, 10, 0.5, 0), LUTRange(10, 20, 0.25, 5)])
    info = scaling_detail(s)
    assert info["kind"] == "lut"
    assert len(info["ranges"]) == 2
    assert info["ranges"][1]["lsb"] == 0.25


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
    print("\nTodos los tests pasan." if not failures else f"\n{failures} tests fallan.")
    return failures


if __name__ == "__main__":
    sys.exit(_run_all())
