from pathlib import Path


def test_schema_loads():
    """schema.json exists, has ≥20 entries, each with required fields."""
    import json
    schema_path = Path(__file__).resolve().parent.parent / "warera_ask" / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert isinstance(schema, list)
    assert len(schema) >= 20
    for entry in schema:
        assert "endpoint" in entry
        assert "description" in entry
        assert "params" in entry
        assert "returns" in entry


def test_endpoint_name_roundtrip():
    """Dot-to-dunder and back is lossless for all schema endpoints."""
    import json
    schema_path = Path(__file__).resolve().parent.parent / "warera_ask" / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for entry in schema:
        original = entry["endpoint"]
        assert "__" not in original, f"Schema endpoint {original!r} contains '__' which breaks round-trip"
        mangled = original.replace(".", "__")
        restored = mangled.replace("__", ".")
        assert restored == original, f"Round-trip failed for {original!r}"


def test_no_eval_in_module():
    """The cog must not contain eval() or exec() — they were removed for security.

    Restricted-builtins eval is escapable via object hierarchy traversal
    (e.g. (1).__class__.__bases__[0].__subclasses__()), so the calculate
    tool was dropped entirely. This test prevents a regression that
    re-introduces a sandbox.
    """
    import ast

    src = Path(__file__).resolve().parent.parent / "warera_ask" / "warera_ask.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("eval", "exec"), (
                f"{node.func.id}() call found at line {node.lineno} — this is "
                "a sandbox-escape risk. Use AST-based evaluation or remove."
            )
