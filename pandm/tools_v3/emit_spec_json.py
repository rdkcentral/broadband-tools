#!/usr/bin/env python3
"""
Emit a Component Interface Definition JSON document conforming to
component_interface_definition.schema.json, directly from repo source.

This is a separate tool from extract_dml.py / extract_rbus_dml.py /
extract_all_dml.py rather than a new --output mode on them, because its
output shape is fundamentally different:

  - Multi-instance objects stay as literal "{i}" templates (the schema
    describes the STATIC data model, not a live device's instance count),
    where the other three tools expand {i} into concrete 1..N numbers.
  - Table objects (Interface.{i}., Marking.{i}., ...) are emitted as
    first-class entries with real addInstance/removeInstance derived from
    XML func_AddEntry/func_DelEntry -- the other tools discard tables
    entirely (XML tables become non_entries only; rbus tables were
    counted but not emitted at all).
  - Every element carries a real "flags" array: the literal ifdef/ifndef
    condition(s) active where it was declared/registered, not a filtered
    view. This is genuine per-line build-flag provenance, not the
    evaluate-then-discard approach the other tools use.
  - Properties get a real "access" list (read/write/subscribeOnChange for
    rbus-registered properties, derived from which callback slots in the
    rbusDataElement_t tuple are non-NULL) instead of a single writable bool.

Parsing approach: RDK-B's CCSP XML and rbus C registration tables both
follow a consistent one-tag-per-line / one-entry-per-line convention in
practice (verified against the actual RdkWanManager repo before writing
this). Rather than a general XML/C parser with full position tracking,
this uses line-oriented scanners tailored to that convention. This is a
deliberate scope trade-off: simpler and more transparent than a real
parser+AST, but will need adjusting if a file's formatting departs
significantly from one-tag-per-line.

Usage:
    python3 tools/emit_spec_json.py --repo-root . \\
        --name RdkWanManager --module-name TR181_RdkWanManager \\
        --description "WAN interface selection, failover, and IP/DHCP/MAP-T/DS-Lite config" \\
        --prefix Device.X_RDK_WanManager \\
        --output wanmanager_interface.json
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


TABLE_TYPES = {"dynamicTable", "writableTable", "staticTable"}

PI_LINE_RE = re.compile(r"^\s*<\?(ifdef|ifndef|else|endif)\s*([^?]*)\?>\s*$", re.IGNORECASE)
INLINE_TAG_RE = re.compile(r"^\s*<(\w+)>(.*?)</\1>\s*$")
OPEN_TAG_RE = re.compile(r"^\s*<(\w+)>\s*$")
CLOSE_TAG_RE = re.compile(r"^\s*</(\w+)>\s*$")


def flags_snapshot(stack: list[dict]) -> list[str]:
    if not stack:
        return [""]
    return [f"{f['kind']} {f['flag']}" for f in stack]


def parse_type(raw_type: str | None) -> str:
    if not raw_type:
        return "string"
    raw = raw_type.strip()
    if raw.startswith("boolean"):
        return "boolean"
    if raw.startswith("unsignedInt") or raw.startswith("uint"):
        return "unsignedInt"
    if raw == "int":
        return "int"
    if raw.startswith("string"):
        return "string(mapped)" if ":" in raw else "string"
    return raw.split(":")[0].split("[")[0].strip()


# ---------------------------------------------------------------------------
# XML line-oriented scanner
# ---------------------------------------------------------------------------


def scan_xml_file(xml_path: Path, elements: dict) -> None:
    lines = xml_path.read_text(encoding="utf-8", errors="replace").splitlines()

    flag_stack: list[dict] = []
    obj_stack: list[dict] = []  # each: {name, path, object_type, max_instance, add_fn, del_fn, in_functions, in_parameters}
    cur_param: dict | None = None

    def current_path_for_children(frame: dict) -> str:
        if frame["path"] is None:
            # Malformed/redundant XML -- an <object> that opened and closed
            # without ever getting a <name>/<n> (seen in practice: two
            # consecutive <object> opening tags with nothing between them).
            # Treat it as a transparent wrapper rather than propagating the
            # literal string "None" into every descendant's path.
            return frame["parent_path"]
        if frame["object_type"] in TABLE_TYPES:
            return f"{frame['path']}.{{i}}"
        return frame["path"]

    for line in lines:
        pi = PI_LINE_RE.match(line)
        if pi:
            kind, arg = pi.group(1).lower(), pi.group(2).strip()
            if kind in ("ifdef", "ifndef"):
                flag_stack.append({"kind": kind, "flag": arg})
            elif kind == "else":
                if flag_stack:
                    top = flag_stack[-1]
                    other = "ifndef" if top["kind"] == "ifdef" else "ifdef"
                    flag_stack[-1] = {"kind": other, "flag": top["flag"]}
            elif kind == "endif":
                if flag_stack:
                    flag_stack.pop()
            continue

        open_m = OPEN_TAG_RE.match(line)
        close_m = CLOSE_TAG_RE.match(line)
        inline_m = INLINE_TAG_RE.match(line)

        if open_m and open_m.group(1) == "object":
            parent_path = current_path_for_children(obj_stack[-1]) if obj_stack else "Device"
            obj_stack.append(
                {
                    "name": None,
                    "path": None,
                    "parent_path": parent_path,
                    "base_obj_name": None,
                    "object_type": "object",
                    "max_instance": "",
                    "add_fn": None,
                    "del_fn": None,
                    "in_functions": False,
                    "in_parameters": False,
                    "flags_at_open": flags_snapshot(flag_stack),
                }
            )
            continue

        if open_m and open_m.group(1) == "functions" and obj_stack:
            obj_stack[-1]["in_functions"] = True
            continue
        if close_m and close_m.group(1) == "functions" and obj_stack:
            obj_stack[-1]["in_functions"] = False
            continue

        if open_m and open_m.group(1) == "parameters" and obj_stack:
            obj_stack[-1]["in_parameters"] = True
            continue
        if close_m and close_m.group(1) == "parameters" and obj_stack:
            obj_stack[-1]["in_parameters"] = False
            continue

        if open_m and open_m.group(1) == "parameter" and obj_stack and obj_stack[-1]["in_parameters"]:
            cur_param = {"name": None, "type": None, "writable": False, "flags": flags_snapshot(flag_stack)}
            continue

        if close_m and close_m.group(1) == "parameter" and cur_param is not None:
            if cur_param["name"] and obj_stack and obj_stack[-1]["path"]:
                child_base = current_path_for_children(obj_stack[-1])
                dm_path = f"{child_base}.{cur_param['name']}"
                access = ["read"] + (["write"] if cur_param["writable"] else [])
                elements[dm_path] = {
                    "access": access,
                    "type": parse_type(cur_param["type"]),
                    "description": "",
                    "persistence": "",
                    "defaultValue": "",
                    "enumeration": [],
                    "flags": cur_param["flags"],
                    "source": {"file": xml_path.name},
                }
            cur_param = None
            continue

        if inline_m:
            tag, text = inline_m.group(1), inline_m.group(2).strip()
            if cur_param is not None:
                if tag in ("n", "name"):
                    cur_param["name"] = text
                elif tag == "type":
                    cur_param["type"] = text
                elif tag == "writable":
                    cur_param["writable"] = text.strip().lower() == "true"
                continue
            if obj_stack:
                frame = obj_stack[-1]
                if tag == "baseObjName" and frame["name"] is None:
                    # Explicit absolute attachment point for this object,
                    # overriding normal nesting -- e.g. a component's own
                    # XML declares <baseObjName>Device.IP.</baseObjName> to
                    # attach its Diagnostics object under an object tree
                    # owned/declared elsewhere, rather than under wherever
                    # this object happens to be nested in THIS file.
                    frame["base_obj_name"] = text.rstrip(".")
                elif tag in ("n", "name") and frame["name"] is None:
                    frame["name"] = text
                    effective_parent = frame["base_obj_name"] if frame["base_obj_name"] else frame["parent_path"]
                    frame["path"] = f"{effective_parent}.{text}"
                elif tag == "objectType":
                    frame["object_type"] = text
                elif tag == "maxInstance":
                    frame["max_instance"] = text
                elif tag == "func_AddEntry" and frame["in_functions"]:
                    frame["add_fn"] = text
                elif tag == "func_DelEntry" and frame["in_functions"]:
                    frame["del_fn"] = text
            continue

        if close_m and close_m.group(1) == "object" and obj_stack:
            frame = obj_stack.pop()
            if frame["path"] and frame["object_type"] in TABLE_TYPES:
                table_path = f"{frame['path']}.{{i}}"
                parent, _, name = frame["path"].rpartition(".")
                noe_path = f"{parent}.{name}NumberOfEntries"
                access = ["read"]
                if frame["add_fn"]:
                    access.append("addInstance")
                if frame["del_fn"]:
                    access.append("removeInstance")
                elements[f"{table_path}."] = {
                    "access": access,
                    "maxInstance": frame["max_instance"],
                    "numberOfEntriesElement": noe_path,
                    "description": "",
                    "flags": frame["flags_at_open"],
                    "source": {
                        "file": xml_path.name,
                        "addFunction": frame["add_fn"],
                        "delFunction": frame["del_fn"],
                    },
                }
                elements[noe_path] = {
                    "access": ["read"],
                    "type": "unsignedInt",
                    "description": "",
                    "persistence": "",
                    "defaultValue": "",
                    "enumeration": [],
                    "flags": frame["flags_at_open"],
                    "source": {"file": xml_path.name},
                }
            continue


# ---------------------------------------------------------------------------
# rbus C line-oriented scanner
# ---------------------------------------------------------------------------

DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(?:\([^)]*\))?\s*(.*?)\s*(?://.*)?$")
DIRECTIVE_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
ARRAY_START_RE = re.compile(r"rbusDataElement_t\s+(\w+)\s*\[\s*\w*\s*\]\s*=\s*\{?\s*$")
ARRAY_END_RE = re.compile(r"^\s*\}\s*;\s*$")
BARE_OPEN_BRACE_RE = re.compile(r"^\s*\{\s*$")
ENTRY_LINE_RE = re.compile(
    r'^\s*\{\s*(?P<name>"[^"]*"|\w+)\s*,\s*(?P<etype>RBUS_ELEMENT_TYPE_\w+)\s*,\s*\{(?P<cb>[^}]*)\}\s*\}\s*,?\s*(?://.*)?$'
)

TYPE_MAP = {
    "RBUS_ELEMENT_TYPE_PROPERTY": "property",
    "RBUS_ELEMENT_TYPE_TABLE": "table",
    "RBUS_ELEMENT_TYPE_EVENT": "event",
    "RBUS_ELEMENT_TYPE_METHOD": "method",
}


def eval_c_expr_bool(expr: str) -> bool:
    # For provenance mode we don't evaluate against an enabled-flag set --
    # we record the condition text itself. This helper is unused here but
    # kept for symmetry / potential --enable-flag filtering later.
    return True


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


def collect_macros_from_headers(source_dir: Path) -> dict[str, list[tuple[str, list[str]]]]:
    """Scan every header for #define macros, respecting the HEADER'S OWN
    #if/#ifdef/#else/#endif structure -- a macro name can be (and in this
    codebase, is) #define'd differently in different branches, e.g.:

        #if defined(WAN_MANAGER_UNIFICATION_ENABLED)
        #define WANMGR_INFACE_WAN_ENABLE   "Device.X_RDK_WanManager.Interface.{i}.Selection.Enable"
        #else
        #define WANMGR_INFACE_WAN_ENABLE   "Device.X_RDK_WanManager.CPEInterface.{i}.Wan.Enable"
        #endif

    Returns name -> [(value, flags_list), ...] preserving EVERY definition
    with the condition it was defined under, rather than collapsing to a
    single value (which would silently keep whichever definition happens to
    be textually last -- the wrong one, in general).
    """
    macros: dict[str, list[tuple[str, list[str]]]] = {}
    for h_path in sorted(source_dir.rglob("*.h")) + sorted(source_dir.rglob("*.hpp")):
        stack: list[dict] = []  # each: {"repr": "ifdef X" | "ifndef X" | "if EXPR"}
        for line in h_path.read_text(encoding="utf-8", errors="replace").splitlines():
            d = DIRECTIVE_RE.match(line)
            if d:
                kind, rest = d.group(1), d.group(2).strip()
                if kind == "ifdef":
                    stack.append({"repr": f"ifdef {rest}"})
                elif kind == "ifndef":
                    stack.append({"repr": f"ifndef {rest}"})
                elif kind in ("if", "elif"):
                    stack.append({"repr": f"if {rest}"})
                elif kind == "else":
                    if stack:
                        top_repr = stack[-1]["repr"]
                        if top_repr.startswith("ifdef "):
                            stack[-1] = {"repr": "ifndef " + top_repr[len("ifdef "):]}
                        elif top_repr.startswith("ifndef "):
                            stack[-1] = {"repr": "ifdef " + top_repr[len("ifndef "):]}
                        else:
                            expr = top_repr.split(" ", 1)[1] if " " in top_repr else ""
                            stack[-1] = {"repr": f"if !({expr})"}
                elif kind == "endif":
                    if stack:
                        stack.pop()
                continue
            m = DEFINE_RE.match(line)
            if m:
                name, value = m.group(1), m.group(2).strip()
                flags_list = [f["repr"] for f in stack] if stack else [""]
                macros.setdefault(name, []).append((value, flags_list))
    return macros


def _flag_name(condition: str) -> str:
    m = re.search(r"defined\s*\(\s*([A-Za-z_]\w*)\s*\)", condition)
    if m:
        return m.group(1)
    parts = condition.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else condition.strip()


def _flag_polarity(condition: str) -> str:
    kind = condition.split(None, 1)[0] if condition.strip() else ""
    return "neg" if kind == "ifndef" else "pos"


def resolve_macro_in_context(name: str, macros: dict[str, list[tuple[str, list[str]]]], local_flags: list[str]) -> str | None:
    """Pick the macro definition whose own governing condition is consistent
    with local_flags (the flags stack at the point of use) -- e.g. an entry
    registered inside #if defined(X) should resolve a macro using that same
    macro's #if defined(X) definition, not its #else counterpart."""
    candidates = macros.get(name)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    local_pos = {_flag_name(f) for f in local_flags if f.strip() and _flag_polarity(f) == "pos"}
    local_neg = {_flag_name(f) for f in local_flags if f.strip() and _flag_polarity(f) == "neg"}

    best_value, best_score = None, -1
    for value, flags_list in candidates:
        score, contradiction = 0, False
        for f in flags_list:
            if not f.strip():
                continue
            fname, pol = _flag_name(f), _flag_polarity(f)
            if pol == "pos":
                if fname in local_pos:
                    score += 1
                elif fname in local_neg:
                    contradiction = True
            else:
                if fname in local_neg:
                    score += 1
                elif fname in local_pos:
                    contradiction = True
        if contradiction:
            continue
        if score > best_score:
            best_score, best_value = score, value
    if best_value is None:
        best_value = candidates[-1][0]  # fallback: no context match, keep old behavior
    return best_value


