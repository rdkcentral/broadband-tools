#!/usr/bin/env python3
"""
extract_wfa_dml.py

Standalone extractor for a THIRD RDK-B datamodel registration convention,
found in OneWiFi's WFA/EasyMesh (Multi-AP, Device.WiFi.DataElements.*) code.
This is deliberately separate from emit_spec_json.py rather than folded
into it, since the convention is structurally different enough to warrant
its own tool:

  1. Registration array type is "bus_data_cb_func_t", not "rbusDataElement_t".
  2. Entries are {NAME_MACRO, {6-tuple}} -- two elements, no explicit
     RBUS_ELEMENT_TYPE_* field -- and span multiple lines.
  3. Path names are never literal strings at the point of registration --
     they're macros built via C string-literal concatenation, often chained
     3+ levels deep, e.g.:
         #define DATAELEMS_NETWORK_OBJ  "Device.WiFi.DataElements.Network"
         #define DATAELEMS_NETWORK      DATAELEMS_NETWORK_OBJ "."
         #define DE_NETWORK_SSID        DATAELEMS_NETWORK     "SSID.{i}."
         #define DE_SSID_SSID           DE_NETWORK_SSID       "SSID"
     A plain grep for "Device.WiFi.DataElements" over the .c registration
     file finds nothing, because that literal text only ever appears in the
     base macro's own #define line and in comments.
  4. Only a handful of entries (object/table-level) are actually registered
     in the bus_data_cb_func_t array -- individual leaf parameters (e.g.
     DE_SSID_BAND, DE_SSID_ENABLE) are handled generically by their parent
     table's single get/set callback, not registered one-by-one. To recover
     them, this tool treats EVERY macro in the given header(s) whose fully
     resolved value starts with "Device." as a real data-model path, and
     separately cross-references the actual registration array to attach
     real access rights (from non-NULL callback slots) to the handful of
     paths that have a direct registration. Paths without a direct
     registration get an access of ["read"] with source.inferred = true,
     since they're reachable through their parent's generic handler but
     their own precise access rights aren't independently declared.

Usage:
    python3 extract_wfa_dml.py \\
        --header source/platform/common/data_model/wfa/wfa_data_model.h \\
        --source source/platform/common/data_model/wfa/wfa_data_model.c \\
        --name OneWifi-WFA \\
        --output onewifi_wfa_interface.json
"""

import argparse
import json
import re
import sys
from pathlib import Path


DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(?:\([^)]*\))?\s+(.+?)\s*(?://.*)?$")
ARRAY_START_RE = re.compile(r"bus_data_cb_func_t\s+(\w+)\s*\[\s*\d*\s*\]\s*=\s*\{?\s*$")
BARE_OPEN_BRACE_RE = re.compile(r"^\s*\{\s*$")
ARRAY_END_RE = re.compile(r"^\s*\}\s*;\s*$")

TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\w+)')


def collect_macros(header_paths: list[Path]) -> dict[str, str]:
    """Collect raw #define macro bodies verbatim (not yet resolved)."""
    macros: dict[str, str] = {}
    for h in header_paths:
        for line in h.read_text(encoding="utf-8", errors="replace").splitlines():
            # Skip commented-out defines
            if re.match(r"^\s*//\s*#\s*define", line):
                continue
            m = DEFINE_RE.match(line)
            if m:
                macros[m.group(1)] = m.group(2).strip()
    return macros


def resolve_macro(name_or_literal: str, macros: dict[str, str], _seen: set[str] | None = None) -> str | None:
    """Resolve a macro name (or already-quoted literal) to its fully
    concatenated string value, following identifier chains and joining
    adjacent string-literal tokens the way the C preprocessor/compiler
    would. Returns None if the name isn't a macro and isn't a literal."""
    _seen = _seen or set()
    token = name_or_literal.strip()

    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]

    if token not in macros:
        return None
    if token in _seen:
        return None  # cycle guard
    _seen = _seen | {token}

    body = macros[token]
    parts: list[str] = []
    for m in TOKEN_RE.finditer(body):
        literal, ident = m.group(1), m.group(2)
        if literal is not None:
            parts.append(literal)
        elif ident is not None:
            resolved = resolve_macro(ident, macros, _seen)
            if resolved is None:
                # Unresolvable identifier (e.g. a non-string macro like
                # MAX_INSTANCE_LEN) -- this macro isn't a path fragment.
                return None
            parts.append(resolved)
    return "".join(parts)


