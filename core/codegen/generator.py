"""Orquestador de la generación de código (spec docs/codegen_spec.md).

Flujo: validar el modelo -> construir el contexto del backend -> renderizar
las plantillas Jinja2 -> escribir los ficheros. Determinista: el mismo
modelo produce la misma salida byte a byte (sin marcas de tiempo).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from core.codegen import ada_backend, python_backend
from core.model import Module
from core.validation import ERROR, validate_module

logger = logging.getLogger("ICDCodegen")

_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates")


class CodegenError(Exception):
    pass


@dataclass
class GenResult:
    package: str
    files: Dict[str, str] = field(default_factory=dict)   # ruta relativa -> contenido
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Paquete '{self.package}': {len(self.files)} ficheros"]
        lines += [f"  {path}" for path in sorted(self.files)]
        lines += [f"  AVISO: {w}" for w in self.warnings]
        return "\n".join(lines)


def _env(language: str) -> Environment:
    return Environment(
        loader=FileSystemLoader(os.path.join(_TEMPLATES, language)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _blocking_errors(module: Module) -> List[str]:
    """Errores de validación que impiden generar.

    Los de referencias explicitas de control de exportación (ficheros de
    reglas no cargados) no afectan al código generado: quedan como aviso.
    """
    issues = validate_module(module)
    return [str(i) for i in issues
            if i.level == ERROR and "explicit" not in i.message]


# Sesiones Ada por carpeta de salida: los módulos generados en la misma
# carpeta comparten índice de tipos (referencias entre módulos via 'with').
_ADA_SESSIONS: set = set()


def generate(module: Module, out_dir: str, language: str = "python") -> GenResult:
    """Genera el código de un módulo ICD en out_dir/<paquete>/..."""
    if language not in ("python", "ada"):
        raise CodegenError(f"lenguaje '{language}' no soportado")

    blocking = _blocking_errors(module)
    if blocking:
        raise CodegenError(
            "el módulo no valida; corrige antes de generar:\n  " + "\n  ".join(blocking))

    if language == "ada":
        return _generate_ada(module, out_dir)

    ctx = python_backend.build_module_ctx(module)
    env = _env("python")
    result = GenResult(package=ctx.package, warnings=list(ctx.warnings))

    # L0: runtime común (compartido por todos los paquetes generados)
    result.files["icd_runtime/__init__.py"] = ""
    result.files["icd_runtime/bitio.py"] = env.get_template("bitio.py.j2").render()

    # Paquete del módulo
    pkg = ctx.package
    result.files[f"{pkg}/__init__.py"] = (
        f'"""Paquete generado del módulo ICD \'{ctx.icd_name}\' '
        f'({ctx.source_file})."""\n')
    result.files[f"{pkg}/types.py"] = env.get_template("types.py.j2").render(m=ctx)

    for fctx in ctx.files:
        needs_factory = any(c.needs_factory for c in fctx.classes)
        template = "message.py.j2" if fctx.message is not None else "composite.py.j2"
        content = env.get_template(template).render(
            m=ctx, f=fctx, msg=fctx.message, needs_factory=needs_factory)
        result.files[f"{pkg}/{fctx.stem}.py"] = content

    _write_files(result, out_dir)
    return result


def _generate_ada(module: Module, out_dir: str) -> GenResult:
    if out_dir not in _ADA_SESSIONS:
        ada_backend.reset_session()
        _ADA_SESSIONS.add(out_dir)

    ctx = ada_backend.build_module_ctx(module)
    env = _env("ada")
    result = GenResult(package=ctx.root_pkg, warnings=list(ctx.warnings))

    # L0: runtime común
    result.files["icd_bitio.ads"] = env.get_template("icd_bitio.ads.j2").render()
    result.files["icd_bitio.adb"] = env.get_template("icd_bitio.adb.j2").render()

    # Paquete raíz + tipos
    root_stem = naming_ada_file(ctx.root_pkg)
    result.files[f"{root_stem}.ads"] = env.get_template("root.ads.j2").render(m=ctx)
    if ctx.scalars or ctx.texts:
        result.files[f"{root_stem}-types.ads"] = \
            env.get_template("types.ads.j2").render(m=ctx)
        if ctx.types_has_body:
            result.files[f"{root_stem}-types.adb"] = \
                env.get_template("types.adb.j2").render(m=ctx)

    for fctx in ctx.files:
        spec_tpl = "message.ads.j2" if fctx.message is not None else "composite.ads.j2"
        body_tpl = "message.adb.j2" if fctx.message is not None else "composite.adb.j2"
        result.files[f"{fctx.file_stem}.ads"] = \
            env.get_template(spec_tpl).render(m=ctx, f=fctx)
        result.files[f"{fctx.file_stem}.adb"] = \
            env.get_template(body_tpl).render(m=ctx, f=fctx)

    _write_files(result, out_dir)
    return result


def naming_ada_file(package_name: str) -> str:
    from core.codegen import naming
    return naming.ada_file(package_name)


def _write_files(result: GenResult, out_dir: str) -> None:
    for rel_path, content in result.files.items():
        path = os.path.join(out_dir, rel_path)
        os.makedirs(os.path.dirname(path) or out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    logger.info("Generados %d ficheros en %s (%s)", len(result.files), out_dir,
                result.package)
