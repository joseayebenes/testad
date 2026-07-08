"""Tests del generador de código Python (verificado contra el codec-oráculo).

Ejecutar con:  python3 tests/test_codegen.py   (o python -m pytest tests/)
"""

import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.codec import encode_message
from core.codegen.generator import CodegenError, generate
from core.model import Message
from core.parser import ICDParser
from core.registry import ICDRegistry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load():
    reg = ICDRegistry()
    p = ICDParser(reg)
    fcs = p.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    base = p.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    nested = p.parse_file(os.path.join(DATA_DIR, "Nested.xmi"))
    broken = p.parse_file(os.path.join(DATA_DIR, "Broken.xmi"))
    reg.resolve_references()
    return reg, fcs, base, nested, broken


def _generate_all(tmp):
    _, fcs, base, nested, _ = _load()
    results = [generate(m, tmp) for m in (fcs, base, nested)]
    return results


def _importer(tmp):
    """Prepara un entorno de import limpio para 'tmp' y devuelve el importador.

    Limpia los paquetes generados UNA sola vez por entorno: si se limpiara en
    cada import, dos módulos que comparten una clase (nav_msg y nav_block)
    acabarían con clases distintas y el __eq__ de dataclass fallaría.
    """
    for name in list(sys.modules):
        if name.split(".")[0] in ("fcs_icd", "base_signals", "nested_icd",
                                  "icd_runtime"):
            del sys.modules[name]
    sys.path.insert(0, tmp)
    importlib.invalidate_caches()

    def _import(module_name):
        return importlib.import_module(module_name)
    return _import


# ---------------------------------------------------------------------- #
def test_generated_encode_matches_oracle():
    """El encode del código generado produce los mismos bytes que el codec."""
    _, fcs, _, _, _ = _load()
    values = {"speedField": 0x1234, "altField": 0x0ABCDE, "counterField": 0x5A}
    oracle = encode_message(fcs.find_one(Message, "NavMsg"), values)

    with tempfile.TemporaryDirectory() as tmp:
        _generate_all(tmp)
        imp = _importer(tmp)
        nav_msg = imp("fcs_icd.nav_msg")
        nav_block = imp("fcs_icd.nav_block")
        msg = nav_block.NavBlock(speed_field=0x1234, alt_field=0x0ABCDE,
                                 counter_field=0x5A)
        assert nav_msg.encode(msg) == oracle
        assert nav_msg.decode(oracle) == msg           # round-trip


def test_nested_composite_and_padding():
    """OuterMsg: registro anidado (import entre ficheros) + padding a 48 bits."""
    _, _, _, nested_icd, _ = _load()
    oracle = encode_message(nested_icd.find_one(Message, "OuterMsg"),
                            {"status": 1, "subBlock": {"innerTemp": 0x1EEF}})
    with tempfile.TemporaryDirectory() as tmp:
        _generate_all(tmp)
        imp = _importer(tmp)
        outer_msg = imp("nested_icd.outer_msg")
        outer = imp("nested_icd.outer_block")
        inner = imp("nested_icd.inner_block")
        msg = outer.OuterBlock(status=1,
                               sub_block=inner.InnerBlock(inner_temp=0x1EEF))
        data = outer_msg.encode(msg)
        assert data == oracle == bytes.fromhex("01001EEF0000")
        assert outer_msg.decode(data) == msg


def test_scaling_helpers_in_types():
    """types.py: lineal, enum y LUT con conversiones correctas."""
    with tempfile.TemporaryDirectory() as tmp:
        _generate_all(tmp)
        imp = _importer(tmp)
        fcs_types = imp("fcs_icd.types")
        assert fcs_types.AirSpeed.to_eng(160) == 10.0
        assert fcs_types.AirSpeed.from_eng(10.0) == 160
        assert fcs_types.GearStatus.to_eng(1) == "DOWN"
        assert fcs_types.GearStatus.from_eng("TRANSIT") == 2
        assert fcs_types.CallSign.CHARS == 8

        nested_types = imp("nested_icd.types")
        assert nested_types.Temperature.to_eng(150) == 87.5   # LUT tramo 2
        assert nested_types.Temperature.from_eng(25.0) == 50


def test_refuses_invalid_module():
    """Un módulo con errores de validación no genera (norma 2.5)."""
    _, _, _, _, broken = _load()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            generate(broken, tmp)
            assert False, "debió rechazar Broken_ICD"
        except CodegenError as exc:
            assert "sin longitud en bits" in str(exc)
        assert not os.listdir(tmp)                     # no escribió nada


def test_unsupported_reported_as_warnings():
    """Variantes y arrays se omiten con aviso (no en silencio)."""
    with tempfile.TemporaryDirectory() as tmp:
        results = _generate_all(tmp)
        fcs_result = next(r for r in results if r.package == "fcs_icd")
        text = "\n".join(fcs_result.warnings)
        assert "Waypoints" in text and "NavHeader" in text


def test_deterministic_output():
    """Misma entrada -> misma salida byte a byte (norma 2.3)."""
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        r1 = {p: c for r in _generate_all(t1) for p, c in r.files.items()}
        r2 = {p: c for r in _generate_all(t2) for p, c in r.files.items()}
        assert r1 == r2


def test_headers_and_traceability():
    """Cabecera de 'no editar' y trazabilidad xmi:id en cada fichero (2.2/2.4)."""
    with tempfile.TemporaryDirectory() as tmp:
        _generate_all(tmp)
        nav_block = open(os.path.join(tmp, "fcs_icd", "nav_block.py")).read()
        assert "NO EDITAR A MANO" in nav_block
        assert "[_st_navblock]" in nav_block           # xmi:id trazable
        assert "FCS_ICD/Messages/Navigation/NavBlock" in nav_block
        # determinismo: sin marcas de tiempo
        import re
        assert not re.search(r"\d{4}-\d{2}-\d{2}", nav_block)


def test_layer_dependencies():
    """Norma 2.1: los mensajes importan compuestos, nunca al revés."""
    with tempfile.TemporaryDirectory() as tmp:
        _generate_all(tmp)
        nav_block = open(os.path.join(tmp, "fcs_icd", "nav_block.py")).read()
        nav_msg = open(os.path.join(tmp, "fcs_icd", "nav_msg.py")).read()
        assert "from fcs_icd.nav_block import NavBlock" in nav_msg    # L3 -> L2
        assert "nav_msg" not in nav_block                             # L2 -/-> L3
        types_py = open(os.path.join(tmp, "fcs_icd", "types.py")).read()
        imports = [l for l in types_py.splitlines()
                   if l.startswith(("import ", "from ")) and "__future__" not in l]
        assert imports == []                                          # L1 sin deps


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
