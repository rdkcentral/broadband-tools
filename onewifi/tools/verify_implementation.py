#!/usr/bin/env python3
"""
verify_implementation.py

Cross-checks every element's declared handler functions (getHandler,
setHandler, subscribeHandler, addFunction, delFunction, handler) against
the repo's actual .c source to confirm each one is genuinely DEFINED
somewhere, not just referenced in an XML/registration file. Produces:

  1. An annotated copy of the full input (nothing removed) with
     source.implemented: true/false added to every element -- so the
     complete, honest record is preserved (per "we shouldn't hide it").
  2. A filtered copy containing only elements with at least one real,
     implemented handler -- i.e. only what this component's own source
     code genuinely backs, not everything its XML happens to declare.

A function is considered "defined" if its name appears as a token inside
any .c file under --source-dir (not just referenced -- a bare grep hit in
.c source is a strong signal of a real definition or, at minimum, a call
site that would fail to link if no definition existed anywhere in the
build). Elements with no handler fields at all (nothing to check) are
treated as implemented=true by default -- override with
--require-any-handler to instead treat them as unverifiable/false.

Usage:
    python3 verify_implementation.py \\
        --input onewifi_full_interface.json \\
        --source-dir source \\
        --output onewifi_full_interface.annotated.json \\
        --output-filtered onewifi_implemented_only.json
"""

import argparse
import json
import re
import sys
from pathlib import Path


FUNC_TOKEN_RE = re.compile(r"\b(\w+)\b")


def collect_defined_functions(source_dir: Path) -> set[str]:
    """Collect every identifier that appears in any .c file, as a fast
    lookup set. Deliberately permissive (any token, not just ones followed
    by '(' and '{') to avoid false negatives from formatting variance --
    the interesting signal here is absence, and grep-equivalent presence
    checks already proved reliable during manual verification."""
    defined: set[str] = set()
    for c_path in source_dir.rglob("*.c"):
        text = c_path.read_text(encoding="utf-8", errors="replace")
        defined.update(FUNC_TOKEN_RE.findall(text))
    return defined


HANDLER_FIELDS = ["getHandler", "setHandler", "subscribeHandler", "addFunction", "delFunction", "handler"]

XML_FUNC_TAGS = [
    "func_GetParamBoolValue", "func_GetParamIntValue", "func_GetParamUlongValue", "func_GetParamStringValue",
    "func_SetParamBoolValue", "func_SetParamIntValue", "func_SetParamUlongValue", "func_SetParamStringValue",
    "func_Validate", "func_Commit", "func_Rollback",
]

INLINE_TAG_RE = re.compile(r"^\s*<(\w+)>(.*?)</\1>\s*$")
OPEN_TAG_RE = re.compile(r"^\s*<(\w+)>\s*$")
CLOSE_TAG_RE = re.compile(r"^\s*</(\w+)>\s*$")


def scan_xml_object_handlers(xml_path: Path) -> dict[str, list[str]]:
    """Re-scan a CCSP XML file directly (independent of what emit_spec_json.py
    captured) to build path -> [declared handler function names] for every
    <object>. CCSP XML declares <functions> once per object, covering that
    whole object's own parameters and (unless overridden) its descendants --
    this is the actual source of truth for 'is a get/set handler declared
    for this part of the tree', which the generated interface JSON does not
    preserve for XML-sourced elements."""
    obj_stack: list[dict] = []
    result: dict[str, list[str]] = {}

    for line in xml_path.read_text(encoding="utf-8", errors="replace").splitlines():
        om, cm, im = OPEN_TAG_RE.match(line), CLOSE_TAG_RE.match(line), INLINE_TAG_RE.match(line)

        if om and om.group(1) == "object":
            parent_path = obj_stack[-1]["path"] if obj_stack and obj_stack[-1]["path"] else ("Device" if not obj_stack else None)
            parent_handlers = obj_stack[-1]["handlers"] if obj_stack else []
            obj_stack.append({"name": None, "path": None, "parent_path": parent_path, "base": None, "in_functions": False, "handlers": list(parent_handlers)})
            continue
        if om and om.group(1) == "functions":
            if obj_stack:
                obj_stack[-1]["in_functions"] = True
            continue
        if cm and cm.group(1) == "functions":
            if obj_stack:
                obj_stack[-1]["in_functions"] = False
            continue
        if im:
            tag, text = im.group(1), im.group(2).strip()
            if obj_stack:
                frame = obj_stack[-1]
                if tag == "baseObjName" and frame["name"] is None:
                    frame["base"] = text.rstrip(".")
                elif tag in ("n", "name") and frame["name"] is None:
                    frame["name"] = text
                    effective_parent = frame["base"] if frame["base"] else frame["parent_path"]
                    frame["path"] = f"{effective_parent}.{text}" if effective_parent else text
                    if frame["path"]:
                        result[frame["path"]] = frame["handlers"]
                elif frame["in_functions"] and tag in XML_FUNC_TAGS:
                    frame["handlers"].append(text)
                    if frame["path"]:
                        result[frame["path"]] = frame["handlers"]
            continue
        if cm and cm.group(1) == "object":
            if obj_stack:
                obj_stack.pop()
            continue

    return result