def scan_c_file(c_path: Path, elements: dict, header_macros: dict[str, list[tuple[str, list[str]]]]) -> None:
    raw = c_path.read_text(encoding="utf-8", errors="replace")

    # Pass 1: collect #define macros local to this .c file, layered on top of
    # the header-derived macro table (local redefinitions win, matching
    # normal C translation-unit semantics closely enough for our purposes).
    macros: dict[str, list[tuple[str, list[str]]]] = {k: list(v) for k, v in header_macros.items()}
    for line in raw.splitlines():
        m = DEFINE_RE.match(line)
        if m:
            macros.setdefault(m.group(1), []).append((m.group(2).strip(), [""]))

    # Pass 1b: collect simple single-substitution snprintf/sprintf aliases --
    # `snprintf(buf, N, "%s", SOME_MACRO_OR_LITERAL);` fully determines buf's
    # value at compile time (unlike multi-substitution calls that splice in
    # a genuinely runtime-varying argument like a component name), so a
    # local buffer populated this way and later used as an entry's name is
    # just as resolvable as a direct macro reference.
    SPRINTF_ALIAS_RE = re.compile(r'\b(?:sn?printf)\(\s*(\w+)\s*(?:,\s*[^,]+)?,\s*"%s"\s*,\s*([\w"]+)\s*\)\s*;')
    local_buffer_aliases: dict[str, str] = {}
    for m in SPRINTF_ALIAS_RE.finditer(raw):
        buf_name, source_token = m.group(1), m.group(2)
        local_buffer_aliases[buf_name] = source_token

    def resolve_name(token: str, local_flags: list[str]) -> str | None:
        token = token.strip()
        if token.startswith('"') and token.endswith('"'):
            return token[1:-1]
        if token in local_buffer_aliases:
            return resolve_name(local_buffer_aliases[token], local_flags)
        value = resolve_macro_in_context(token, macros, local_flags)
        if value is not None:
            return resolve_name(value, local_flags)
        return None

    def compute_noe_path(table_path: str) -> str:
        p = table_path.rstrip(".")
        if p.endswith(".{i}"):
            p = p[: -len(".{i}")]
        parent, _, name = p.rpartition(".")
        return f"{parent}.{name}NumberOfEntries"

    flag_stack: list[dict] = []
    in_array = False
    array_name = ""
    pending_array_name: str | None = None  # declaration line seen, brace not yet found

    for line in raw.splitlines():
        d = DIRECTIVE_RE.match(line)
        if d:
            kind, rest = d.group(1), d.group(2).strip()
            if kind in ("ifdef", "ifndef"):
                flag = rest.split()[0] if rest else ""
                flag_stack.append({"kind": kind, "flag": flag})
            elif kind == "if":
                # record the raw expression verbatim as provenance text
                flag_stack.append({"kind": "if", "flag": rest})
            elif kind == "elif":
                if flag_stack:
                    flag_stack[-1] = {"kind": "elif", "flag": rest}
            elif kind == "else":
                if flag_stack:
                    top = flag_stack[-1]
                    other = "ifndef" if top["kind"] == "ifdef" else "ifdef"
                    flag_stack[-1] = {"kind": other, "flag": top.get("flag", "")}
            elif kind == "endif":
                if flag_stack:
                    flag_stack.pop()
            continue

        # Handles both `rbusDataElement_t NAME[] = {` (brace on the same line,
        # as in RdkWanManager/CcspEthAgent) and `rbusDataElement_t NAME[] =`
        # followed by a bare `{` on the next line (as in this repo's
        # DevicePrioritization/LatencyMeasurement/dmltad files).
        if pending_array_name is not None:
            if BARE_OPEN_BRACE_RE.match(line):
                in_array = True
                array_name = pending_array_name
            elif line.strip():
                pass  # unexpected content between declaration and brace -- give up on this array
            pending_array_name = None
            continue

        if not in_array:
            am = ARRAY_START_RE.search(line)
            if am:
                if line.rstrip().endswith("{"):
                    in_array = True
                    array_name = am.group(1)
                else:
                    pending_array_name = am.group(1)
            continue

        if ARRAY_END_RE.match(line):
            in_array = False
            array_name = ""
            continue

        em = ENTRY_LINE_RE.match(line)
        if not em:
            continue

        etype = TYPE_MAP.get(em.group("etype"))
        if etype is None:
            continue
        entry_local_flags = flags_snapshot(flag_stack)
        resolved = resolve_name(em.group("name"), entry_local_flags)
        if resolved is None:
            continue

        cb_fields = split_args(em.group("cb"))
        get_h = cb_fields[0].strip() if len(cb_fields) > 0 else "NULL"
        set_h = cb_fields[1].strip() if len(cb_fields) > 1 else "NULL"
        add_h = cb_fields[2].strip() if len(cb_fields) > 2 else "NULL"
        del_h = cb_fields[3].strip() if len(cb_fields) > 3 else "NULL"
        sub_h = cb_fields[4].strip() if len(cb_fields) > 4 else "NULL"
        method_h = cb_fields[5].strip() if len(cb_fields) > 5 else "NULL"
        flags = flags_snapshot(flag_stack)

        if etype == "table":
            # tableAddRowHandler/tableRemoveRowHandler are cb_fields[2]/[3] --
            # same tuple position pattern already used for get/set/subscribe/
            # method. A table only gets addInstance/removeInstance if these
            # are genuinely non-NULL in this specific table's registration --
            # verified per-table, not assumed uniform (feedback point 4).
            access = ["read"]
            if add_h != "NULL":
                access.append("addInstance")
            if del_h != "NULL":
                access.append("removeInstance")
            noe_path = compute_noe_path(resolved)
            source = {"file": c_path.name, "table": array_name}
            source["addFunction"] = add_h if add_h != "NULL" else None
            source["delFunction"] = del_h if del_h != "NULL" else None
            elements[resolved if resolved.endswith(".") else resolved + "."] = {
                "access": access,
                "maxInstance": "",
                "numberOfEntriesElement": noe_path,
                "description": "",
                "flags": flags,
                "source": source,
            }
            continue

        if etype == "event":
            # Pure notification element: no read/write, subscribeOnChange only,
            # with a placeholder 'output' payload array per feedback point 6
            # (populate with real payload fields where known -- e.g. the
            # WCC_EVENT struct fields for WanMgr_Configure_WCC_Thread-driven
            # events -- this is left as an empty array here since payload
            # shape isn't declared in the rbusDataElement_t registration
            # itself and needs source-level documentation).
            elements[resolved] = {
                "access": ["subscribeOnChange"],
                "description": "",
                "output": [],
                "flags": flags,
                "source": {
                    "file": c_path.name,
                    "table": array_name,
                    **({"subscribeHandler": sub_h} if sub_h != "NULL" else {}),
                },
            }
            continue

        if etype == "method":
            # The rbus registration table's RBUS_ELEMENT_TYPE_METHOD marker is
            # the authoritative signal that this is a method -- some source
            # (e.g. webcfg's WEBCFG_UTIL_METHOD macro) omits the conventional
            # trailing "()" from the literal path string even though it's
            # genuinely registered and invoked as a method. Normalize the key
            # so it matches the schema's method pattern regardless.
            method_key = resolved if resolved.endswith("()") else resolved + "()"
            elements[method_key] = {
                "access": ["invokeSync"],
                "description": "",
                "input": [],
                "output": [],
                "flags": flags,
                "source": {
                    "file": c_path.name,
                    "table": array_name,
                    **({"handler": method_h} if method_h != "NULL" else {}),
                },
            }
            continue

        # leaf property
        access = ["read"]
        if set_h != "NULL":
            access.append("write")
        if sub_h != "NULL":
            access.append("subscribeOnChange")
        source = {"file": c_path.name, "table": array_name}
        if get_h != "NULL":
            source["getHandler"] = get_h
        if set_h != "NULL":
            source["setHandler"] = set_h
        if sub_h != "NULL":
            source["subscribeHandler"] = sub_h

        # A path already present (typically from an earlier XML scan of the
        # same object) may carry a real declared type -- XML's <type> tag is
        # explicit, while a bare rbus C registration has no type information
        # at all and would otherwise silently downgrade it to the generic
        # "string" default. Enrich rather than overwrite: keep the more
        # specific type and union the access rights instead of discarding
        # what the earlier scan already established.
        existing = elements.get(resolved)
        entry_type = "string"
        if existing and existing.get("type") and existing["type"] != "string":
            entry_type = existing["type"]
        if existing and "access" in existing:
            for a in existing["access"]:
                if a not in access:
                    access.append(a)

        elements[resolved] = {
            "access": access,
            "type": entry_type,
            "description": "",
            "persistence": "",
            "defaultValue": "",
            "enumeration": [],
            "flags": flags,
            "source": source,
        }


