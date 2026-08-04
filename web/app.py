"""Interfaz web (NiceGUI) para mostrar y editar ICDs.

Arranque:
    python3 -m web.app            # abre en http://localhost:8080
    python3 -m web.app --folder tests/data   # y carga una carpeta al inicio

La UI es una capa fina sobre WorkSession (web/session.py), que a su vez
envuelve core/. La interfaz nunca toca el modelo directamente.

Estructura de pantalla:
    ┌───────────────────────────────────────────────────────────┐
    │ barra: carpeta · cargar · guardar · estado (errores/avisos)│
    ├───────────────┬───────────────────────────────────────────┤
    │ árbol (lazy)  │ ficha: atributos editables · layout · refs │
    │ + búsqueda    │       · incidencias de la entidad          │
    └───────────────┴───────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import urllib.parse
from typing import Any, Dict, List, Optional

from nicegui import ui

from core import codec
from core.model import (
    ArrayType, Bus, CompositeType, Entity, Field, Message, MessageSlot,
    Module, Network, Port, RecordType, Reference, ScalarType, TextType,
    TypeDef, VariantType,
)
from web.session import WorkSession
from core import views

# Atributos que nunca se muestran/editan en la ficha (navegación interna).
_HIDDEN_ATTRS = {"parent"}
# Iconos por tipo, para el árbol.
_ICONS = {
    "Module": "folder_special",
    "Folder": "folder",
    "ScalarType": "tag",
    "TextType": "text_fields",
    "RecordType": "view_list",
    "ArrayType": "data_array",
    "VariantType": "call_split",
    "Message": "mail",
    "Field": "square",
    "Network": "lan",
    "Port": "settings_ethernet",
    "Bus": "power_input",
    "MessageSlot": "schedule",
}


class ICDApp:
    def __init__(self, session: WorkSession) -> None:
        self.session = session
        # id de nodo -> entidad, y control de qué ramas ya se poblaron.
        self._entity_by_node: Dict[str, Entity] = {}
        self._loaded: set[str] = set()
        self.selected: Optional[Entity] = None
        # Estado de colapso del visor de mensaje (claves de fila colapsadas).
        self._collapsed: set[str] = set()
        self._collapsed_entity: Optional[Entity] = None
        # Historial del navegador: True mientras restauramos desde atrás/adelante
        # (para no re-empujar estados) y último hash empujado (evitar duplicados).
        self._restoring_history = False
        self._last_hash = ""

        # refs a componentes que se refrescan
        self.tree: Optional[ui.tree] = None
        self.status_label: Optional[ui.label] = None
        self.save_btn: Optional[ui.button] = None
        self.detail_view = None  # ui.refreshable, creado en build()

    # ================================================================== #
    # Construcción del árbol (lazy)
    # ================================================================== #
    def _node_id(self, entity: Entity) -> str:
        # id único de nodo: el xmi:id si existe, si no la ruta (campos sin id).
        return entity.id or entity.path

    def _make_shell(self, entity: Entity) -> dict:
        """Nodo del árbol con un placeholder si la entidad tiene hijos."""
        node_id = self._node_id(entity)
        self._entity_by_node[node_id] = entity
        label = entity.name or f"<{type(entity).__name__}>"
        node = {"id": node_id, "label": label, "icon": _ICONS.get(type(entity).__name__, "circle")}
        if entity.children:
            node["children"] = [{"id": node_id + "::loading", "label": "…"}]
        return node

    def _populate(self, node_id: str) -> None:
        """Sustituye el placeholder de un nodo por sus hijos reales."""
        if node_id in self._loaded:
            return
        entity = self._entity_by_node.get(node_id)
        if entity is None:
            return
        children = [self._make_shell(c) for c in entity.children]
        self._loaded.add(node_id)
        # actualizar la estructura de nodos del árbol
        self._replace_children(self.tree.props["nodes"], node_id, children)
        self.tree.update()

    def _replace_children(self, nodes: List[dict], node_id: str, children: List[dict]) -> bool:
        for node in nodes:
            if node["id"] == node_id:
                node["children"] = children
                return True
            if node.get("children") and self._replace_children(node["children"], node_id, children):
                return True
        return False

    def _root_nodes(self) -> List[dict]:
        self._entity_by_node = {}
        self._loaded = set()
        return [self._make_shell(m) for m in self.session.modules]

    def _on_expand(self, event) -> None:
        for node_id in event.value:
            self._populate(node_id)

    # ================================================================== #
    # Historial del navegador (botón atrás/adelante)
    # ================================================================== #
    def _push_history(self, entity: Optional[Entity]) -> None:
        """Empuja la selección al historial (#<id>) para que 'atrás' funcione."""
        if self._restoring_history:
            return
        tag = urllib.parse.quote(self._node_id(entity), safe="") if entity else ""
        if tag == self._last_hash:
            return
        self._last_hash = tag
        if tag:
            ui.run_javascript(f'history.pushState(null, "", "#" + {json.dumps(tag)});')
        else:
            ui.run_javascript('history.pushState(null, "", window.location.pathname);')

    def _handle_history(self, event) -> None:
        """popstate (atrás/adelante) o carga con hash: restaurar la selección."""
        hash_ = ((event.args or {}).get("hash") or "").lstrip("#")
        node_id = urllib.parse.unquote(hash_)
        self._restoring_history = True
        try:
            self._last_hash = urllib.parse.quote(node_id, safe="") if node_id else ""
            if not node_id:
                self.selected = None
                self._render_detail()
                return
            entity = self.session.get(node_id) or self.session.find_by_path(node_id)
            if entity is not None:
                self._goto(entity)
        finally:
            self._restoring_history = False

    def _on_select(self, event) -> None:
        node_id = event.value
        entity = self._entity_by_node.get(node_id) if node_id else None
        self.selected = entity
        self._push_history(entity)
        # Seleccionar un nodo con hijos también lo expande (UX de navegador).
        if entity is not None and entity.children:
            self._populate(node_id)
            expanded = list(self.tree.props.get("expanded") or [])
            if node_id not in expanded:
                expanded.append(node_id)
                self.tree.props["expanded"] = expanded
                self.tree.update()
        self._render_detail()

    # ================================================================== #
    # Panel de detalle
    # ================================================================== #
    def _editable_fields(self, entity: Entity) -> List[dataclasses.Field]:
        out = []
        for f in dataclasses.fields(entity):
            if f.name in _HIDDEN_ATTRS:
                continue
            value = getattr(entity, f.name)
            if isinstance(value, (str, int, float, bool)):
                out.append(f)
        return out

    def _render_detail(self) -> None:
        """Refresca el panel de detalle (seguro desde cualquier handler)."""
        if self.detail_view is not None:
            self.detail_view.refresh()

    def _render_detail_body(self) -> None:
        entity = self.selected
        with ui.column().classes("w-full gap-2"):
            if entity is None:
                ui.label("Selecciona una entidad en el árbol.").classes("text-grey")
                return

            ui.label(f"{type(entity).__name__}  ·  {entity.path}").classes(
                "text-sm text-grey-7"
            )
            with ui.row().classes("items-center w-full"):
                ui.label(entity.name or "(sin nombre)").classes("text-h6")
                ui.space()
                self._action_bar(entity)

            # --- atributos editables ---
            with ui.card().classes("w-full"):
                ui.label("Atributos").classes("text-bold")
                with ui.grid(columns=2).classes("w-full gap-2"):
                    for f in self._editable_fields(entity):
                        self._attr_input(entity, f)

            # --- referencias ---
            if self.session.reference_slots(entity) or entity.references():
                with ui.card().classes("w-full"):
                    ui.label("Referencias").classes("text-bold")
                    self._render_references(entity)

            # --- visor específico según el tipo de entidad ---
            if isinstance(entity, Message):
                self._message_viewer(entity)
                self._codec_panel(entity)
            elif isinstance(entity, TypeDef):
                self._type_viewer(entity)
                if isinstance(entity, (RecordType, VariantType)):
                    self._codec_panel(entity)
            elif isinstance(entity, Field) and isinstance(entity.datatype, CompositeType) \
                    and not isinstance(entity.datatype, ArrayType):
                self._codec_panel(entity)
            elif callable(getattr(entity, "describe", None)):
                with ui.card().classes("w-full"):
                    ui.label("Layout").classes("text-bold")
                    ui.code(entity.describe()).classes("w-full").style("white-space:pre-wrap")

            # --- incidencias de esta entidad y su subárbol ---
            issues = self.session.issues_for(entity)
            if issues:
                with ui.card().classes("w-full"):
                    ui.label(f"Incidencias ({len(issues)})").classes("text-bold")
                    for issue in issues:
                        self._issue_row(issue, entity)

    def _action_bar(self, entity: Entity) -> None:
        """Botones de crear hijo / borrar para la entidad seleccionada."""
        allowed = self.session.allowed_children(entity)
        if allowed:
            with ui.button("Añadir", icon="add").props("dense"):
                with ui.menu() as menu:
                    for label, cls in allowed.items():
                        ui.menu_item(label, on_click=lambda c=cls: self._add_child(entity, c, menu))
        if entity.parent is not None and not isinstance(entity, Module):
            ui.button("Borrar", icon="delete", color="negative",
                      on_click=lambda: self._delete(entity)).props("dense")

    def _add_child(self, parent: Entity, cls: type, menu) -> None:
        menu.close()
        self._create_child(parent, cls)

    def _create_child(self, parent: Entity, cls: type) -> None:
        try:
            new = self.session.add_child(parent, cls)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        self._rebuild_branch(parent)
        self._refresh_status()
        self._goto(new)
        ui.notify(f"Creado {cls.__name__} '{new.name}'", type="positive")

    def _delete(self, entity: Entity) -> None:
        parent = entity.parent
        dangling = self.session.delete(entity)
        self._rebuild_branch(parent)
        self.selected = parent
        self._refresh_status()
        self._render_detail()
        msg = f"Borrado '{entity.name}'"
        if dangling:
            msg += f" · {len(dangling)} referencias quedaron sin resolver"
        ui.notify(msg, type="warning" if dangling else "positive")

    def _rebuild_branch(self, parent: Entity) -> None:
        """Repuebla los hijos de 'parent' en el árbol tras crear/borrar."""
        node_id = self._node_id(parent)
        self._loaded.discard(node_id)
        # el nodo padre debe tener el arreglo de hijos actualizado
        children = [self._make_shell(c) for c in parent.children]
        self._loaded.add(node_id)
        if not self._replace_children(self.tree.props["nodes"], node_id, children):
            # el padre es una raíz (módulo): reconstruir nodos raíz
            self.tree.props["nodes"] = self._root_nodes_keeping()
        self.tree.update()

    def _root_nodes_keeping(self) -> list:
        return [self._make_shell(m) for m in self.session.modules]

    _GRID_COLS = ("minmax(180px,2.2fr) 80px 110px minmax(140px,1.6fr) "
                  "120px minmax(150px,1.8fr) 60px 56px")
    _GRID_HEADERS = ("Campo", "length (bit)", "max_position", "Tipo",
                     "Codificación", "Escalado", "Cond.", "Ref")

    def _message_viewer(self, message: Message) -> None:
        """Visor de mensaje: el mensaje completo aplanado, colapsable, con enlaces."""
        rows = views.message_rows(message)
        # reiniciar el estado de colapso al cambiar de mensaje
        if self._collapsed_entity is not message:
            self._collapsed = set()
            self._collapsed_entity = message
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label("Visor de mensaje").classes("text-bold")
                ui.space()
                st = message.structure
                ui.label(f"{len(rows)} campos · payload: {st.name if st else '(sin resolver)'}") \
                    .classes("text-sm text-grey")
                if isinstance(st, CompositeType):
                    ui.button("Editar campos", icon="edit",
                              on_click=lambda s=st: self._goto(s)).props("flat dense") \
                        .tooltip("ir a la estructura del payload para añadir/borrar campos")
            if not rows:
                ui.label("El mensaje no tiene payload resuelto.").classes("text-negative")
                return
            self._render_field_grid(rows)

    # ================================================================== #
    # Panel de codificación/decodificación (usa core/codec.py)
    # ================================================================== #
    @staticmethod
    def _parse_scalar_str(s: str) -> Any:
        """'0x1234' -> 4660, '10.5' -> 10.5, 'DOWN' -> 'DOWN'."""
        try:
            return int(s, 0)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s

    @staticmethod
    def _codec_target(entity: Entity) -> Optional[CompositeType]:
        """El tipo compuesto sobre el que opera el panel, según la entidad."""
        if isinstance(entity, Message):
            st = entity.structure
            return st if isinstance(st, CompositeType) else None
        if isinstance(entity, Field):
            dt = entity.datatype
            return dt if isinstance(dt, CompositeType) else None
        if isinstance(entity, CompositeType):
            return entity
        return None

    def _codec_panel(self, entity: Entity) -> None:
        comp = self._codec_target(entity)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label("Codificar / Decodificar").classes("text-bold")
                if comp is not None and comp is not entity:
                    ui.label(f"({type(comp).__name__} '{comp.name}')") \
                        .classes("text-sm text-grey")
                ui.space()
                eng = ui.switch("unidades de ingeniería", value=True).props("dense")
            if comp is None:
                ui.label("Sin estructura resuelta que codificar.").classes("text-negative")
                return
            if isinstance(comp, ArrayType):
                ui.label("Array variable: codifícalo a través de la estructura "
                         "que lo contiene.").classes("text-grey")
                return

            # Dos vistas de los mismos valores: tabla campo a campo y JSON.
            with ui.tabs().props("dense") as tabs:
                tab_table = ui.tab("Tabla")
                tab_json = ui.tab("JSON")
            rows: List[Dict[str, Any]] = []
            with ui.tab_panels(tabs, value=tab_table).classes("w-full"):
                with ui.tab_panel(tab_table).classes("p-0"):
                    self._build_root_rows(comp, rows)
                with ui.tab_panel(tab_json).classes("p-0"):
                    json_area = ui.textarea("valores (JSON)").props("outlined dense") \
                        .classes("w-full").style("font-family:monospace;min-height:120px;")

            hex_in = ui.input("bytes (hex)").props("outlined dense") \
                .classes("w-full").style("font-family:monospace;")
            result_box = ui.column().classes("w-full")

            def gather() -> Dict[str, Any]:
                # tabs.value es el objeto tab al crear, o su nombre tras un clic
                if tabs.value in (tab_json, "JSON"):
                    return json.loads(json_area.value or "{}")
                return self._gather_rows(rows)

            def show(values: Dict[str, Any]) -> None:
                """Refleja los valores en la tabla, el JSON y el resultado."""
                self._fill_rows(rows, values)
                json_area.set_value(json.dumps(values, indent=2, ensure_ascii=False))
                result_box.clear()
                with result_box:
                    ui.code(json.dumps(values, indent=2, ensure_ascii=False)).classes("w-full")

            def do_encode() -> None:
                try:
                    values = gather()
                    data = codec.encode_type(comp, values, engineering=eng.value)
                except (codec.CodecError, ValueError, json.JSONDecodeError) as exc:
                    ui.notify(f"Error al codificar: {exc}", type="negative")
                    return
                hex_in.set_value(data.hex(" ").upper())
                # sincronizar la otra vista con lo codificado
                json_area.set_value(json.dumps(values, indent=2, ensure_ascii=False))
                ui.notify(f"{len(data)} bytes", type="positive")

            def do_decode() -> None:
                raw = (hex_in.value or "").replace(" ", "").replace("\n", "")
                # el caso de una variante raíz se toma de la fila/JSON '_case'
                case = None
                try:
                    case = gather().get("_case")
                except (ValueError, json.JSONDecodeError):
                    pass
                try:
                    data = bytes.fromhex(raw)
                    values = codec.decode_type(
                        comp, data, engineering=eng.value,
                        case=str(case) if case is not None else None)
                except (codec.CodecError, ValueError) as exc:
                    ui.notify(f"Error al decodificar: {exc}", type="negative")
                    return
                show(values)

            with ui.row().classes("gap-2"):
                ui.button("Codificar →", icon="arrow_downward", on_click=do_encode) \
                    .props("dense")
                ui.button("← Decodificar", icon="arrow_upward", on_click=do_decode) \
                    .props("dense outline")

    def _build_root_rows(self, comp: CompositeType, rows: List[Dict[str, Any]]) -> None:
        """Filas de la tabla para el compuesto raíz del panel.

        Para una variante raíz: campos comunes + fila '_case' (selector del
        caso) + fila 'value' (contenido del caso, JSON parcial)."""
        if isinstance(comp, VariantType):
            common = [f for f in comp.fields if not f.is_conditional]
            for f in common:
                self._codec_field_row(f, rows, prefix="", level=0)
            cases = ", ".join(f.condition for f in comp.cases) or "—"
            with ui.row().classes("items-center w-full no-wrap gap-2"):
                case_in = ui.input("_case", placeholder=f"casos: {cases}") \
                    .props("dense outlined").classes("w-full").style("max-width:460px;")
            rows.append({"path": "_case", "input": case_in, "kind": "scalar"})
            with ui.row().classes("items-center w-full no-wrap gap-2"):
                ui.label("value").classes("text-sm").style("width:180px;")
                val_in = ui.input(placeholder='JSON del caso: {"campo": ...}') \
                    .props("dense outlined").classes("w-full") \
                    .style("font-family:monospace;max-width:460px;")
            rows.append({"path": "value", "input": val_in, "kind": "json"})
            return
        self._build_codec_rows(comp, rows, prefix="", level=0)

    # -- tabla de campos: una fila por campo hoja -------------------------- #
    def _build_codec_rows(self, comp: CompositeType, rows: List[Dict[str, Any]],
                          prefix: str, level: int) -> None:
        """Construye las filas de la tabla. Los registros anidados se
        despliegan; los arrays/variantes son una fila con valor JSON parcial."""
        for f in comp.fields:
            self._codec_field_row(f, rows, prefix, level)

    def _codec_field_row(self, f: Field, rows: List[Dict[str, Any]],
                         prefix: str, level: int) -> None:
        dt = f.datatype
        name = f.name or "(campo)"
        path = f"{prefix}.{f.name}" if prefix else f.name
        pad = level * 16
        if isinstance(dt, (ArrayType, VariantType)) or dt is None:
            hint = ('[{...}, ...]' if isinstance(dt, ArrayType)
                    else '{"_case": "1", "value": {...}}' if isinstance(dt, VariantType)
                    else "sin resolver")
            with ui.row().classes("items-center w-full no-wrap gap-2") \
                    .style(f"padding-left:{pad}px;"):
                ui.label(name).classes("text-sm").style("width:180px;")
                inp = ui.input(placeholder=f"JSON: {hint}").props("dense outlined") \
                    .classes("w-full").style("font-family:monospace;max-width:460px;")
            rows.append({"path": path, "input": inp, "kind": "json"})
        elif isinstance(dt, CompositeType):
            ui.label(name).classes("text-bold text-sm").style(f"margin-left:{pad}px;")
            self._build_codec_rows(dt, rows, path, level + 1)
        else:
            hint = dt.summary()
            with ui.row().classes("items-center w-full no-wrap gap-2") \
                    .style(f"padding-left:{pad}px;"):
                inp = ui.input(name, placeholder=hint).props("dense outlined") \
                    .classes("w-full").style("max-width:460px;")
            rows.append({"path": path, "input": inp, "kind": "scalar"})

    def _gather_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for row in rows:
            s = str(row["input"].value or "").strip()
            if s == "":
                continue
            value = json.loads(s) if row["kind"] == "json" else self._parse_scalar_str(s)
            node = out
            parts = row["path"].split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return out

    def _fill_rows(self, rows: List[Dict[str, Any]], values: Dict[str, Any]) -> None:
        for row in rows:
            node: Any = values
            for part in row["path"].split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is None:
                row["input"].set_value("")
            elif row["kind"] == "json":
                row["input"].set_value(json.dumps(node, ensure_ascii=False))
            else:
                row["input"].set_value(str(node))

    def _render_field_grid(self, rows) -> None:
        by_key = {r.key: r for r in rows}
        with ui.element("div").classes("w-full").style(
                f"display:grid;grid-template-columns:{self._GRID_COLS};align-items:center;"):
            for h in self._GRID_HEADERS:
                ui.label(h).classes("text-xs text-bold").style(
                    "padding:4px 6px;border-bottom:1px solid rgba(0,0,0,.25);")
            for r in rows:
                if self._row_hidden(r, by_key):
                    continue
                self._render_field_row(r)

    def _row_hidden(self, r, by_key) -> bool:
        p = r.parent_key
        while p:
            if p in self._collapsed:
                return True
            parent = by_key.get(p)
            p = parent.parent_key if parent else ""
        return False

    def _cell(self, text: str, right: bool = False) -> None:
        ui.label(text or "").classes("text-xs" + (" text-right" if right else "")).style(
            "padding:3px 6px;border-bottom:1px solid rgba(0,0,0,.06);"
            "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;")

    def _render_field_row(self, r) -> None:
        # celda del nombre: sangría por nivel + toggle si tiene hijos
        with ui.row().classes("items-center no-wrap").style(
                f"padding:1px 6px 1px {6 + r.level * 16}px;"
                "border-bottom:1px solid rgba(0,0,0,.06);gap:2px;"):
            if r.has_children:
                collapsed = r.key in self._collapsed
                ui.button(icon="chevron_right" if collapsed else "expand_more",
                          on_click=lambda k=r.key: self._toggle_collapse(k)) \
                    .props("flat dense round size=sm")
            else:
                ui.element("div").style("width:24px;")
            name_label = ui.label(r.name or "—").classes("text-xs")
            if r.description:
                name_label.tooltip(r.description)
                ui.icon("info", size="14px").classes("text-grey-5").tooltip(r.description)
        self._cell(r.length, right=True)
        self._cell(r.position, right=True)
        self._cell(r.type_name)
        self._cell(r.coding)
        self._cell(r.scaling)
        self._cell(r.condition)
        # celda de enlace a la referencia (si la hay y está resuelta)
        if r.ref_id:
            with ui.element("div").style("border-bottom:1px solid rgba(0,0,0,.06);text-align:center;"):
                ui.button(icon="north_east", on_click=lambda i=r.ref_id: self._goto_id(i)) \
                    .props("flat dense round size=sm").tooltip("ir a la referencia")
        else:
            ui.element("div").style("border-bottom:1px solid rgba(0,0,0,.06);")

    def _issue_row(self, issue, current: Entity) -> None:
        """Una incidencia con enlace a la entidad afectada."""
        color = "negative" if issue.level == "ERROR" else "warning"
        target = self.session.resolve_issue_target(issue)
        with ui.row().classes("items-center w-full no-wrap gap-2"):
            ui.icon("error" if issue.level == "ERROR" else "warning", color=color, size="18px")
            with ui.column().classes("gap-0"):
                ui.label(issue.message).classes(f"text-{color} text-sm")
                # ruta relativa a la entidad actual, para ubicar la incidencia
                if issue.path != current.path:
                    ui.label(issue.path).classes("text-xs text-grey-6")
            ui.space()
            # enlace: solo si la entidad afectada existe y no es la ya seleccionada
            if target is not None and target is not current:
                ui.button(icon="north_east", on_click=lambda t=target: self._goto(t)) \
                    .props("flat dense round size=sm").tooltip("ir al elemento")

    def _toggle_collapse(self, key: str) -> None:
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._render_detail()

    def _goto_id(self, entity_id: str) -> None:
        target = self.session.get(entity_id)
        if target is not None:
            self._goto(target)
        else:
            ui.notify("La referencia no está cargada", type="warning")

    def _type_viewer(self, typedef: TypeDef) -> None:
        """Visor de tipo: propiedades + escalado editable + campos editables."""
        view = views.type_view(typedef)
        with ui.card().classes("w-full"):
            ui.label(f"Visor de tipo · {view['kind']}").classes("text-bold")
            if view["props"]:
                ui.table(
                    columns=[{"name": "prop", "label": "Propiedad", "field": "prop", "align": "left"},
                             {"name": "valor", "label": "Valor", "field": "valor", "align": "left"}],
                    rows=view["props"], row_key="prop",
                ).classes("w-full").props("dense flat bordered hide-header")

        if isinstance(typedef, ScalarType):
            self._scaling_editor(typedef)
        if isinstance(typedef, CompositeType):
            self._composite_grid(typedef)
            self._fields_editor(typedef)

    # ---- editor de escalado -------------------------------------------- #
    _SCALING_KINDS = {"none": "Sin escalado", "linear": "Lineal", "enum": "Enum", "lut": "LUT"}

    @staticmethod
    def _scaling_kind(scalar: ScalarType) -> str:
        from core.model import EnumScaling, LinearScaling, LUTScaling
        s = scalar.scaling
        if isinstance(s, LinearScaling):
            return "linear"
        if isinstance(s, EnumScaling):
            return "enum"
        if isinstance(s, LUTScaling):
            return "lut"
        return "none"

    def _scaling_editor(self, scalar: ScalarType) -> None:
        from core.model import EnumScaling, LinearScaling, LUTScaling
        kind = self._scaling_kind(scalar)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label("Escalado").classes("text-bold")
                ui.space()
                sel = ui.select(self._SCALING_KINDS, value=kind).props("dense outlined").classes("w-40")
                sel.on_value_change(lambda e: self._change_scaling_kind(scalar, e.value))

            s = scalar.scaling
            if isinstance(s, LinearScaling):
                with ui.row().classes("items-center gap-3"):
                    self._scaling_num(scalar, "lsb", s.lsb, "lsb")
                    self._scaling_num(scalar, "offset", s.offset, "offset")
                    self._scaling_txt(scalar, "units", s.units, "unidades")
                ui.markdown(f"`valor = crudo × {s.lsb:g}"
                            + (f" + {s.offset:g}" if s.offset else "") + "`").classes("text-grey")
            elif isinstance(s, EnumScaling):
                self._scaling_txt(scalar, "units", s.units, "unidades")
                for i, (val, lab) in enumerate(list(s.labels.items())):
                    self._enum_row(scalar, i, val, lab)
                ui.button("Añadir estado", icon="add",
                          on_click=lambda: self._add_enum(scalar)).props("flat dense")
            elif isinstance(s, LUTScaling):
                self._scaling_txt(scalar, "units", s.units, "unidades")
                with ui.row().classes("items-center gap-2 text-xs text-grey"):
                    for h in ("desde", "hasta", "lsb", "offset", ""):
                        ui.label(h).style("width:88px;")
                for i, r in enumerate(s.ranges):
                    self._lut_row(scalar, i, r)
                ui.button("Añadir tramo", icon="add",
                          on_click=lambda: self._add_lut(scalar)).props("flat dense")

    def _scaling_num(self, scalar, attr, value, label):
        inp = ui.number(label, value=value, format="%g").props("dense outlined").classes("w-28")
        inp.on("blur", lambda _=None, c=inp: self._commit_scaling(scalar, attr, c.value))

    def _scaling_txt(self, scalar, attr, value, label):
        inp = ui.input(label, value=value).props("dense outlined").classes("w-28")
        inp.on("blur", lambda _=None, c=inp: self._commit_scaling(scalar, attr, c.value))

    def _enum_row(self, scalar, index, value, label):
        with ui.row().classes("items-center gap-2"):
            vi = ui.input("valor", value=value).props("dense outlined").classes("w-24")
            li = ui.input("estado", value=label).props("dense outlined").classes("w-48")
            commit = lambda _=None, v=vi, l=li: self._commit_enum(scalar, index, v.value, l.value)
            vi.on("blur", commit)
            li.on("blur", commit)
            ui.button(icon="delete", color="negative",
                      on_click=lambda: self._remove_enum(scalar, index)).props("flat dense round")

    def _lut_row(self, scalar, index, r):
        with ui.row().classes("items-center gap-2"):
            for attr, val in (("begin", r.begin), ("end", r.end), ("lsb", r.lsb), ("offset", r.offset)):
                inp = ui.number(value=val, format="%g").props("dense outlined").style("width:88px;")
                inp.on("blur", lambda _=None, a=attr, c=inp: self._commit_lut(scalar, index, a, c.value))
            ui.button(icon="delete", color="negative",
                      on_click=lambda: self._remove_lut(scalar, index)).props("flat dense round")

    # commits: inline (sin reconstruir, para no perder el foco) vs estructurales
    def _commit_scaling(self, scalar, attr, value):
        self.session.edit_scaling_attr(scalar, attr, str(value))
        self._refresh_status()

    def _commit_enum(self, scalar, index, value, label):
        self.session.set_enum_row(scalar, index, str(value), str(label))
        self._refresh_status()

    def _commit_lut(self, scalar, index, attr, value):
        self.session.set_lut_cell(scalar, index, attr, str(value))
        self._refresh_status()

    def _change_scaling_kind(self, scalar, kind):
        self.session.set_scaling_kind(scalar, kind)
        self._refresh_status(); self._render_detail()

    def _add_enum(self, scalar):
        self.session.add_enum_label(scalar); self._refresh_status(); self._render_detail()

    def _remove_enum(self, scalar, index):
        self.session.remove_enum_row(scalar, index); self._refresh_status(); self._render_detail()

    def _add_lut(self, scalar):
        self.session.add_lut_range(scalar); self._refresh_status(); self._render_detail()

    def _remove_lut(self, scalar, index):
        self.session.remove_lut_range(scalar, index); self._refresh_status(); self._render_detail()

    def _composite_grid(self, comp: CompositeType) -> None:
        """La misma tabla del visor de mensaje, para un tipo compuesto:
        campos con length/max_position/codificación/escalado, subestructuras
        colapsables y enlaces a las referencias."""
        rows = views.flatten(comp)
        if not rows:
            return
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label("Vista de campos").classes("text-bold")
                ui.space()
                ui.label(f"{len(rows)} filas").classes("text-sm text-grey")
            if self._collapsed_entity is not comp:
                self._collapsed = set()
                self._collapsed_entity = comp
            self._render_field_grid(rows)

    # ---- editor de campos de un compuesto ------------------------------ #
    def _fields_editor(self, comp: CompositeType) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full"):
                ui.label(f"Campos ({len(comp.fields)})").classes("text-bold")
                ui.space()
                ui.button("Añadir campo", icon="add",
                          on_click=lambda: self._create_child(comp, Field)).props("flat dense")
            if not comp.fields:
                ui.label("Sin campos.").classes("text-grey")
            for f in comp.fields:
                with ui.row().classes("items-center w-full gap-2"):
                    dt = f.datatype
                    label = f.name or "(campo)"
                    detail = f" · {type(dt).__name__} {dt.name}" if dt else ""
                    ui.icon("square", size="14px").classes("text-grey-5")
                    ui.button(f"{label}{detail}", on_click=lambda e=f: self._goto(e)) \
                        .props("flat dense align-left").classes("normal-case")
                    ui.space()
                    ui.button(icon="delete", color="negative",
                              on_click=lambda e=f: self._delete(e)).props("flat dense round") \
                        .tooltip("borrar campo")

    def _attr_input(self, entity: Entity, f: dataclasses.Field) -> None:
        value = getattr(entity, f.name)
        label = f.name
        if isinstance(value, bool):
            comp = ui.checkbox(label, value=value)
            comp.on_value_change(lambda e, a=f.name: self._apply(entity, a, e.value))
        else:
            comp = ui.input(label, value=str(value))
            comp.on(
                "blur",
                lambda _=None, a=f.name, c=comp: self._apply(entity, a, c.value),
            )
        comp.classes("w-full")

    def _render_references(self, entity: Entity) -> None:
        """Referencias editables: cambiar destino, limpiar, o asignar si falta."""
        slots = self.session.reference_slots(entity)
        if not slots:
            # entidades con referencias no editables (p. ej. no en _REFERENCE_SLOTS)
            for ref in entity.references():
                self._ref_row_readonly(ref)
            return
        for attr, role, ref in slots:
            with ui.row().classes("items-center gap-2 w-full"):
                if ref is not None and ref.is_resolved:
                    ui.icon("link", color="positive")
                    ui.label(f"{role} → {ref.target.name}  [{ref.href}]")
                    ui.button("ir", on_click=lambda t=ref.target: self._goto(t)).props("flat dense")
                elif ref is not None:
                    ui.icon("link_off", color="negative")
                    ui.label(f"{role} → {ref.href}  (sin resolver)").classes("text-negative")
                else:
                    ui.icon("link_off", color="grey")
                    ui.label(f"{role}: sin asignar").classes("text-grey")
                ui.space()
                ui.button(icon="edit", on_click=lambda e=entity, a=attr: self._open_ref_picker(e, a)).props("flat dense round")
                if ref is not None:
                    ui.button(icon="delete", color="negative",
                              on_click=lambda e=entity, a=attr: self._clear_ref(e, a)).props("flat dense round")

    def _ref_row_readonly(self, ref: Reference) -> None:
        with ui.row().classes("items-center gap-2"):
            if ref.is_resolved:
                ui.icon("link", color="positive")
                ui.label(f"{ref.role} → {ref.target.name}  [{ref.href}]")
                ui.button("ir", on_click=lambda t=ref.target: self._goto(t)).props("flat dense")
            else:
                ui.icon("link_off", color="negative")
                ui.label(f"{ref.role} → {ref.href}  (sin resolver)").classes("text-negative")

    def _clear_ref(self, entity: Entity, attr: str) -> None:
        self.session.clear_reference(entity, attr)
        self._refresh_status()
        self._render_detail()

    def _open_ref_picker(self, entity: Entity, attr: str) -> None:
        """Diálogo con búsqueda para elegir la entidad destino de la referencia."""
        with ui.dialog() as dialog, ui.card().classes("w-96"):
            ui.label(f"Asignar referencia · {attr}").classes("text-bold")
            search = ui.input("buscar entidad por nombre").props("dense autofocus clearable").classes("w-full")
            results = ui.column().classes("w-full")

            def do_search():
                results.clear()
                with results:
                    matches = self.session.search(search.value or "")
                    if not matches:
                        ui.label("sin coincidencias").classes("text-grey")
                    for target in matches[:40]:
                        def pick(t=target):
                            self.session.set_reference(entity, attr, t)
                            dialog.close()
                            self._refresh_status()
                            self._render_detail()
                            ui.notify(f"Referencia → {t.name}", type="positive")
                        ui.button(f"{type(target).__name__}: {target.name}  ({target.path})",
                                  on_click=pick).props("flat dense align-left").classes("w-full")
            search.on("keydown.enter", lambda _: do_search())
            search.on("input", lambda _: do_search())
            ui.button("cerrar", on_click=dialog.close).props("flat")
        dialog.open()

    def _goto(self, entity: Entity) -> None:
        self.selected = entity
        self._push_history(entity)
        node_id = self._node_id(entity)
        # asegurar que la rama está expandida hasta la entidad
        chain = []
        node = entity
        while node is not None:
            chain.append(self._node_id(node))
            node = node.parent
        for nid in reversed(chain):
            self._populate(nid)
        self.tree.props["expanded"] = chain
        self.tree.props["selected"] = node_id
        self.tree.update()
        self._render_detail()

    def _apply(self, entity: Entity, attr: str, raw_value) -> None:
        try:
            self.session.edit_attribute(entity, attr, str(raw_value))
        except (ValueError, TypeError) as exc:
            ui.notify(f"Valor inválido para {attr}: {exc}", type="negative")
            return
        # editar el nombre cambia la etiqueta del árbol
        if attr == "name":
            self._entity_by_node.get(self._node_id(entity))  # (id no cambia)
        self.session.revalidate()
        self._refresh_status()
        self._render_detail()

    # ================================================================== #
    # Barra de estado / carga / guardado
    # ================================================================== #
    def _refresh_status(self) -> None:
        s = self.session
        text = (
            f"{len(s.modules)} módulos · {len(s._index)} entidades · "
            f"refs {s.resolved}✓/{s.unresolved}✗ · "
            f"{s.error_count} errores, {s.warning_count} avisos"
        )
        if s.dirty:
            text += "  · cambios sin guardar"
        self.status_label.set_text(text)
        # el botón Guardar resalta cuando hay cambios pendientes
        if self.save_btn is not None:
            self.save_btn.props(f"color={'orange' if s.dirty else 'primary'}")
            self.save_btn.set_text("Guardar *" if s.dirty else "Guardar")

    def _reload_tree(self, message: str) -> None:
        if self.session.load_errors:
            ui.notify(f"{len(self.session.load_errors)} ficheros con error", type="warning")
        self.tree.props["nodes"] = self._root_nodes()
        self.tree.props["expanded"] = []
        self.tree.update()
        self.selected = None
        self._collapsed_entity = None
        self._render_detail()
        self._refresh_status()
        ui.notify(message, type="positive")

    def _open_json(self, folder: str) -> None:
        """Abrir un proyecto guardado en JSON (modo de trabajo normal)."""
        if not folder:
            ui.notify("Indica la carpeta del proyecto JSON", type="warning")
            return
        self.session.open_json(folder)
        if not self.session.modules and not self.session.load_errors:
            ui.notify(f"No hay .json en '{folder}'. ¿Importar XML primero?", type="warning")
        self._reload_tree(f"Proyecto abierto desde {folder}")

    def _import_xml(self, xml_folder: str, json_folder: str) -> None:
        """Importar XML (una vez) y persistirlo como proyecto JSON."""
        if not xml_folder or not json_folder:
            ui.notify("Indica la carpeta XML y la de proyecto JSON", type="warning")
            return
        written = self.session.import_xml(xml_folder, json_folder)
        self._reload_tree(f"XML importado y guardado como JSON ({len(written)} módulos) en {json_folder}")

    def _open_codegen(self) -> None:
        """Diálogo de generación de código: lenguaje, carpeta y resultados."""
        if not self.session.modules:
            ui.notify("Carga un proyecto antes de generar código", type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("w-[560px]"):
            ui.label("Generar código").classes("text-bold")
            with ui.row().classes("items-center gap-3 w-full"):
                lang = ui.select({"python": "Python", "ada": "Ada 95"},
                                 value="python", label="lenguaje") \
                    .props("dense outlined").classes("w-40")
                out_in = ui.input("carpeta de salida", value="gen") \
                    .props("dense outlined").classes("w-64")
            results_box = ui.column().classes("w-full")

            def do_generate() -> None:
                results_box.clear()
                results, errors = self.session.generate_code(
                    out_in.value or "gen", language=lang.value)
                total = sum(len(r.files) for r in results)
                with results_box:
                    ui.label(f"{len(results)} módulos generados · {total} ficheros "
                             f"en '{out_in.value}'").classes("text-positive")
                    for r in results:
                        with ui.expansion(f"{r.package} — {len(r.files)} ficheros"
                                          + (f" · {len(r.warnings)} avisos" if r.warnings else "")) \
                                .classes("w-full"):
                            for path in sorted(r.files):
                                ui.label(path).classes("text-xs").style("font-family:monospace;")
                            for w in r.warnings:
                                ui.label(f"AVISO: {w}").classes("text-warning text-xs")
                    for name, reason in errors:
                        with ui.expansion(f"✗ {name} — no generado").classes("w-full"):
                            ui.label(reason).classes("text-negative text-xs") \
                                .style("white-space:pre-wrap;")
                if errors:
                    ui.notify(f"{len(errors)} módulos con errores de validación",
                              type="warning")
                else:
                    ui.notify("Generación completada", type="positive")

            with ui.row().classes("gap-2"):
                ui.button("Generar", icon="code", on_click=do_generate).props("dense")
                ui.button("cerrar", on_click=dialog.close).props("flat dense")
        dialog.open()

    def _save(self) -> None:
        if not self.session.modules:
            ui.notify("No hay nada que guardar", type="warning")
            return
        if not self.session.project_dir:
            ui.notify("Abre o importa un proyecto antes de guardar", type="warning")
            return
        written = self.session.save_project()
        self._refresh_status()
        ui.notify(f"Guardados {len(written)} módulos en {self.session.project_dir}", type="positive")

    # ================================================================== #
    # Montaje de la página
    # ================================================================== #
    def build(self) -> None:
        # El botón atrás/adelante del navegador restaura la selección, y una
        # URL con #<id> restaura la entidad al (re)cargar la página.
        ui.add_body_html("""<script>
            window.addEventListener('popstate', () => {
                emitEvent('icd_nav', {hash: window.location.hash});
            });
            window.addEventListener('load', () => {
                if (window.location.hash) {
                    emitEvent('icd_nav', {hash: window.location.hash});
                }
            });
        </script>""")
        ui.on("icd_nav", self._handle_history)

        with ui.header().classes("items-center gap-2"):
            ui.label("ICDMS").classes("text-h6")
            # --- proyecto JSON: abrir y guardar (modo de trabajo normal) ---
            proj_in = ui.input("proyecto (JSON)", value="project_json") \
                .props("dense dark").classes("w-48")
            ui.button("Abrir", icon="folder_open",
                      on_click=lambda: self._open_json(proj_in.value)).props("dense")
            self.save_btn = ui.button("Guardar", icon="save", on_click=lambda: self._save()) \
                .props("dense")
            ui.separator().props("vertical dark")
            # --- importar XML (una sola vez) -> se guarda como JSON ---
            xml_in = ui.input("importar XML", value="tests/data") \
                .props("dense dark").classes("w-40")
            ui.button("Importar", icon="upload_file",
                      on_click=lambda: self._import_xml(xml_in.value, proj_in.value)) \
                .props("dense outline").tooltip("Cargar XML una vez y guardarlo como JSON de proyecto")
            ui.separator().props("vertical dark")
            ui.button("Generar código", icon="code", on_click=self._open_codegen) \
                .props("dense outline")
            ui.space()
            self.status_label = ui.label("").classes("text-sm")

        with ui.splitter(value=35).classes("w-full").style("height: calc(100vh - 90px)") as splitter:
            with splitter.before:
                with ui.column().classes("w-full p-2 gap-2"):
                    search = ui.input("buscar por nombre", ).props("dense clearable").classes("w-full")
                    results = ui.column().classes("w-full")

                    def do_search():
                        results.clear()
                        with results:
                            for entity in self.session.search(search.value or ""):
                                ui.button(
                                    f"{type(entity).__name__}: {entity.name}",
                                    on_click=lambda e=entity: self._goto(e),
                                ).props("flat dense align-left").classes("w-full")
                    search.on("keydown.enter", lambda _: do_search())

                    self.tree = ui.tree(
                        [], node_key="id", label_key="label", children_key="children",
                        on_select=self._on_select, on_expand=self._on_expand,
                    ).classes("w-full")
                    self.tree.add_slot("default-header", r'''
                        <div class="row items-center">
                          <q-icon :name="props.node.icon || 'circle'" size="18px" class="q-mr-xs" />
                          <span>{{ props.node.label }}</span>
                        </div>
                    ''')
            with splitter.after:
                with ui.column().classes("w-full p-3"):
                    self.detail_view = ui.refreshable(self._render_detail_body)
                    self.detail_view()

        self._refresh_status()


def create_app(json_folder: Optional[str] = None, xml_folder: Optional[str] = None) -> WorkSession:
    session = WorkSession()
    # Al iniciar: abrir el proyecto JSON si existe; si no, importar el XML.
    if json_folder:
        session.open_json(json_folder)
    if not session.modules and xml_folder:
        session.import_xml(xml_folder, json_folder or "project_json")

    @ui.page("/")
    def index() -> None:
        app = ICDApp(session)
        app.build()
        if session.modules:
            app.tree.props["nodes"] = app._root_nodes()
            app.tree.update()
            app._refresh_status()

    return session


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz web para ICDs (NiceGUI).")
    parser.add_argument("--json", help="carpeta de proyecto JSON a abrir al iniciar")
    parser.add_argument("--import-xml", dest="xml", help="carpeta XML a importar si no hay JSON")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    create_app(args.json, args.xml)
    ui.run(port=args.port, title="ICDMS", reload=False, show=False)


# ui.run debe ejecutarse a nivel de módulo cuando se lanza con python -m
if __name__ in {"__main__", "__mp_main__"}:
    main()
