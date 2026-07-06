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
from typing import Dict, List, Optional

from nicegui import ui

from core.model import (
    Bus, CompositeType, Entity, Field, Message, MessageSlot, Module,
    Network, Port, Reference, ScalarType, TypeDef,
)
from web.session import WorkSession
from web import views

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

        # refs a componentes que se refrescan
        self.tree: Optional[ui.tree] = None
        self.status_label: Optional[ui.label] = None
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

    def _on_select(self, event) -> None:
        node_id = event.value
        entity = self._entity_by_node.get(node_id) if node_id else None
        self.selected = entity
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
            elif isinstance(entity, TypeDef):
                self._type_viewer(entity)
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
            if not rows:
                ui.label("El mensaje no tiene payload resuelto.").classes("text-negative")
                return
            self._render_field_grid(rows)

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
        """Visor de tipo: cómo se decodifica (propiedades + escalado + campos)."""
        view = views.type_view(typedef)
        with ui.card().classes("w-full"):
            ui.label(f"Visor de tipo · {view['kind']}").classes("text-bold")

            # propiedades de decodificación
            if view["props"]:
                ui.table(
                    columns=[{"name": "prop", "label": "Propiedad", "field": "prop", "align": "left"},
                             {"name": "valor", "label": "Valor", "field": "valor", "align": "left"}],
                    rows=view["props"], row_key="prop",
                ).classes("w-full").props("dense flat bordered hide-header")

            # escalado detallado
            self._scaling_block(view["scaling"])

            # campos (si es compuesto)
            if view["fields"]:
                ui.label("Campos").classes("text-bold q-mt-sm")
                ui.table(columns=views.MESSAGE_COLUMNS, rows=view["fields"], row_key="name") \
                    .classes("w-full").props("dense flat bordered wrap-cells")

    def _scaling_block(self, scaling: dict) -> None:
        kind = scaling.get("kind")
        if kind is None:
            return
        with ui.column().classes("w-full q-mt-sm gap-1"):
            if kind == "linear":
                ui.label("Escalado lineal").classes("text-bold")
                ui.markdown(f"`{scaling['formula']}`")
            elif kind == "enum":
                ui.label("Estados (Enum)").classes("text-bold")
                ui.table(
                    columns=[{"name": "valor", "label": "Valor", "field": "valor", "align": "right"},
                             {"name": "estado", "label": "Estado", "field": "estado", "align": "left"}],
                    rows=scaling["labels"], row_key="valor",
                ).classes("w-full").props("dense flat bordered")
            elif kind == "lut":
                ui.label(f"Tabla de tramos (LUT){' · ' + scaling['units'] if scaling.get('units') else ''}") \
                    .classes("text-bold")
                ui.table(
                    columns=[{"name": c, "label": c.capitalize(), "field": c, "align": "right"}
                             for c in ("desde", "hasta", "lsb", "offset")],
                    rows=scaling["ranges"], row_key="desde",
                ).classes("w-full").props("dense flat bordered")
            elif kind == "raw" and scaling.get("units"):
                ui.label(f"Sin escalado · unidades: {scaling['units']}").classes("text-grey")

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

    def _do_load(self, folder: str) -> None:
        if not folder:
            ui.notify("Indica una carpeta", type="warning")
            return
        self.session.load_folder(folder)
        if self.session.load_errors:
            ui.notify(f"{len(self.session.load_errors)} ficheros con error", type="warning")
        self.tree.props["nodes"] = self._root_nodes()
        self.tree.props["expanded"] = []
        self.tree.update()
        self.selected = None
        self._render_detail()
        self._refresh_status()
        ui.notify("Carga completada", type="positive")

    def _do_save(self, folder: str) -> None:
        if not self.session.modules:
            ui.notify("No hay nada que guardar", type="warning")
            return
        written = self.session.save_all_json(folder or "output_json")
        self._refresh_status()
        ui.notify(f"Guardados {len(written)} módulos en JSON", type="positive")

    # ================================================================== #
    # Montaje de la página
    # ================================================================== #
    def build(self) -> None:
        with ui.header().classes("items-center gap-3"):
            ui.label("ICDMS").classes("text-h6")
            folder_in = ui.input("carpeta ICD", value="tests/data").props("dense dark").classes("w-64")
            ui.button("Cargar", icon="folder_open",
                      on_click=lambda: self._do_load(folder_in.value)).props("dense")
            save_in = ui.input("carpeta salida", value="output_json").props("dense dark").classes("w-48")
            ui.button("Guardar JSON", icon="save",
                      on_click=lambda: self._do_save(save_in.value)).props("dense")
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


def create_app(initial_folder: Optional[str] = None) -> WorkSession:
    session = WorkSession()
    if initial_folder:
        session.load_folder(initial_folder)

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
    parser.add_argument("--folder", help="carpeta a cargar al iniciar")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    create_app(args.folder)
    ui.run(port=args.port, title="ICDMS", reload=False, show=False)


# ui.run debe ejecutarse a nivel de módulo cuando se lanza con python -m
if __name__ in {"__main__", "__mp_main__"}:
    main()
