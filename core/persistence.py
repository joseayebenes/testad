"""Persistencia JSON del modelo ICD.

Formato propio de la herramienta: un módulo por archivo, con esta forma:

    {
      "format": "icdms-module",
      "version": 1,
      "module": { "class": "Module", "name": ..., ... }
    }

Principios:

* JSON limpio: solo se escriben los valores con contenido (nada de campos
  vacíos ni ceros por defecto), para que el archivo sea legible y diffeable.
* Cada entidad lleva un discriminador ``class`` con el nombre de su clase.
* Las referencias se guardan por ``target_id`` (+ ``file`` si cruzan
  archivos) y se re-enlazan tras la carga con
  ``ICDRegistry.resolve_references()``, igual que al parsear XML.
* ``Module.source_file`` se conserva: es la clave con la que otras
  referencias externas localizan este módulo.

Uso:
    save_module(module, "FCS_ICD.json")
    module = load_module("FCS_ICD.json", registry)
    registry.resolve_references()
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.model import (
    ArrayType, BitPosition, Bus, Entity, EnumScaling, Field, Folder,
    LinearScaling, LUTRange, LUTScaling, Message, MessageSlot, Module,
    Network, Port, RecordType, Reference, ScalarType, Scaling, TextType,
    TypeDef, VariantType,
)

logger = logging.getLogger("ICDPersistence")

FORMAT_NAME = "icdms-module"
FORMAT_VERSION = 1


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _put(d: Dict[str, Any], key: str, value: Any) -> None:
    """Escribe solo valores con contenido (omite '', 0, [], {}, None)."""
    if value:
        d[key] = value


def _common(e: Entity) -> Dict[str, Any]:
    d: Dict[str, Any] = {"class": type(e).__name__}
    _put(d, "id", e.id)
    _put(d, "name", e.name)
    _put(d, "security", e.security)
    _put(d, "remarks", e.remarks)
    _put(d, "description", e.description)
    return d


def _load_common(e: Entity, d: Dict[str, Any], parent: Optional[Entity]) -> None:
    e.id = d.get("id", "")
    e.name = d.get("name", "")
    e.security = d.get("security", "")
    e.remarks = d.get("remarks", "")
    e.description = d.get("description", "")
    e.parent = parent


# ---------------------------------------------------------------------- #
# Referencias, posición y escalado
# ---------------------------------------------------------------------- #
def _dump_ref(r: Reference) -> Dict[str, Any]:
    d: Dict[str, Any] = {"target_id": r.target_id}
    _put(d, "file", r.file)
    if r.role != "with":
        d["role"] = r.role
    return d


def _load_ref(d: Dict[str, Any], owner: Entity) -> Reference:
    return Reference(
        target_id=d.get("target_id", ""),
        file=d.get("file", ""),
        role=d.get("role", "with"),
        owner=owner,
    )


def _dump_position(p: BitPosition) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    _put(d, "word16", p.word16)
    _put(d, "bit16", p.bit16)
    _put(d, "word12", p.word12)
    _put(d, "bit12", p.bit12)
    _put(d, "max_position", p.max_position)
    _put(d, "pre_padding", p.pre_padding)
    return d


def _load_position(d: Dict[str, Any]) -> BitPosition:
    return BitPosition(
        word16=d.get("word16", 0),
        bit16=d.get("bit16", 0),
        word12=d.get("word12", 0),
        bit12=d.get("bit12", 0),
        max_position=d.get("max_position", 0),
        pre_padding=d.get("pre_padding", 0),
    )


def _dump_scaling(s: Scaling) -> Dict[str, Any]:
    if isinstance(s, LinearScaling):
        d: Dict[str, Any] = {"kind": "linear", "lsb": s.lsb}
        _put(d, "offset", s.offset)
    elif isinstance(s, EnumScaling):
        d = {"kind": "enum", "labels": dict(s.labels)}
    elif isinstance(s, LUTScaling):
        d = {
            "kind": "lut",
            "ranges": [
                {"begin": r.begin, "end": r.end, "lsb": r.lsb, "offset": r.offset}
                for r in s.ranges
            ],
        }
    else:
        d = {"kind": "raw"}
    _put(d, "units", s.units)
    return d


def _load_scaling(d: Dict[str, Any]) -> Scaling:
    kind = d.get("kind", "raw")
    units = d.get("units", "")
    if kind == "linear":
        return LinearScaling(units=units, lsb=d.get("lsb", 0.0), offset=d.get("offset", 0.0))
    if kind == "enum":
        return EnumScaling(units=units, labels=dict(d.get("labels", {})))
    if kind == "lut":
        return LUTScaling(units=units, ranges=[
            LUTRange(
                begin=r.get("begin", 0.0), end=r.get("end", 0.0),
                lsb=r.get("lsb", 0.0), offset=r.get("offset", 0.0),
            )
            for r in d.get("ranges", [])
        ])
    return Scaling(units=units)


# ---------------------------------------------------------------------- #
# Sistema de tipos
# ---------------------------------------------------------------------- #
def _dump_type(t: TypeDef) -> Dict[str, Any]:
    d = _common(t)
    if isinstance(t, ScalarType):
        _put(d, "bit_length", t.bit_length)
        _put(d, "encoding", t.encoding)
        _put(d, "default_value", t.default_value)
        if t.scaling is not None:
            d["scaling"] = _dump_scaling(t.scaling)
    elif isinstance(t, TextType):
        _put(d, "max_chars", t.max_chars)
        _put(d, "length_mode", t.length_mode)
        _put(d, "encoding", t.encoding)
        _put(d, "bit_endianness", t.bit_endianness)
    elif isinstance(t, ArrayType):
        _put(d, "counter_type", t.counter_type)
        _put(d, "counter_bits", t.counter_bits)
        _put(d, "max_count", t.max_count)
        _put(d, "fields", [_dump_field(f) for f in t.fields])
    elif isinstance(t, VariantType):
        _put(d, "discriminator", t.discriminator)
        _put(d, "fields", [_dump_field(f) for f in t.fields])
    elif isinstance(t, RecordType):
        _put(d, "bit_length", t.bit_length)
        _put(d, "fields", [_dump_field(f) for f in t.fields])
    return d


def _dump_field(f: Field) -> Dict[str, Any]:
    d = _common(f)
    _put(d, "condition", f.condition)
    _put(d, "position", _dump_position(f.position))
    if f.ref is not None:
        d["ref"] = _dump_ref(f.ref)
    if f.inline is not None:
        d["inline"] = _dump_type(f.inline)
    return d


TYPE_CLASSES = {
    "TypeDef": TypeDef,
    "ScalarType": ScalarType,
    "TextType": TextType,
    "RecordType": RecordType,
    "ArrayType": ArrayType,
    "VariantType": VariantType,
}


def _load_type(d: Dict[str, Any], parent: Optional[Entity]) -> TypeDef:
    cls = TYPE_CLASSES.get(d.get("class", ""))
    if cls is None:
        raise ValueError(f"Clase de tipo desconocida en JSON: '{d.get('class')}'")
    t = cls()
    _load_common(t, d, parent)

    if isinstance(t, ScalarType):
        t.bit_length = d.get("bit_length", 0)
        t.encoding = d.get("encoding", "")
        t.default_value = d.get("default_value", "")
        if "scaling" in d:
            t.scaling = _load_scaling(d["scaling"])
    elif isinstance(t, TextType):
        t.max_chars = d.get("max_chars", 0)
        t.length_mode = d.get("length_mode", "")
        t.encoding = d.get("encoding", "")
        t.bit_endianness = d.get("bit_endianness", "")
    elif isinstance(t, ArrayType):
        t.counter_type = d.get("counter_type", "")
        t.counter_bits = d.get("counter_bits", 0)
        t.max_count = d.get("max_count", 0)
        t.fields = [_load_field(fd, t) for fd in d.get("fields", [])]
    elif isinstance(t, VariantType):
        t.discriminator = d.get("discriminator", "")
        t.fields = [_load_field(fd, t) for fd in d.get("fields", [])]
    elif isinstance(t, RecordType):
        t.bit_length = d.get("bit_length", 0)
        t.fields = [_load_field(fd, t) for fd in d.get("fields", [])]
    return t


def _load_field(d: Dict[str, Any], parent: Entity) -> Field:
    f = Field()
    _load_common(f, d, parent)
    f.condition = d.get("condition", "")
    f.position = _load_position(d.get("position", {}))
    if "ref" in d:
        f.ref = _load_ref(d["ref"], f)
    if "inline" in d:
        f.inline = _load_type(d["inline"], f)
    return f


# ---------------------------------------------------------------------- #
# Transmisión
# ---------------------------------------------------------------------- #
def _dump_message(m: Message) -> Dict[str, Any]:
    d = _common(m)
    _put(d, "period", m.period)
    _put(d, "rate_mode", m.rate_mode)
    if m.payload is not None:
        d["payload"] = _dump_ref(m.payload)
    if m.body is not None:
        d["body"] = _dump_type(m.body)
    return d


def _load_message(d: Dict[str, Any], parent: Optional[Entity]) -> Message:
    m = Message()
    _load_common(m, d, parent)
    m.period = d.get("period", 0)
    m.rate_mode = d.get("rate_mode", "")
    if "payload" in d:
        m.payload = _load_ref(d["payload"], m)
    if "body" in d:
        body = _load_type(d["body"], m)
        if isinstance(body, RecordType):
            m.body = body
    return m


def _dump_network(n: Network) -> Dict[str, Any]:
    d = _common(n)
    _put(d, "protocol", n.protocol)
    _put(d, "alias", n.alias)
    _put(d, "ports", [_dump_port(p) for p in n.ports])
    _put(d, "buses", [_dump_bus(b) for b in n.buses])
    return d


def _dump_port(p: Port) -> Dict[str, Any]:
    d = _common(p)
    _put(d, "number", p.number)
    _put(d, "role", p.role)
    _put(d, "ip_address", p.ip_address)
    _put(d, "send", p.send)
    _put(d, "receive", p.receive)
    return d


def _dump_bus(b: Bus) -> Dict[str, Any]:
    d = _common(b)
    _put(d, "coding", b.coding)
    _put(d, "speed", b.speed)
    _put(d, "slots", [_dump_slot(s) for s in b.slots])
    return d


def _dump_slot(s: MessageSlot) -> Dict[str, Any]:
    d = _common(s)
    _put(d, "period", s.period)
    _put(d, "rate_mode", s.rate_mode)
    _put(d, "multicast_ip", s.multicast_ip)
    _put(d, "max_peak_rate", s.max_peak_rate)
    if s.message is not None:
        d["message"] = _dump_ref(s.message)
    return d


def _load_network(d: Dict[str, Any], parent: Optional[Entity]) -> Network:
    n = Network()
    _load_common(n, d, parent)
    n.protocol = d.get("protocol", "")
    n.alias = d.get("alias", "")
    for pd in d.get("ports", []):
        p = Port()
        _load_common(p, pd, n)
        p.number = pd.get("number", 0)
        p.role = pd.get("role", "")
        p.ip_address = pd.get("ip_address", "")
        p.send = pd.get("send", "")
        p.receive = pd.get("receive", "")
        n.ports.append(p)
    for bd in d.get("buses", []):
        b = Bus()
        _load_common(b, bd, n)
        b.coding = bd.get("coding", "")
        b.speed = bd.get("speed", "")
        for sd in bd.get("slots", []):
            s = MessageSlot()
            _load_common(s, sd, b)
            s.period = sd.get("period", 0)
            s.rate_mode = sd.get("rate_mode", "")
            s.multicast_ip = sd.get("multicast_ip", "")
            s.max_peak_rate = sd.get("max_peak_rate", 0)
            if "message" in sd:
                s.message = _load_ref(sd["message"], s)
            b.slots.append(s)
        n.buses.append(b)
    return n


# ---------------------------------------------------------------------- #
# Organización
# ---------------------------------------------------------------------- #
def _dump_folder(f: Folder) -> Dict[str, Any]:
    d = _common(f)
    _put(d, "folders", [_dump_folder(sub) for sub in f.folders])
    _put(d, "types", [_dump_type(t) for t in f.types])
    _put(d, "messages", [_dump_message(m) for m in f.messages])
    return d


def _load_folder_content(f: Folder, d: Dict[str, Any]) -> None:
    f.folders = [_load_folder(fd, f) for fd in d.get("folders", [])]
    f.types = [_load_type(td, f) for td in d.get("types", [])]
    f.messages = [_load_message(md, f) for md in d.get("messages", [])]


def _load_folder(d: Dict[str, Any], parent: Optional[Entity]) -> Folder:
    f = Folder()
    _load_common(f, d, parent)
    _load_folder_content(f, d)
    return f


def module_to_dict(module: Module) -> Dict[str, Any]:
    d = _dump_folder(module)
    d["class"] = "Module"
    _put(d, "source_file", module.source_file)
    _put(d, "national_export_control", module.national_export_control)
    _put(d, "us_export_control", module.us_export_control)
    if module.explicit_national_ec is not None:
        d["explicit_national_ec"] = _dump_ref(module.explicit_national_ec)
    if module.explicit_us_ec is not None:
        d["explicit_us_ec"] = _dump_ref(module.explicit_us_ec)
    _put(d, "networks", [_dump_network(n) for n in module.networks])
    return d


def module_from_dict(d: Dict[str, Any]) -> Module:
    if d.get("class") != "Module":
        raise ValueError(f"Se esperaba class='Module', encontrado '{d.get('class')}'")
    m = Module()
    _load_common(m, d, None)
    m.source_file = d.get("source_file", "")
    m.national_export_control = d.get("national_export_control", "")
    m.us_export_control = d.get("us_export_control", "")
    _load_folder_content(m, d)
    if "explicit_national_ec" in d:
        m.explicit_national_ec = _load_ref(d["explicit_national_ec"], m)
        m.explicit_national_ec.role = "explicitNational_EC"
    if "explicit_us_ec" in d:
        m.explicit_us_ec = _load_ref(d["explicit_us_ec"], m)
        m.explicit_us_ec.role = "explicitUS_EC"
    m.networks = [_load_network(nd, m) for nd in d.get("networks", [])]
    return m


# ---------------------------------------------------------------------- #
# API pública
# ---------------------------------------------------------------------- #
def save_module(module: Module, path: str) -> None:
    """Guarda un módulo como JSON legible (un módulo por archivo)."""
    logger.info("Guardando módulo '%s' en %s", module.name, path)
    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "module": module_to_dict(module),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_module(path: str, registry=None) -> Module:
    """Carga un módulo desde JSON y, si se pasa registro, lo registra.

    Tras cargar todos los módulos, llamar a registry.resolve_references()
    para re-enlazar las referencias, igual que al parsear XML.
    """
    logger.info("Cargando módulo desde %s", path)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if payload.get("format") != FORMAT_NAME:
        raise ValueError(f"{path} no es un archivo {FORMAT_NAME}")
    version = payload.get("version", 0)
    if version > FORMAT_VERSION:
        raise ValueError(
            f"{path} usa la versión {version} del formato; "
            f"esta herramienta soporta hasta la {FORMAT_VERSION}"
        )

    module = module_from_dict(payload["module"])
    if registry is not None:
        registry.register_module(module)
    return module
