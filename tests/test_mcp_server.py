"""Tests del servidor MCP: arranca el proceso real y llama a sus herramientas.

Cubre la capa de consulta (core/query.py) y el servidor MCP end-to-end por
stdio, como lo haría un agente.

Ejecutar con:  python3 tests/test_mcp_server.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.query import ICDQuery, QueryError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "tests", "data")


def _q() -> ICDQuery:
    return ICDQuery(DATA_DIR)


# ---------------------------------------------------------------------- #
# Capa de consulta
# ---------------------------------------------------------------------- #
def test_load_and_modules():
    q = _q()
    info = q.load(DATA_DIR)
    assert info["source"] == "xml"
    assert set(info["modules"]) == {"BaseSignals", "Broken_ICD", "FCS_ICD", "Nested_ICD"}
    names = {m["name"]: m for m in q.modules()}
    assert names["FCS_ICD"]["messages"] == 1
    assert names["FCS_ICD"]["types"] >= 4


def test_message_fields_flattened():
    q = _q()
    msg = q.message("NavMsg")
    assert msg["total_bits"] == 48 and msg["total_bytes"] == 6
    assert msg["period"] == 40
    fields = {f["field"]: f for f in msg["fields"]}
    assert set(fields) == {"speedField", "altField", "counterField"}
    # posición 1-based y longitud, tal como los usa el codec
    assert fields["speedField"]["length_bits"] == 16
    assert fields["speedField"]["max_position"] == 16
    assert fields["altField"]["max_position"] == 40
    assert "0.0625" in fields["speedField"]["scaling"]
    assert fields["altField"]["reference_id"] == "_sig_alt"    # entre archivos
    assert fields["counterField"]["origin"] == "inline"


def test_nested_message_descends_into_substructures():
    q = _q()
    msg = q.message("OuterMsg")
    fields = [f["field"] for f in msg["fields"]]
    assert "status" in fields and "subBlock" in fields and "innerTemp" in fields
    inner = next(f for f in msg["fields"] if f["field"] == "innerTemp")
    assert inner["level"] == 1                                  # anidado
    assert "LUT" in inner["scaling"]


def test_type_decoding_info():
    q = _q()
    gear = q.type("GearStatus")
    assert gear["kind"] == "ScalarType"
    assert gear["scaling"]["kind"] == "enum"
    labels = {r["valor"]: r["estado"] for r in gear["scaling"]["labels"]}
    assert labels == {"0": "UP", "1": "DOWN", "2": "TRANSIT"}

    temp = q.type("Temperature")
    assert temp["scaling"]["kind"] == "lut"
    assert len(temp["scaling"]["ranges"]) == 2

    block = q.type("NavBlock")
    assert block["kind"] == "RecordType"
    assert block["total_bits"] == 48
    assert len(block["fields"]) == 3


def test_get_field_and_search():
    q = _q()
    fld = q.field("NavMsg", "altField")
    assert fld["length_bits"] == 24 and fld["container"] == "NavMsg"
    hits = {h["name"] for h in q.search("nav")}
    assert {"NavMsg", "NavBlock", "NavHeader"} <= hits


def test_encode_decode_round_trip():
    q = _q()
    enc = q.encode("NavMsg", {"speedField": 10.0, "altField": 1000,
                              "counterField": 7})
    assert enc["hex"] == "00 A0 00 03 E8 07"
    dec = q.decode("NavMsg", enc["hex"])
    assert dec["values"] == {"speedField": 10.0, "altField": 1000, "counterField": 7}
    # crudo: el escalado no se aplica
    raw = q.decode("NavMsg", enc["hex"], engineering=False)
    assert raw["values"]["speedField"] == 160


def test_errors_are_helpful():
    q = _q()
    for call, expected in (
        (lambda: q.message("NoExiste"), "no se encontró"),
        (lambda: q.field("NavMsg", "nope"), "Campos:"),
        (lambda: q.decode("NavMsg", "zz"), "hex inválido"),
    ):
        try:
            call()
            assert False, "debía fallar"
        except QueryError as exc:
            assert expected in str(exc), str(exc)


def test_issues_listing():
    q = _q()
    errors = q.issues(level="ERROR")
    assert any("sin longitud en bits" in i["message"] for i in errors)
    broken = q.issues(module="Broken_ICD")
    assert broken and all(i["module"] == "Broken_ICD" for i in broken)


# ---------------------------------------------------------------------- #
# Servidor MCP end-to-end (proceso real, transporte stdio)
# ---------------------------------------------------------------------- #
async def _with_session(fn):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "mcp_server.py"), "--project", DATA_DIR],
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _payload(result):
    """Contenido estructurado de un resultado de herramienta."""
    data = getattr(result, "structured_content", None)
    if data:
        return data.get("result", data)
    return json.loads(result.content[0].text)


def test_mcp_tools_exposed():
    async def go(session):
        tools = await session.list_tools()
        return {t.name for t in tools.tools}
    names = asyncio.run(_with_session(go))
    assert {"list_modules", "list_messages", "get_message", "get_field",
            "list_types", "get_type", "search", "list_issues",
            "decode_message", "encode_message", "load_project"} <= names


def test_mcp_get_message():
    async def go(session):
        res = await session.call_tool("get_message", {"name_or_id": "NavMsg"})
        return _payload(res)
    msg = asyncio.run(_with_session(go))
    assert msg["name"] == "NavMsg" and msg["total_bytes"] == 6
    fields = {f["field"]: f for f in msg["fields"]}
    assert fields["speedField"]["max_position"] == 16
    assert fields["altField"]["length_bits"] == 24


def test_mcp_encode_decode():
    async def go(session):
        enc = await session.call_tool(
            "encode_message",
            {"name_or_id": "OuterMsg",
             "values": {"status": "ON", "subBlock": {"innerTemp": 25.0}}})
        enc_data = _payload(enc)
        dec = await session.call_tool(
            "decode_message",
            {"name_or_id": "OuterMsg", "data_hex": enc_data["hex"]})
        return enc_data, _payload(dec)
    enc, dec = asyncio.run(_with_session(go))
    assert enc["hex"] == "01 00 00 32 00 00"
    assert dec["values"] == {"status": "ON", "subBlock": {"innerTemp": 25.0}}


def test_mcp_reports_errors():
    async def go(session):
        res = await session.call_tool("get_message", {"name_or_id": "NoExiste"})
        return res
    res = asyncio.run(_with_session(go))
    assert res.is_error, "debía marcarse como error"
    assert "no se encontró" in res.content[0].text


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
