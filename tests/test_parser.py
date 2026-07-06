"""Tests del parser, el modelo genérico y el registro.

Ejecutar con:  python -m pytest tests/  (o  python tests/test_parser.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.model import ICDNode
from core.parser import ICDParser
from core.registry import ICDRegistry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def load_all():
    registry = ICDRegistry()
    parser = ICDParser(registry)
    main = parser.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    base = parser.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    report = registry.resolve_references()
    return registry, main, base, report


# ---------------------------------------------------------------------- #
# Estructura del árbol
# ---------------------------------------------------------------------- #
def test_module_root():
    _, main, _, _ = load_all()
    assert main.kind == "Module"
    assert main.name == "FCS_ICD"
    assert main.id == "_mod_fcs"
    assert main.national_export_control == "ES:DUAL"
    # El namespace original queda disponible para el writer
    assert main.nsmap["Data"] == "http://www.ads.org/icdms/Data"
    assert main.nsmap["IP"] == "http://www.ads.org/icdms/IP"


def test_folder_hierarchy():
    _, main, _, _ = load_all()
    assert [f.name for f in main.folders] == ["Signals", "Messages"]
    messages = main.folders[1]
    nav = messages.folders[0]
    assert nav.name == "Navigation"
    # Los data quedan DENTRO de su carpeta, no colgando de la raíz
    assert main.data_elements == []
    assert {d.name for d in nav.data_elements} == {"NavBlock", "NavHeader", "NavMsg"}


def test_kinds_from_xsi_type():
    _, main, _, _ = load_all()
    signals = main.folders[0]
    kinds = {d.name: d.kind for d in signals.data_elements}
    assert kinds == {
        "AirSpeed": "Signal",
        "CallSign": "TextSignal",
        "Waypoints": "VariableArray",
    }
    nav = main.folders[1].folders[0]
    assert nav.find_one(name="NavBlock").kind == "Structure"
    assert nav.find_one(name="NavHeader").kind == "Header"
    assert nav.find_one(name="NavMsg").kind == "Message"


def test_all_attributes_preserved():
    """Ningún atributo del XML original puede perderse (requisito del writer)."""
    _, main, _, _ = load_all()
    speed = main.find_one(name="AirSpeed")
    assert speed.attrs == {
        "xsi:type": "Data:Signal",
        "id": "_sig_speed",
        "name": "AirSpeed",
        "length": "16",
        "coding": "twoComplement",
        "units": "kt",
        "security": "UNCLAS",
    }
    # Y las propiedades tipadas leen de ahí
    assert speed.length == 16
    assert speed.units == "kt"
    assert speed.coding == "twoComplement"


def test_layout_positions():
    _, main, _, _ = load_all()
    navblock = main.find_one(name="NavBlock")
    assert [f.name for f in navblock.fields] == ["speedField", "altField", "counterField"]
    alt_field = navblock.fields[1]
    assert alt_field.layout["w16"] == 1
    assert alt_field.layout["w16_b"] == 0


def test_owns_inline_containment():
    _, main, _, _ = load_all()
    counter_field = main.find_one(name="counterField")
    owned = counter_field.owns
    assert owned is not None
    assert owned.name == "frameCounter"
    assert owned.kind == "Signal"
    assert owned.length == 8
    # el owns cuelga del dataField en el árbol (parent correcto)
    assert owned.parent is counter_field


def test_header_members():
    _, main, _, _ = load_all()
    header = main.find_one(name="NavHeader")
    assert header.get("key") == "opcode"
    members = [f for f in header.fields if f.kind == "IsMember"]
    assert len(members) == 1
    assert members[0].key_selector == "1"


def test_network_and_ports():
    _, main, _, _ = load_all()
    assert len(main.networks) == 1
    net = main.networks[0]
    assert net.kind == "UDPNetwork"
    assert net.get("alias") == "LAN-A"
    port = net.find_one(kind="Port")
    assert port.get("ipAddress") == "10.0.0.1"
    assert port.get_int("port") == 5001
    bus = net.find_one(kind="Bus")
    assert bus.coding == "ETH"
    slot = bus.find_one(kind="MessageSlot")
    assert slot.period == 40
    assert slot.get("multicastIP") == "224.0.0.5"


def test_text_props():
    _, main, _, _ = load_all()
    assert main.text_props.get("NationalExportControl") == "ES:DUAL"


# ---------------------------------------------------------------------- #
# Registro y resolución de referencias
# ---------------------------------------------------------------------- #
def test_registry_indexing():
    registry, main, base, _ = load_all()
    assert registry.get("_sig_speed").name == "AirSpeed"
    assert registry.get("_sig_alt").name == "Altitude"
    assert registry.get_module("FCS_ICD") is main
    assert registry.get_module("BaseSignals") is base
    assert not registry.duplicate_ids


def test_resolve_local_attribute_ref():
    """with="_sig_speed" como atributo se resuelve dentro del mismo archivo."""
    _, main, _, _ = load_all()
    speed_field = main.find_one(name="speedField")
    ref = speed_field.with_ref
    assert ref is not None and ref.is_resolved
    assert ref.target.name == "AirSpeed"


def test_resolve_cross_file_href():
    """<with href="BaseSignals.xmi#_sig_alt"/> cruza archivos."""
    _, main, _, _ = load_all()
    alt_field = main.find_one(name="altField")
    ref = alt_field.with_ref
    assert ref is not None and ref.is_resolved
    assert ref.target.name == "Altitude"
    assert ref.target.source_file == "BaseSignals.xmi"
    assert ref.xsi_type == "Data:Signal"


def test_resolve_chain_message_to_struct():
    """NavMsg -> NavBlock y la ranura del bus -> NavMsg."""
    _, main, _, _ = load_all()
    msg = main.find_one(name="NavMsg")
    assert msg.with_ref.target.name == "NavBlock"
    slot = main.find_one(kind="MessageSlot")
    assert slot.with_ref.target is msg


def test_unresolved_reported_not_silent():
    """El href a ExportRules.xmi (no cargado) debe aparecer en el informe."""
    _, _, _, report = load_all()
    assert len(report.unresolved) == 1
    ref, reason = report.unresolved[0]
    assert ref.tag == "explicitNational_EC"
    assert "ExportRules.xmi" in reason
    # speedField, altField, isMember, NavMsg y la ranura del bus
    assert report.resolved == 5


# ---------------------------------------------------------------------- #
# Utilidades del modelo
# ---------------------------------------------------------------------- #
def test_find_and_path():
    _, main, _, _ = load_all()
    all_signals = main.find(kind="Signal")  # AirSpeed + frameCounter (inline)
    assert {s.name for s in all_signals} == {"AirSpeed", "frameCounter"}
    navblock = main.find_one(name="NavBlock")
    assert navblock.path == "FCS_ICD/Messages/Navigation/NavBlock"


def test_edit_in_memory():
    """El modelo es editable: cambiar un atributo se refleja en attrs (writer-ready)."""
    _, main, _, _ = load_all()
    speed = main.find_one(name="AirSpeed")
    speed.set("length", 32)
    assert speed.length == 32
    assert speed.attrs["length"] == "32"
    speed.name = "AirSpeedCAS"
    assert speed.attrs["name"] == "AirSpeedCAS"


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
