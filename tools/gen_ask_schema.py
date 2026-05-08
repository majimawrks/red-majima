#!/usr/bin/env python3
"""Compile _warera-fetch/specs/*/spec.md -> warera_ask/schema.json"""
import json
from pathlib import Path

SPECS_DIR = Path(__file__).resolve().parent.parent / "_warera-fetch" / "specs"
OUT = Path(__file__).resolve().parent.parent / "warera_ask" / "schema.json"

# Endpoints that require JWT auth — exclude from schema
EXCLUDE = {"referral.getUserReferrals", "referral.getUserReferralsPaginated"}


def parse_spec(spec_path: Path) -> dict | None:
    endpoint_name = spec_path.parent.name
    if endpoint_name in EXCLUDE:
        return None

    text = spec_path.read_text(encoding="utf-8")

    # Extract description (first non-heading, non-blank line after the h1)
    desc = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if line and not line.startswith("#"):
            desc = line
            break

    # Extract params from the markdown table under ## Input
    params = {}
    in_input = False
    for line in lines:
        if line.strip().startswith("## Input"):
            in_input = True
            continue
        if in_input and line.strip().startswith("## "):
            break
        if in_input and "|" in line and "---" not in line and "Parameter" not in line:
            normalized = line.replace(r"\|", "\x00")
            cols = [c.replace("\x00", "|").strip().strip("`") for c in normalized.split("|")[1:-1]]
            if len(cols) >= 4:
                params[cols[0]] = {
                    "type": cols[1],
                    "required": "yes" in cols[2].lower(),
                    "description": cols[3],
                }

    # Extract key return fields from ## Output section
    returns = []
    in_output = False
    for line in lines:
        if line.strip().startswith("## Output"):
            in_output = True
            continue
        if in_output and line.strip().startswith("## "):
            break
        if in_output and line.strip().startswith("- "):
            field = line.strip().lstrip("- ").split(" — ")[0].strip("`")
            returns.append(field)

    # Extract auth requirement
    auth = "none"
    for idx, line in enumerate(lines):
        if line.strip().lower().startswith("## auth"):
            if idx + 1 < len(lines):
                auth = lines[idx + 1].strip().lower()
            break

    return {
        "endpoint": endpoint_name,
        "description": desc,
        "auth": auth,
        "params": params,
        "returns": returns,
    }


def main():
    specs = []
    for spec_dir in sorted(SPECS_DIR.iterdir()):
        if not spec_dir.is_dir():
            continue
        spec_md = spec_dir / "spec.md"
        if spec_md.exists():
            parsed = parse_spec(spec_md)
            if parsed:
                specs.append(parsed)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(specs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(specs)} endpoints to {OUT}")


if __name__ == "__main__":
    main()
