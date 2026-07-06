"""Parser XML/XMI genérico para ICDs exportados desde Eclipse EMF.

Diferencias respecto a la versión anterior:

* Sin dependencias externas: usa ``xml.etree.ElementTree`` (stdlib) en lugar
  de BeautifulSoup.
* Genérico: no hay una rama de código por tipo de elemento. Cada elemento XML
  se convierte en un ``ICDNode`` conservando TODOS sus atributos, así que un
  tag o atributo nuevo en producción no requiere tocar el parser.
* Sigue siendo inmune a los prefijos de namespace: los tags y atributos se
  normalizan a su forma con prefijo canónico ('xsi:type', 'xmi:id'...), y los
  namespaces declarados en el archivo se guardan en ``root.nsmap`` para que
  el futuro ICDWriter reserialice con los prefijos originales.

Reglas de interpretación (las únicas tres del formato EMF que necesitamos):

1. Elemento hijo con atributo ``href`` y sin hijos  -> es una referencia
   (``<with href="Base.xmi#_id"/>``, ``<explicitNational_EC href=.../>``).
2. Atributo ``with="_id"``                          -> referencia local.
3. Elemento hijo sin atributos, sin hijos, con texto -> propiedad textual
   (``<NationalExportControl>ES:DUAL</NationalExportControl>``).

Todo lo demás es un nodo normal del árbol.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Dict, Optional

from core.model import ICDNode, Ref, REFERENCE_ATTRS

logger = logging.getLogger("ICDParser")

# Prefijos canónicos para los namespaces estándar, por si el archivo
# los declara con otro alias.
CANONICAL_PREFIXES = {
    "http://www.omg.org/XMI": "xmi",
    "http://www.w3.org/2001/XMLSchema-instance": "xsi",
}


class ICDParser:
    """Convierte archivos XMI de ICD en un árbol de ICDNode."""

    def __init__(self, registry=None) -> None:
        self.registry = registry

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def parse_file(self, file_path: str) -> ICDNode:
        logger.info("Analizando XML aeronáutico: %s", file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return self.parse_string(content, source_file=os.path.basename(file_path))

    def parse_string(self, content: str, source_file: str = "") -> ICDNode:
        nsmap = self._collect_namespaces(content)
        uri_to_prefix = self._build_reverse_map(nsmap)

        try:
            xml_root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError(f"XML mal formado en {source_file or '<string>'}: {exc}") from exc

        root_tag = self._localname(xml_root.tag)
        if root_tag != "Module":
            raise ValueError(
                f"Se esperaba raíz 'Module' y se encontró '{root_tag}' en {source_file or '<string>'}"
            )

        root = self._build_node(xml_root, uri_to_prefix, parent=None, source_file=source_file)
        root.nsmap = nsmap

        if self.registry is not None:
            self.registry.register_tree(root, source_file)
        return root

    # ------------------------------------------------------------------ #
    # Namespaces
    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_namespaces(content: str) -> Dict[str, str]:
        """Extrae las declaraciones xmlns del documento (prefijo -> URI)."""
        nsmap: Dict[str, str] = {}
        for event, payload in ET.iterparse(StringIO(content), events=("start-ns",)):
            prefix, uri = payload
            nsmap.setdefault(prefix, uri)
        return nsmap

    @staticmethod
    def _build_reverse_map(nsmap: Dict[str, str]) -> Dict[str, str]:
        """URI -> prefijo, aplicando prefijos canónicos para xmi/xsi."""
        reverse: Dict[str, str] = {}
        for prefix, uri in nsmap.items():
            reverse.setdefault(uri, prefix)
        reverse.update(CANONICAL_PREFIXES)
        return reverse

    @staticmethod
    def _localname(qname: str) -> str:
        """'{http://...}folder' -> 'folder'."""
        if qname.startswith("{"):
            return qname.split("}", 1)[1]
        return qname.split(":")[-1]

    def _qualify(self, qname: str, uri_to_prefix: Dict[str, str]) -> str:
        """'{uri}type' -> 'xsi:type'; sin namespace queda igual."""
        if not qname.startswith("{"):
            return qname
        uri, local = qname[1:].split("}", 1)
        prefix = uri_to_prefix.get(uri, "")
        return f"{prefix}:{local}" if prefix else local

    # ------------------------------------------------------------------ #
    # Construcción del árbol
    # ------------------------------------------------------------------ #
    def _build_node(
        self,
        elem: ET.Element,
        uri_to_prefix: Dict[str, str],
        parent: Optional[ICDNode],
        source_file: str,
    ) -> ICDNode:
        node = ICDNode(
            tag=self._localname(elem.tag),
            attrs={self._qualify(k, uri_to_prefix): v for k, v in elem.attrib.items()},
            parent=parent,
            source_file=source_file,
        )

        # Referencias declaradas como atributo local (with="_id _id2").
        for attr in REFERENCE_ATTRS:
            raw = node.attrs.get(attr, "")
            for ref_id in raw.split():
                node.refs.append(Ref(tag=attr, ref_id=ref_id, source=node))

        for child in elem:
            tag = self._localname(child.tag)
            child_attrs = {self._qualify(k, uri_to_prefix): v for k, v in child.attrib.items()}
            has_children = len(child) > 0
            text = (child.text or "").strip()

            if "href" in child_attrs and not has_children:
                # Regla 1: referencia (with, explicitNational_EC, ...)
                node.refs.append(
                    Ref(
                        tag=tag,
                        href=child_attrs["href"],
                        xsi_type=child_attrs.get("xsi:type", ""),
                        source=node,
                    )
                )
            elif not child_attrs and not has_children and text:
                # Regla 3: propiedad textual
                node.text_props[tag] = text
            else:
                # Nodo normal: recursión
                node.children.append(
                    self._build_node(child, uri_to_prefix, parent=node, source_file=source_file)
                )

        return node
