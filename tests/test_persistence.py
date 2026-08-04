"""Tests de la persistencia JSON (guardar/cargar el modelo).

Ejecutar con:  python3 tests/test_persistence.py   (o python -m pytest tests/)
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.model import ArrayType, EnumScaling, Message, ScalarType, VariantType
from core.parser import ICDParser
from core.persistence import load_module, module_to_dict, save_module
from core.registry import ICDRegistry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _parse_originals():
    registry = ICDRegistry()
    parser = ICDParser(registry)
    main = parser.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    base = parser.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    registry.resolve_references()
    return registry, main, base


def _round_trip(module, tmpdir, name):
    path = os.path.join(tmpdir, name)
    save_module(module, path)
    return load_module(path)


# ---------------------------------------------------------------------- #
def test_round_trip_preserves_everything():
    """XML -> modelo -> JSON -> modelo': ambos producen el mismo dict."""
    _, main, base = _parse_originals()
    with tempfile.TemporaryDirectory() as tmp:
        main2 = _round_trip(main, tmp, "main.json")
        base2 = _round_trip(base, tmp, "base.json")
    assert module_to_dict(main2) == module_to_dict(main)
    assert module_to_dict(base2) == module_to_dict(base)


def test_references_resolve_after_load():
    """Las referencias guardadas por id se re-enlazan tras cargar el JSON."""
    _, main, base = _parse_originals()
    with tempfile.TemporaryDirectory() as tmp:
        save_module(main, os.path.join(tmp, "main.json"))
        save_module(base, os.path.join(tmp, "base.json"))

        registry = ICDRegistry()
        main2 = load_module(os.path.join(tmp, "main.json"), registry)
        load_module(os.path.join(tmp, "base.json"), registry)
        report = registry.resolve_references()

    # misma situación que con los XMI: todo resuelto salvo ExportRules
    assert report.resolved == 6
    assert len(report.unresolved) == 1

    alt_field = main2.find_one(name="altField")
    assert alt_field.ref.target.name == "Altitude"
    msg = main2.find_one(Message, "NavMsg")
    assert msg.structure.name == "NavBlock"


def test_describe_identical_after_reload():
    """La ficha de un mensaje es idéntica tras pasar por JSON."""
    _, main, base = _parse_originals()
    expected = main.find_one(Message, "NavMsg").describe()

    with tempfile.TemporaryDirectory() as tmp:
        save_module(main, os.path.join(tmp, "main.json"))
        save_module(base, os.path.join(tmp, "base.json"))
        registry = ICDRegistry()
        main2 = load_module(os.path.join(tmp, "main.json"), registry)
        load_module(os.path.join(tmp, "base.json"), registry)
        registry.resolve_references()

    assert main2.find_one(Message, "NavMsg").describe() == expected


def test_typed_content_survives():
    _, main, _ = _parse_originals()
    with tempfile.TemporaryDirectory() as tmp:
        main2 = _round_trip(main, tmp, "main.json")

    gear = main2.find_one(ScalarType, "GearStatus")
    assert isinstance(gear.scaling, EnumScaling)
    assert gear.scaling.labels["1"] == "DOWN"

    waypoints = main2.find_one(ArrayType, "Waypoints")
    assert waypoints.counter_type == "uint8" and waypoints.max_count == 32

    header = main2.find_one(VariantType, "NavHeader")
    assert header.discriminator == "opcode"
    assert header.cases[0].condition == "1"

    net = main2.networks[0]
    assert net.protocol == "UDP" and net.buses[0].slots[0].multicast_ip == "224.0.0.5"

    # los parent quedan bien enlazados al cargar
    navblock = main2.find_one(name="NavBlock")
    assert navblock.path == "FCS_ICD/Messages/Navigation/NavBlock"


def test_json_is_clean():
    """El JSON no arrastra valores por defecto ni nulos."""
    _, main, _ = _parse_originals()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "main.json")
        save_module(main, path)
        text = open(path, encoding="utf-8").read()
        data = json.loads(text)

    assert ": null" not in text          # ningún valor JSON nulo
    assert '"parent"' not in text          # el back-pointer no se serializa
    assert ': ""' not in text and ": 0," not in text   # ni cadenas/enteros por defecto
    assert data["format"] == "icdms-module" and data["version"] == 1


def test_edit_then_save_reload():
    """Flujo completo: cargar XML, editar, guardar JSON, recargar."""
    _, main, _ = _parse_originals()
    speed = main.find_one(ScalarType, "AirSpeed")
    speed.bit_length = 32
    speed.scaling.lsb = 0.125

    with tempfile.TemporaryDirectory() as tmp:
        main2 = _round_trip(main, tmp, "edited.json")

    speed2 = main2.find_one(ScalarType, "AirSpeed")
    assert speed2.bit_length == 32
    assert speed2.scaling.lsb == 0.125


def test_invalid_file_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "other.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": "otra-cosa", "module": {}}, f)
        try:
            load_module(path)
            assert False, "debería haber lanzado ValueError"
        except ValueError as exc:
            assert "icdms-module" in str(exc)


def _run_all():
    failures = 0
    for fn_name, fn in sorted(globals().items()):
        if fn_name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK   {fn_name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {fn_name}: {exc}")
    print("\nTodos los tests pasan." if not failures else f"\n{failures} tests fallan.")
    return failures


if __name__ == "__main__":
    sys.exit(_run_all())
