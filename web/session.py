"""Estado de una sesión de trabajo de la web (independiente de NiceGUI).

Envuelve el registro del modelo y ofrece las operaciones de alto nivel que la
UI necesita —cargar carpeta, resolver, validar, editar un atributo, guardar—
sin que la interfaz toque nunca ``core`` directamente. Así la lógica es
testeable sin levantar el navegador.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Dict, List, Optional

from core.model import Entity, Module
from core.parser import ICDParser
from core.persistence import load_module, save_module
from core.registry import ICDRegistry
from core.validation import Issue, validate_module

logger = logging.getLogger("ICDWebSession")

LOAD_PATTERNS = ("*.module", "*.xmi", "*.xml")


class WorkSession:
    """Un conjunto de módulos cargados sobre el que se navega y edita."""

    def __init__(self) -> None:
        self.registry = ICDRegistry()
        self.issues: List[Issue] = []
        self.resolved = 0
        self.unresolved = 0
        self.load_errors: List[str] = []
        # id -> entidad, para localizar rápido desde el árbol de la UI.
        self._index: Dict[str, Entity] = {}
        self.dirty = False

    # ------------------------------------------------------------------ #
    # Carga
    # ------------------------------------------------------------------ #
    def load_folder(self, folder: str) -> None:
        """Reemplaza la sesión por los ICD encontrados en la carpeta."""
        self.__init__()  # sesión limpia
        parser = ICDParser(self.registry)
        files: List[str] = []
        for pattern in LOAD_PATTERNS:
            files.extend(glob.glob(os.path.join(folder, "**", pattern), recursive=True))
        for path in sorted(set(files)):
            try:
                parser.parse_file(path)
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(f"{os.path.basename(path)}: {exc}")
                logger.error("Error parseando %s: %s", path, exc)
        self._finish_load()

    def load_json_files(self, paths: List[str]) -> None:
        self.__init__()
        for path in paths:
            try:
                load_module(path, self.registry)
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(f"{os.path.basename(path)}: {exc}")
        self._finish_load()

    def _finish_load(self) -> None:
        report = self.registry.resolve_references()
        self.resolved = report.resolved
        self.unresolved = len(report.unresolved)
        self._reindex()
        self.revalidate()

    def _reindex(self) -> None:
        self._index = {}
        for module in self.registry.modules:
            for entity in module.walk():
                if entity.id:
                    self._index[entity.id] = entity

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #
    @property
    def modules(self) -> List[Module]:
        return sorted(self.registry.modules, key=lambda m: m.name)

    def get(self, entity_id: str) -> Optional[Entity]:
        return self._index.get(entity_id)

    def search(self, needle: str, limit: int = 50) -> List[Entity]:
        needle = needle.lower().strip()
        if not needle:
            return []
        out = []
        for entity in self._index.values():
            if needle in entity.name.lower():
                out.append(entity)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------------------ #
    # Edición
    # ------------------------------------------------------------------ #
    def edit_attribute(self, entity: Entity, attr: str, raw_value: str) -> None:
        """Asigna un valor respetando el tipo del atributo del dataclass."""
        current = getattr(entity, attr)
        if isinstance(current, bool):
            value = raw_value.strip().lower() in ("true", "1", "sí", "si", "yes")
        elif isinstance(current, int):
            value = int(raw_value) if raw_value.strip() else 0
        elif isinstance(current, float):
            value = float(raw_value) if raw_value.strip() else 0.0
        else:
            value = raw_value
        setattr(entity, attr, value)
        self.dirty = True

    def revalidate(self) -> None:
        self.issues = []
        for module in self.registry.modules:
            self.issues.extend(validate_module(module))

    def issues_for(self, entity: Entity) -> List[Issue]:
        prefix = entity.path
        return [i for i in self.issues if i.path == prefix or i.path.startswith(prefix + "/")]

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "WARNING")

    # ------------------------------------------------------------------ #
    # Guardado
    # ------------------------------------------------------------------ #
    def save_all_json(self, folder: str) -> List[str]:
        os.makedirs(folder, exist_ok=True)
        written = []
        for module in self.registry.modules:
            path = os.path.join(folder, f"{module.name}.json")
            save_module(module, path)
            written.append(path)
        self.dirty = False
        return written
