"""Traductor XML/XMI (Eclipse EMF) -> modelo de dominio (core/model.py).

El parser es la única pieza que conoce la estructura del XML. Traduce cada
concepto del formato de origen a su equivalente del modelo:

    XML (EMF)                          Modelo
    ---------------------------------  --------------------------------
    Data:Signal                        ScalarType
    Data:TextSignal                    TextType
    Data:Structure                     RecordType
    Data:VariableArray                 ArrayType (campos variables)
    Data:Header + isMember             VariantType (campos condicionales)
    Data:Message                       Message
    dataField / array                  Field (posición + tipo)
    <owns ...> (contención inline)     Field.inline / definición in situ
    with="_id" o <with href=".."/>     Reference
    Scal:Lineal / Enums / LUT          LinearScaling / EnumScaling / LUTScaling
    IP:UDPNetwork, port, bus, message  Network, Port, Bus, MessageSlot

Reglas de robustez:

* Inmune a prefijos de namespace (tags y atributos se normalizan).
* Ningún atributo se pierde: lo no mapeado va al dict ``extra`` de la entidad.
* Tipos xsi:type desconocidos -> RecordType (si tienen campos) o TypeDef,
  con el tipo original en ``source_type``.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Dict, Optional, Tuple

from core.model import (
    ArrayType, BitPosition, Bus, Entity, EnumScaling, Field, Folder,
    LinearScaling, LUTRange, LUTScaling, Message, MessageSlot, Module,
    Network, Port, RecordType, Reference, ScalarType, Scaling, TextType,
    TypeDef, VariantType,
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
    "VariableArray": ArrayType,
    "Header": VariantType,
    "Message": Message,
}


class ICDParser:
    def __init__(self, registry=None) -> None:
        self.registry = registry

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def parse_file(self, file_path: str) -> Module:
        logger.info("Analizando ICD: %s", file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self.parse_string(content, source_file=os.path.basename(file_path))

    def parse_string(self, content: str, source_file: str = "") -> Module:
        nsmap = self._collect_namespaces(content)
        self._uri_to_prefix = self._reverse(nsmap)

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
        module.nsmap = nsmap

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

    def _qualify(self, qname: str) -> str:
        if not qname.startswith("{"):
            return qname
        uri, local = qname[1:].split("}", 1)
        prefix = self._uri_to_prefix.get(uri, "")
        return f"{prefix}:{local}" if prefix else local

    # ------------------------------------------------------------------ #
    # Helpers de construcción
    # ------------------------------------------------------------------ #
    def _attrs(self, elem: ET.Element) -> Dict[str, str]:
        return {self._qualify(k): v for k, v in elem.attrib.items()}

    @staticmethod
    def _pop_int(attrs: Dict[str, str], key: str, default: int = 0) -> int:
        raw = attrs.pop(key, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _pop_float(attrs: Dict[str, str], key: str, default: float = 0.0) -> float:
        raw = attrs.pop(key, "")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _base(self, cls, elem: ET.Element, attrs: Dict[str, str]) -> Entity:
        """Crea la entidad y consume los atributos comunes; el resto de
        atributos que queden en ``attrs`` al final debe asignarse a extra."""
        e = cls()
        e.id = attrs.pop("id", attrs.pop("xmi:id", ""))
        e.name = attrs.pop("name", "")
        e.security = attrs.pop("security", "")
        e.remarks = attrs.pop("remarks", "")
        e.source_type = attrs.pop("xsi:type", "")
        e.source_tag = self._local(elem.tag)
        return e

    @staticmethod
    def _attach(child: Entity, parent: Entity) -> None:
        child.parent = parent

    def _parse_reference(self, elem: ET.Element, role: str, owner: Entity) -> Reference:
        """<with href="Base.xmi#_id"/> o <explicitNational_EC href=".."/>."""
        attrs = self._attrs(elem)
        href = attrs.get("href", "")
        fname, _, frag = href.partition("#")
        if not frag:                      # href sin '#': es un id directo
            fname, frag = "", fname
        return Reference(
            target_id=frag, file=fname, role=role,
            hint_type=attrs.get("xsi:type", ""), owner=owner,
        )

    @staticmethod
    def _local_ref(target_id: str, role: str, owner: Entity) -> Reference:
        return Reference(target_id=target_id, role=role, owner=owner)

    # ------------------------------------------------------------------ #
    # Module y carpetas
    # ------------------------------------------------------------------ #
    def _parse_module(self, elem: ET.Element) -> Module:
        attrs = self._attrs(elem)
        module: Module = self._base(Module, elem, attrs)
        attrs.pop("xmi:version", None)
        module.national_export_control = attrs.pop("NationalExportControl", "")
        module.us_export_control = attrs.pop("USExportControl", "")
        self._parse_container_children(elem, module)
        module.extra = attrs
        return module

    def _parse_folder(self, elem: ET.Element) -> Folder:
        attrs = self._attrs(elem)
        folder: Folder = self._base(Folder, elem, attrs)
        self._parse_container_children(elem, folder)
        folder.extra = attrs
        return folder

    def _parse_container_children(self, elem: ET.Element, container: Folder) -> None:
        for child in elem:
            tag = self._local(child.tag)
            if tag == "folder":
                sub = self._parse_folder(child)
                self._attach(sub, container)
                container.folders.append(sub)
            elif tag == "data" or tag == "owns":
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
                container.explicit_national_ec = self._parse_reference(child, tag, container)
            elif tag == "explicitUS_EC" and isinstance(container, Module):
                container.explicit_us_ec = self._parse_reference(child, tag, container)
            else:
                self._absorb_text_prop(child, tag, container)

    def _absorb_text_prop(self, child: ET.Element, tag: str, entity: Entity) -> None:
        """Elemento hijo de solo texto -> propiedad; lo demás -> extra."""
        text = (child.text or "").strip()
        if child.attrib or len(child) > 0 or not text:
            logger.warning("Elemento no reconocido '%s' en '%s' (conservado en extra)", tag, entity.path)
            return
        if tag == "NationalExportControl" and isinstance(entity, Module):
            entity.national_export_control = entity.national_export_control or text
        elif tag == "USExportControl" and isinstance(entity, Module):
            entity.us_export_control = entity.us_export_control or text
        else:
            entity.extra[tag] = text

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
            # Tipo desconocido: registro si tiene campos, TypeDef plano si no.
            has_fields = any(self._local(c.tag) in FIELD_TAGS for c in elem)
            cls = RecordType if has_fields else TypeDef
            if kind:
                logger.info("xsi:type no modelado '%s': se conserva como %s", xsi, cls.__name__)

        if cls is Message:
            return self._parse_message(elem, attrs)

        entity = self._base(cls, elem, attrs)

        if isinstance(entity, ScalarType):
            entity.bit_length = self._pop_int(attrs, "length")
            entity.encoding = attrs.pop("coding", "")
            entity.default_value = attrs.pop("defaultValue", "")
            units = attrs.pop("units", "")
            if units and entity.scaling is None:
                entity.scaling = Scaling(units=units)
        elif isinstance(entity, TextType):
            entity.max_chars = self._pop_int(attrs, "textLength")
            entity.length_mode = attrs.pop("lengthType", "")
            entity.encoding = attrs.pop("coding", "")
            entity.bit_endianness = attrs.pop("bitEndianness", "")
        elif isinstance(entity, ArrayType):
            entity.counter_type = attrs.pop("counterType", "")
            entity.counter_bits = self._pop_int(attrs, "counterSize")
            entity.max_count = self._pop_int(attrs, "maxLength")
        elif isinstance(entity, VariantType):
            entity.discriminator = attrs.pop("key", "")
        elif isinstance(entity, RecordType):
            entity.bit_length = self._pop_int(attrs, "length")

        # Hijos: campos, escalado y metadatos
        for child in elem:
            tag = self._local(child.tag)
            if tag in FIELD_TAGS and isinstance(entity, (RecordType, ArrayType, VariantType)):
                f = self._parse_field(child)
                self._attach(f, entity)
                entity.fields.append(f)
            elif tag == "scal" and isinstance(entity, ScalarType):
                scaling = self._parse_scaling(child)
                if scaling is not None:
                    # conservar las unidades si ya venían como atributo
                    if entity.scaling and entity.scaling.units and not scaling.units:
                        scaling.units = entity.scaling.units
                    entity.scaling = scaling
            elif tag == "with":
                logger.warning("Referencia 'with' inesperada en tipo '%s'", entity.name)
            else:
                self._absorb_text_prop(child, tag, entity)

        entity.extra = attrs
        return entity

    def _parse_field(self, elem: ET.Element) -> Field:
        attrs = self._attrs(elem)
        f: Field = self._base(Field, elem, attrs)
        f.condition = attrs.pop("keySelector", "")
        f.position = BitPosition(
            word16=self._pop_int(attrs, "w16"),
            bit16=self._pop_int(attrs, "w16_b"),
            word12=self._pop_int(attrs, "w12"),
            bit12=self._pop_int(attrs, "w12_b"),
            max_position=self._pop_int(attrs, "maxPosition"),
            pre_padding=self._pop_int(attrs, "prePadding"),
        )

        with_attr = attrs.pop("with", "")
        if with_attr:
            f.ref = self._local_ref(with_attr, "with", f)

        for child in elem:
            tag = self._local(child.tag)
            if tag == "with":
                f.ref = self._parse_reference(child, "with", f)
            elif tag == "owns":
                inline = self._parse_type(child)
                if isinstance(inline, TypeDef):
                    self._attach(inline, f)
                    f.inline = inline
                else:
                    logger.warning("owns con contenido no-tipo en campo '%s'", f.name)
            else:
                self._absorb_text_prop(child, tag, f)

        f.extra = attrs
        return f

    def _parse_message(self, elem: ET.Element, attrs: Dict[str, str]) -> Message:
        msg: Message = self._base(Message, elem, attrs)
        msg.period = self._pop_int(attrs, "period")
        msg.rate_mode = attrs.pop("rateMode", "")

        with_attr = attrs.pop("with", "")
        if with_attr:
            msg.payload = self._local_ref(with_attr, "with", msg)

        inline_fields = []
        for child in elem:
            tag = self._local(child.tag)
            if tag == "with":
                msg.payload = self._parse_reference(child, "with", msg)
            elif tag in FIELD_TAGS:
                inline_fields.append(self._parse_field(child))
            else:
                self._absorb_text_prop(child, tag, msg)

        if inline_fields:
            body = RecordType(name=f"{msg.name}Body")
            for f in inline_fields:
                self._attach(f, body)
            body.fields = inline_fields
            self._attach(body, msg)
            msg.body = body

        msg.extra = attrs
        return msg

    # ------------------------------------------------------------------ #
    # Escalado
    # ------------------------------------------------------------------ #
    def _parse_scaling(self, elem: ET.Element) -> Optional[Scaling]:
        attrs = self._attrs(elem)
        xsi = attrs.pop("xsi:type", "")
        kind = xsi.split(":")[-1]
        units = attrs.pop("units", "")

        if kind == "Lineal":
            s = LinearScaling(
                units=units,
                lsb=self._pop_float(attrs, "lsb"),
                offset=self._pop_float(attrs, "offset"),
            )
        elif kind == "Enums":
            s = EnumScaling(units=units)
            for item in elem:
                item_attrs = self._attrs(item)
                value = item_attrs.get("value", "")
                label = item_attrs.get("name", item_attrs.get("tag", ""))
                if value != "":
                    s.labels[value] = label
        elif kind == "LUT":
            s = LUTScaling(units=units)
            for item in elem:
                item_attrs = self._attrs(item)
                s.ranges.append(LUTRange(
                    begin=float(item_attrs.get("begin", 0) or 0),
                    end=float(item_attrs.get("end", 0) or 0),
                    lsb=float(item_attrs.get("lsb", 0) or 0),
                    offset=float(item_attrs.get("offset", 0) or 0),
                ))
        else:
            logger.warning("Tipo de escalado no reconocido: '%s'", xsi)
            return None

        s.extra = attrs
        return s

    # ------------------------------------------------------------------ #
    # Arquitectura de comunicaciones
    # ------------------------------------------------------------------ #
    def _parse_network(self, elem: ET.Element, tag: str) -> Network:
        attrs = self._attrs(elem)
        net: Network = self._base(Network, elem, attrs)
        net.protocol = tag.replace("Network", "").upper()

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
                self._absorb_text_prop(child, ctag, net)

        net.extra = attrs
        return net

    def _parse_port(self, elem: ET.Element) -> Port:
        attrs = self._attrs(elem)
        port: Port = self._base(Port, elem, attrs)
        port.number = self._pop_int(attrs, "port")
        port.role = attrs.pop("role", "")
        port.ip_address = attrs.pop("ipAddress", "")
        port.send = attrs.pop("send", "")
        port.receive = attrs.pop("receive", "")
        port.extra = attrs
        return port

    def _parse_bus(self, elem: ET.Element) -> Bus:
        attrs = self._attrs(elem)
        bus: Bus = self._base(Bus, elem, attrs)
        bus.coding = attrs.pop("coding", "")

        for child in elem:
            tag = self._local(child.tag)
            if tag == "message":
                slot = self._parse_slot(child)
                self._attach(slot, bus)
                bus.slots.append(slot)
            elif tag == "owns":
                # El <owns> de un bus es su propia definición (velocidad, nombre).
                own_attrs = self._attrs(child)
                bus.speed = own_attrs.pop("speed", bus.speed)
                bus.name = bus.name or own_attrs.pop("name", "")
                # id y resto de atributos del owns, conservados para el writer
                for k, v in own_attrs.items():
                    bus.extra[f"owns.{k}"] = v
            else:
                self._absorb_text_prop(child, tag, bus)

        bus.extra.update(attrs)
        return bus

    def _parse_slot(self, elem: ET.Element) -> MessageSlot:
        attrs = self._attrs(elem)
        slot: MessageSlot = self._base(MessageSlot, elem, attrs)
        slot.period = self._pop_int(attrs, "period")
        slot.rate_mode = attrs.pop("rateMode", "")
        slot.multicast_ip = attrs.pop("multicastIP", "")
        slot.max_peak_rate = self._pop_int(attrs, "maxPeakRate")

        with_attr = attrs.pop("with", "")
        if with_attr:
            slot.message = self._local_ref(with_attr, "with", slot)

        for child in elem:
            tag = self._local(child.tag)
            if tag == "with":
                slot.message = self._parse_reference(child, "with", slot)
            else:
                self._absorb_text_prop(child, tag, slot)

        slot.extra = attrs
        return slot
