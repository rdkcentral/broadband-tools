#!/usr/bin/env python3
"""
merge_interface_json.py

Merge two or more Component Interface Definition JSON files (same schema,
e.g. output from emit_spec_json.py and extract_wfa_dml.py for the same
component) into a single file. Useful when a component's data model is
split across multiple registration conventions that need separate
extractors (like OneWiFi: standard rbusDataElement_t/XML + the WFA
bus_data_cb_func_t convention).

Key collisions (the same dm_path present in more than one input file) are
reported and, by default, resolved by keeping the FIRST file's version
(pass --last-wins to prefer the last file instead) -- either way, nothing
is silently dropped without being reported.

Usage:
    python3 merge_interface_json.py \\
        --name OneWifi --version 0.1.0-draft \\
        --description "WiFi management, radio/VAP configuration, client steering, and mesh networking, including EasyMesh Multi-AP." \\
        --input onewifi_interface.json \\
        --input onewifi_wfa_interface.json \\
        --output onewifi_full_interface.json
"""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge multiple Component Interface Definition JSON files into one.")
    parser.add_argument("--input", action="append", required=True, help="Input JSON file. Repeatable, order matters for collision resolution.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--name", help="Merged document's component name. Defaults to the first input file's name.")
    parser.add_argument("--version", default="0.1.0-draft")
    parser.add_argument("--description", help="Merged document's description. Defaults to the first input file's description.")
    parser.add_argument("--module-name", help="Optional moduleName for the merged header.")
    parser.add_argument("--last-wins", action="store_true", help="On key collision, keep the LAST file's version instead of the first.")
    args = parser.parse_args()

    docs = []
    for path in args.input:
        docs.append((path, json.loads(Path(path).read_text())))

    if not docs:
        print("ERROR: no input files given", file=sys.stderr)
        return 1

    first_cid = docs[0][1].get("componentInterfaceDefinition", {})
    merged_elements: dict = {}
    origin: dict[str, str] = {}
    collisions = []

    ordered = docs if not args.last_wins else list(reversed(docs))
    for path, doc in ordered:
        els = doc.get("elements", {})
        for k, v in els.items():
            if k in merged_elements:
                collisions.append((k, origin[k], path))
                continue
            merged_elements[k] = v
            origin[k] = path

    print(f"[*] Merged {len(docs)} file(s): {[p for p, _ in docs]}", file=sys.stderr)
    for path, doc in docs:
        print(f"      {path}: {len(doc.get('elements', {}))} element(s)", file=sys.stderr)
    print(f"[*] Total unique elements after merge: {len(merged_elements)}", file=sys.stderr)
    if collisions:
        print(f"[*] {len(collisions)} key collision(s) found -- kept the {'last' if args.last_wins else 'first'} file's version for each:", file=sys.stderr)
        for k, kept_from, dropped_from in collisions:
            print(f"      {k}  (kept from {kept_from}, dropped from {dropped_from})", file=sys.stderr)

    merged_doc = {
        "componentInterfaceDefinition": {
            "name": args.name or first_cid.get("name", "Unknown"),
            **({"moduleName": args.module_name} if args.module_name else ({"moduleName": first_cid["moduleName"]} if "moduleName" in first_cid else {})),
            "version": args.version,
            "description": args.description or first_cid.get("description", ""),
        },
        "elements": dict(sorted(merged_elements.items())),
    }

    Path(args.output).write_text(json.dumps(merged_doc, indent=2), encoding="utf-8")
    print(f"[*] Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