def split_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current))
    return [a.strip() for a in args]


def split_balanced(text: str, open_ch: str, close_ch: str) -> tuple[str, int]:
    depth = 1
    for idx, ch in enumerate(text):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[:idx], idx + 1
    return text, len(text)


def split_top_level_entries(body: str) -> list[str]:
    entries: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "{":
            depth += 1
            if depth == 1:
                current = []
                continue
        elif ch == "}":
            if depth == 1:
                entries.append("".join(current))
                depth -= 1
                continue
            depth -= 1
        if depth >= 1:
            current.append(ch)
    return entries


def find_registration_entries(source_path: Path, macros: dict[str, str]) -> dict[str, dict]:
    """Parse the bus_data_cb_func_t[] array: entries are
    { NAME_TOKEN, { get, set, add_row, rm_row, event_sub, method } }.
    Returns resolved_path -> {access-relevant handler names}."""
    raw = source_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    registrations: dict[str, dict] = {}
    in_array = False
    pending_name = None
    array_name = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_array:
            am = ARRAY_START_RE.search(line)
            if am:
                array_name = am.group(1)
                if line.rstrip().endswith("{"):
                    in_array = True
                else:
                    pending_name = array_name
            i += 1
            continue
        if pending_name is not None:
            if BARE_OPEN_BRACE_RE.match(line):
                in_array = True
            pending_name = None
            i += 1
            continue
        if ARRAY_END_RE.match(line):
            break
        i += 1

    if not in_array and not array_name:
        return registrations

    # Re-extract the whole array body via brace balancing for robust
    # multi-line entry parsing (entries in this file span several lines).
    m = re.search(rf"{re.escape(array_name)}\s*\[\s*\d*\s*\]\s*=\s*", raw)
    if not m:
        return registrations
    rest = raw[m.end():]
    brace_idx = rest.find("{")
    if brace_idx == -1:
        return registrations
    body, _ = split_balanced(rest[brace_idx + 1:], "{", "}")

    for entry_text in split_top_level_entries(body):
        parts = split_args(entry_text)
        if len(parts) < 2:
            continue
        name_token, cb_text = parts[0].strip(), parts[1].strip()
        if cb_text.startswith("{") and cb_text.endswith("}"):
            cb_text = cb_text[1:-1]
        cb_fields = split_args(cb_text)
        cb_fields = (cb_fields + ["NULL"] * 6)[:6]
        get_h, set_h, add_h, del_h, sub_h, method_h = [f.strip() for f in cb_fields]

        resolved = resolve_macro(name_token, macros)
        if resolved is None:
            continue

        registrations[resolved] = {
            "get": None if get_h == "NULL" else get_h,
            "set": None if set_h == "NULL" else set_h,
            "add": None if add_h == "NULL" else add_h,
            "del": None if del_h == "NULL" else del_h,
            "sub": None if sub_h == "NULL" else sub_h,
            "method": None if method_h == "NULL" else method_h,
        }

    return registrations


