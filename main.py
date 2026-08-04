#!/usr/bin/env python3
"""Carga todos los ICD de una carpeta y muestra los resultados.

Parsea cada fichero .module/.xmi/.xml, registra todos los módulos, resuelve
las referencias ENTRE ellos (imprescindible para que los href cruzados se
enlacen) y muestra: módulos cargados, árbol, referencias e incidencias.

Uso:
    python3 main.py <carpeta>
    python3 main.py <carpeta> --tree          # imprime el árbol de cada módulo
    python3 main.py <carpeta> --describe NavMsg   # ficha de un mensaje
    python3 main.py <carpeta> --quiet         # sin logs de progreso del parser

Ejemplos:
    python3 main.py tests/data
    python3 main.py ./icds --tree
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys

from core.parser import ICDParser
from core.registry import ICDRegistry
from core.validation import ERROR, WARNING, validate_module

EXTENSIONS = ("*.module", "*.xmi", "*.xml")


def find_files(folder: str) -> list[str]:
    files: list[str] = []
    for pattern in EXTENSIONS:
        files.extend(glob.glob(os.path.join(folder, "**", pattern), recursive=True))
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga y analiza ICDs de una carpeta.")
    parser.add_argument("folder", help="carpeta con ficheros .module/.xmi/.xml")
    parser.add_argument("--tree", action="store_true", help="imprime el árbol de cada módulo")
    parser.add_argument("--describe", metavar="NOMBRE", help="ficha del mensaje/tipo indicado")
    parser.add_argument("--quiet", action="store_true", help="silencia los logs del parser")
    args = parser.parse_args(argv)

    # El parser informa del progreso por log (INFO); --quiet lo silencia.
    logging.basicConfig(
        level=logging.ERROR if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # El registro y el validador también logean cada incidencia, pero aquí las
    # presentamos nosotros en un resumen ordenado: evitamos la duplicación.
    logging.getLogger("ICDRegistry").setLevel(logging.CRITICAL)
    logging.getLogger("ICDValidator").setLevel(logging.CRITICAL)

    if not os.path.isdir(args.folder):
        print(f"error: '{args.folder}' no es una carpeta", file=sys.stderr)
        return 2

    files = find_files(args.folder)
    if not files:
        print(f"No se encontraron ficheros {'/'.join(EXTENSIONS)} en {args.folder}")
        return 1

    # 1. Parsear todos los ficheros en un registro común.
    registry = ICDRegistry()
    icd_parser = ICDParser(registry)
    parsed_errors: list[tuple[str, str]] = []
    for path in files:
        try:
            icd_parser.parse_file(path)
        except Exception as exc:  # noqa: BLE001 - queremos seguir con el resto
            parsed_errors.append((path, str(exc)))

    # 2. Resolver referencias entre TODOS los módulos ya cargados.
    report = registry.resolve_references()

    # ------------------------------------------------------------------ #
    # Resultados
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 70)
    print(f"RESUMEN — {args.folder}")
    print("=" * 70)
    print(f"Ficheros encontrados : {len(files)}")
    print(f"Módulos cargados     : {len(registry.modules)}")
    print(f"Entidades indexadas  : {len(registry)}")
    if registry.duplicate_ids:
        print(f"Ids duplicados       : {len(registry.duplicate_ids)}")

    if parsed_errors:
        print(f"\nFicheros con error de parseo ({len(parsed_errors)}):")
        for path, msg in parsed_errors:
            print(f"  ✗ {os.path.basename(path)}: {msg}")

    print("\nMódulos:")
    for module in registry.modules:
        n_types = len(module.find(cls=None)) - 1
        print(f"  • {module.name:30} ({module.source_file})  —  {n_types} entidades")

    # 3. Validación de cada módulo.
    total_err = total_warn = 0
    all_issues = []
    for module in registry.modules:
        issues = validate_module(module)
        all_issues.extend(issues)
        total_err += sum(1 for i in issues if i.level == ERROR)
        total_warn += sum(1 for i in issues if i.level == WARNING)

    print(f"\nReferencias resueltas: {report.resolved}   sin resolver: {len(report.unresolved)}")
    print(f"Validación: {total_err} errores, {total_warn} avisos")

    # Detalle de incidencias (limitado para no inundar la consola).
    if all_issues:
        print("\nIncidencias (primeras 20):")
        for issue in all_issues[:20]:
            print(f"  {issue}")
        if len(all_issues) > 20:
            print(f"  ... y {len(all_issues) - 20} más")

    if report.unresolved:
        print("\nReferencias sin resolver (primeras 10):")
        for ref, reason in report.unresolved[:10]:
            owner = ref.owner.path if ref.owner else "?"
            print(f"  {owner}  →  {ref.href}  ({reason})")
        if len(report.unresolved) > 10:
            print(f"  ... y {len(report.unresolved) - 10} más")

    # ------------------------------------------------------------------ #
    # Opciones de inspección
    # ------------------------------------------------------------------ #
    if args.tree:
        for module in registry.modules:
            print("\n" + "-" * 70)
            print(module.pretty())

    if args.describe:
        needle = args.describe.lower()
        # Coincidencia exacta primero; si no, por subcadena (nombres largos).
        exact = [e for m in registry.modules for e in m.walk()
                 if e.name.lower() == needle]
        matches = exact or [e for m in registry.modules for e in m.walk()
                            if needle in e.name.lower()]

        if not matches:
            print(f"\nNo se encontró ninguna entidad que contenga '{args.describe}'")
        elif len(matches) > 1 and not exact:
            print(f"\n{len(matches)} coincidencias para '{args.describe}' "
                  "(afina el nombre o usa el exacto):")
            for e in matches[:30]:
                print(f"  {type(e).__name__:12} {e.path}")
            if len(matches) > 30:
                print(f"  ... y {len(matches) - 30} más")
        else:
            for e in matches:
                print("\n" + "-" * 70)
                describe = getattr(e, "describe", None)
                print(describe() if callable(describe) else repr(e))

    print()
    return 0 if not parsed_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