# ---------------------------------------------------------------------------
# Legacy "DSLH" convention (e.g. CcspTr069Pa's DslhManagementServer): neither
# rbusDataElement_t nor <dataModelInfo> XML -- parameters declared as flat
# msParameterInfo[] tuple arrays, attached to an object path through a
# separate objectInfo[ID].name / objectInfo[ID].parameters binding.
# ---------------------------------------------------------------------------

DSLH_ARRAY_START_RE = re.compile(r"msParameterInfo\s+(\w+)\s*\[\s*\]\s*=\s*")
DSLH_OBJ_NAME_ASSIGN_RE = re.compile(r"objectInfo\[(\w+)\]\.name\s*=\s*(?:AnscCloneString\()?\s*(\w+)\s*\)?\s*;")
DSLH_OBJ_PARAMS_ASSIGN_RE = re.compile(r"objectInfo\[(\w+)\]\.parameters\s*=\s*(\w+)\s*;")
DSLH_DYNAMIC_PARAM_NAME_RE = re.compile(r'objectInfo\[(\w+)\]\.parameters\[\w+\]\.name\s*=\s*AnscCloneString\(\s*"([^"]+)"\s*\)\s*;')
DSLH_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(?:\([^)]*\))?\s+(.+?)\s*(?://.*)?$")
DSLH_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\w+)')

