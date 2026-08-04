"""Traductor XML/XMI (Eclipse EMF) -> modelo de dominio (core/model.py).

Traducción de un solo sentido: los XML solo se leen (la persistencia de la
herramienta será JSON). El parser es la única pieza del sistema que conoce
el formato de origen, y extrae únicamente la información con significado de
ingeniería; los atributos sin mapeo se ignoran.

    XML (EMF)                          Modelo
    ---------------------------------  --------------------------------
    Data:Signal                        ScalarType
    Data:TextSignal                    TextType
    Data:Structure                     RecordType
    Data:VariableArray                 ArrayType (campos variables)
    Data:Header + isMember             VariantType (campos condicionales)
    Data:Message                       Message
    dataField / array / isMember       Field (posición + tipo + condición)
    <owns ...> (contención inline)     Field.inline / tipo in situ
    with="_id" o <with href=".."/>     Reference
    Scal:Lineal / Enums / LUT          LinearScaling / EnumScaling / LUTScaling
    IP:UDPNetwork, port, bus, message  Network, Port, Bus, MessageSlot

Robustez:

* Inmune a prefijos de namespace (tags y atributos se normalizan).
* xsi:type desconocido -> RecordType si tiene campos, TypeDef plano si no.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Dict, List, Optional

from core.model import (
    ArrayType, BitPosition, Bus, CompositeType, Entity, EnumScaling, Field,
    Folder, LinearScaling, LUTRange, LUTScaling, Message, MessageSlot,
    Module, Network, Port, RecordType, Reference, ScalarType, Scaling,
    TextType, TypeDef, VariantType,
)

logger = logging.getLogger("ICDParser")

# Prefijos canónicos aunque el archivo declare otros alias.
CANONICAL_PREFIXES = {
    "http://www.omg.org/XMI": "xmi",
    "http://www.w3.org/2001/XMLSchema-instance": "xsi",
}

FIELD_TAGS = {"dataField", "array", "isMember"}

TYPE_MAP = {
    "Signal": ScalarType,
    "TextSignal": TextType,
    "Structure": RecordType,
    "Struct": RecordType,
    "VariableArray": ArrayType,
    "Header": VariantType,
    "Message": Message,
}


def _to_int(raw: Optional[str], default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_float(raw: Optional[str], default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class ICDParser:
    def __init__(self, registry=None) -> None:
        self.registry = registry
        self._uri_to_prefix: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def parse_file(self, file_path: str) -> Module:
        logger.info("Analizando ICD: %s", file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self.parse_string(content, source_file=os.path.basename(file_path))

    def parse_string(self, content: str, source_file: str = "") -> Module:
        self._uri_to_prefix = self._reverse(self._collect_namespaces(content))

        try:
            root_elem = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError(f"XML mal formado en {source_file or '<string>'}: {exc}") from exc

        if self._local(root_elem.tag) != "Module":
            raise ValueError(
                f"Se esperaba raíz 'Module', encontrado "
                f"'{self._local(root_elem.tag)}' en {source_file or '<string>'}"
            )

        module = self._parse_module(root_elem)
        module.source_file = source_file

        if self.registry is not None:
            self.registry.register_module(module)
        return module

    # ------------------------------------------------------------------ #
    # Namespaces
    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_namespaces(content: str) -> Dict[str, str]:
        nsmap: Dict[str, str] = {}
        for _event, (prefix, uri) in ET.iterparse(StringIO(content), events=("start-ns",)):
            nsmap.setdefault(prefix, uri)
        return nsmap

    @staticmethod
    def _reverse(nsmap: Dict[str, str]) -> Dict[str, str]:
        reverse: Dict[str, str] = {}
        for prefix, uri in nsmap.items():
            reverse.setdefault(uri, prefix)
        reverse.update(CANONICAL_PREFIXES)
        return reverse

    @staticmethod
    def _local(qname: str) -> str:
        if qname.startswith("{"):
            return qname.split("}", 1)[1]
        return qname.split(":")[-1]

    def _attrs(self, elem: ET.Element) -> Dict[str, str]:
        """Atributos con nombres normalizados ('xsi:type', 'id', 'name'...)."""
        out: Dict[str, str] = {}
        for key, value in elem.attrib.items():
            if key.startswith("{"):
                uri, local = key[1:].split("}", 1)
                prefix = self._uri_to_prefix.get(uri, "")
                key = f"{prefix}:{local}" if prefix else local
            out[key] = value
        return out

    # ------------------------------------------------------------------ #
    # Helpers de construcción
    # ------------------------------------------------------------------ #
    @staticmethod
    def _base(cls, attrs: Dict[str, str]) -> Entity:
        e = cls()
        e.id = attrs.get("id", attrs.get("xmi:id", ""))
        e.name = attrs.get("name", "")
        e.security = attrs.get("security", "")
        e.remarks = attrs.get("remarks", "")
        e.description = attrs.get("description", "")
        return e

    @staticmethod
    def _attach(child: Entity, parent: Entity) -> None:
        child.parent = parent

    @staticmethod
    def _href_ref(href: str, role: str, owner: Entity) -> Reference:
        """'Base.xmi#_id' -> Reference(file='Base.xmi', target_id='_id')."""
        fname, _, frag = href.partition("#")
        if not frag:                    # href sin '#': es un id directo
            fname, frag = "", fname
        return Reference(target_id=frag, file=fname, role=role, owner=owner)

    # ------------------------------------------------------------------ #
    # Module y carpetas
    # ------------------------------------------------------------------ #
    def _parse_module(self, elem: ET.Element) -> Module:
        attrs = self._attrs(elem)
        module: Module = self._base(Module, attrs)
        module.national_export_control = attrs.get("NationalExportControl", "")
        module.us_export_control = attrs.get("USExportControl", "")
        self._parse_container_children(elem, module)
        return module

    def _parse_folder(self, elem: ET.Element) -> Folder:
        folder: Folder = self._base(Folder, self._attrs(elem))
        self._parse_container_children(elem, folder)
        return folder

    def _parse_container_children(self, elem: ET.Element, container: Folder) -> None:
        for child in elem:
            tag = self._local(child.tag)
            if tag == "folder":
                sub = self._parse_folder(child)
                self._attach(sub, container)
                container.folders.append(sub)
            elif tag in ("data", "owns"):
                item = self._parse_type(child)
                self._attach(item, container)
                if isinstance(item, Message):
                    container.messages.append(item)
                else:
                    container.types.append(item)
            elif tag.endswith("Network"):
                if isinstance(container, Module):
                    net = self._parse_network(child, tag)
                    self._attach(net, container)
                    container.networks.append(net)
            elif tag == "explicitNational_EC" and isinstance(container, Module):
                container.explicit_national_ec = self._href_ref(
                    self._attrs(child).get("href", ""), tag, container)
            elif tag == "explicitUS_EC" and isinstance(container, Module):
                container.explicit_us_ec = self._href_ref(
                    self._attrs(child).get("href", ""), tag, container)
            elif tag == "NationalExportControl" and isinstance(container, Module):
                container.national_export_control = (
                    container.national_export_control or (child.text or "").strip())
            elif tag == "USExportControl" and isinstance(container, Module):
                container.us_export_control = (
                    container.us_export_control or (child.text or "").strip())
            else:
                logger.debug("Elemento ignorado '%s' en '%s'", tag, container.path)

    # ------------------------------------------------------------------ #
    # Sistema de tipos
    # ------------------------------------------------------------------ #
    def _parse_type(self, elem: ET.Element) -> Entity:
        """Traduce <data xsi:type="Data:X"> (o <owns>) a TypeDef/Message."""
        attrs = self._attrs(elem)
        xsi = attrs.get("xsi:type", "")
        kind = xsi.split(":")[-1] if xsi else ""

        cls = TYPE_MAP.get(kind)
        if cls is None:
            has_fields = any(self._local(c.tag) in FIELD_TAGS for c in elem)
            cls = RecordType if has_fields else TypeDef
            if kind:
                logger.info("xsi:type no modelado '%s': se conserva como %s", xsi, cls.__name__)

        if cls is Message:
            return self._parse_message(elem, attrs)

        entity = self._base(cls, attrs)

        if isinstance(entity, ScalarType):
            entity.bit_length = _to_int(attrs.get("length"))
            entity.encoding = attrs.get("coding", "")
            entity.default_value = attrs.get("defaultValue", "")
            units = attrs.get("units", "")
            if units:
                entity.scaling = Scaling(units=units)
        elif isinstance(entity, TextType):
            entity.max_chars = _to_int(attrs.get("textLength"))
            entity.length_mode = attrs.get("lengthType", "")
            entity.encoding = attrs.get("coding", "")
            entity.bit_endianness = attrs.get("bitEndianness", "")
        elif isinstance(entity, ArrayType):
            entity.counter_type = attrs.get("counterType", "")
            entity.counter_bits = _to_int(attrs.get("counterSize"))
            entity.max_count = _to_int(attrs.get("maxLength"))
        elif isinstance(entity, VariantType):
            entity.discriminator = attrs.get("key", "")
        elif isinstance(entity, RecordType):
            entity.bit_length = _to_int(attrs.get("length"))

        for child in elem:
            tag = self._local(child.tag)
            if tag in FIELD_TAGS and isinstance(entity, CompositeType):
                f = self._parse_field(child)
                self._attach(f, entity)
                entity.fields.append(f)
            elif tag == "scal" and isinstance(entity, ScalarType):
                scaling = self._parse_scaling(child)
                if scaling is not None:
                    if entity.scaling and entity.scaling.units and not scaling.units:
                        scaling.units = entity.scaling.units
                    entity.scaling = scaling
            else:
                logger.debug("Elemento ignorado '%s' en tipo '%s'", tag, entity.name)

        return entity

    def _parse_field(self, elem: ET.Element) -> Field:
        attrs = self._attrs(elem)
        f: Field = self._base(Field, attrs)
        f.condition = attrs.get("keySelector", "")
        f.position = BitPosition(
            word16=_to_int(attrs.get("w16")),
            bit16=_to_int(attrs.get("w16_b")),
            word12=_to_int(attrs.get("w12")),
            bit12=_to_int(attrs.get("w12_b")),
            max_position=_to_int(attrs.get("maxPosition")),
            pre_padding=_to_int(attrs.get("prePadding")),
        )

        if attrs.get("with"):
            f.ref = Reference(target_id=attrs["with"], role="with", owner=f)

        for child in elem:
            tag = self._local(child.tag)
            if tag == "with":
                f.ref = self._href_ref(self._attrs(child).get("href", ""), "with", f)
            elif tag == "owns":
                inline = self._parse_type(child)
                if isinstance(inline, TypeDef):
                    self._attach(inline, f)
                    f.inline = inline
                else:
                    logger.warning("owns con contenido no-tipo en campo '%s'", f.name)
            else:
                logger.debug("Elemento ignorado '%s' en campo '%s'", tag, f.name)

        return f

    def _parse_message(self, elem: ET.Element, attrs: Dict[str, str]) -> Message:
        msg: Message = self._base(Message, attrs)
        msg.period = _to_int(attrs.get("period"))
        msg.rate_mode = attrs.get("rateMode", "")

        if attrs.get("with"):
            msg.payload = Reference(target_id=attrs["with"], role="with", owner=msg)

        inline_fields: List[Field] = []
        for child in elem:
            tag = self._local(child.tag)
            if tag == "with":
                msg.payload = self._href_ref(self._attrs(child).get("href", ""), "with", msg)
            elif tag in FIELD_TAGS:
                inline_fields.append(self._parse_field(child))

        if inline_fields:
            body = RecordType(name=f"{msg.name}Body")
            for f in inline_fields:
                self._attach(f, body)
            body.fields = inline_fields
            self._attach(body, msg)
            msg.body = body

        return msg

    # ------------------------------------------------------------------ #
    # Escalado
    # ------------------------------------------------------------------ #
    def _parse_scaling(self, scal_elem: ET.Element) -> Optional[Scaling]:
        """Parsea el escalado de un elemento <scal>.

        Dos formas admitidas:
        * Real (EMF): <scal> sin tipo que envuelve <owns xsi:type="Scal:...">
          (mismo patrón de contención inline que dataField > owns).
        * Directa: <scal xsi:type="Scal:..."> con el tipo en el propio nodo.
        """
        attrs = self._attrs(scal_elem)
        if "xsi:type" in attrs:
            return self._parse_scaling_object(scal_elem, attrs)
        for child in scal_elem:
            if self._local(child.tag) == "owns":
                child_attrs = self._attrs(child)
                if "xsi:type" in child_attrs:
                    return self._parse_scaling_object(child, child_attrs)
            # <with href=...>: escalado compartido por referencia (no soportado aún)
        logger.warning("Escalado sin tipo reconocible en <scal>")
        return None

    def _parse_scaling_object(self, elem: ET.Element, attrs: Dict[str, str]) -> Optional[Scaling]:
        kind = attrs.get("xsi:type", "").split(":")[-1]
        units = attrs.get("units", "")

        if kind == "Lineal":
            return LinearScaling(
                units=units,
                lsb=_to_float(attrs.get("lsb")),
                offset=_to_float(attrs.get("offset")),
            )
        if kind in ("Enums", "Enum"):
            s = EnumScaling(units=units)
            for item in elem:
                if self._local(item.tag) != "tag":
                    continue  # ignora <metaData> y otros
                ia = self._attrs(item)
                value = ia.get("value", "")
                if value != "":
                    s.labels[value] = ia.get("name", "") or ia.get("alias", "")
            return s
        if kind in ("LUT", "Lut"):
            s = LUTScaling(units=units)
            for item in elem:
                if self._local(item.tag) == "metaData":
                    continue
                ia = self._attrs(item)
                if not any(k in ia for k in ("begin", "end", "lsb", "offset")):
                    continue
                s.ranges.append(LUTRange(
                    begin=_to_float(ia.get("begin")),
                    end=_to_float(ia.get("end")),
                    lsb=_to_float(ia.get("lsb")),
                    offset=_to_float(ia.get("offset")),
                ))
            return s

        logger.warning("Tipo de escalado no reconocido: '%s'", attrs.get("xsi:type", ""))
        return None

    # ------------------------------------------------------------------ #
    # Arquitectura de comunicaciones
    # ------------------------------------------------------------------ #
    def _parse_network(self, elem: ET.Element, tag: str) -> Network:
        attrs = self._attrs(elem)
        net: Network = self._base(Network, attrs)
        net.protocol = tag.replace("Network", "").upper()
        net.alias = attrs.get("alias", "")

        for child in elem:
            ctag = self._local(child.tag)
            if ctag == "port":
                port = self._parse_port(child)
                self._attach(port, net)
                net.ports.append(port)
            elif ctag == "bus":
                bus = self._parse_bus(child)
                self._attach(bus, net)
                net.buses.append(bus)
            else:
                logger.debug("Elemento ignorado '%s' en red '%s'", ctag, net.name)

        return net

    def _parse_port(self, elem: ET.Element) -> Port:
        attrs = self._attrs(elem)
        port: Port = self._base(Port, attrs)
        port.number = _to_int(attrs.get("port"))
        port.role = attrs.get("role", "")
        port.ip_address = attrs.get("ipAddress", "")
        port.send = attrs.get("send", "")
        port.receive = attrs.get("receive", "")
        return port

    def _parse_bus(self, elem: ET.Element) -> Bus:
        attrs = self._attrs(elem)
        bus: Bus = self._base(Bus, attrs)
        bus.coding = attrs.get("coding", "")

        for child in elem:
            tag = self._local(child.tag)
            if tag == "message":
                slot = self._parse_slot(child)
                self._attach(slot, bus)
                bus.slots.append(slot)
            elif tag == "owns":
                # El <owns> de un bus es su propia definición (velocidad, nombre).
                own_attrs = self._attrs(child)
                bus.speed = own_attrs.get("speed", bus.speed)
                bus.name = bus.name or own_attrs.get("name", "")
            else:
                logger.debug("Elemento ignorado '%s' en bus '%s'", tag, bus.name)

        return bus

    def _parse_slot(self, elem: ET.Element) -> MessageSlot:
        attrs = self._attrs(elem)
        slot: MessageSlot = self._base(MessageSlot, attrs)
        slot.period = _to_int(attrs.get("period"))
        slot.rate_mode = attrs.get("rateMode", "")
        slot.multicast_ip = attrs.get("multicastIP", "")
        slot.max_peak_rate = _to_int(attrs.get("maxPeakRate"))

        if attrs.get("with"):
            slot.message = Reference(target_id=attrs["with"], role="with", owner=slot)

        for child in elem:
            if self._local(child.tag) == "with":
                slot.message = self._href_ref(self._attrs(child).get("href", ""), "with", slot)

        return slot
