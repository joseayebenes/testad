"""Registro global de entidades ICD.

Indexa todas las entidades por id (globalmente y por archivo de origen) y
resuelve los ``Reference`` del modelo: enlaces locales (``with="_id"``) y
entre archivos (``href="BaseSignals.xmi#_sig_alt"``).

``resolve_references()`` devuelve un ``ResolutionReport`` con lo resuelto y
lo pendiente (p. ej. archivos aún no cargados), pensado para mostrarse en la
futura UI, no solo para el log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.model import Entity, Module, Reference

logger = logging.getLogger("ICDRegistry")


@dataclass
class ResolutionReport:
    resolved: int = 0
    unresolved: List[Tuple[Reference, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def summary(self) -> str:
        lines = [f"Referencias resueltas: {self.resolved}"]
        for ref, reason in self.unresolved:
            owner = ref.owner.path if ref.owner else "?"
            lines.append(f"  SIN RESOLVER [{owner}] {ref.role} -> {ref.href}: {reason}")
        return "\n".join(lines)


class ICDRegistry:
    def __init__(self) -> None:
        # Índice global id -> entidad (el primero gana; duplicados anotados).
        self._by_id: Dict[str, Entity] = {}
        # Índice por archivo, para hrefs cualificados con nombre de archivo.
        self._by_file: Dict[str, Dict[str, Entity]] = {}
        self._modules: Dict[str, Module] = {}
        self.duplicate_ids: List[str] = []

    # ------------------------------------------------------------------ #
    # Registro
    # ------------------------------------------------------------------ #
    def register_module(self, module: Module) -> None:
        """Registra un módulo completo (raíz y todos sus descendientes)."""
        self._modules[module.name] = module
        file_index = self._by_file.setdefault(module.source_file, {})
        for entity in module.walk():
            if not entity.id:
                continue
            if entity.id in self._by_id and self._by_id[entity.id] is not entity:
                self.duplicate_ids.append(entity.id)
                logger.warning("id duplicado entre archivos: %s", entity.id)
            else:
                self._by_id[entity.id] = entity
            file_index[entity.id] = entity

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #
    def get(self, entity_id: str, source_file: str = "") -> Optional[Entity]:
        """Busca por id; si se indica archivo, ese índice tiene prioridad."""
        if source_file and source_file in self._by_file:
            entity = self._by_file[source_file].get(entity_id)
            if entity is not None:
                return entity
        return self._by_id.get(entity_id)

    def get_module(self, name: str) -> Optional[Module]:
        return self._modules.get(name)

    @property
    def modules(self) -> List[Module]:
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
            for entity in module.walk():
                for ref in entity.references():
                    if ref.is_resolved:
                        report.resolved += 1
                        continue
                    if not ref.target_id:
                        report.unresolved.append((ref, "referencia vacía"))
                        continue
                    target = self.get(ref.target_id, source_file=ref.file)
                    if target is not None:
                        ref.target = target
                        report.resolved += 1
                    else:
                        reason = f"id '{ref.target_id}' no encontrado"
                        if ref.file and ref.file not in self._by_file:
                            reason += f" (archivo '{ref.file}' no cargado)"
                        report.unresolved.append((ref, reason))
                        logger.error(
                            "Fallo al enlazar %s '%s' en '%s': %s",
                            ref.role, ref.href, entity.path, reason,
                        )
        return report
