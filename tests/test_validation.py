"""Tests del validador de modelos ICD.

Ejecutar con:  python3 tests/test_validation.py   (o python -m pytest tests/)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.parser import ICDParser
from core.registry import ICDRegistry
from core.validation import ERROR, WARNING, summarize, validate_module

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load(*files):
    registry = ICDRegistry()
    parser = ICDParser(registry)
    modules = [parser.parse_file(os.path.join(DATA_DIR, f)) for f in files]
    registry.resolve_references()
    return modules


def _messages(issues, level=None):
    return [i.message for i in issues if level is None or i.level == level]


# ---------------------------------------------------------------------- #
def test_valid_module_only_reports_missing_file():
    """El módulo de ejemplo solo tiene un problema real: el archivo de
    reglas de exportación no cargado."""
    main, _base = _load("FCS_ICD.xmi", "BaseSignals.xmi")
    issues = validate_module(main)
    assert len(issues) == 1
    assert issues[0].level == ERROR
    assert "ExportRules.xmi" in issues[0].message
    assert issues[0].path == "FCS_ICD"


def test_missing_mandatory_fields():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    errors = _messages(issues, ERROR)
    warnings = _messages(issues, WARNING)

    assert any("sin longitud en bits" in m for m in errors)          # NoLength
    assert any("sin codificación" in m for m in warnings)            # NoCoding
    assert any("sin contador" in m for m in errors)                  # NoCounter
    assert any("sin longitud máxima" in m for m in warnings)         # NoCounter
    assert any("sin definición del elemento" in m for m in warnings) # NoCounter


def test_duplicate_ids_detected():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    assert any("id duplicado" in m and "_s_nocod" in m for m in _messages(issues, ERROR))


def test_field_problems():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    assert any("campo vacío" in m for m in _messages(issues, ERROR))          # emptyField
    assert any("campos solapados" in m and "0:0" in m
               for m in _messages(issues, WARNING))                           # a y b


def test_variant_problems():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    errors = _messages(issues, ERROR)
    assert any("sin campo discriminador" in m for m in errors)                # NoKey
    assert any("condición duplicada '1'" in m for m in errors)                # _m_1/_m_2


def test_message_problems():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    assert any("mensaje sin payload" in m for m in _messages(issues, ERROR))  # NoPayload
    assert any("periódico sin periodo" in m for m in _messages(issues, WARNING))
    assert any("sin resolver" in m and "_missing_id" in m
               for m in _messages(issues, ERROR))                             # BadRef


def test_empty_network_warned():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    assert any("red sin puertos ni buses" in m for m in _messages(issues, WARNING))


def test_issue_paths_point_to_entity():
    """Cada Issue lleva la ruta de la entidad afectada, lista para la UI."""
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    by_path = {i.path for i in issues}
    assert "Broken_ICD/Bad/NoLength" in by_path
    assert "Broken_ICD/Bad/NoKey" in by_path
    assert "Broken_ICD/Bad/Overlap/emptyField" in by_path


def test_summary_output():
    (broken,) = _load("Broken.xmi")
    issues = validate_module(broken)
    text = summarize(issues)
    assert "errores" in text and "avisos" in text
    assert "Validación correcta" in summarize([])


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
