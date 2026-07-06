"""Registro global de entidades ICD.

Indexa todos los nodos por xmi:id (globalmente y por archivo) y resuelve las
referencias débiles del modelo:

* ``<with href="BaseSignals.xmi#_sig_alt"/>``  — referencia entre archivos.
* ``with="_sig_speed"``                        — referencia local por atributo.

``resolve_references()`` devuelve un informe con lo resuelto y lo pendiente,
en lugar de solo loguear errores, para que la futura UI pueda mostrarlo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.model import ICDNode, Ref

logger = logging.getLogger("ICDRegistry")


@dataclass
class ResolutionReport:
    resolved: int = 0
    unresolved: List[Tuple[Ref, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def summary(self) -> str:
        lines = [f"Referencias resueltas: {self.resolved}"]
        for ref, reason in self.unresolved:
            owner = ref.source.path if ref.source else "?"
            lines.append(f"  SIN RESOLVER [{owner}] {ref.tag} -> {ref.href or ref.ref_id}: {reason}")
        return "\n".join(lines)


class ICDRegistry:
    def __init__(self) -> None:
        # id -> nodo, índice global (primer registro gana; los duplicados se anotan)
        self._by_id: Dict[str, ICDNode] = {}
        # archivo -> (id -> nodo), para resolver hrefs con nombre de archivo
        self._by_file: Dict[str, Dict[str, ICDNode]] = {}
        # nombre de módulo -> nodo raíz
        self._modules: Dict[str, ICDNode] = {}
        self.duplicate_ids: List[str] = []

    # ------------------------------------------------------------------ #
    # Registro
    # ------------------------------------------------------------------ #
    def register_tree(self, root: ICDNode, source_file: str = "") -> None:
        """Registra un módulo completo (raíz + todos sus descendientes)."""
        fname = source_file or root.source_file
        if root.kind == "Module":
            self._modules[root.name] = root

        file_index = self._by_file.setdefault(fname, {})
        for node in root.walk():
            node_id = node.id
            if not node_id:
                continue
            if node_id in self._by_id and self._by_id[node_id] is not node:
                self.duplicate_ids.append(node_id)
                logger.warning("xmi:id duplicado entre archivos: %s", node_id)
            else:
                self._by_id[node_id] = node
            file_index[node_id] = node

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #
    def get(self, xmi_id: str, source_file: str = "") -> Optional[ICDNode]:
        """Busca por id; si se indica archivo, ese índice tiene prioridad."""
        if source_file and source_file in self._by_file:
            node = self._by_file[source_file].get(xmi_id)
            if node is not None:
                return node
        return self._by_id.get(xmi_id)

    def get_module(self, name: str) -> Optional[ICDNode]:
        return self._modules.get(name)

    @property
    def modules(self) -> List[ICDNode]:
        return list(self._modules.values())

    @property
    def files(self) -> List[str]:
        return list(self._by_file.keys())

    def __len__(self) -> int:
        return len(self._by_id)

    # ------------------------------------------------------------------ #
    # Resolución de referencias
    # ------------------------------------------------------------------ #
    def resolve_references(self) -> ResolutionReport:
        report = ResolutionReport()
        for module in self._modules.values():
            for node in module.walk():
                for ref in node.refs:
                    if ref.is_resolved:
                        report.resolved += 1
                        continue
                    target_id = ref.target_id
                    if not target_id:
                        report.unresolved.append((ref, "referencia vacía"))
                        continue
                    target = self.get(target_id, source_file=ref.target_file)
                    if target is not None:
                        ref.target = target
                        report.resolved += 1
                    else:
                        reason = (
                            f"id '{target_id}' no encontrado"
                            + (f" (archivo '{ref.target_file}' no cargado)"
                               if ref.target_file and ref.target_file not in self._by_file
                               else "")
                        )
                        report.unresolved.append((ref, reason))
                        logger.error(
                            "Fallo al enlazar %s '%s' en '%s': %s",
                            ref.tag, ref.href or ref.ref_id, node.path, reason,
                        )
        return report