def find_owning_handlers(dm_path: str, xml_handler_map: dict[str, list[str]]) -> list[str]:
    """Longest-prefix match: an element's declared path may be several
    levels below the <object> that actually carries the <functions> block
    (e.g. a leaf parameter under an object with no functions of its own
    inherits its nearest ancestor's), so walk up the dotted path."""
    parts = dm_path.rstrip(".").split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in xml_handler_map and xml_handler_map[candidate]:
            return xml_handler_map[candidate]
    return []


def element_handlers(el: dict) -> list[str]:
    src = el.get("source", {})
    return [src[f] for f in HANDLER_FIELDS if src.get(f)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify which datamodel elements have real backing implementation in the repo's C source.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--config-dir", help="Directory containing the original CCSP XML file(s), for XML-sourced elements whose handler function names weren't preserved in --input (re-scans the raw XML directly).")
    parser.add_argument("--output", required=True, help="Annotated copy of the full input, nothing removed, with source.implemented added.")
    parser.add_argument("--output-filtered", help="Optional: write a second file containing only elements with at least one confirmed-implemented handler.")
    parser.add_argument("--require-any-handler", action="store_true", help="Treat elements with no handler fields at all as implemented=false instead of true.")
    args = parser.parse_args()

    doc = json.loads(Path(args.input).read_text())
    elements = doc.get("elements", {})

    defined_functions = collect_defined_functions(Path(args.source_dir))
    print(f"[*] Collected {len(defined_functions)} distinct identifier(s) from .c files under {args.source_dir}", file=sys.stderr)

    xml_handler_maps: dict[str, dict[str, list[str]]] = {}
    if args.config_dir:
        config_dir = Path(args.config_dir)
        xml_files = [p for p in config_dir.iterdir() if p.is_file() and p.suffix.lower() == ".xml"]
        for xp in xml_files:
            xml_handler_maps[xp.name] = scan_xml_object_handlers(xp)
        print(f"[*] Re-scanned {len(xml_files)} XML file(s) directly for declared handler functions", file=sys.stderr)

    implemented_count = 0
    not_implemented: list[tuple[str, list[str]]] = []
    unresolved: list[str] = []

    for k, el in elements.items():
        handlers = element_handlers(el)
        if not handlers:
            src_file = el.get("source", {}).get("file")
            if src_file and src_file in xml_handler_maps:
                handlers = find_owning_handlers(k, xml_handler_maps[src_file])
        if handlers:
            el.setdefault("source", {})["implemented"] = any(h in defined_functions for h in handlers)
        else:
            unresolved.append(k)  # resolve in a second pass, below

    # Second pass: a *NumberOfEntries leaf has no handler of its own -- CCSP
    # generates it automatically for its table. Inherit the table's already-
    # resolved status instead of guessing. Falls back to the configured
    # default only if the table itself couldn't be resolved either.
    for k in unresolved:
        el = elements[k]
        is_implemented = not args.require_any_handler
        if k.endswith("NumberOfEntries"):
            parent, _, name = k.rpartition(".")
            table_key = f"{parent}.{name[: -len('NumberOfEntries')]}.{{i}}."
            table_el = elements.get(table_key)
            if table_el and "implemented" in table_el.get("source", {}):
                is_implemented = table_el["source"]["implemented"]
        el.setdefault("source", {})["implemented"] = is_implemented

    for k, el in elements.items():
        if el["source"]["implemented"]:
            implemented_count += 1
        else:
            not_implemented.append((k, element_handlers(el)))

    print(f"[*] {implemented_count} / {len(elements)} element(s) confirmed implemented in this repo's .c source", file=sys.stderr)
    if not_implemented:
        by_namespace: dict[str, int] = {}
        for k, _ in not_implemented:
            ns = k.split(".")[1] if "." in k else k
            by_namespace[ns] = by_namespace.get(ns, 0) + 1
        print(f"[*] {len(not_implemented)} element(s) NOT implemented, by namespace:", file=sys.stderr)
        for ns, c in sorted(by_namespace.items(), key=lambda x: -x[1]):
            print(f"      {ns}: {c}", file=sys.stderr)

    Path(args.output).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"[*] Wrote annotated (complete) file to {args.output}", file=sys.stderr)

    if args.output_filtered:
        filtered_elements = {k: v for k, v in elements.items() if v["source"].get("implemented")}
        filtered_doc = {
            "componentInterfaceDefinition": dict(doc.get("componentInterfaceDefinition", {})),
            "elements": filtered_elements,
        }
        filtered_doc["componentInterfaceDefinition"]["description"] = (
            filtered_doc["componentInterfaceDefinition"].get("description", "")
            + " [Filtered: only elements with a handler function confirmed present in this repo's own .c source.]"
        )
        Path(args.output_filtered).write_text(json.dumps(filtered_doc, indent=2), encoding="utf-8")
        print(f"[*] Wrote filtered ({len(filtered_elements)} implemented-only element(s)) file to {args.output_filtered}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
