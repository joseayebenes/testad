"""Tests de la lógica de edición de WorkSession (crear/borrar/referencias)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.model import (
    ArrayType, Field, Folder, Message, RecordType, ScalarType, VariantType,
)
from web.session import WorkSession

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _session():
    s = WorkSession()
    s.load_folder(DATA_DIR)
    return s


# ---------------------------------------------------------------------- #
# Crear
# ---------------------------------------------------------------------- #
def test_allowed_children():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    signals = fcs.folders[0]
    assert "Señal (ScalarType)" in s.allowed_children(signals)
    navblock = fcs.find_one(RecordType, "NavBlock")
    assert set(s.allowed_children(navblock)) == {"Campo (Field)"}
    assert s.allowed_children(fcs.find_one(ScalarType, "AirSpeed")) == {}


def test_add_signal_to_folder():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    signals = fcs.folders[0]
    n_before = len(signals.types)
    new = s.add_child(signals, ScalarType)
    assert isinstance(new, ScalarType)
    assert new in signals.types and len(signals.types) == n_before + 1
    assert new.parent is signals
    assert new.id and s.get(new.id) is new           # indexada
    assert s.registry.get(new.id) is new             # y en el registro
    assert s.dirty


def test_add_field_and_reference_it():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    navblock = fcs.find_one(RecordType, "NavBlock")
    airspeed = fcs.find_one(ScalarType, "AirSpeed")
    field = s.add_child(navblock, Field)
    assert field in navblock.fields
    s.set_reference(field, "ref", airspeed)
    assert field.ref.is_resolved and field.ref.target is airspeed
    assert field.ref.file == ""                       # misma módulo -> local


def test_new_ids_are_unique():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    a = s.add_child(fcs, Folder)
    b = s.add_child(fcs, Folder)
    assert a.id != b.id


# ---------------------------------------------------------------------- #
# Borrar
# ---------------------------------------------------------------------- #
def test_delete_removes_and_reports_dangling():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    airspeed = fcs.find_one(ScalarType, "AirSpeed")
    # speedField y el campo del array referencian AirSpeed
    referrers_before = s.referrers(airspeed)
    assert len(referrers_before) >= 1

    dangling = s.delete(airspeed)
    assert airspeed not in fcs.folders[0].types
    assert s.get(airspeed.id) is None
    assert s.registry.get(airspeed.id) is None
    # las referencias que apuntaban a AirSpeed quedan sin resolver y avisadas
    assert {id(r) for r in dangling} == {id(r) for r in referrers_before}
    assert all(not r.is_resolved for r in dangling)
    # y la validación lo refleja
    assert any("sin resolver" in i.message for i in s.issues)


def test_cannot_delete_module():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    try:
        s.delete(fcs)
        assert False, "debería impedir borrar el módulo raíz"
    except ValueError:
        pass


def test_delete_then_revalidate_counts():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    gear = fcs.find_one(ScalarType, "GearStatus")   # nadie lo referencia
    assert s.referrers(gear) == []
    s.delete(gear)
    assert s.get(gear.id) is None


# ---------------------------------------------------------------------- #
# Referencias
# ---------------------------------------------------------------------- #
def test_reference_slots():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    msg = fcs.find_one(Message, "NavMsg")
    slots = s.reference_slots(msg)
    assert [a for a, _, _ in slots] == ["payload"]


def test_change_message_payload():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    msg = fcs.find_one(Message, "NavMsg")
    header = fcs.find_one(VariantType, "NavHeader")
    s.set_reference(msg, "payload", header)
    assert msg.payload.target is header
    assert msg.structure is header


def test_clear_reference():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    msg = fcs.find_one(Message, "NavMsg")
    s.clear_reference(msg, "payload")
    assert msg.payload is None
    # ahora el mensaje no tiene payload -> error de validación
    assert any("sin payload" in i.message and i.path == msg.path for i in s.issues)


def test_cross_module_reference_sets_file():
    s = _session()
    fcs = s.registry.get_module("FCS_ICD")
    base = s.registry.get_module("BaseSignals")
    altitude = base.find_one(ScalarType, "Altitude")
    field = fcs.find_one(Field, "speedField")
    s.set_reference(field, "ref", altitude)
    assert field.ref.target is altitude
    assert field.ref.file == "BaseSignals.xmi"        # referencia entre archivos


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
