"""Backend Python del generador: modelo -> contexto para las plantillas.

Toda la lógica específica de Python vive aquí (mapeo de tipos, sentencias de
pack/unpack, imports, orden de unidades); las plantillas Jinja2 solo
presentan. La convención de bits (1-based, MSB-first) NO se reimplementa:
los spans salen de core.codec.field_span, la misma fuente que el oráculo.

Capas (spec 2.1): types.py (L1) <- <composite>.py (L2) <- <mensaje>.py (L3),
todas sobre icd_runtime.bitio (L0). Dependencias unidireccionales.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from core.codec import CodecError, field_span, type_bit_length
from core.codegen import naming
from core.model import (
    ArrayType, CompositeType, EnumScaling, Field, Folder, LinearScaling,
    LUTScaling, Message, Module, RecordType, ScalarType, TextType, TypeDef,
    VariantType,
)


class Unsupported(Exception):
    """Construcción aún no soportada por el generador (se omite con aviso)."""


# ---------------------------------------------------------------------- #
# Contextos (lo que ven las plantillas)
# ---------------------------------------------------------------------- #
@dataclass
class ScalarCtx:
    class_name: str
    icd_name: str
    icd_id: str
    path: str
    bits: int
    encoding: str
    units: str
    scaling_kind: str                    # none | linear | enum | lut
    lsb: float = 0.0
    offset: float = 0.0
    labels: List[Tuple[int, str]] = dc_field(default_factory=list)
    ranges: List[Tuple[float, float, float, float]] = dc_field(default_factory=list)


@dataclass
class TextCtx:
    class_name: str
    icd_name: str
    icd_id: str
    path: str
    chars: int
    encoding: str
    length_mode: str


@dataclass
class FieldCtx:
    py_name: str
    icd_name: str
    start: int                            # 0-based interno
    length: int
    bit_first: int                        # 1-based, para comentarios (ICD)
    bit_last: int
    py_type: str                          # int | float | str | <Clase>
    default_expr: str
    pack_stmt: str
    unpack_expr: str
    comment: str


@dataclass
class CompositeCtx:
    class_name: str
    module_stem: str                      # nombre de fichero (sin .py)
    kind_label: str
    icd_name: str
    icd_id: str
    path: str
    bit_length: int
    fields: List[FieldCtx] = dc_field(default_factory=list)
    needs_factory: bool = False           # algún default con default_factory


@dataclass
class MessageCtx:
    module_stem: str
    icd_name: str
    icd_id: str
    path: str
    period: int
    rate_mode: str
    byte_length: int
    payload_class: str
    payload_import: Optional[Tuple[str, str]]   # (módulo, clase) o None si inline
    inline_classes: List[CompositeCtx] = dc_field(default_factory=list)


@dataclass
class FileCtx:
    """Un fichero .py generado: sus imports y las clases que contiene."""
    stem: str
    docstring: str
    imports: List[Tuple[str, str]] = dc_field(default_factory=list)
    classes: List[CompositeCtx] = dc_field(default_factory=list)
    message: Optional[MessageCtx] = None


@dataclass
class ModuleCtx:
    package: str
    icd_name: str
    source_file: str
    scalars: List[ScalarCtx] = dc_field(default_factory=list)
    texts: List[TextCtx] = dc_field(default_factory=list)
    files: List[FileCtx] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)


# ---------------------------------------------------------------------- #
# Escalares y texto (L1)
# ---------------------------------------------------------------------- #
def _scalar_ctx(s: ScalarType, ns: naming.Namespace) -> ScalarCtx:
    kind, lsb, offset, labels, ranges = "none", 0.0, 0.0, [], []
    sc = s.scaling
    if isinstance(sc, LinearScaling):
        kind, lsb, offset = "linear", sc.lsb, sc.offset
    elif isinstance(sc, EnumScaling):
        kind = "enum"
        labels = [(int(v), t) for v, t in sc.labels.items()]
    elif isinstance(sc, LUTScaling):
        kind = "lut"
        ranges = [(r.begin, r.end, r.lsb, r.offset) for r in sc.ranges]
    return ScalarCtx(
        class_name=ns.assign(s, naming.pascal(s.name)),
        icd_name=s.name, icd_id=s.id, path=s.path,
        bits=s.bit_length, encoding=s.encoding, units=s.units,
        scaling_kind=kind, lsb=lsb, offset=offset, labels=labels, ranges=ranges,
    )


def _text_ctx(t: TextType, ns: naming.Namespace) -> TextCtx:
    return TextCtx(
        class_name=ns.assign(t, naming.pascal(t.name)),
        icd_name=t.name, icd_id=t.id, path=t.path,
        chars=t.max_chars, encoding=t.encoding, length_mode=t.length_mode,
    )


# ---------------------------------------------------------------------- #
# Campos (pack/unpack)
# ---------------------------------------------------------------------- #
_ENC = {
    "twocomplement": ("int", "twoc"),
    "": ("int", "uint"),
    "unsigned": ("int", "uint"),
    "ascii": ("int", "uint"),
    "bcd": ("int", "bcd"),
    "ieee754": ("float", None),          # f32/f64 según bits
}


def _scalar_pack(f_ident: str, start: int, length: int, encoding: str) -> Tuple[str, str, str]:
    """(py_type, pack_stmt, unpack_expr) para un campo escalar."""
    key = (encoding or "").lower()
    if key not in _ENC:
        raise Unsupported(f"codificación '{encoding}' no soportada")
    py_type, enc = _ENC[key]
    if key == "ieee754":
        if length not in (32, 64):
            raise Unsupported(f"IEEE754 de {length} bits")
        fn = "f32" if length == 32 else "f64"
        pack = (f"buf.set_bits(base + {start}, {length}, "
                f"bitio.enc_{fn}(self.{f_ident}))")
        unpack = f"bitio.dec_{fn}(buf.get_bits(base + {start}, {length}))"
        return py_type, pack, unpack
    pack = (f"buf.set_bits(base + {start}, {length}, "
            f"bitio.enc_{enc}(self.{f_ident}, {length}))")
    unpack = f"bitio.dec_{enc}(buf.get_bits(base + {start}, {length}), {length})"
    return py_type, pack, unpack


def _field_ctx(f: Field, ns_fields: naming.Namespace,
               comp_of: Dict[int, CompositeCtx],
               imports: List[Tuple[str, str]],
               inline_out: List[CompositeCtx],
               package: str, file_ns: naming.Namespace) -> FieldCtx:
    dt = f.datatype
    if dt is None:
        target = f.ref.href if f.ref else "(vacío)"
        raise Unsupported(f"campo '{f.name}' sin tipo resuelto -> {target}")
    if isinstance(dt, (ArrayType, VariantType)):
        raise Unsupported(
            f"campo '{f.name}': {type(dt).__name__} aún no soportado por el generador")

    start, length = field_span(f)
    ident = ns_fields.assign(f, naming.snake(f.name or "campo"))
    ref_note = f" -> {dt.name}" if dt.name and dt.name != f.name else ""
    comment = f"bits {start + 1}..{start + length} (ICD: {f.name or '?'}{ref_note})"

    if isinstance(dt, ScalarType):
        py_type, pack, unpack_e = _scalar_pack(ident, start, length, dt.encoding)
        default = "0.0" if py_type == "float" else "0"
        return FieldCtx(ident, f.name, start, length, start + 1, start + length,
                        py_type, default, pack,
                        f"{ident}={unpack_e}", comment)

    if isinstance(dt, TextType):
        pack = (f"buf.set_bits(base + {start}, {length}, "
                f"bitio.enc_ascii(self.{ident}, {dt.max_chars}))")
        unpack = (f"{ident}=bitio.dec_ascii("
                  f"buf.get_bits(base + {start}, {length}), {dt.max_chars})")
        return FieldCtx(ident, f.name, start, length, start + 1, start + length,
                        "str", '""', pack, unpack, comment)

    if isinstance(dt, CompositeType):
        # compartido (en carpeta) -> import; inline -> clase en este fichero
        if id(dt) in comp_of:
            cls = comp_of[id(dt)].class_name
            module = f"{_package_of(dt)}.{comp_of[id(dt)].module_stem}"
            imp = (module, cls)
            if imp not in imports:
                imports.append(imp)
        else:
            inline = build_composite(dt, comp_of, imports, inline_out,
                                     package, file_ns, inline=True)
            inline_out.append(inline)
            cls = inline.class_name
        pack = f"self.{ident}.pack_into(buf, base + {start})"
        unpack = f"{ident}={cls}.unpack_from(buf, base + {start})"
        return FieldCtx(ident, f.name, start, length, start + 1, start + length,
                        cls, f"field(default_factory={cls})", pack, unpack, comment)

    raise Unsupported(f"campo '{f.name}': tipo {type(dt).__name__} no soportado")


def _package_of(entity: TypeDef) -> str:
    node = entity
    while node.parent is not None:
        node = node.parent
    return naming.py_module(getattr(node, "name", "icd"))


# ---------------------------------------------------------------------- #
# Compuestos (L2) y mensajes (L3)
# ---------------------------------------------------------------------- #
def build_composite(comp: CompositeType, comp_of: Dict[int, CompositeCtx],
                    imports: List[Tuple[str, str]],
                    inline_out: List[CompositeCtx],
                    package: str, file_ns: naming.Namespace,
                    inline: bool = False) -> CompositeCtx:
    if isinstance(comp, (ArrayType, VariantType)):
        raise Unsupported(f"{type(comp).__name__} '{comp.name}' aún no soportado")
    ctx = CompositeCtx(
        class_name=file_ns.assign(comp, naming.pascal(comp.name or "Anon")),
        module_stem=naming.snake(comp.name or "anon"),
        kind_label=type(comp).__name__,
        icd_name=comp.name, icd_id=comp.id, path=comp.path,
        bit_length=type_bit_length(comp),
    )
    ns_fields = naming.Namespace()
    for f in comp.fields:
        fc = _field_ctx(f, ns_fields, comp_of, imports, inline_out,
                        package, file_ns)
        ctx.fields.append(fc)
        if fc.default_expr.startswith("field("):
            ctx.needs_factory = True
    return ctx


def build_module_ctx(module: Module) -> ModuleCtx:
    """Construye el contexto completo de un módulo ICD para las plantillas."""
    package = naming.py_module(module.name)
    m = ModuleCtx(package=package, icd_name=module.name,
                  source_file=module.source_file)

    ns_types = naming.Namespace()
    shared_comps: List[CompositeType] = []
    messages: List[Message] = []

    for entity in module.walk():
        # solo definiciones compartidas: las que cuelgan de una carpeta
        if entity.parent is None or not isinstance(entity.parent, Folder):
            continue
        if isinstance(entity, ScalarType):
            m.scalars.append(_scalar_ctx(entity, ns_types))
        elif isinstance(entity, TextType):
            m.texts.append(_text_ctx(entity, ns_types))
        elif isinstance(entity, (RecordType,)):
            shared_comps.append(entity)
        elif isinstance(entity, (ArrayType, VariantType)):
            m.warnings.append(
                f"{type(entity).__name__} '{entity.name}' omitido: aún no "
                "soportado por el generador")
        elif isinstance(entity, Message):
            messages.append(entity)

    # Compuestos compartidos, en orden de dependencia (Kahn intra-módulo)
    ordered = _topo_sort(shared_comps, m.warnings)

    comp_of: Dict[int, CompositeCtx] = {}
    file_ns = naming.Namespace()
    for comp in ordered:
        imports: List[Tuple[str, str]] = []
        inline_classes: List[CompositeCtx] = []
        try:
            ctx = build_composite(comp, comp_of, imports, inline_classes,
                                  package, file_ns)
        except (Unsupported, CodecError) as exc:
            m.warnings.append(f"'{comp.name}' omitido: {exc}")
            continue
        comp_of[id(comp)] = ctx
        m.files.append(FileCtx(
            stem=ctx.module_stem,
            docstring=f"{ctx.kind_label} '{ctx.icd_name}' — ICD: {ctx.path} [{ctx.icd_id}]",
            imports=imports,
            classes=[*inline_classes, ctx],
        ))

    # Mensajes
    for msg in messages:
        try:
            m.files.append(_message_file(msg, comp_of, package, file_ns))
        except (Unsupported, CodecError) as exc:
            m.warnings.append(f"mensaje '{msg.name}' omitido: {exc}")

    return m


def _message_file(msg: Message, comp_of: Dict[int, CompositeCtx],
                  package: str, file_ns: naming.Namespace) -> FileCtx:
    st = msg.structure
    if st is None:
        raise Unsupported("sin payload resuelto")
    imports: List[Tuple[str, str]] = []
    inline_classes: List[CompositeCtx] = []

    if id(st) in comp_of:
        payload = comp_of[id(st)]
        payload_class = payload.class_name
        payload_import = (f"{_package_of(st)}.{payload.module_stem}", payload_class)
    elif isinstance(st, CompositeType):
        body = build_composite(st, comp_of, imports, inline_classes,
                               package, file_ns, inline=True)
        inline_classes.append(body)
        payload_class = body.class_name
        payload_import = None
    else:
        raise Unsupported("payload no compuesto")

    bits = type_bit_length(st)
    mc = MessageCtx(
        module_stem=naming.snake(msg.name or "mensaje"),
        icd_name=msg.name, icd_id=msg.id, path=msg.path,
        period=msg.period, rate_mode=msg.rate_mode,
        byte_length=(bits + 7) // 8,
        payload_class=payload_class,
        payload_import=payload_import,
        inline_classes=inline_classes,
    )
    return FileCtx(
        stem=mc.module_stem,
        docstring=f"Message '{msg.name}' — ICD: {msg.path} [{msg.id}]",
        imports=imports + ([mc.payload_import] if mc.payload_import else []),
        classes=inline_classes,
        message=mc,
    )


def _topo_sort(comps: List[CompositeType], warnings: List[str]) -> List[CompositeType]:
    """Orden de dependencias entre compuestos compartidos del módulo."""
    ids = {id(c): c for c in comps}
    deps: Dict[int, set] = {id(c): set() for c in comps}
    for c in comps:
        for f in c.fields:
            dt = f.datatype
            if dt is not None and id(dt) in ids and dt is not c:
                deps[id(c)].add(id(dt))
    ordered: List[CompositeType] = []
    ready = [c for c in comps if not deps[id(c)]]
    done: set = set()
    while ready:
        c = ready.pop(0)
        ordered.append(c)
        done.add(id(c))
        for other in comps:
            if id(other) not in done and other not in ready \
                    and deps[id(other)] <= done:
                ready.append(other)
    for c in comps:
        if id(c) not in done:
            warnings.append(f"'{c.name}' omitido: dependencia circular de tipos")
    return ordered
