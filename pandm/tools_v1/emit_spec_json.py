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
ARRAY_START_RE = re.compile(r"rbusDataElement_t\s+(\w+)\s*\[\s*\]\s*=\s*\{?\s*$")
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
    for h_path in sorted(source_dir.rglob("*.h")):
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

    def resolve_name(token: str, local_flags: list[str]) -> str | None:
        token = token.strip()
        if token.startswith('"') and token.endswith('"'):
            return token[1:-1]
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
        sub_h = cb_fields[4].strip() if len(cb_fields) > 4 else "NULL"
        method_h = cb_fields[5].strip() if len(cb_fields) > 5 else "NULL"
        flags = flags_snapshot(flag_stack)

        if etype == "table":
            noe_path = compute_noe_path(resolved)
            elements[resolved if resolved.endswith(".") else resolved + "."] = {
                "access": ["read"],
                "maxInstance": "",
                "numberOfEntriesElement": noe_path,
                "description": "",
                "flags": flags,
                "source": {"file": c_path.name, "table": array_name, "addFunction": None, "delFunction": None},
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
            elements[resolved] = {
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
        elements[resolved] = {
            "access": access,
            "type": "string",
            "description": "",
            "persistence": "",
            "defaultValue": "",
            "enumeration": [],
            "flags": flags,
            "source": source,
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
        c_files = [p for p in sorted(source_dir.rglob("*.c")) if "rbusDataElement_t" in p.read_text(encoding="utf-8", errors="replace")]
        for c_path in c_files:
            print(f"[*] Scanning {c_path.relative_to(source_dir)} ...", file=sys.stderr)
            scan_c_file(c_path, elements, header_macros)
    else:
        print(f"WARNING: source dir not found: {source_dir}", file=sys.stderr)

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