def classify_and_build(resolved: str, reg: dict | None, macro_name: str) -> tuple[str, dict]:
    """Return (dm_key, element_dict) for the schema shapes used elsewhere
    in this toolset (leaf / table / method), inferring access from the
    registration entry when one exists, else a conservative read-only
    default with an 'inferred' marker."""
    is_table = resolved.rstrip(".").endswith("}")  # ends in "{i}" (with or without trailing '.')
    is_method = resolved.endswith("()")

    if is_table:
        key = resolved if resolved.endswith(".") else resolved + "."
        access = ["read"]
        add_fn = del_fn = None
        inferred = reg is None
        if reg:
            if reg["add"]:
                access.append("addInstance")
                add_fn = reg["add"]
            if reg["del"]:
                access.append("removeInstance")
                del_fn = reg["del"]
        parent, _, name = key.rstrip(".").removesuffix("{i}").rstrip(".").rpartition(".")
        noe_guess = f"{parent}.{name}NumberOfEntries" if parent else ""
        return key, {
            "access": access,
            "maxInstance": "",
            "numberOfEntriesElement": noe_guess,
            "description": "",
            "flags": [""],
            "source": {"macro": macro_name, "addFunction": add_fn, "delFunction": del_fn, "inferred": inferred},
        }

    if is_method:
        access = ["invokeSync"]
        handler = reg["method"] if reg else None
        source = {"macro": macro_name, "inferred": reg is None}
        if handler:
            source["handler"] = handler
        return resolved, {
            "access": access,
            "description": "",
            "input": [],
            "output": [],
            "flags": [""],
            "source": source,
        }

    # leaf property
    access = ["read"]
    inferred = reg is None
    get_h = set_h = sub_h = None
    if reg:
        get_h, set_h, sub_h = reg["get"], reg["set"], reg["sub"]
        if reg["set"]:
            access.append("write")
        if reg["sub"]:
            access.append("subscribeOnChange")
    source = {"macro": macro_name, "inferred": inferred}
    if get_h:
        source["getHandler"] = get_h
    if set_h:
        source["setHandler"] = set_h
    if sub_h:
        source["subscribeHandler"] = sub_h
    return resolved, {
        "access": access,
        "type": "string",
        "description": "",
        "persistence": "",
        "defaultValue": "",
        "enumeration": [],
        "flags": [""],
        "source": source,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Device.WiFi.DataElements.* (WFA/EasyMesh) elements from OneWiFi's macro-based bus_data_cb_func_t convention.")
    parser.add_argument("--header", required=True, action="append", help="Header file(s) with the #define path macros. Repeatable.")
    parser.add_argument("--source", required=True, action="append", help="Source file(s) with the bus_data_cb_func_t[] registration array. Repeatable.")
    parser.add_argument("--name", default="OneWifi-WFA")
    parser.add_argument("--version", default="0.1.0-draft")
    parser.add_argument("--description", default="EasyMesh Multi-AP (Device.WiFi.DataElements.*) data model, extracted from OneWiFi's macro-based bus_data_cb_func_t registration convention.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    header_paths = [Path(h) for h in args.header]
    source_paths = [Path(s) for s in args.source]

    macros = collect_macros(header_paths)
    print(f"[*] Collected {len(macros)} macro(s) from {len(header_paths)} header file(s)", file=sys.stderr)

    registrations: dict[str, dict] = {}
    for sp in source_paths:
        found = find_registration_entries(sp, macros)
        print(f"[*] {sp.name}: {len(found)} direct registration entrie(s)", file=sys.stderr)
        registrations.update(found)

    elements: dict = {}
    resolved_count = 0
    for macro_name in macros:
        resolved = resolve_macro(macro_name, macros)
        if resolved is None or not resolved.startswith("Device."):
            continue
        resolved_count += 1
        reg = registrations.get(resolved)
        key, el = classify_and_build(resolved, reg, macro_name)
        elements[key] = el

    directly_registered = sum(1 for v in elements.values() if not v["source"].get("inferred"))
    inferred = len(elements) - directly_registered

    print(f"[*] {resolved_count} macro(s) resolved to a Device.* path", file=sys.stderr)
    print(f"[*] Total unique elements: {len(elements)}  ({directly_registered} with real registered access, {inferred} inferred read-only via parent table)", file=sys.stderr)

    doc = {
        "componentInterfaceDefinition": {
            "name": args.name,
            "version": args.version,
            "description": args.description,
        },
        "elements": dict(sorted(elements.items())),
    }
    Path(args.output).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"[*] Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
