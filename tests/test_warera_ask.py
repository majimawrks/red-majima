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
        mangled = original.replace(".", "__")
        restored = mangled.replace("__", ".")
        assert restored == original, f"Round-trip failed for {original!r}"


def test_safe_builtins_blocks_dangerous_calls():
    """_SAFE_BUILTINS does not expose __import__ or open."""
    import ast

    src = Path(__file__).resolve().parent.parent / "warera_ask" / "warera_ask.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    safe_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_SAFE_BUILTINS":
                    if isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant):
                                safe_names.add(key.value)

    assert safe_names, "_SAFE_BUILTINS not found or empty in warera_ask.py"

    # Dangerous builtins must not be present
    assert "__import__" not in safe_names
    assert "open" not in safe_names
    assert "exec" not in safe_names
    assert "eval" not in safe_names

    # Safe builtins must be present
    assert "len" in safe_names
    assert "sum" in safe_names
    assert "sorted" in safe_names

    # Verify that eval with these extracted names cannot call __import__
    # Build a minimal builtins dict from the actual module-level values
    safe_builtins_live = {
        name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
        for name in safe_names
        if (
            isinstance(__builtins__, dict) and name in __builtins__
        ) or (
            not isinstance(__builtins__, dict) and hasattr(__builtins__, name)
        )
    }
    try:
        eval("__import__('os')", {"__builtins__": safe_builtins_live}, {})
        assert False, "Should have raised NameError"
    except NameError:
        pass  # Expected
