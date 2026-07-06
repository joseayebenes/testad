"""Validación del modelo ICD: detecta y registra problemas de definición.

Recorre un módulo completo y comprueba, por tipo de entidad, los campos
obligatorios y las incoherencias típicas de ingeniería:

* Escalares sin longitud en bits, texto sin tamaño, arrays sin contador...
* Variantes sin discriminador o con condiciones duplicadas.
* Campos vacíos (sin referencia ni definición inline) o solapados.
* Mensajes sin payload, periódicos sin periodo, ranuras sin mensaje.
* Ids duplicados dentro del módulo y referencias sin resolver.

Cada problema se emite por el log (ERROR/WARNING) y se acumula en una lista
de ``Issue``, pensada para mostrarse en la futura UI.

Uso:
    issues = validate_module(module)          # tras resolve_references()
    print(summarize(issues))
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List

from core.model import (
    ArrayType, Bus, Entity, Field, Folder, Message, MessageSlot, Module,
    Network, Port, RecordType, ScalarType, TextType, TypeDef, VariantType,
)

logger = logging.getLogger("ICDValidator")

ERROR = "ERROR"
WARNING = "WARNING"


@dataclass
class Issue:
    level: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.path}: {self.message}"


def summarize(issues: List[Issue]) -> str:
    if not issues:
        return "Validación correcta: sin problemas."
    errors = sum(1 for i in issues if i.level == ERROR)
    warnings = len(issues) - errors
    lines = [f"Validación: {errors} errores, {warnings} avisos"]
    lines += [f"  {i}" for i in issues]
    return "\n".join(lines)


class Validator:
    def __init__(self) -> None:
        self.issues: List[Issue] = []

    # ------------------------------------------------------------------ #
    def _add(self, level: str, entity: Entity, message: str) -> None:
        issue = Issue(level, entity.path, message)
        self.issues.append(issue)
        log = logger.error if level == ERROR else logger.warning
        log("%s: %s", issue.path, issue.message)

    def error(self, entity: Entity, message: str) -> None:
        self._add(ERROR, entity, message)

    def warning(self, entity: Entity, message: str) -> None:
        self._add(WARNING, entity, message)

    # ------------------------------------------------------------------ #
    def validate(self, module: Module) -> List[Issue]:
        self.issues = []
        self._check_duplicate_ids(module)
        for entity in module.walk():
            self._check_entity(entity)
        self._check_references(module)
        return self.issues

    # ------------------------------------------------------------------ #
    # Comprobaciones transversales
    # ------------------------------------------------------------------ #
    def _check_duplicate_ids(self, module: Module) -> None:
        by_id: Dict[str, List[Entity]] = {}
        for entity in module.walk():
            if entity.id:
                by_id.setdefault(entity.id, []).append(entity)
        for entity_id, entities in by_id.items():
            if len(entities) > 1:
                for e in entities[1:]:
                    self.error(e, f"id duplicado en el módulo: '{entity_id}'")

    def _check_references(self, module: Module) -> None:
        for entity in module.walk():
            for ref in entity.references():
                if not ref.target_id:
                    self.error(entity, f"referencia '{ref.role}' vacía (sin destino)")
                elif not ref.is_resolved:
                    self.error(
                        entity,
                        f"referencia '{ref.role}' sin resolver -> {ref.href} "
                        "(¿archivo no cargado o id inexistente?)",
                    )

    # ------------------------------------------------------------------ #
    # Comprobaciones por tipo de entidad
    # ------------------------------------------------------------------ #
    def _check_entity(self, e: Entity) -> None:
        # Las definiciones compartidas (hijas de carpeta) deben tener id y nombre.
        if isinstance(e.parent, Folder):
            if not e.id:
                self.warning(e, "definición sin id (no podrá ser referenciada)")
            if not e.name and not isinstance(e, Network):
                self.warning(e, "definición sin nombre")

        if isinstance(e, ScalarType):
            self._check_scalar(e)
        elif isinstance(e, TextType):
            self._check_text(e)
        elif isinstance(e, ArrayType):
            self._check_array(e)
        elif isinstance(e, VariantType):
            self._check_variant(e)
        elif isinstance(e, RecordType):
            self._check_record(e)
        elif isinstance(e, Field):
            self._check_field(e)
        elif isinstance(e, Message):
            self._check_message(e)
        elif isinstance(e, MessageSlot):
            self._check_slot(e)
        elif isinstance(e, Network):
            self._check_network(e)
        elif isinstance(e, Port):
            self._check_port(e)

    def _check_scalar(self, e: ScalarType) -> None:
        if e.bit_length <= 0:
            self.error(e, "señal sin longitud en bits (atributo obligatorio)")
        if not e.encoding:
            self.warning(e, "señal sin codificación definida")

    def _check_text(self, e: TextType) -> None:
        if e.max_chars <= 0:
            self.error(e, "texto sin longitud definida (textLength)")
        if not e.length_mode:
            self.warning(e, "texto sin modo de longitud (fixed/variable)")

    def _check_array(self, e: ArrayType) -> None:
        if not e.counter_type and e.counter_bits <= 0:
            self.error(e, "array variable sin contador definido")
        if e.max_count <= 0:
            self.warning(e, "array variable sin longitud máxima")
        if not e.fields:
            self.warning(e, "array variable sin definición del elemento")

    def _check_record(self, e: RecordType) -> None:
        if not e.fields:
            self.warning(e, "registro sin campos")
        positions = Counter(
            (f.position.word16, f.position.bit16) for f in e.fields
        )
        for (word, bit), count in positions.items():
            if count > 1:
                self.warning(
                    e, f"{count} campos solapados en la posición w16 {word}:{bit}"
                )

    def _check_variant(self, e: VariantType) -> None:
        if not e.discriminator:
            self.error(e, "variante sin campo discriminador (key)")
        if not e.cases:
            self.warning(e, "variante sin casos definidos")
        conditions = Counter(f.condition for f in e.cases)
        for condition, count in conditions.items():
            if count > 1:
                self.error(
                    e, f"condición duplicada '{condition}' en {count} casos de la variante"
                )

    def _check_field(self, e: Field) -> None:
        if e.ref is None and e.inline is None:
            self.error(e, "campo vacío: sin referencia ni definición inline")
        if e.condition and not isinstance(e.parent, VariantType):
            self.warning(e, "campo con condición fuera de una variante")

    def _check_message(self, e: Message) -> None:
        if e.payload is None and e.body is None:
            self.error(e, "mensaje sin payload (ni referencia ni campos propios)")
        if e.rate_mode.lower() == "periodic" and e.period <= 0:
            self.warning(e, "mensaje periódico sin periodo definido")

    def _check_slot(self, e: MessageSlot) -> None:
        if e.message is None:
            self.error(e, "ranura de bus sin mensaje asignado")

    def _check_network(self, e: Network) -> None:
        if not e.ports and not e.buses:
            self.warning(e, "red sin puertos ni buses")

    def _check_port(self, e: Port) -> None:
        if e.number <= 0:
            self.warning(e, "puerto sin número asignado")


def validate_module(module: Module) -> List[Issue]:
    """Atajo: valida un módulo y devuelve la lista de problemas (ya logados)."""
    return Validator().validate(module)
