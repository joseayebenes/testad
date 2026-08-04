"""Backend Ada 95 del generador: modelo -> contexto para las plantillas.

Misma arquitectura por capas que el backend Python (spec 2.1):
    icd_bitio (L0) <- <Pkg>.Types (L1) <- <Pkg>.<Compuesto> (L2)
                                        <- <Pkg>.<Mensaje> (L3)

Restricciones de Ada 95 (spec 2.7): sin aspects (cláusulas de representación
y pragmas), sin Scalar_Storage_Order (el endianness lo materializa el runtime
icd_bitio campo a campo), paquetes hijos, tipos modulares.

El tipo principal de cada paquete de compuesto se llama ``T`` (evita el
homógrafo con el nombre del propio paquete); los tipos inline conservan su
nombre dentro del paquete del contenedor.

Los spans de bits salen de core.codec.field_span: misma fuente que el
oráculo, convención 1-based garantizada por construcción.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from core.codec import CodecError, field_span, type_bit_length
from core.codegen import naming
from core.codegen.python_backend import Unsupported, _topo_sort
from core.model import (
    ArrayType, CompositeType, EnumScaling, Field, Folder, LinearScaling,
    LUTScaling, Message, Module, RecordType, ScalarType, TextType, TypeDef,
    VariantType,
)


def _real(v: float) -> str:
    """Literal real Ada (siempre con punto decimal)."""
    out = repr(float(v))
    return out if ("." in out or "e" in out or "E" in out) else out + ".0"


# ---------------------------------------------------------------------- #
# Contextos
# ---------------------------------------------------------------------- #
@dataclass
class ScalarCtxA:
    ada_name: str                       # Air_Speed
    raw_type: str                       # Air_Speed_Raw
    decl_lines: List[str]               # declaración del tipo crudo + Size
    icd_name: str = ""
    icd_id: str = ""
    path: str = ""
    units: str = ""
    kind: str = "none"                  # none | linear | enum | lut
    lsb: str = "0.0"
    offset: str = "0.0"
    eng_type: str = ""                  # enum: nombre del tipo enumerado
    literals: List[Tuple[str, int]] = dc_field(default_factory=list)
    ranges: List[Tuple[str, str, str, str]] = dc_field(default_factory=list)

    @property
    def has_functions(self) -> bool:
        return self.kind in ("linear", "enum", "lut")


@dataclass
class TextCtxA:
    ada_name: str
    subtype_name: str
    chars: int
    icd_name: str = ""
    icd_id: str = ""
    path: str = ""


@dataclass
class RecordComp:
    name: str
    type_name: str
    default: str                        # '' si no aplica
    comment: str


@dataclass
class ClassCtxA:
    type_name: str                      # 'T' para el principal; nombre propio si inline
    kind_label: str = ""
    icd_name: str = ""
    icd_id: str = ""
    path: str = ""
    bit_length: int = 0
    local_decls: List[str] = dc_field(default_factory=list)   # tipos inline (raw)
    components: List[RecordComp] = dc_field(default_factory=list)
    pack_stmts: List[str] = dc_field(default_factory=list)
    unpack_stmts: List[str] = dc_field(default_factory=list)


@dataclass
class MessageCtxA:
    payload_ref: str                    # 'Fcs_Icd.Nav_Block.T' o clase local
    payload_pack: str                   # prefijo para Pack/Unpack ('Fcs_Icd.Nav_Block.' o '')
    byte_length: int = 0
    period: int = 0
    rate_mode: str = ""


@dataclass
class FileCtxA:
    package_name: str                   # Fcs_Icd.Nav_Block
    file_stem: str                      # fcs_icd-nav_block
    doc: str = ""
    withs: List[str] = dc_field(default_factory=list)
    classes: List[ClassCtxA] = dc_field(default_factory=list)
    message: Optional[MessageCtxA] = None


@dataclass
class ModuleCtxA:
    root_pkg: str                       # Fcs_Icd
    icd_name: str = ""
    source_file: str = ""
    scalars: List[ScalarCtxA] = dc_field(default_factory=list)
    texts: List[TextCtxA] = dc_field(default_factory=list)
    files: List[FileCtxA] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)

    @property
    def types_has_body(self) -> bool:
        return any(s.has_functions for s in self.scalars)


# ---------------------------------------------------------------------- #
# Escalares (L1)
# ---------------------------------------------------------------------- #
def _raw_decl(name: str, s: ScalarType) -> List[str]:
    n = s.bit_length
    enc = (s.encoding or "").lower()
    if enc == "twocomplement":
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
        return [f"type {name} is range {lo} .. {hi};",
                f"for {name}'Size use {n};"]
    if enc == "ieee754":
        base = "Float" if n == 32 else "Long_Float"
        return [f"subtype {name} is Standard.{base};"]
    if enc == "bcd":
        hi = 10 ** (n // 4) - 1
        return [f"type {name} is range 0 .. {hi};",
                f"for {name}'Size use {n};"]
    # unsigned / ascii / sin codificación
    return [f"type {name} is mod 2 ** {n};",
            f"for {name}'Size use {n};"]


def _scalar_ctx(s: ScalarType, ns: naming.Namespace) -> ScalarCtxA:
    ada_name = ns.assign(s, naming.ada_ident(s.name))
    ctx = ScalarCtxA(
        ada_name=ada_name, raw_type=f"{ada_name}_Raw",
        decl_lines=_raw_decl(f"{ada_name}_Raw", s),
        icd_name=s.name, icd_id=s.id, path=s.path, units=s.units,
    )
    sc = s.scaling
    if isinstance(sc, LinearScaling):
        ctx.kind, ctx.lsb, ctx.offset = "linear", _real(sc.lsb), _real(sc.offset)
    elif isinstance(sc, EnumScaling):
        ctx.kind = "enum"
        ctx.eng_type = f"{ada_name}_Eng"
        lit_ns = naming.Namespace()
        ctx.literals = [(lit_ns.assign(None, naming.ada_ident(t or f"V{v}")), int(v))
                        for v, t in sc.labels.items()]
    elif isinstance(sc, LUTScaling):
        ctx.kind = "lut"
        ctx.ranges = [(_real(r.begin), _real(r.end), _real(r.lsb), _real(r.offset))
                      for r in sc.ranges]
    return ctx


# ---------------------------------------------------------------------- #
# Campos
# ---------------------------------------------------------------------- #
def _root_pkg_of(entity: TypeDef) -> str:
    node = entity
    while node.parent is not None:
        node = node.parent
    return naming.ada_ident(getattr(node, "name", "Icd"))


class _Scope:
    """Estado de construcción de un fichero de compuesto/mensaje Ada."""

    def __init__(self, module_ctx: ModuleCtxA, scalar_of: Dict[int, ScalarCtxA],
                 text_of: Dict[int, TextCtxA], comp_of: Dict[int, FileCtxA]):
        self.m = module_ctx
        self.scalar_of = scalar_of
        self.text_of = text_of
        self.comp_of = comp_of
        self.withs: List[str] = []
        self.local_ns = naming.Namespace()

    def add_with(self, unit: str) -> None:
        if unit not in self.withs:
            self.withs.append(unit)


def _scalar_pack_ada(item: str, comp_type: str, start: int, length: int,
                     encoding: str) -> Tuple[str, str, str]:
    """(default, pack_stmt, unpack_stmt) para un componente escalar."""
    enc = (encoding or "").lower()
    if enc == "twocomplement":
        pack = (f"ICD_Bitio.Set_Bits (Data, Base + {start}, {length}, "
                f"ICD_Bitio.Enc_Twoc (Interfaces.Integer_64 ({item}), {length}));")
        unpack = (f"{item} := {comp_type} (ICD_Bitio.Dec_Twoc "
                  f"(ICD_Bitio.Get_Bits (Data, Base + {start}, {length}), {length}));")
        return " := 0", pack, unpack
    if enc == "ieee754":
        fn = "F32" if length == 32 else "F64"
        pack = (f"ICD_Bitio.Set_Bits (Data, Base + {start}, {length}, "
                f"ICD_Bitio.Enc_{fn} ({item}));")
        unpack = (f"{item} := ICD_Bitio.Dec_{fn} "
                  f"(ICD_Bitio.Get_Bits (Data, Base + {start}, {length}));")
        return " := 0.0", pack, unpack
    fn = "Bcd" if enc == "bcd" else "Uint"
    pack = (f"ICD_Bitio.Set_Bits (Data, Base + {start}, {length}, "
            f"ICD_Bitio.Enc_{fn} (Interfaces.Unsigned_64 ({item}), {length}));")
    unpack = (f"{item} := {comp_type} (ICD_Bitio.Dec_{fn} "
              f"(ICD_Bitio.Get_Bits (Data, Base + {start}, {length}), {length}));")
    return " := 0", pack, unpack


def _field_comp(f: Field, scope: _Scope, cls: ClassCtxA) -> None:
    dt = f.datatype
    if dt is None:
        target = f.ref.href if f.ref else "(vacío)"
        raise Unsupported(f"campo '{f.name}' sin tipo resuelto -> {target}")
    if isinstance(dt, (ArrayType, VariantType)):
        raise Unsupported(
            f"campo '{f.name}': {type(dt).__name__} aún no soportado por el generador")

    start, length = field_span(f)
    if length > 64 and not isinstance(dt, (TextType, CompositeType)):
        raise Unsupported(f"campo '{f.name}': {length} bits > 64")
    name = scope.local_ns.assign(f, naming.ada_ident(f.name or "Campo"))
    ref_note = f" -> {dt.name}" if dt.name and dt.name != f.name else ""
    comment = f"bits {start + 1} .. {start + length} (ICD: {f.name or '?'}{ref_note})"
    item = f"Item.{name}"

    if isinstance(dt, ScalarType):
        # tipo crudo: compartido (types del módulo que sea) o inline (local)
        if id(dt) in scope.scalar_of:
            pkg = f"{_root_pkg_of(dt)}.Types"
            comp_type = f"{pkg}.{scope.scalar_of[id(dt)].raw_type}"
            scope.add_with(pkg)
        else:
            local = naming.ada_ident(dt.name or f.name or "Campo") + "_Raw"
            local = scope.local_ns.assign(dt, local)
            cls.local_decls.extend(_raw_decl(local, dt))
            comp_type = local
        default, pack, unpack = _scalar_pack_ada(item, comp_type, start, length,
                                                 dt.encoding)
        cls.components.append(RecordComp(name, comp_type, default, comment))
        cls.pack_stmts.append(pack)
        cls.unpack_stmts.append(unpack)
        return

    if isinstance(dt, TextType):
        if id(dt) in scope.text_of:
            pkg = f"{_root_pkg_of(dt)}.Types"
            comp_type = f"{pkg}.{scope.text_of[id(dt)].subtype_name}"
            scope.add_with(pkg)
        else:
            comp_type = f"String (1 .. {dt.max_chars})"
        cls.components.append(RecordComp(name, comp_type, " := (others => ' ')",
                                         comment))
        cls.pack_stmts.append(
            f"ICD_Bitio.Put_Ascii (Data, Base + {start}, {item});")
        cls.unpack_stmts.append(
            f"ICD_Bitio.Get_Ascii (Data, Base + {start}, {item});")
        return

    if isinstance(dt, CompositeType):
        if id(dt) in scope.comp_of:
            target = scope.comp_of[id(dt)]
            comp_type = f"{target.package_name}.T"
            prefix = f"{target.package_name}."
            scope.add_with(target.package_name)
        else:
            inline = _build_class(dt, scope, inline=True)
            # el inline se declara antes en el mismo paquete
            scope.pending_inline.append(inline)
            comp_type = inline.type_name
            prefix = ""
        cls.components.append(RecordComp(name, comp_type, "", comment))
        cls.pack_stmts.append(f"{prefix}Pack ({item}, Data, Base + {start});")
        cls.unpack_stmts.append(f"{prefix}Unpack ({item}, Data, Base + {start});")
        return

    raise Unsupported(f"campo '{f.name}': tipo {type(dt).__name__} no soportado")


def _build_class(comp: CompositeType, scope: _Scope, inline: bool = False) -> ClassCtxA:
    if isinstance(comp, (ArrayType, VariantType)):
        raise Unsupported(f"{type(comp).__name__} '{comp.name}' aún no soportado")
    type_name = ("T" if not inline
                 else scope.local_ns.assign(comp, naming.ada_ident(comp.name or "Anon")))
    cls = ClassCtxA(
        type_name=type_name, kind_label=type(comp).__name__,
        icd_name=comp.name, icd_id=comp.id, path=comp.path,
        bit_length=type_bit_length(comp),
    )
    for f in comp.fields:
        _field_comp(f, scope, cls)
    return cls


# ---------------------------------------------------------------------- #
# Módulo completo
# ---------------------------------------------------------------------- #
def build_module_ctx(module: Module,
                     shared_index: Optional[Dict[int, object]] = None) -> ModuleCtxA:
    root_pkg = naming.ada_ident(module.name)
    m = ModuleCtxA(root_pkg=root_pkg, icd_name=module.name,
                   source_file=module.source_file)

    ns_types = naming.Namespace()
    shared_comps: List[CompositeType] = []
    messages: List[Message] = []
    scalar_of: Dict[int, ScalarCtxA] = dict(_GLOBAL_SCALARS)
    text_of: Dict[int, TextCtxA] = dict(_GLOBAL_TEXTS)

    for entity in module.walk():
        if entity.parent is None or not isinstance(entity.parent, Folder):
            continue
        if isinstance(entity, ScalarType):
            ctx = _scalar_ctx(entity, ns_types)
            m.scalars.append(ctx)
            scalar_of[id(entity)] = ctx
        elif isinstance(entity, TextType):
            ada = ns_types.assign(entity, naming.ada_ident(entity.name))
            ctx_t = TextCtxA(ada_name=ada, subtype_name=f"{ada}_Text",
                             chars=entity.max_chars, icd_name=entity.name,
                             icd_id=entity.id, path=entity.path)
            m.texts.append(ctx_t)
            text_of[id(entity)] = ctx_t
        elif isinstance(entity, RecordType):
            shared_comps.append(entity)
        elif isinstance(entity, (ArrayType, VariantType)):
            m.warnings.append(
                f"{type(entity).__name__} '{entity.name}' omitido: aún no "
                "soportado por el generador")
        elif isinstance(entity, Message):
            messages.append(entity)

    # publicar los tipos de este módulo para referencias entre módulos
    _GLOBAL_SCALARS.update({k: v for k, v in scalar_of.items()})
    _GLOBAL_TEXTS.update({k: v for k, v in text_of.items()})

    comp_of: Dict[int, FileCtxA] = dict(_GLOBAL_COMPS)
    pkg_ns = naming.Namespace()

    for comp in _topo_sort(shared_comps, m.warnings):
        pkg = f"{root_pkg}.{pkg_ns.assign(comp, naming.ada_ident(comp.name or 'Anon'))}"
        fctx = FileCtxA(package_name=pkg, file_stem=naming.ada_file(pkg))
        scope = _Scope(m, scalar_of, text_of, comp_of)
        scope.pending_inline = []
        try:
            main = _build_class(comp, scope)
        except (Unsupported, CodecError) as exc:
            m.warnings.append(f"'{comp.name}' omitido: {exc}")
            continue
        fctx.doc = f"{main.kind_label} '{comp.name}' -- ICD: {comp.path} [{comp.id}]"
        fctx.withs = scope.withs
        fctx.classes = [*scope.pending_inline, main]
        comp_of[id(comp)] = fctx
        _GLOBAL_COMPS[id(comp)] = fctx
        m.files.append(fctx)

    for msg in messages:
        try:
            m.files.append(_message_file(msg, m, root_pkg, pkg_ns,
                                         scalar_of, text_of, comp_of))
        except (Unsupported, CodecError) as exc:
            m.warnings.append(f"mensaje '{msg.name}' omitido: {exc}")

    return m


def _message_file(msg: Message, m: ModuleCtxA, root_pkg: str,
                  pkg_ns: naming.Namespace, scalar_of, text_of, comp_of) -> FileCtxA:
    st = msg.structure
    if st is None:
        raise Unsupported("sin payload resuelto")
    pkg = f"{root_pkg}.{pkg_ns.assign(msg, naming.ada_ident(msg.name or 'Mensaje'))}"
    fctx = FileCtxA(package_name=pkg, file_stem=naming.ada_file(pkg),
                    doc=f"Message '{msg.name}' -- ICD: {msg.path} [{msg.id}]")
    scope = _Scope(m, scalar_of, text_of, comp_of)
    scope.pending_inline = []

    if id(st) in comp_of:
        target = comp_of[id(st)]
        payload_ref, payload_pack = f"{target.package_name}.T", f"{target.package_name}."
        scope.add_with(target.package_name)
    elif isinstance(st, CompositeType):
        body = _build_class(st, scope, inline=True)
        scope.pending_inline.append(body)
        payload_ref, payload_pack = body.type_name, ""
    else:
        raise Unsupported("payload no compuesto")

    bits = type_bit_length(st)
    fctx.withs = scope.withs
    fctx.classes = scope.pending_inline
    fctx.message = MessageCtxA(
        payload_ref=payload_ref, payload_pack=payload_pack,
        byte_length=(bits + 7) // 8, period=msg.period, rate_mode=msg.rate_mode)
    return fctx


# Índices entre módulos (misma sesión de generación): un módulo generado
# publica sus tipos para que otros los referencien por 'with'.
_GLOBAL_SCALARS: Dict[int, ScalarCtxA] = {}
_GLOBAL_TEXTS: Dict[int, TextCtxA] = {}
_GLOBAL_COMPS: Dict[int, FileCtxA] = {}


def reset_session() -> None:
    """Limpia los índices entre módulos (inicio de una generación nueva)."""
    _GLOBAL_SCALARS.clear()
    _GLOBAL_TEXTS.clear()
    _GLOBAL_COMPS.clear()
