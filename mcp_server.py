#!/usr/bin/env python3
"""Servidor MCP del ICDMS: expone mensajes, campos y tipos a un agente.

Arranque (stdio, el transporte habitual para clientes MCP):

    python3 mcp_server.py --project ./project_json
    python3 mcp_server.py --project ./icds_xml        # también acepta XML
    ICDMS_PROJECT=./project_json python3 mcp_server.py

Configuración en un cliente MCP (p. ej. claude_desktop_config.json o
.mcp.json):

    {
      "mcpServers": {
        "icdms": {
          "command": "python3",
          "args": ["/ruta/a/testad/mcp_server.py", "--project", "/ruta/al/proyecto"]
        }
      }
    }

Si no se indica proyecto al arrancar, el agente puede cargarlo con la
herramienta `load_project`.

La lógica vive en core/query.py (ICDQuery); aquí solo se declaran las
herramientas MCP, para que la misma API sea utilizable sin MCP.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from core.query import ICDQuery, QueryError

INSTRUCTIONS = """\
Acceso al modelo de ICDs (Interface Control Documents) aeronáuticos: \
mensajes, sus campos y los tipos de datos.

Flujo habitual:
1. `list_modules` / `list_messages` para descubrir qué hay cargado.
2. `get_message` para el detalle de un mensaje: devuelve la tabla de campos
   ya aplanada (subestructuras incluidas), cada uno con su longitud en bits,
   posición absoluta (`max_position`), codificación y escalado.
3. `get_type` para saber cómo se decodifica un tipo concreto (escalado
   lineal, estados de un enum o tramos de una LUT).
4. `decode_message` / `encode_message` para convertir entre bytes y valores.

Convención de bits del ICD: la numeración empieza en 1 y va MSB primero; un \
campo de longitud L con `max_position` P ocupa los bits [P-L+1 .. P].

