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
    Network, Port, Reference, ScalarType,
)
from web.session import WorkSession

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

        # refs a componentes que se refrescan
        self.tree: Optional[ui.tree] = None
        self.status_label: Optional[ui.label] = None
        self.detail_container: Optional[ui.column] = None

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
        self.detail_container.clear()
        entity = self.selected
        with self.detail_container:
            if entity is None:
                ui.label("Selecciona una entidad en el árbol.").classes("text-grey")
                return

            ui.label(f"{type(entity).__name__}  ·  {entity.path}").classes(
                "text-sm text-grey-7"
            )
            ui.label(entity.name or "(sin nombre)").classes("text-h6")

            # --- atributos editables ---
            with ui.card().classes("w-full"):
                ui.label("Atributos").classes("text-bold")
                with ui.grid(columns=2).classes("w-full gap-2"):
                    for f in self._editable_fields(entity):
                        self._attr_input(entity, f)

            # --- referencias ---
            refs = entity.references()
            if refs:
                with ui.card().classes("w-full"):
                    ui.label("Referencias").classes("text-bold")
                    for ref in refs:
                        self._ref_row(ref)

            # --- layout / describe ---
            describe = getattr(entity, "describe", None)
            if callable(describe):
                with ui.card().classes("w-full"):
                    ui.label("Layout").classes("text-bold")
                    ui.code(describe()).classes("w-full").style("white-space:pre-wrap")

            # --- incidencias de esta entidad ---
            issues = self.session.issues_for(entity)
            if issues:
                with ui.card().classes("w-full"):
                    ui.label(f"Incidencias ({len(issues)})").classes("text-bold")
                    for issue in issues:
                        color = "negative" if issue.level == "ERROR" else "warning"
                        ui.label(f"{issue.level}: {issue.message}").classes(f"text-{color}")

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

    def _ref_row(self, ref: Reference) -> None:
        with ui.row().classes("items-center gap-2"):
            if ref.is_resolved:
                ui.icon("link", color="positive")
                ui.label(f"{ref.role} → {ref.target.name}  [{ref.href}]")
                ui.button(
                    "ir", on_click=lambda t=ref.target: self._goto(t)
                ).props("flat dense")
            else:
                ui.icon("link_off", color="negative")
                ui.label(f"{ref.role} → {ref.href}  (sin resolver)").classes("text-negative")

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
        self.tree._props["selected"] = node_id  # type: ignore[attr-defined]
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
                self.detail_container = ui.column().classes("w-full p-3 gap-2")

        self._render_detail()
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
