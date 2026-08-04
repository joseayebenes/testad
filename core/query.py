"""API de consulta del modelo ICD, orientada a agentes y herramientas.

Devuelve estructuras JSON-serializables (dicts/listas de tipos básicos) con
la información de mensajes, campos y tipos. Es la capa que expone el
servidor MCP (mcp_server.py), y es utilizable directamente desde scripts.

Resolución de nombres: los métodos aceptan el `xmi:id` o el nombre de la
entidad (exacto, o único ignorando mayúsculas). Si el nombre es ambiguo se
devuelve un error con los candidatos, en vez de elegir uno en silencio.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional

from core import codec, views
from core.model import (
    ArrayType, CompositeType, Entity, Field, Message, Module, RecordType,
    ScalarType, TextType, TypeDef, VariantType,
)
from core.parser import ICDParser
from core.persistence import load_module
from core.registry import ICDRegistry
from core.validation import validate_module

XML_PATTERNS = ("*.module", "*.xmi", "*.xml")


class QueryError(Exception):
    """Consulta inválida: entidad no encontrada, ambigua o mal formada."""


class ICDQuery:
    """Modelo ICD cargado, consultable por nombre o id."""

    def __init__(self, folder: Optional[str] = None) -> None:
        self.folder = ""
        self.registry = ICDRegistry()
        self.load_errors: List[str] = []
        self._issues_cache: Optional[List[Dict[str, str]]] = None
        if folder:
            self.load(folder)

    # ------------------------------------------------------------------ #
    # Carga
    # ------------------------------------------------------------------ #
    def load(self, folder: str) -> Dict[str, Any]:
        """Carga un proyecto JSON o, si no hay .json, los XML de la carpeta."""
        if not os.path.isdir(folder):
            raise QueryError(f"'{folder}' no es una carpeta")
        self.folder = folder
        self.registry = ICDRegistry()
        self.load_errors = []
        self._issues_cache = None

        json_files = sorted(glob.glob(os.path.join(folder, "*.json")))
        if json_files:
            for path in json_files:
                try:
                    load_module(path, self.registry)
                except Exception as exc:  # noqa: BLE001
                    self.load_errors.append(f"{os.path.basename(path)}: {exc}")
            source = "json"
        else:
            parser = ICDParser(self.registry)
            paths: List[str] = []
            for pattern in XML_PATTERNS:
                paths.extend(glob.glob(os.path.join(folder, "**", pattern),
                                       recursive=True))
            for path in sorted(set(paths)):
                try:
                    parser.parse_file(path)
                except Exception as exc:  # noqa: BLE001
                    self.load_errors.append(f"{os.path.basename(path)}: {exc}")
            source = "xml"

        report = self.registry.resolve_references()
        return {
            "folder": folder,
            "source": source,
            "modules": [m.name for m in self._modules()],
            "entities": len(self.registry),
            "references_resolved": report.resolved,
            "references_unresolved": len(report.unresolved),
            "load_errors": self.load_errors,
        }

    def _modules(self) -> List[Module]:
        return sorted(self.registry.modules, key=lambda m: m.name)

    def _require_loaded(self) -> None:
        if not self.registry.modules:
            raise QueryError(
                "no hay ningún ICD cargado; usa load(folder) con la carpeta "
                "del proyecto (JSON) o de los XML")

    # ------------------------------------------------------------------ #
    # Resolución de entidades
    # ------------------------------------------------------------------ #
    def _all(self, cls=None) -> List[Entity]:
        out: List[Entity] = []
        for module in self._modules():
            for entity in module.walk():
                if cls is None or isinstance(entity, cls):
                    out.append(entity)
        return out

    def _resolve(self, ref: str, cls=None, what: str = "entidad") -> Entity:
        self._require_loaded()
        if not ref:
            raise QueryError(f"indica el nombre o id de {'la ' + what}")
        by_id = self.registry.get(ref)
        if by_id is not None and (cls is None or isinstance(by_id, cls)):
            return by_id
        candidates = [e for e in self._all(cls) if e.name == ref]
        if not candidates:
            needle = ref.lower()
            candidates = [e for e in self._all(cls) if e.name.lower() == needle]
        if not candidates:
            partial = [e for e in self._all(cls) if ref.lower() in e.name.lower()]
            hint = ("; ¿quizá " + ", ".join(sorted({e.name for e in partial})[:5]) + "?"
                    if partial else "")
            raise QueryError(f"no se encontró {what} '{ref}'{hint}")
        if len(candidates) > 1:
            paths = ", ".join(sorted(e.path for e in candidates)[:8])
            raise QueryError(
                f"'{ref}' es ambiguo ({len(candidates)} coincidencias): {paths}. "
                "Usa el id o la ruta completa.")
        return candidates[0]

    @staticmethod
    def _module_of(entity: Entity) -> str:
        node: Optional[Entity] = entity
        while node is not None:
            if isinstance(node, Module):
                return node.name
            node = node.parent
        return ""

    # ------------------------------------------------------------------ #
    # Listados
    # ------------------------------------------------------------------ #
    def modules(self) -> List[Dict[str, Any]]:
        self._require_loaded()
        out = []
        for m in self._modules():
            entities = list(m.walk())
            out.append({
                "name": m.name,
                "id": m.id,
                "source_file": m.source_file,
                "messages": sum(1 for e in entities if isinstance(e, Message)),
                "types": sum(1 for e in entities if isinstance(e, TypeDef)),
                "entities": len(entities),
                "national_export_control": m.national_export_control,
                "us_export_control": m.us_export_control,
            })
        return out

    def messages(self, module: Optional[str] = None,
                 name_contains: Optional[str] = None,
                 limit: int = 200) -> List[Dict[str, Any]]:
        self._require_loaded()
        out = []
        for msg in self._all(Message):
            mod = self._module_of(msg)
            if module and mod.lower() != module.lower():
                continue
            if name_contains and name_contains.lower() not in msg.name.lower():
                continue
            st = msg.structure
            out.append({
                "name": msg.name,
                "id": msg.id,
                "module": mod,
                "path": msg.path,
                "period": msg.period,
                "rate_mode": msg.rate_mode,
                "payload": st.name if st is not None else None,
                "resolved": st is not None,
                "description": msg.description,
            })
            if len(out) >= limit:
                break
        return out

    def types(self, module: Optional[str] = None, kind: Optional[str] = None,
              name_contains: Optional[str] = None,
              limit: int = 200) -> List[Dict[str, Any]]:
        """Tipos definidos (ScalarType, TextType, RecordType, ArrayType, VariantType)."""
        self._require_loaded()
        out = []
        for t in self._all(TypeDef):
            mod = self._module_of(t)
            if module and mod.lower() != module.lower():
                continue
            if kind and type(t).__name__.lower() != kind.lower():
                continue
            if name_contains and name_contains.lower() not in t.name.lower():
                continue
            entry: Dict[str, Any] = {
                "name": t.name,
                "id": t.id,
                "kind": type(t).__name__,
                "module": mod,
                "path": t.path,
                "description": t.description,
            }
            if isinstance(t, ScalarType):
                entry["bits"] = t.bit_length
                entry["encoding"] = t.encoding
                entry["units"] = t.units
            elif isinstance(t, TextType):
                entry["chars"] = t.max_chars
            elif isinstance(t, CompositeType):
                entry["fields"] = len(t.fields)
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Busca entidades cuyo nombre contenga 'query' (o por id exacto)."""
        self._require_loaded()
        needle = (query or "").lower().strip()
        if not needle:
            raise QueryError("la búsqueda no puede estar vacía")
        out = []
        for entity in self._all():
            if needle in entity.name.lower() or entity.id == query:
                out.append({
                    "name": entity.name,
                    "id": entity.id,
                    "kind": type(entity).__name__,
                    "module": self._module_of(entity),
                    "path": entity.path,
                })
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------------------ #
    # Detalle
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            out.append({
                "field": r.name,
                "level": r.level,
                "length_bits": int(r.length) if r.length.isdigit() else r.length,
                "max_position": int(r.position) if r.position.isdigit() else r.position,
                "type": r.type_name,
                "encoding": r.coding,
                "scaling": r.scaling,
                "condition": r.condition,
                "origin": r.note,
                "reference_id": r.ref_id,
                "description": r.description,
            })
        return out

    def message(self, name_or_id: str) -> Dict[str, Any]:
        """Mensaje completo con sus campos aplanados (incluye subestructuras)."""
        msg: Message = self._resolve(name_or_id, Message, "mensaje")  # type: ignore
        st = msg.structure
        rows = views.message_rows(msg)
        total_bits = codec.type_bit_length(st) if isinstance(st, CompositeType) else 0
        return {
            "name": msg.name,
            "id": msg.id,
            "module": self._module_of(msg),
            "path": msg.path,
            "period": msg.period,
            "rate_mode": msg.rate_mode,
            "description": msg.description,
            "payload": st.name if st is not None else None,
            "payload_kind": type(st).__name__ if st is not None else None,
            "total_bits": total_bits,
            "total_bytes": (total_bits + 7) // 8,
            "bit_numbering": "1-based, MSB-first",
            "fields": self._rows_to_dicts(rows),
        }

    def type(self, name_or_id: str) -> Dict[str, Any]:
        """Cómo se decodifica un tipo: propiedades, escalado y campos."""
        t: TypeDef = self._resolve(name_or_id, TypeDef, "tipo")  # type: ignore
        view = views.type_view(t)
        out: Dict[str, Any] = {
            "name": t.name,
            "id": t.id,
            "kind": view["kind"],
            "module": self._module_of(t),
            "path": t.path,
            "description": t.description,
            "properties": {p["prop"]: p["valor"] for p in view["props"]},
            "scaling": view["scaling"],
        }
        if isinstance(t, CompositeType):
            out["fields"] = self._rows_to_dicts(views.flatten(t))
            try:
                bits = codec.type_bit_length(t)
                out["total_bits"] = bits
                out["total_bytes"] = (bits + 7) // 8
            except codec.CodecError as exc:
                out["total_bits"] = None
                out["note"] = str(exc)
        return out

    def field(self, message_or_type: str, field_name: str) -> Dict[str, Any]:
        """Un campo concreto dentro de un mensaje o tipo compuesto."""
        entity = self._resolve(message_or_type, None, "mensaje o tipo")
        if isinstance(entity, Message):
            rows = views.message_rows(entity)
        elif isinstance(entity, CompositeType):
            rows = views.flatten(entity)
        else:
            raise QueryError(
                f"'{message_or_type}' es {type(entity).__name__}, no un mensaje "
                "ni un tipo compuesto")
        needle = field_name.lower()
        matches = [r for r in self._rows_to_dicts(rows)
                   if (r["field"] or "").lower() == needle]
        if not matches:
            names = ", ".join(sorted({r.name for r in rows if r.name})[:12])
            raise QueryError(
                f"'{message_or_type}' no tiene el campo '{field_name}'. "
                f"Campos: {names}")
        result = dict(matches[0])
        result["container"] = entity.name
        return result

    # ------------------------------------------------------------------ #
    # Codificación / decodificación
    # ------------------------------------------------------------------ #
    def decode(self, name_or_id: str, data_hex: str,
               engineering: bool = True,
               case: Optional[str] = None) -> Dict[str, Any]:
        """Decodifica bytes (hex) a valores de campo."""
        entity = self._resolve(name_or_id, None, "mensaje o tipo")
        raw = "".join((data_hex or "").split()).replace("0x", "")
        try:
            data = bytes.fromhex(raw)
        except ValueError as exc:
            raise QueryError(f"hex inválido: {exc}") from exc
        try:
            if isinstance(entity, Message):
                values = codec.decode_message(entity, data, engineering=engineering,
                                              case=case)
            elif isinstance(entity, CompositeType):
                values = codec.decode_type(entity, data, engineering=engineering,
                                           case=case)
            else:
                raise QueryError(
                    f"'{name_or_id}' es {type(entity).__name__}: no decodificable")
        except codec.CodecError as exc:
            raise QueryError(str(exc)) from exc
        return {
            "name": entity.name,
            "bytes": len(data),
            "engineering": engineering,
            "values": values,
        }

    def encode(self, name_or_id: str, values: Dict[str, Any],
               engineering: bool = True) -> Dict[str, Any]:
        """Codifica valores de campo a bytes (hex)."""
        entity = self._resolve(name_or_id, None, "mensaje o tipo")
        try:
            if isinstance(entity, Message):
                data = codec.encode_message(entity, values, engineering=engineering)
            elif isinstance(entity, CompositeType):
                data = codec.encode_type(entity, values, engineering=engineering)
            else:
                raise QueryError(
                    f"'{name_or_id}' es {type(entity).__name__}: no codificable")
        except codec.CodecError as exc:
            raise QueryError(str(exc)) from exc
        return {
            "name": entity.name,
            "bytes": len(data),
            "engineering": engineering,
            "hex": data.hex(" ").upper(),
        }

    # ------------------------------------------------------------------ #
    # Validación
    # ------------------------------------------------------------------ #
    def issues(self, module: Optional[str] = None, level: Optional[str] = None,
               limit: int = 200) -> List[Dict[str, str]]:
        self._require_loaded()
        if self._issues_cache is None:
            cached = []
            for m in self._modules():
                for issue in validate_module(m):
                    cached.append({
                        "level": issue.level,
                        "path": issue.path,
                        "message": issue.message,
                        "entity_id": issue.entity_id,
                        "module": issue.path.split("/")[0],
                    })
            self._issues_cache = cached
        out = self._issues_cache
        if module:
            out = [i for i in out if i["module"].lower() == module.lower()]
        if level:
            out = [i for i in out if i["level"].lower() == level.lower()]
        return out[:limit]
