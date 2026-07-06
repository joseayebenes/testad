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

from core.model import (
    ArrayType, Bus, CompositeType, Entity, Field, Folder, Message,
    MessageSlot, Module, Network, Port, RecordType, Reference, ScalarType,
    TextType, TypeDef, VariantType,
)
from core.parser import ICDParser
from core.persistence import load_module, save_module
from core.registry import ICDRegistry
from core.validation import Issue, validate_module

logger = logging.getLogger("ICDWebSession")

LOAD_PATTERNS = ("*.module", "*.xmi", "*.xml")

# Tipos de hijo que se pueden crear bajo cada tipo de contenedor (label -> clase).
_FOLDER_CHILDREN = {
    "Carpeta": Folder,
    "Señal (ScalarType)": ScalarType,
    "Texto (TextType)": TextType,
    "Registro (RecordType)": RecordType,
    "Array variable (ArrayType)": ArrayType,
    "Variante (VariantType)": VariantType,
    "Mensaje (Message)": Message,
}
# Atributo que guarda cada referencia editable, con su 'role'.
_REFERENCE_SLOTS = {
    Field: [("ref", "with")],
    Message: [("payload", "with")],
    MessageSlot: [("message", "with")],
    Module: [("explicit_national_ec", "explicitNational_EC"),
             ("explicit_us_ec", "explicitUS_EC")],
}


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

    # ------------------------------------------------------------------ #
    # Crear / borrar entidades
    # ------------------------------------------------------------------ #
    @staticmethod
    def root_module(entity: Entity) -> Optional[Module]:
        node: Optional[Entity] = entity
        while node is not None:
            if isinstance(node, Module):
                return node
            node = node.parent
        return None

    def allowed_children(self, parent: Entity) -> Dict[str, type]:
        """Tipos de entidad que se pueden crear bajo 'parent' (label -> clase)."""
        if isinstance(parent, Module):
            return {**_FOLDER_CHILDREN, "Red (Network)": Network}
        if isinstance(parent, Folder):
            return dict(_FOLDER_CHILDREN)
        if isinstance(parent, CompositeType):  # Record / Array / Variant
            return {"Campo (Field)": Field}
        if isinstance(parent, Network):
            return {"Puerto (Port)": Port, "Bus": Bus}
        if isinstance(parent, Bus):
            return {"Ranura (MessageSlot)": MessageSlot}
        return {}

    def _new_id(self) -> str:
        n = 1
        while f"_ui_{n}" in self._index or self.registry.get(f"_ui_{n}"):
            n += 1
        return f"_ui_{n}"

    def add_child(self, parent: Entity, cls: type) -> Entity:
        """Crea una entidad del tipo dado y la engancha bajo 'parent'."""
        entity = cls()
        entity.id = self._new_id()
        entity.name = f"nuevo_{cls.__name__}"
        entity.parent = parent

        if isinstance(parent, (Folder, Module)):
            if cls is Folder:
                parent.folders.append(entity)
            elif cls is Message:
                parent.messages.append(entity)
            elif cls is Network and isinstance(parent, Module):
                parent.networks.append(entity)
            else:
                parent.types.append(entity)
        elif isinstance(parent, CompositeType) and cls is Field:
            parent.fields.append(entity)
        elif isinstance(parent, Network):
            (parent.ports if cls is Port else parent.buses).append(entity)
        elif isinstance(parent, Bus) and cls is MessageSlot:
            parent.slots.append(entity)
        else:
            raise ValueError(f"No se puede crear {cls.__name__} bajo {type(parent).__name__}")

        root = self.root_module(parent)
        if root is not None:
            self.registry.index_entity(entity, root.source_file)
        self._index[entity.id] = entity
        self.dirty = True
        self.revalidate()
        return entity

    def _child_lists(self, parent: Entity) -> List[List[Entity]]:
        lists = []
        for attr in ("folders", "types", "messages", "networks", "fields", "ports", "buses", "slots"):
            if hasattr(parent, attr):
                lists.append(getattr(parent, attr))
        return lists

    def delete(self, entity: Entity) -> List[Reference]:
        """Borra una entidad de su padre. Devuelve las referencias que quedan
        colgando (apuntaban a ella) para poder avisar."""
        parent = entity.parent
        if parent is None or isinstance(entity, Module):
            raise ValueError("No se puede borrar el módulo raíz")

        removed = False
        for lst in self._child_lists(parent):
            if entity in lst:
                lst.remove(entity)
                removed = True
                break
        if not removed:
            raise ValueError("La entidad no cuelga de su padre (estado inconsistente)")

        dangling = self.referrers(entity)
        for ref in dangling:
            ref.target = None

        root = self.root_module(parent)
        if root is not None:
            self.registry.deindex(entity, root.source_file)
        self._reindex()
        self.dirty = True
        self.revalidate()
        return dangling

    # ------------------------------------------------------------------ #
    # Referencias
    # ------------------------------------------------------------------ #
    def reference_slots(self, entity: Entity) -> List[tuple]:
        """(atributo, role, referencia_actual_o_None) editables de la entidad."""
        for cls, slots in _REFERENCE_SLOTS.items():
            if isinstance(entity, cls):
                return [(attr, role, getattr(entity, attr)) for attr, role in slots]
        return []

    def set_reference(self, entity: Entity, attr: str, target: Entity) -> None:
        role = next((r for a, r, _ in self.reference_slots(entity) if a == attr), "with")
        ref = getattr(entity, attr)
        if ref is None:
            ref = Reference(role=role, owner=entity)
            setattr(entity, attr, ref)
        owner_module = self.root_module(entity)
        target_module = self.root_module(target)
        ref.file = (target_module.source_file
                    if target_module is not None and target_module is not owner_module else "")
        ref.target_id = target.id
        ref.target = target
        self.dirty = True
        self.revalidate()

    def clear_reference(self, entity: Entity, attr: str) -> None:
        setattr(entity, attr, None)
        self.dirty = True
        self.revalidate()

    def referrers(self, entity: Entity) -> List[Reference]:
        """Todas las referencias del modelo que apuntan a 'entity'."""
        out = []
        for module in self.registry.modules:
            for other in module.walk():
                for ref in other.references():
                    if ref.target is entity or (ref.target_id and ref.target_id == entity.id):
                        out.append(ref)
        return out

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
