"""Tests del backend Ada 95: compila con GNAT (-gnat95) y compara con el oráculo.

Si no hay gnatmake instalado, los tests de compilación se marcan SKIP (el
render y las normas se comprueban igualmente).

Ejecutar con:  python3 tests/test_codegen_ada.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.codec import encode_message
from core.codegen.generator import generate
from core.model import Message
from core.parser import ICDParser
from core.registry import ICDRegistry

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GNATMAKE = shutil.which("gnatmake")

_HARNESS = """\
with Ada.Text_IO;
with ICD_Bitio;
with Fcs_Icd.Nav_Block;
with Fcs_Icd.Nav_Msg;
with Fcs_Icd.Types;
with Nested_Icd.Outer_Block;
with Nested_Icd.Outer_Msg;
with Nested_Icd.Types;

procedure Main is
   use Ada.Text_IO;
   Hex : constant String := "0123456789ABCDEF";

   procedure Put_Hex (Data : ICD_Bitio.Byte_Array) is
   begin
      for I in Data'Range loop
         Put (Hex (Natural (Data (I)) / 16 + 1));
         Put (Hex (Natural (Data (I)) mod 16 + 1));
      end loop;
   end Put_Hex;

   Nav      : Fcs_Icd.Nav_Block.T;
   Nav2     : Fcs_Icd.Nav_Block.T;
   Nav_Data : Fcs_Icd.Nav_Msg.Message_Bytes;
   Outer      : Nested_Icd.Outer_Block.T;
   Outer2     : Nested_Icd.Outer_Block.T;
   Outer_Data : Nested_Icd.Outer_Msg.Message_Bytes;
   use type Fcs_Icd.Nav_Block.T;
   use type Nested_Icd.Outer_Block.T;
begin
   Nav.Speed_Field   := 16#1234#;
   Nav.Alt_Field     := 16#0ABCDE#;
   Nav.Counter_Field := 16#5A#;
   Fcs_Icd.Nav_Msg.Encode (Nav, Nav_Data);
   Put ("NAV=");  Put_Hex (Nav_Data);  New_Line;
   Fcs_Icd.Nav_Msg.Decode (Nav_Data, Nav2);
   if Nav2 = Nav then Put_Line ("NAV_RT=OK"); else Put_Line ("NAV_RT=FAIL"); end if;

   Outer.Status := 1;
   Outer.Sub_Block.Inner_Temp := 16#1EEF#;
   Nested_Icd.Outer_Msg.Encode (Outer, Outer_Data);
   Put ("OUTER=");  Put_Hex (Outer_Data);  New_Line;
   Nested_Icd.Outer_Msg.Decode (Outer_Data, Outer2);
   if Outer2 = Outer then Put_Line ("OUTER_RT=OK"); else Put_Line ("OUTER_RT=FAIL"); end if;

   Put_Line ("ENG=" & Float'Image (Fcs_Icd.Types.To_Eng (Fcs_Icd.Types.Air_Speed_Raw (160))));
   Put_Line ("LUT=" & Float'Image (Nested_Icd.Types.To_Eng (Nested_Icd.Types.Temperature_Raw (150))));
end Main;
"""


def _load():
    reg = ICDRegistry()
    p = ICDParser(reg)
    base = p.parse_file(os.path.join(DATA_DIR, "BaseSignals.xmi"))
    fcs = p.parse_file(os.path.join(DATA_DIR, "FCS_ICD.xmi"))
    nested = p.parse_file(os.path.join(DATA_DIR, "Nested.xmi"))
    reg.resolve_references()
    return fcs, base, nested


def _generate_ada(tmp):
    fcs, base, nested = _load()
    # base primero: fcs referencia sus tipos (Altitude) via 'with'
    return [generate(m, tmp, language="ada") for m in (base, fcs, nested)]


# ---------------------------------------------------------------------- #
def test_render_files_and_norms():
    with tempfile.TemporaryDirectory() as tmp:
        results = _generate_ada(tmp)
        files = {p for r in results for p in r.files}
        # un fichero por elemento + raíz + tipos + runtime (spec 2.1/D-2)
        for expected in ("icd_bitio.ads", "icd_bitio.adb", "fcs_icd.ads",
                         "fcs_icd-types.ads", "fcs_icd-types.adb",
                         "fcs_icd-nav_block.ads", "fcs_icd-nav_msg.adb",
                         "nested_icd-outer_block.ads"):
            assert expected in files, f"falta {expected}"

        nav_block = open(os.path.join(tmp, "fcs_icd-nav_block.ads")).read()
        assert "NO EDITAR A MANO" in nav_block
        assert "[_st_navblock]" in nav_block                 # trazabilidad
        # Ada 95: cláusulas de representación, no aspects (spec 2.7)
        types = open(os.path.join(tmp, "fcs_icd-types.ads")).read()
        assert "for Air_Speed_Raw'Size use 16;" in types
        assert "with Size" not in types                      # sintaxis Ada 2012
        # referencia entre módulos via with
        assert "with Base_Signals.Types;" in nav_block


def test_cross_module_component_type():
    with tempfile.TemporaryDirectory() as tmp:
        _generate_ada(tmp)
        nav_block = open(os.path.join(tmp, "fcs_icd-nav_block.ads")).read()
        assert "Base_Signals.Types.Altitude_Raw" in nav_block


def test_deterministic_output():
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        r1 = {p: c for r in _generate_ada(t1) for p, c in r.files.items()}
        r2 = {p: c for r in _generate_ada(t2) for p, c in r.files.items()}
        assert r1 == r2


def test_compile_and_run_matches_oracle():
    """Compila con -gnat95 y compara los bytes con el codec-oráculo."""
    if GNATMAKE is None:
        print("    (SKIP: gnatmake no disponible)")
        return
    fcs, _, nested = _load()
    oracle_nav = encode_message(
        fcs.find_one(Message, "NavMsg"),
        {"speedField": 0x1234, "altField": 0x0ABCDE, "counterField": 0x5A})
    oracle_outer = encode_message(
        nested.find_one(Message, "OuterMsg"),
        {"status": 1, "subBlock": {"innerTemp": 0x1EEF}})

    with tempfile.TemporaryDirectory() as tmp:
        _generate_ada(tmp)
        with open(os.path.join(tmp, "main.adb"), "w") as fh:
            fh.write(_HARNESS)
        build = subprocess.run([GNATMAKE, "-gnat95", "-q", "main"],
                               cwd=tmp, capture_output=True, text=True)
        assert build.returncode == 0, f"no compila en Ada 95:\n{build.stdout}{build.stderr}"
        run = subprocess.run([os.path.join(tmp, "main")],
                             capture_output=True, text=True)
        out = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)

    assert out["NAV"] == oracle_nav.hex().upper()
    assert out["OUTER"] == oracle_outer.hex().upper()
    assert out["NAV_RT"] == "OK" and out["OUTER_RT"] == "OK"
    assert float(out["ENG"]) == 10.0        # lineal: 160 * 0.0625
    assert float(out["LUT"]) == 87.5        # LUT tramo 2: 150*0.25+50


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("\nTodos los tests pasan." if not failures else f"\n{failures} tests fallan.")
    return failures


if __name__ == "__main__":
    sys.exit(_run_all())