Los valores se manejan en unidades de ingeniería por defecto \
(`engineering=true`: aplica el escalado y usa las etiquetas de los enums); \
con `engineering=false` se trabaja con los valores crudos del bus.\
"""

server = MCPServer(
    name="icdms",
    title="ICDMS — ICDs aeronáuticos",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)

_query = ICDQuery()


def _q() -> ICDQuery:
    return _query


def _guard(fn, *args, **kwargs):
    """Traduce los errores de consulta a errores MCP legibles por el agente."""
    try:
        return fn(*args, **kwargs)
    except QueryError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------- #
# Carga
# ---------------------------------------------------------------------- #
@server.tool(
    title="Cargar proyecto ICD",
    description="Carga los ICDs de una carpeta: usa los .json del proyecto si "
                "los hay, y si no los XML (.module/.xmi/.xml). Devuelve el "
                "resumen de módulos, entidades y referencias resueltas.",
)
def load_project(folder: str) -> Dict[str, Any]:
    return _guard(_q().load, folder)


@server.tool(
    title="Listar módulos",
    description="Módulos ICD cargados, con su número de mensajes y tipos.",
)
def list_modules() -> List[Dict[str, Any]]:
    return _guard(_q().modules)


# ---------------------------------------------------------------------- #
# Mensajes
# ---------------------------------------------------------------------- #
@server.tool(
    title="Listar mensajes",
    description="Mensajes definidos, con su periodo, modo de transmisión y "
                "el tipo de payload. Filtra por módulo o por subcadena del nombre.",
)
def list_messages(module: Optional[str] = None,
                  name_contains: Optional[str] = None,
                  limit: int = 200) -> List[Dict[str, Any]]:
    return _guard(_q().messages, module, name_contains, limit)


@server.tool(
    title="Detalle de un mensaje",
    description="Mensaje completo con su tabla de campos aplanada "
                "(subestructuras anidadas incluidas). Cada campo lleva su "
                "longitud en bits, max_position (1-based), tipo, codificación, "
                "escalado, condición (en variantes) y origen (referencia o "
                "inline). Acepta el nombre o el xmi:id.",
)
def get_message(name_or_id: str) -> Dict[str, Any]:
    return _guard(_q().message, name_or_id)


@server.tool(
    title="Detalle de un campo",
    description="Un campo concreto de un mensaje o tipo compuesto, con su "
                "posición, longitud, codificación y escalado.",
)
def get_field(message_or_type: str, field_name: str) -> Dict[str, Any]:
    return _guard(_q().field, message_or_type, field_name)


# ---------------------------------------------------------------------- #
# Tipos
# ---------------------------------------------------------------------- #
@server.tool(
    title="Listar tipos",
    description="Tipos definidos. 'kind' filtra por clase: ScalarType, "
                "TextType, RecordType, ArrayType o VariantType.",
)
def list_types(module: Optional[str] = None, kind: Optional[str] = None,
               name_contains: Optional[str] = None,
               limit: int = 200) -> List[Dict[str, Any]]:
    return _guard(_q().types, module, kind, name_contains, limit)


@server.tool(
    title="Detalle de un tipo",
    description="Cómo se decodifica un tipo: propiedades (bits, codificación, "
                "contador de array, discriminador de variante) y el escalado "
                "detallado (fórmula lineal, estados de enum o tramos de LUT). "
                "Si es compuesto, incluye también sus campos.",
)
def get_type(name_or_id: str) -> Dict[str, Any]:
    return _guard(_q().type, name_or_id)


# ---------------------------------------------------------------------- #
# Búsqueda y validación
# ---------------------------------------------------------------------- #
@server.tool(
    title="Buscar entidades",
    description="Busca por subcadena del nombre (o por xmi:id exacto) en "
                "todos los módulos: mensajes, tipos, campos, redes...",
)
def search(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _guard(_q().search, query, limit)


@server.tool(
    title="Incidencias de validación",
    description="Problemas detectados en los ICD cargados: campos obligatorios "
                "ausentes, referencias sin resolver, solapamientos, ids "
                "duplicados... Filtra por módulo o nivel (ERROR / WARNING).",
)
def list_issues(module: Optional[str] = None, level: Optional[str] = None,
                limit: int = 200) -> List[Dict[str, str]]:
    return _guard(_q().issues, module, level, limit)


# ---------------------------------------------------------------------- #
# Codec
# ---------------------------------------------------------------------- #
@server.tool(
    title="Decodificar bytes",
    description="Decodifica una trama (hex, con o sin espacios) a los valores "
                "de sus campos, según el layout del mensaje o tipo indicado. "
                "engineering=true aplica el escalado (unidades físicas y "
                "etiquetas de enum); false devuelve los valores crudos.",
)
def decode_message(name_or_id: str, data_hex: str, engineering: bool = True,
                   case: Optional[str] = None) -> Dict[str, Any]:
    return _guard(_q().decode, name_or_id, data_hex, engineering, case)


@server.tool(
    title="Codificar valores",
    description="Codifica un diccionario {campo: valor} a bytes (hex) según el "
                "layout del mensaje o tipo. Las subestructuras se anidan como "
                "diccionarios. engineering=true interpreta los valores en "
                "unidades físicas/etiquetas de enum.",
)
def encode_message(name_or_id: str, values: Dict[str, Any],
                   engineering: bool = True) -> Dict[str, Any]:
    return _guard(_q().encode, name_or_id, values, engineering)


# ---------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Servidor MCP que expone los ICDs (mensajes, campos y tipos).")
    parser.add_argument("--project", default=os.environ.get("ICDMS_PROJECT"),
                        help="carpeta del proyecto JSON o de los XML "
                             "(o variable de entorno ICDMS_PROJECT)")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--verbose", action="store_true",
                        help="log detallado del parser (siempre por stderr: "
                             "stdout es el canal del protocolo MCP)")
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr,
                        level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("ICDParser").setLevel(logging.ERROR)

    if args.project:
        try:
            info = _query.load(args.project)
            # el log va a stderr: stdout es el canal del protocolo MCP
            print(f"ICDMS MCP: {len(info['modules'])} módulos desde "
                  f"{args.project} ({info['entities']} entidades)", file=sys.stderr)
        except QueryError as exc:
            print(f"ICDMS MCP: no se pudo cargar '{args.project}': {exc}",
                  file=sys.stderr)

    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