DSLH_TYPE_MAP = {
    "ccsp_boolean": "boolean", "ccsp_string": "string", "ccsp_unsignedInt": "unsignedInt",
    "ccsp_int": "int", "ccsp_dateTime": "dateTime", "ccsp_base64": "base64", "ccsp_hexBinary": "hexBinary",
}


def dslh_present(c_files: list[Path]) -> bool:
    """Cheap check so this convention's (slower, whole-repo) macro pass only
    runs for repos that actually use it."""
    for c_path in c_files:
        if "msParameterInfo" in c_path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def dslh_collect_macros(source_dir: Path) -> dict[str, str]:
    macros: dict[str, str] = {}
    for path in list(source_dir.rglob("*.h")) + list(source_dir.rglob("*.hpp")) + list(source_dir.rglob("*.c")) + list(source_dir.rglob("*.cpp")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = DSLH_DEFINE_RE.match(line)
            if m:
                macros[m.group(1)] = m.group(2).strip()
    return macros


def dslh_resolve_macro(token: str, macros: dict[str, str], root_name: str, _seen: set[str] | None = None) -> str | None:
    _seen = _seen or set()
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    if token == "DM_ROOTNAME":
        return root_name
    if token not in macros or token in _seen:
        return None
    _seen = _seen | {token}
    parts: list[str] = []
    for m in DSLH_TOKEN_RE.finditer(macros[token]):
        literal, ident = m.group(1), m.group(2)
        if literal is not None:
            parts.append(literal)
        elif ident is not None:
            resolved = dslh_resolve_macro(ident, macros, root_name, _seen)
            if resolved is None:
                return None
            parts.append(resolved)
    return "".join(parts)


def dslh_split_balanced(text: str, open_ch: str, close_ch: str) -> str:
    depth = 1
    for idx, ch in enumerate(text):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[:idx]
    return text


def dslh_split_top_level_entries(body: str) -> list[str]:
    entries, depth, current = [], 0, []
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


def dslh_parse_parameter_arrays(text: str) -> dict[str, list[tuple[str, str, str]]]:
    arrays: dict[str, list[tuple[str, str, str]]] = {}
    for m in DSLH_ARRAY_START_RE.finditer(text):
        array_name = m.group(1)
        rest = text[m.end():]
        brace_idx = rest.find("{")
        if brace_idx == -1:
            continue
        body = dslh_split_balanced(rest[brace_idx + 1:], "{", "}")
        entries = []
        for entry_text in dslh_split_top_level_entries(body):
            fields = split_args(entry_text)
            if len(fields) < 4:
                continue
            name_token = fields[0].strip()
            if not (name_token.startswith('"') and name_token.endswith('"')):
                continue
            entries.append((name_token[1:-1], fields[2].strip(), fields[3].strip()))
        arrays[array_name] = entries
    return arrays


def dslh_parse_object_bindings(text: str) -> dict[str, dict[str, str]]:
    bindings: dict[str, dict[str, str]] = {}
    for m in DSLH_OBJ_NAME_ASSIGN_RE.finditer(text):
        bindings.setdefault(m.group(1), {})["name_token"] = m.group(2)
    for m in DSLH_OBJ_PARAMS_ASSIGN_RE.finditer(text):
        bindings.setdefault(m.group(1), {})["params_array"] = m.group(2)
    return bindings


def dslh_parse_dynamic_param_names(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for m in DSLH_DYNAMIC_PARAM_NAME_RE.finditer(text):
        obj_id, name = m.group(1), m.group(2)
        result.setdefault(obj_id, [])
        if name not in result[obj_id]:
            result[obj_id].append(name)
    return result


# ---------------------------------------------------------------------------
# C++ "rbus_table + registerPath" convention (e.g. DSM's dsm_rbus_provider.cpp):
# a hand-written provider class where top-level paths are registered via
#   registerPath( PATH_EXPR, rbus_data({..., rbusElementType_t::RBUS_ELEMENT_TYPE_X, ...}) );
# and multi-instance tables are declared via
#   rbus_table VAR ("TableName", {
#       {"FieldName", rbus_data({..., rbusElementType_t::RBUS_ELEMENT_TYPE_X, ...})},
#       ...
#   });
# ---------------------------------------------------------------------------

CPP_TABLE_TYPE_RE = re.compile(r"RBUS_ELEMENT_TYPE_(\w+)")
CPP_REGISTER_PATH_START_RE = re.compile(r"\bregisterPath\s*\(")
CPP_RBUS_TABLE_START_RE = re.compile(r'\brbus_table\s+\w+\s*\(\s*"([^"]+)"\s*,\s*\{')
CPP_CURRENT_TYPE_RE = re.compile(r'tables\["(\w+)"\]\.rows\[\w+\]\["(\w+)"\]\.current_type\s*=\s*rbusValueType_t::(\w+)\s*;')

CPP_RBUS_VALUE_TYPE_MAP = {
    "RBUS_BOOLEAN": "boolean", "RBUS_STRING": "string",
    "RBUS_INT8": "int", "RBUS_UINT8": "unsignedInt", "RBUS_INT16": "int", "RBUS_UINT16": "unsignedInt",
    "RBUS_INT32": "int", "RBUS_UINT32": "unsignedInt", "RBUS_INT64": "int", "RBUS_UINT64": "unsignedInt",
    "RBUS_SINGLE": "float", "RBUS_DOUBLE": "double", "RBUS_DATETIME": "dateTime",
    "RBUS_BYTE": "unsignedInt", "RBUS_CHAR": "int",
}


def cpp_collect_field_value_types(text: str) -> dict[tuple[str, str], str]:
    """Field declarations in this convention only carry a PROPERTY/METHOD
    kind marker, not a value type -- but elsewhere in the same file, code
    that populates a row's live value typically assigns
    tables["Table"].rows[i]["Field"].current_type = rbusValueType_t::RBUS_X,
    which is a real, precise signal for that field's actual type."""
    result: dict[tuple[str, str], str] = {}
    for m in CPP_CURRENT_TYPE_RE.finditer(text):
        table_name, field_name, rbus_type = m.group(1), m.group(2), m.group(3)
        if rbus_type in CPP_RBUS_VALUE_TYPE_MAP:
            result[(table_name, field_name)] = CPP_RBUS_VALUE_TYPE_MAP[rbus_type]
    return result


def cpp_extract_call_args(text: str, start_idx: int) -> tuple[str, int]:
    """text[start_idx] is the '(' right after a call name; returns
    (contents-between-the-balanced-parens, index just past the closing paren)."""
    depth = 1
    i = start_idx + 1
    begin = i
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[begin:i - 1], i


def cpp_resolve_path_expr(expr: str, prefix_macros: dict[str, str]) -> str | None:
    """Resolve a path expression like 'DEVICE_SM_PREFIX "InstallDU()"' against
    the specific macro(s) actually defined in this repo (name -> resolved
    "Device.*" value), or a bare string literal on its own."""
    expr = expr.strip()
    parts = []
    for m in DSLH_TOKEN_RE.finditer(expr):
        literal, ident = m.group(1), m.group(2)
        if literal is not None:
            parts.append(literal)
        elif ident is not None:
            if ident in prefix_macros:
                parts.append(prefix_macros[ident])
            else:
                return None  # unresolvable identifier (e.g. a local variable)
    return "".join(parts) if parts else None


def scan_cpp_file_rbustable(c_path: Path, elements: dict, prefix_macros: dict[str, str]) -> None:
    text = c_path.read_text(encoding="utf-8", errors="replace")
    if "registerPath" not in text and "rbus_table" not in text:
        return

    for m in CPP_REGISTER_PATH_START_RE.finditer(text):
        args_text, _ = cpp_extract_call_args(text, m.end() - 1)
        args = split_args(args_text)
        if len(args) < 2:
            continue
        path = cpp_resolve_path_expr(args[0], prefix_macros)
        if not path or not path.startswith("Device."):
            continue  # internal/non-TR-181 signal (e.g. "ee_external_change") -- not a data model path
        type_m = CPP_TABLE_TYPE_RE.search(args[1])
        etype = type_m.group(1) if type_m else "PROPERTY"
        if etype == "METHOD":
            elements[path if path.endswith("()") else path + "()"] = {
                "access": ["invokeSync"], "description": "", "input": [], "output": [],
                "flags": [""], "source": {"file": c_path.name},
            }
        elif etype == "EVENT":
            elements[path] = {
                "access": ["subscribeOnChange"], "description": "", "output": [],
                "flags": [""], "source": {"file": c_path.name},
            }
        else:
            elements[path] = {
                "access": ["read"], "type": "string", "description": "", "persistence": "",
                "defaultValue": "", "enumeration": [], "flags": [""], "source": {"file": c_path.name},
            }

    if not prefix_macros:
        return
    # Table declarations don't carry an explicit prefix token in the source
    # (rbus_table's constructor just takes a bare table name, e.g.
    # "ExecEnv") -- if this repo has exactly one resolved Device.* prefix,
    # use it; with more than one, skip rather than guess which applies.
    if len(prefix_macros) != 1:
        return
    root_name = next(iter(prefix_macros.values()))

    for m in CPP_RBUS_TABLE_START_RE.finditer(text):
        table_name = m.group(1)
        table_path = f"{root_name.rstrip('.')}.{table_name}.{{i}}."
        parent = root_name.rstrip(".")
        elements[table_path] = {
            "access": ["read", "addInstance", "removeInstance"], "maxInstance": "",
            "numberOfEntriesElement": f"{parent}.{table_name}NumberOfEntries",
            "description": "", "flags": [""],
            "source": {"file": c_path.name, "addFunction": None, "delFunction": None},
        }
        brace_idx = text.index("{", m.end() - 1)
        body = dslh_split_balanced(text[brace_idx + 1:], "{", "}")
        field_types = cpp_collect_field_value_types(text)
        for entry_text in dslh_split_top_level_entries(body):
            fields = split_args(entry_text)
            if len(fields) < 2:
                continue
            field_name_token = fields[0].strip()
            if not (field_name_token.startswith('"') and field_name_token.endswith('"')):
                continue
            field_name = field_name_token[1:-1]
            type_m = CPP_TABLE_TYPE_RE.search(fields[1])
            etype = type_m.group(1) if type_m else "PROPERTY"
            dm_path = f"{table_path.rstrip('.')}.{field_name}"
            if etype == "METHOD":
                elements[dm_path if dm_path.endswith("()") else dm_path + "()"] = {
                    "access": ["invokeSync"], "description": "", "input": [], "output": [],
                    "flags": [""], "source": {"file": c_path.name, "table": table_name},
                }
            else:
                # The callback-table reference itself is often named with a
                # "read_write"/"read_only" hint (e.g.
                # "rbus_table_dsm_read_write") -- the only concrete access
                # signal this convention's field entries carry.
                access = ["read"] + (["write"] if "read_write" in fields[1] else [])
                value_type = field_types.get((table_name, field_name), "string")
                elements[dm_path] = {
                    "access": access, "type": value_type, "description": "", "persistence": "",
                    "defaultValue": "", "enumeration": [], "flags": [""],
                    "source": {"file": c_path.name, "table": table_name},
                }


def scan_c_file_dslh(c_path: Path, elements: dict, macros: dict[str, str], root_name: str) -> None:
    text = c_path.read_text(encoding="utf-8", errors="replace")
    arrays = dslh_parse_parameter_arrays(text)
    if not arrays and not dslh_parse_dynamic_param_names(text):
        return
    bindings = dslh_parse_object_bindings(text)

    array_to_object: dict[str, str] = {}
    for obj_id, b in bindings.items():
        if "name_token" in b and "params_array" in b:
            resolved = dslh_resolve_macro(b["name_token"], macros, root_name)
            if resolved:
                array_to_object[b["params_array"]] = resolved

    for array_name, entries in arrays.items():
        obj_path = array_to_object.get(array_name)
        if not obj_path:
            continue
        for param_name, ccsp_type, access_token in entries:
            dm_path = f"{obj_path.rstrip('.')}.{param_name}"
            access = ["read"] + (["write"] if access_token == "CCSP_RW" else [])
            elements[dm_path] = {
                "access": access,
                "type": DSLH_TYPE_MAP.get(ccsp_type, "string"),
                "description": "", "persistence": "", "defaultValue": "", "enumeration": [],
                "flags": [""],
                "source": {"file": c_path.name, "table": array_name},
            }

    dynamic_names = dslh_parse_dynamic_param_names(text)
    if not dynamic_names:
        return
    obj_name_macros = [n for n, b in macros.items() if n.startswith("_") and n.endswith("ObjectName") and "DM_ROOTNAME" in b]
    file_stem_words = set(re.split(r"[_.]+", c_path.stem.lower())) - {"ccsp"}

    def _relevance(name: str) -> int:
        words = {w.lower() for w in re.findall(r"[A-Z][a-z0-9]*", name)}
        return len(words & file_stem_words)

    obj_name_macros.sort(key=_relevance, reverse=True)

    for obj_id, param_names in dynamic_names.items():
        base_path = None
        b = bindings.get(obj_id, {})
        if b.get("name_token"):
            base_path = dslh_resolve_macro(b["name_token"], macros, root_name)
        if not base_path:
            for macro_name in obj_name_macros:
                resolved = dslh_resolve_macro(macro_name, macros, root_name)
                if resolved:
                    base_path = resolved
                    break
        if not base_path:
            continue
        table_path = f"{base_path.rstrip('.')}.{{i}}."
        parent, _, tname = base_path.rstrip(".").rpartition(".")
        elements[table_path] = {
            "access": ["read"], "maxInstance": "",
            "numberOfEntriesElement": f"{parent}.{tname}NumberOfEntries" if parent else "",
            "description": "", "flags": [""],
            "source": {"file": c_path.name, "addFunction": None, "delFunction": None},
        }
        for param_name in param_names:
            elements[f"{table_path.rstrip('.')}.{param_name}"] = {
                "access": ["read"], "type": "string",
                "description": "", "persistence": "", "defaultValue": "", "enumeration": [],
                "flags": [""],
                "source": {"file": c_path.name},
            }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def git_info(repo_root: Path) -> dict:
    """Best-effort git branch/commit lookup, for CI-driven regeneration
    (feedback point 12: each Component Interface Definition version should
    be traceable to the code branch/commit it describes). Returns {} if
    repo_root isn't a git checkout or git isn't available -- never fatal."""
    info: dict = {}
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if branch.returncode == 0:
            branch_name = branch.stdout.strip()
            if branch_name and branch_name != "HEAD":
                info["sourceBranch"] = branch_name
            else:
                tag = subprocess.run(
                    ["git", "-C", str(repo_root), "describe", "--tags", "--exact-match"],
                    capture_output=True, text=True, timeout=5,
                )
                if tag.returncode == 0 and tag.stdout.strip():
                    info["sourceBranch"] = tag.stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if commit.returncode == 0 and commit.stdout.strip():
            info["sourceCommit"] = commit.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def parse_args() -> argparse.Namespace:
    default_repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Emit a schema-conformant Component Interface Definition JSON directly from repo source.")
    parser.add_argument("--repo-root", default=str(default_repo_root))
    parser.add_argument("--config-dir", help="XML config dir (default: <repo-root>/config)")
    parser.add_argument("--source-dir", help="C/C++ source dir (default: <repo-root>/source)")
    parser.add_argument("--xml-file", action="append", default=None, metavar="FILENAME", help="Restrict to specific XML file(s). Default: all *.xml in --config-dir.")
    parser.add_argument("--dm-root-name", default="Device.", help="Resolved value of DM_ROOTNAME, used only by the legacy DSLH convention (msParameterInfo arrays) -- that macro is typically defined in a shared external CCSP header not present in a single component's repo. Default 'Device.', verified against live device captures.")
    parser.add_argument("--extra-header-dir", action="append", default=None, metavar="DIR", help="Additional directory to search for header macros beyond --source-dir. Some components register rbus elements using macro names #include'd from a DIFFERENT component's repo at build time (a real, shared CCSP dependency, not a bug) -- point this at that other repo's checkout to resolve them. Repeatable.")
    parser.add_argument("--prefix", help="Only include element paths starting with this prefix")
    parser.add_argument("--name", required=True, help="Component name for componentInterfaceDefinition.name")
    parser.add_argument("--module-name", help="CCSP moduleName, if applicable")
    parser.add_argument("--version", default="0.1.0-draft")
    parser.add_argument("--description", default="")
    parser.add_argument("--output", required=True, help="Output JSON file path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    config_dir = Path(args.config_dir).resolve() if args.config_dir else repo_root / "config"
    source_dir = Path(args.source_dir).resolve() if args.source_dir else repo_root / "source"

    elements: dict = {}

    if config_dir.is_dir():
        # Case-insensitive match -- some repos use .XML (all-caps) extensions,
        # and Path.glob("*.xml") is case-sensitive on Linux, so a plain glob
        # would silently miss those files entirely.
        xml_files = sorted(p for p in config_dir.iterdir() if p.is_file() and p.suffix.lower() == ".xml")
        if args.xml_file:
            wanted = set(args.xml_file)
            xml_files = [p for p in xml_files if p.name in wanted]
        for xml_path in xml_files:
            print(f"[*] Scanning {xml_path.name} ...", file=sys.stderr)
            scan_xml_file(xml_path, elements)
    else:
        print(f"WARNING: config dir not found: {config_dir}", file=sys.stderr)

    if source_dir.is_dir():
        header_macros = collect_macros_from_headers(source_dir)
        for extra_dir in args.extra_header_dir or []:
            extra_macros = collect_macros_from_headers(Path(extra_dir))
            for name, defs in extra_macros.items():
                header_macros.setdefault(name, []).extend(defs)
        all_c_files = sorted(source_dir.rglob("*.c")) + sorted(source_dir.rglob("*.cpp"))
        c_files = [p for p in all_c_files if "rbusDataElement_t" in p.read_text(encoding="utf-8", errors="replace")]
        for c_path in c_files:
            print(f"[*] Scanning {c_path.relative_to(source_dir)} ...", file=sys.stderr)
            scan_c_file(c_path, elements, header_macros)

        # Legacy "DSLH" convention (msParameterInfo tuple arrays) -- a
        # different registration mechanism than rbusDataElement_t, found in
        # some older CCSP components (e.g. CcspTr069Pa's DslhManagementServer).
        # Only pays the extra whole-repo macro-collection cost when the repo
        # actually uses it.
        dslh_files = [p for p in all_c_files if "msParameterInfo" in p.read_text(encoding="utf-8", errors="replace")]
        if dslh_files:
            dslh_macros = dslh_collect_macros(source_dir)
            for c_path in dslh_files:
                print(f"[*] Scanning {c_path.relative_to(source_dir)} (DSLH convention) ...", file=sys.stderr)
                scan_c_file_dslh(c_path, elements, dslh_macros, args.dm_root_name)

        # C++ "rbus_table + registerPath" convention (e.g. DSM). The DM
        # root prefix here is whichever value the repo's own *_PREFIX macro
        # resolves to (e.g. DEVICE_SM_PREFIX = "Device.SoftwareModules."),
        # not necessarily args.dm_root_name -- resolved directly per call
        # rather than through the DSLH macro table.
        cpp_files = [p for p in all_c_files if p.suffix == ".cpp" and ("registerPath" in p.read_text(encoding="utf-8", errors="replace") or "rbus_table" in p.read_text(encoding="utf-8", errors="replace"))]
        if cpp_files:
            prefix_macros = {
                n: v.strip().strip('"') for n, v in dslh_collect_macros(source_dir).items()
                if n.endswith("_PREFIX") and v.strip().startswith('"Device.')
            }
            for c_path in cpp_files:
                print(f"[*] Scanning {c_path.relative_to(source_dir)} (C++ rbus_table convention) ...", file=sys.stderr)
                scan_cpp_file_rbustable(c_path, elements, prefix_macros)
    else:
        print(f"WARNING: source dir not found: {source_dir}", file=sys.stderr)

    # Drop anything that isn't a real TR-181 data-model path. Some components
    # register rbus-internal helper properties or generic RPC methods under
    # their own component namespace (e.g. "com.cisco.spvtg.ccsp.pam.Helper.*")
    # rather than under "Device." -- these are real, legitimate elements in
    # the source, but they aren't data model elements and don't belong in a
    # Component Interface Definition, which is schema-scoped to Device.*.
    non_device = sorted(k for k in elements if not k.startswith("Device."))
    if non_device:
        print(f"[*] Excluding {len(non_device)} non-Device.-prefixed element(s) (not TR-181 data model paths):", file=sys.stderr)
        for k in non_device:
            print(f"      {k}", file=sys.stderr)
        elements = {k: v for k, v in elements.items() if k.startswith("Device.")}

    if args.prefix:
        elements = {k: v for k, v in elements.items() if k.startswith(args.prefix)}

    doc = {
        "componentInterfaceDefinition": {
            "name": args.name,
            **({"moduleName": args.module_name} if args.module_name else {}),
            "version": args.version,
            "description": args.description,
            **git_info(repo_root),
            "generated": __import__("datetime").date.today().isoformat(),
            "generatedBy": " ".join(sys.argv),
        },
        "elements": dict(sorted(elements.items())),
    }

    print(f"[*] Total elements: {len(elements)}", file=sys.stderr)
    from collections import Counter

    def infer_kind(path: str) -> str:
        if path.endswith("{i}."):
            return "table"
        if path.endswith("()"):
            return "method"
        return "leaf"

    kind_counts = Counter(infer_kind(k) for k in elements)
    for kind, count in kind_counts.items():
        print(f"    {kind}: {count}", file=sys.stderr)

    Path(args.output).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"[*] Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
