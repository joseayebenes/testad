"""Modelo de datos simplificado para ICDs (Interface Control Documents).

Filosofía de diseño
-------------------
Los XML de producción tienen decenas de tipos y atributos, lo que hacía el
metamodelo anterior (una dataclass por xsi:type) difícil de entender y de
mantener. Este modelo lo sustituye por UN único tipo de nodo genérico:

* ``ICDNode``  — cualquier elemento del XML (Module, folder, data, dataField,
  owns, port, ...). Forma un árbol (patrón Composite).
* ``Ref``      — cualquier referencia a otra entidad, ya venga como elemento
  hijo ``<with href="..."/>`` o como atributo ``with="_id"``.

Toda la información original se conserva:

* ``attrs``       — TODOS los atributos XML tal cual (claves con su prefijo
  normalizado: ``xsi:type``, ``xmi:id``, ``name``...). Esto garantiza que el
  writer podrá reserializar sin pérdida.
* ``text_props``  — elementos hijo que solo llevan texto
  (``<NationalExportControl>ES:DUAL</NationalExportControl>``).
* ``nsmap``       — en el nodo raíz, los namespaces declarados en el archivo.

La semántica se expone mediante ``kind`` (derivado de ``xsi:type`` o del tag)
y propiedades de conveniencia (``name``, ``length``, ``layout``...), de modo
que el resto del código nunca necesita conocer los detalles crudos del XML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

# Tags de elementos hijo que son referencias puras (llevan href y nada más).
REFERENCE_TAGS = {"with", "explicitNational_EC", "explicitUS_EC"}

# Atributos que contienen referencias por id dentro del mismo archivo
# (estilo EMF: las referencias locales van como atributo, las externas como
# elemento hijo con href).
REFERENCE_ATTRS = {"with"}

# kind por defecto cuando el elemento no lleva xsi:type.
DEFAULT_KINDS = {
    "Module": "Module",
    "folder": "Folder",
    "data": "Data",
    "dataField": "DataField",
    "array": "Array",
    "isMember": "IsMember",
    "owns": "Owns",
    "port": "Port",
    "bus": "Bus",
    "message": "MessageSlot",  # <message> dentro de un bus: ranura de transmisión
}

# kinds que actúan como contenedores de la jerarquía lógica.
CONTAINER_KINDS = {"Module", "Folder"}


@dataclass
class Ref:
    """Referencia hacia otra entidad del modelo (local o entre archivos).

    ``tag``   — de dónde salió: 'with', 'explicitNational_EC', ...
    ``href``  — forma 'Archivo.xmi#_id' (referencia externa) o vacío.
    ``ref_id``— id directo cuando la referencia venía como atributo local.
    ``target``— nodo real, rellenado por ICDRegistry.resolve_references().
    """

    tag: str = "with"
    href: str = ""
    ref_id: str = ""
    xsi_type: str = ""
    source: Optional["ICDNode"] = field(default=None, repr=False)
    target: Optional["ICDNode"] = field(default=None, repr=False)

    @property
    def target_id(self) -> str:
        """Fragmento id al que apunta, venga de href o de atributo."""
        if self.ref_id:
            return self.ref_id
        return self.href.split("#")[-1] if self.href else ""

    @property
    def target_file(self) -> str:
        """Nombre de archivo del href ('' si es referencia local)."""
        if "#" in self.href:
            fname = self.href.split("#", 1)[0]
            return fname
        return ""

    @property
    def is_resolved(self) -> bool:
        return self.target is not None


@dataclass
class ICDNode:
    """Nodo genérico del árbol ICD. Representa cualquier elemento XML."""

    tag: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["ICDNode"] = field(default_factory=list)
    refs: List[Ref] = field(default_factory=list)
    text_props: Dict[str, str] = field(default_factory=dict)
    parent: Optional["ICDNode"] = field(default=None, repr=False, compare=False)
    source_file: str = ""
    # Solo el nodo raíz (Module) lo rellena: prefijo -> URI.
    nsmap: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Identidad y semántica
    # ------------------------------------------------------------------ #
    @property
    def id(self) -> str:
        return self.attrs.get("id", self.attrs.get("xmi:id", ""))

    @property
    def name(self) -> str:
        return self.attrs.get("name", "")

    @name.setter
    def name(self, value: str) -> None:
        self.attrs["name"] = value

    @property
    def xsi_type(self) -> str:
        return self.attrs.get("xsi:type", "")

    @property
    def kind(self) -> str:
        """Tipo semántico: 'Signal', 'Message', 'Folder'...

        Prioridad: xsi:type ('Data:VariableArray' -> 'VariableArray'),
        después el tag con su alias por defecto.
        """
        if self.xsi_type:
            return self.xsi_type.split(":")[-1]
        if self.tag in DEFAULT_KINDS:
            return DEFAULT_KINDS[self.tag]
        # Solo mayúscula inicial: 'UDPNetwork' debe seguir siendo 'UDPNetwork'
        return self.tag[:1].upper() + self.tag[1:]

    @property
    def is_container(self) -> bool:
        return self.kind in CONTAINER_KINDS

    # ------------------------------------------------------------------ #
    # Acceso genérico a atributos
    # ------------------------------------------------------------------ #
    def get(self, attr: str, default: str = "") -> str:
        return self.attrs.get(attr, default)

    def set(self, attr: str, value: Any) -> None:
        self.attrs[attr] = str(value)

    def get_int(self, attr: str, default: int = 0) -> int:
        raw = self.attrs.get(attr, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_float(self, attr: str, default: float = 0.0) -> float:
        raw = self.attrs.get(attr, "")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ #
    # Propiedades de conveniencia (las más consultadas en ingeniería)
    # ------------------------------------------------------------------ #
    @property
    def security(self) -> str:
        return self.get("security")

    @property
    def coding(self) -> str:
        return self.get("coding")

    @property
    def length(self) -> int:
        return self.get_int("length")

    @property
    def units(self) -> str:
        return self.get("units")

    @property
    def period(self) -> int:
        return self.get_int("period")

    @property
    def key_selector(self) -> str:
        return self.get("keySelector")

    @property
    def national_export_control(self) -> str:
        return self.attrs.get(
            "NationalExportControl",
            self.text_props.get("NationalExportControl", ""),
        )

    @property
    def us_export_control(self) -> str:
        return self.attrs.get(
            "USExportControl",
            self.text_props.get("USExportControl", ""),
        )

    @property
    def layout(self) -> Dict[str, int]:
        """Posición física en memoria (palabras de 16/12 bits + offset)."""
        return {
            "w16": self.get_int("w16"),
            "w16_b": self.get_int("w16_b"),
            "w12": self.get_int("w12"),
            "w12_b": self.get_int("w12_b"),
            "maxPosition": self.get_int("maxPosition"),
            "prePadding": self.get_int("prePadding"),
        }

    # ------------------------------------------------------------------ #
    # Navegación
    # ------------------------------------------------------------------ #
    @property
    def folders(self) -> List["ICDNode"]:
        return [c for c in self.children if c.kind == "Folder"]

    @property
    def data_elements(self) -> List["ICDNode"]:
        return [c for c in self.children if c.tag == "data"]

    @property
    def networks(self) -> List["ICDNode"]:
        return [c for c in self.children if c.kind.endswith("Network")]

    @property
    def fields(self) -> List["ICDNode"]:
        """Campos de layout de una estructura/mensaje (dataField, array, isMember)."""
        return [c for c in self.children if c.tag in ("dataField", "array", "isMember")]

    @property
    def owns(self) -> Optional["ICDNode"]:
        """Entidad contenida inline (declaración in-site), si existe."""
        for c in self.children:
            if c.tag == "owns":
                return c
        return None

    @property
    def with_ref(self) -> Optional[Ref]:
        """La referencia 'with' principal del nodo, si existe."""
        for r in self.refs:
            if r.tag == "with":
                return r
        return None

    @property
    def path(self) -> str:
        """Ruta legible desde la raíz: 'FCS_ICD/Signals/NavBlock'."""
        parts: List[str] = []
        node: Optional[ICDNode] = self
        while node is not None:
            parts.append(node.name or node.id or node.tag)
            node = node.parent
        return "/".join(reversed(parts))

    def walk(self) -> Iterator["ICDNode"]:
        """Recorre el subárbol completo en profundidad (incluido self)."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        **attr_filters: str,
    ) -> List["ICDNode"]:
        """Busca nodos en el subárbol por kind, name y/o atributos exactos."""
        results = []
        for node in self.walk():
            if kind is not None and node.kind != kind:
                continue
            if name is not None and node.name != name:
                continue
            if any(node.get(k) != v for k, v in attr_filters.items()):
                continue
            results.append(node)
        return results

    def find_one(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        **attr_filters: str,
    ) -> Optional["ICDNode"]:
        matches = self.find(kind=kind, name=name, **attr_filters)
        return matches[0] if matches else None

    def add_child(self, child: "ICDNode") -> "ICDNode":
        child.parent = self
        self.children.append(child)
        return child

    # ------------------------------------------------------------------ #
    # Depuración
    # ------------------------------------------------------------------ #
    def pretty(self, indent: int = 0) -> str:
        """Representación en árbol para inspección rápida."""
        pad = "  " * indent
        label = f"{pad}{self.kind}"
        if self.name:
            label += f" '{self.name}'"
        if self.id:
            label += f" [{self.id}]"
        extras = []
        for r in self.refs:
            state = "->" if r.is_resolved else "-?>"
            target = r.target.name if r.target else (r.href or r.ref_id)
            extras.append(f"{r.tag}{state}{target}")
        if extras:
            label += "  (" + ", ".join(extras) + ")"
        lines = [label]
        for child in self.children:
            lines.append(child.pretty(indent + 1))
        return "\n".join(lines)

    def __repr__(self) -> str:  # repr corto: el de dataclass es inmanejable
        return f"<{self.kind} '{self.name}' id={self.id!r}>"
