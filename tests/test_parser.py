"""Tests del modelo de dominio, el parser y el registro.

Ejecutar con:  python3 tests/test_parser.py   (o python -m pytest tests/)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.model import (
    ArrayType, Bus, EnumScaling, Field, Folder, LinearScaling, Message,
    MessageSlot, Module, Network, Port, RecordType, ScalarType, TextType,
    VariantType,
)
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
# Módulo y organización
# ---------------------------------------------------------------------- #
def test_module_root():
    _, main, _, _ = load_all()
    assert isinstance(main, Module)
    assert main.name == "FCS_ICD"
    assert main.id == "_mod_fcs"
    assert main.national_export_control == "ES:DUAL"
    assert main.us_export_control == "NONE:null"
    # el archivo de origen se conserva para resolver referencias entre módulos
    assert main.source_file == "FCS_ICD.xmi"


def test_folder_hierarchy():
    _, main, _, _ = load_all()
    assert [f.name for f in main.folders] == ["Signals", "Messages"]
    nav = main.folders[1].folders[0]
    assert nav.name == "Navigation"
    # Los tipos y mensajes viven DENTRO de su carpeta
    assert main.types == [] and main.messages == []
    assert {t.name for t in nav.types} == {"NavBlock", "NavHeader"}
    assert [m.name for m in nav.messages] == ["NavMsg"]


# ---------------------------------------------------------------------- #
# Sistema de tipos
# ---------------------------------------------------------------------- #
def test_scalar_with_linear_scaling():
    _, main, _, _ = load_all()
    speed = main.find_one(ScalarType, "AirSpeed")
    assert speed.bit_length == 16
    assert speed.encoding == "twoComplement"
    assert isinstance(speed.scaling, LinearScaling)
    assert speed.scaling.lsb == 0.0625
    assert speed.units == "kt"
    assert speed.description == "Velocidad aerodinámica calibrada"


def test_scalar_with_enum_scaling():
    _, main, _, _ = load_all()
    gear = main.find_one(ScalarType, "GearStatus")
    assert isinstance(gear.scaling, EnumScaling)
    assert gear.scaling.labels == {"0": "UP", "1": "DOWN", "2": "TRANSIT"}


def test_real_nested_scaling_format():
    """Formato real EMF: <scal> sin tipo que envuelve <owns xsi:type="Scal:..">.
    Cubre lineal, enum y LUT (con <metaData> a ignorar)."""
    from core.model import LinearScaling, LUTScaling
    registry = ICDRegistry()
    parser = ICDParser(registry)
    nested = parser.parse_file(os.path.join(DATA_DIR, "Nested.xmi"))
    registry.resolve_references()

    temp = nested.find_one(ScalarType, "Temperature")
    assert isinstance(temp.scaling, LUTScaling)
    assert temp.scaling.units == "degC"
    assert len(temp.scaling.ranges) == 2                     # <metaData> ignorado
    assert temp.scaling.ranges[1].lsb == 0.25
    assert temp.scaling.ranges[1].offset == 50.0

    # enum definido inline dentro de un dataField > owns > scal > owns
    status = nested.find_one(ScalarType, "STATUS")
    assert isinstance(status.scaling, EnumScaling)
    assert status.scaling.labels == {"0": "OFF", "1": "ON"}


def test_text_type():
    _, main, _, _ = load_all()
    callsign = main.find_one(TextType, "CallSign")
    assert callsign.max_chars == 8
    assert callsign.length_mode == "fixed"
    assert callsign.encoding == "ASCII"


def test_variable_array():
    """Campos variables: array regido por contador transmitido."""
    _, main, _, _ = load_all()
    waypoints = main.find_one(ArrayType, "Waypoints")
    assert waypoints.counter_type == "uint8"
    assert waypoints.counter_bits == 8
    assert waypoints.max_count == 32
    # el elemento del array se describe con sus fields
    assert len(waypoints.fields) == 1
    assert waypoints.fields[0].datatype.name == "AirSpeed"


def test_record_fields_and_positions():
    _, main, _, _ = load_all()
    navblock = main.find_one(RecordType, "NavBlock")
    assert navblock.bit_length == 48
    assert [f.name for f in navblock.fields] == ["speedField", "altField", "counterField"]
    alt = navblock.fields[1]
    assert alt.position.word16 == 1
    assert alt.position.bit16 == 0


def test_inline_type_definition():
    """<owns> = tipo definido in situ dentro del campo."""
    _, main, _, _ = load_all()
    counter_field = main.find_one(Field, "counterField")
    inline = counter_field.inline
    assert isinstance(inline, ScalarType)
    assert inline.name == "frameCounter"
    assert inline.bit_length == 8
    assert counter_field.datatype is inline
    assert inline.parent is counter_field


def test_variant_conditional_fields():
    """Campos condicionales: VariantType multiplexa por discriminador."""
    _, main, _, _ = load_all()
    header = main.find_one(VariantType, "NavHeader")
    assert header.discriminator == "opcode"
    assert len(header.cases) == 1
    case = header.cases[0]
    assert case.condition == "1"
    assert case.is_conditional
    assert case.datatype.name == "NavBlock"


def test_message_payload():
    _, main, _, _ = load_all()
    msg = main.find_one(Message, "NavMsg")
    assert msg.period == 40
    assert msg.rate_mode == "periodic"
    assert msg.structure.name == "NavBlock"


# ---------------------------------------------------------------------- #
# Arquitectura de comunicaciones
# ---------------------------------------------------------------------- #
def test_network_ports_bus_slots():
    _, main, _, _ = load_all()
    assert len(main.networks) == 1
    net = main.networks[0]
    assert isinstance(net, Network)
    assert net.protocol == "UDP"
    assert net.alias == "LAN-A"

    port = net.ports[0]
    assert isinstance(port, Port)
    assert port.number == 5001
    assert port.ip_address == "10.0.0.1"
    assert port.role == "server"

    bus = net.buses[0]
    assert isinstance(bus, Bus)
    assert bus.coding == "ETH"
    assert bus.speed == "100"          # viene del <owns> interno
    assert bus.name == "mainBus"

    slot = bus.slots[0]
    assert isinstance(slot, MessageSlot)
    assert slot.period == 40
    assert slot.multicast_ip == "224.0.0.5"
    assert slot.max_peak_rate == 25
    assert slot.message.target.name == "NavMsg"


# ---------------------------------------------------------------------- #
# Registro y referencias
# ---------------------------------------------------------------------- #
def test_registry_indexing():
    registry, main, base, _ = load_all()
    assert registry.get("_sig_speed").name == "AirSpeed"
    assert registry.get("_sig_alt").name == "Altitude"
    assert registry.get_module("FCS_ICD") is main
    assert registry.get_module("BaseSignals") is base
    assert not registry.duplicate_ids


def test_resolve_local_reference():
    """with="_sig_speed" (atributo) se resuelve en el mismo archivo."""
    _, main, _, _ = load_all()
    speed_field = main.find_one(Field, "speedField")
    assert speed_field.ref.is_resolved
    assert speed_field.datatype.name == "AirSpeed"


def test_resolve_cross_file_reference():
    """<with href="BaseSignals.xmi#_sig_alt"/> cruza archivos."""
    _, main, base, _ = load_all()
    alt_field = main.find_one(Field, "altField")
    ref = alt_field.ref
    assert ref.file == "BaseSignals.xmi"
    assert ref.is_resolved
    assert ref.target.name == "Altitude"
    assert ref.target is base.find_one(name="Altitude")


def test_unresolved_reported():
    """El href a ExportRules.xmi (no cargado) aparece en el informe."""
    _, main, _, report = load_all()
    assert len(report.unresolved) == 1
    ref, reason = report.unresolved[0]
    assert ref.role == "explicitNational_EC"
    assert ref is main.explicit_national_ec
    assert "ExportRules.xmi" in reason
    # waypoint, speedField, altField, isMember, NavMsg y ranura del bus
    assert report.resolved == 6


# ---------------------------------------------------------------------- #
# Robustez y edición
# ---------------------------------------------------------------------- #
def test_unknown_attributes_ignored():
    """Los atributos sin significado de ingeniería (UniqueID...) se ignoran
    sin romper el parseo; el modelo queda limpio, sin restos del XML."""
    _, main, _, _ = load_all()
    speed = main.find_one(ScalarType, "AirSpeed")
    assert speed.bit_length == 16
    assert not hasattr(speed, "extra")
    assert not hasattr(speed, "source_type")


def test_edit_in_memory():
    """El modelo es pythónico: se edita con atributos normales."""
    _, main, _, _ = load_all()
    speed = main.find_one(ScalarType, "AirSpeed")
    speed.bit_length = 32
    speed.name = "AirSpeedCAS"
    assert main.find_one(ScalarType, "AirSpeedCAS").bit_length == 32


def test_message_describe():
    """describe() muestra el layout completo siguiendo referencias resueltas."""
    _, main, _, _ = load_all()
    out = main.find_one(Message, "NavMsg").describe()
    assert "Message 'NavMsg' — periodo=40, periodic" in out
    assert "RecordType 'NavBlock' — 3 campos, 48 bits" in out
    assert "[w16 0:0] speedField: AirSpeed — 16 bits, twoComplement, v=raw×0.0625 kt" in out
    assert "[w16 1:0] altField: Altitude — 24 bits" in out          # resuelta entre archivos
    assert "counterField (inline): frameCounter — 8 bits" in out    # tipo definido in situ


def test_paths_and_find():
    _, main, _, _ = load_all()
    navblock = main.find_one(RecordType, "NavBlock")
    assert navblock.path == "FCS_ICD/Messages/Navigation/NavBlock"
    # find por clase en todo el módulo: los 3 escalares del folder + inline
    scalars = main.find(ScalarType)
    assert {s.name for s in scalars} == {"AirSpeed", "GearStatus", "frameCounter"}


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
