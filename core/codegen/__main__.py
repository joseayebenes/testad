"""CLI del generador de código.

Uso:
    python3 -m core.codegen <carpeta_icd> --out gen/ [--lang python]

La carpeta puede contener el proyecto JSON (*.json) o los XML originales
(.module/.xmi/.xml); se cargan todos, se resuelven las referencias entre
ellos y se genera un paquete por módulo ICD.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys

from core.codegen.generator import CodegenError, generate
from core.parser import ICDParser
from core.persistence import load_module
from core.registry import ICDRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera código desde ICDs.")
    parser.add_argument("folder", help="carpeta con el proyecto (JSON o XML)")
    parser.add_argument("--out", default="gen", help="carpeta de salida (def: gen/)")
    parser.add_argument("--lang", default="python", choices=["python"],
                        help="lenguaje destino (Ada 95 en preparación)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("ICDValidator").setLevel(logging.CRITICAL)
    logging.getLogger("ICDRegistry").setLevel(logging.CRITICAL)

    registry = ICDRegistry()
    json_files = sorted(glob.glob(os.path.join(args.folder, "*.json")))
    if json_files:
        for path in json_files:
            load_module(path, registry)
    else:
        icd_parser = ICDParser(registry)
        for pattern in ("*.module", "*.xmi", "*.xml"):
            for path in sorted(glob.glob(os.path.join(args.folder, "**", pattern),
                                         recursive=True)):
                icd_parser.parse_file(path)
    if not registry.modules:
        print(f"No se encontraron ICDs en {args.folder}", file=sys.stderr)
        return 2
    registry.resolve_references()

    failures = 0
    for module in sorted(registry.modules, key=lambda m: m.name):
        try:
            result = generate(module, args.out, language=args.lang)
        except CodegenError as exc:
            failures += 1
            print(f"\n✗ {module.name}: {exc}")
            continue
        print(f"\n✓ {result.summary()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
