#!/usr/bin/env python3
"""
RDK rbus DML extractor.

Discovers rbusDataElement_t[] registration tables in RDK-B component C
source (e.g. wanmgr_rbus_handler_apis.c), resolves the path strings through
the component's #define macros, and evaluates the REAL C preprocessor
conditionals (#ifdef / #ifndef / #if defined(...) / #elif / #else / #endif)
that gate them -- not the CCSP-XML-style <?ifdef?> markers extract_dml.py
handles.

This is the "code that runs" counterpart to extract_dml.py's "XML that
describes the model" -- many rbus properties (status/read-only values wired
directly to C getter/setter callbacks) are never declared in any XML file
at all, and entire branches of a component's data model are compiled in or
out based on build flags like WAN_MANAGER_UNIFICATION_ENABLED.

Usage:
    python3 tools/extract_rbus_dml.py --repo-root .
    python3 tools/extract_rbus_dml.py --repo-root . \\
        --enable-flag RBUS_BUILD_FLAG_ENABLE \\
        --enable-flag WAN_MANAGER_UNIFICATION_ENABLED \\
        --output json
"""

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Mini C preprocessor: strips inactive #if/#ifdef/#ifndef/#elif/#else blocks
# and collects #define macros, all relative to a caller-supplied enabled-flag
# set. This is intentionally a pragmatic subset (defined(X), &&, ||, !, bare
# flag identifiers) -- enough for RDK-B's real-world usage patterns, not a
# full C preprocessor.
# ---------------------------------------------------------------------------

DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(?:\([^)]*\))?\s*(.*?)\s*(?://.*)?$")
DIRECTIVE_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")


def eval_c_expr(expr: str, enabled: set[str]) -> bool:
    expr = expr.strip()
    if not expr:
        return True

    def repl_defined(match: re.Match) -> str:
        return "True" if match.group(1) in enabled else "False"

    py_expr = re.sub(r"defined\s*\(\s*(\w+)\s*\)", repl_defined, expr)
    py_expr = re.sub(r"defined\s+(\w+)", repl_defined, py_expr)

    def repl_bare(match: re.Match) -> str:
        tok = match.group(1)
        if tok in ("True", "False", "and", "or", "not"):
            return tok
        return "True" if tok in enabled else "False"

    py_expr = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", repl_bare, py_expr)
    py_expr = py_expr.replace("&&", " and ").replace("||", " or ").replace("!", " not ")

    try:
        return bool(eval(py_expr, {"__builtins__": {}}, {}))  # noqa: S307 - trusted local repo source
    except Exception:
        return False


def preprocess_c(raw: str, enabled: set[str], mode: str = "strip") -> tuple[str, dict[str, str]]:
    """Return (visible_source_with_directives_removed, macro_table).

    mode == "strip" (default): real conditional evaluation against `enabled`
    -- a branch is only visible if its condition holds. This is what you
    want to reproduce one specific build variant.

    mode == "keep": ignore conditions entirely: EVERY #ifdef/#ifndef/#if,
    #elif and #else branch is treated as visible. This mirrors extract_dml.py's
    XML "keep" mode ("full picture" / union of all branches) so a component
    whose entire registration table sits behind a single top-level
    #ifdef FEATURE_FLAG (as many RDK-B .c files do) doesn't silently
    disappear just because that flag wasn't in --enable-flag.
    Note: under "keep", a macro #define'd differently in an #if/#else pair
    will end up holding whichever definition appears LAST in the file, since
    both branches are visible and later text overwrites the same key.
    """
    macros: dict[str, str] = {}
    out_lines: list[str] = []
    stack: list[dict] = []  # {"emit": bool, "taken": bool}

    def visible() -> bool:
        return all(frame["emit"] for frame in stack)

    for line in raw.splitlines():
        match = DIRECTIVE_RE.match(line)
        if match:
            kind, rest = match.group(1), match.group(2).strip()
            if mode == "keep":
                if kind in ("if", "ifdef", "ifndef"):
                    stack.append({"emit": True, "taken": True})
                elif kind == "elif":
                    if stack:
                        stack[-1]["emit"] = True
                elif kind == "else":
                    if stack:
                        stack[-1]["emit"] = True
                elif kind == "endif":
                    if stack:
                        stack.pop()
                continue

            if kind == "ifdef":
                cond = bool(rest) and rest.split()[0] in enabled
                stack.append({"emit": cond, "taken": cond})
            elif kind == "ifndef":
                cond = (not rest) or rest.split()[0] not in enabled
                stack.append({"emit": cond, "taken": cond})
            elif kind == "if":
                cond = eval_c_expr(rest, enabled)
                stack.append({"emit": cond, "taken": cond})
            elif kind == "elif":
                if stack:
                    frame = stack[-1]
                    cond = (not frame["taken"]) and eval_c_expr(rest, enabled)
                    frame["emit"] = cond
                    frame["taken"] = frame["taken"] or cond
            elif kind == "else":
                if stack:
                    frame = stack[-1]
                    frame["emit"] = not frame["taken"]
                    frame["taken"] = True
            elif kind == "endif":
                if stack:
                    stack.pop()
            continue

        if not visible():
            continue

        define_match = DEFINE_RE.match(line)
        if define_match:
            name, value = define_match.group(1), define_match.group(2).strip()
            macros[name] = value

        out_lines.append(line)

    return "\n".join(out_lines), macros


# ---------------------------------------------------------------------------
# rbusDataElement_t[] table parsing
# ---------------------------------------------------------------------------

ARRAY_START_RE = re.compile(r"rbusDataElement_t\s+(\w+)\s*\[\s*\]\s*=\s*")


def split_balanced(text: str, open_ch: str, close_ch: str) -> tuple[str, int]:
    """Given text starting right after an opening `open_ch` (already consumed),
    return (content_up_to_matching_close, index_just_past_close)."""
    depth = 1
    for idx, ch in enumerate(text):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[:idx], idx + 1
    return text, len(text)


def find_rbus_arrays(source: str) -> list[tuple[str, str]]:
    """Return [(array_var_name, array_body), ...] for each rbusDataElement_t[] found."""
    arrays: list[tuple[str, str]] = []
    for match in ARRAY_START_RE.finditer(source):
        var_name = match.group(1)
        rest = source[match.end():]
        brace_idx = rest.find("{")
        if brace_idx == -1:
            continue
        body, _ = split_balanced(rest[brace_idx + 1:], "{", "}")
        arrays.append((var_name, body))
    return arrays


def split_top_level_entries(body: str) -> list[str]:
    """Split an array body into the raw text of each {...} entry (braces stripped)."""
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


def split_args(text: str) -> list[str]:
    """Split on top-level commas, respecting nested {}, (), []."""
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


def resolve_name(token: str, macros: dict[str, str]) -> str | None:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    if token in macros:
        return resolve_name(macros[token], macros)
    return None


TYPE_MAP = {
    "RBUS_ELEMENT_TYPE_PROPERTY": "property",
    "RBUS_ELEMENT_TYPE_TABLE": "table",
    "RBUS_ELEMENT_TYPE_EVENT": "event",
    "RBUS_ELEMENT_TYPE_METHOD": "method",
}


@dataclass(frozen=True)
class RbusParameter:
    dm_path: str
    param_name: str
    data_type: str
    writable: bool
    object_path: str
    source: str
    c_file: str
    array_name: str


def expand_instances(path: str, instances: int) -> list[str]:
    if "{i}" not in path:
        return [path]
    idx = path.index("{i}")
    prefix, suffix = path[:idx], path[idx + 3:]
    results = []
    for i in range(1, instances + 1):
        for rest in expand_instances(suffix, instances):
            results.append(f"{prefix}{i}{rest}")
    return results


def parse_rbus_source(
    c_path: Path, macros: dict[str, str], instances: int
) -> tuple[list[RbusParameter], int]:
    raw = c_path.read_text(encoding="utf-8", errors="replace")
    results: list[RbusParameter] = []
    table_count = 0

    for array_name, body in find_rbus_arrays(raw):
        for entry_text in split_top_level_entries(body):
            fields = split_args(entry_text)
            if len(fields) < 2:
                continue
            name_token, etype_token = fields[0], fields[1]
            etype = TYPE_MAP.get(etype_token.strip())
            if etype is None:
                continue

            resolved = resolve_name(name_token, macros)
            if resolved is None:
                continue

            if etype == "table":
                table_count += 1
                continue

            writable = False
            if len(fields) >= 3:
                cb_body = fields[2].strip()
                if cb_body.startswith("{") and cb_body.endswith("}"):
                    cb_body = cb_body[1:-1]
                cb_fields = split_args(cb_body)
                if len(cb_fields) >= 2:
                    set_handler = cb_fields[1].strip()
                    writable = set_handler not in ("", "NULL")

            for expanded_path in expand_instances(resolved, instances):
                param_name = expanded_path.rsplit(".", 1)[-1]
                object_path = expanded_path.rsplit(".", 1)[0] if "." in expanded_path else expanded_path
                results.append(
                    RbusParameter(
                        dm_path=expanded_path,
                        param_name=param_name,
                        data_type=etype,  # property/event/method -- rbus tables carry no XML-style type
                        writable=writable,
                        object_path=object_path,
                        source=f"rbus_{etype}",
                        c_file=c_path.name,
                        array_name=array_name,
                    )
                )

    return results, table_count


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_c_files(source_dir: Path) -> list[Path]:
    found = []
    for c_path in sorted(source_dir.rglob("*.c")):
        text = c_path.read_text(encoding="utf-8", errors="replace")
        if "rbusDataElement_t" in text and "RBUS_ELEMENT_TYPE_" in text:
            found.append(c_path)
    return found


def discover_h_files(source_dir: Path) -> list[Path]:
    return sorted(source_dir.rglob("*.h"))


def build_macro_table(h_files: list[Path], enabled: set[str], mode: str = "strip") -> dict[str, str]:
    macros: dict[str, str] = {}
    for h_path in h_files:
        raw = h_path.read_text(encoding="utf-8", errors="replace")
        _, file_macros = preprocess_c(raw, enabled, mode)
        macros.update(file_macros)
    return macros


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def dedupe(parameters: list[RbusParameter]) -> list[RbusParameter]:
    unique: dict[str, RbusParameter] = {}
    for param in parameters:
        unique.setdefault(param.dm_path, param)
    return sorted(unique.values(), key=lambda item: item.dm_path)


def filter_prefix(parameters: list[RbusParameter], prefix: str | None) -> list[RbusParameter]:
    if not prefix:
        return parameters
    return [p for p in parameters if p.dm_path.startswith(prefix)]


def format_text(parameters: list[RbusParameter]) -> str:
    lines = [f"rbus Parameters: {len(parameters)}", "=" * 100]
    for index, param in enumerate(parameters, start=1):
        access = "RW" if param.writable else "RO"
        lines.append(f"[{index:>4}] {param.dm_path}")
        lines.append(
            f"       kind={param.source:<14} {access}  file={param.c_file} table={param.array_name}"
        )
    return "\n".join(lines)


def format_json(parameters: list[RbusParameter]) -> str:
    return json.dumps([asdict(p) for p in parameters], indent=2)


def format_csv(parameters: list[RbusParameter]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["index", "dm_path", "param_name", "data_type", "writable", "object_path", "source", "c_file", "array_name"]
    )
    for index, p in enumerate(parameters, start=1):
        writer.writerow(
            [index, p.dm_path, p.param_name, p.data_type, p.writable, p.object_path, p.source, p.c_file, p.array_name]
        )
    return output.getvalue()


FORMATTERS = {"text": format_text, "json": format_json, "csv": format_csv}


def parse_args() -> argparse.Namespace:
    default_repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Extract rbus-registered RDK-B datamodel elements from C source, "
        "evaluating real #ifdef/#if defined() build flags."
    )
    parser.add_argument("--repo-root", default=str(default_repo_root))
    parser.add_argument("--source-dir", help="Root dir to scan for .c/.h (default: <repo-root>/source)")
    parser.add_argument("--instances", type=int, default=2)
    parser.add_argument("--output", choices=sorted(FORMATTERS), default="text")
    parser.add_argument("--prefix")
    parser.add_argument(
        "--enable-flag",
        action="append",
        default=[],
        metavar="FLAG",
        help="Treat FLAG as defined for #ifdef/#if defined() evaluation. Repeatable.",
    )
    parser.add_argument(
        "--build-flags",
        choices=["strip", "keep"],
        default="strip",
        help=(
            "strip (default): real evaluation against --enable-flag -- reproduces one "
            "specific build variant, and a component whose whole file sits behind an "
            "un-enabled #ifdef will correctly show 0 elements. "
            "keep: ignore all conditionals, every branch visible (full picture)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    source_dir = Path(args.source_dir).resolve() if args.source_dir else repo_root / "source"

    if not source_dir.is_dir():
        print(f"ERROR: source dir not found: {source_dir}", file=sys.stderr)
        return 1
    if args.instances < 1:
        print("ERROR: --instances must be >= 1", file=sys.stderr)
        return 1

    enabled = set(args.enable_flag)

    c_files = discover_c_files(source_dir)
    h_files = discover_h_files(source_dir)
    print(f"[*] Found {len(c_files)} candidate rbus C file(s), {len(h_files)} header(s) in {source_dir}", file=sys.stderr)
    print(f"[*] Enabled flags: {sorted(enabled) or '(none)'}", file=sys.stderr)

    macros = build_macro_table(h_files, enabled, args.build_flags)

    all_params: list[RbusParameter] = []
    total_tables = 0
    for c_path in c_files:
        cleaned, file_macros = preprocess_c(
            c_path.read_text(encoding="utf-8", errors="replace"), enabled, args.build_flags
        )
        local_macros = dict(macros)
        local_macros.update(file_macros)

        # Re-parse arrays against the *visible* (preprocessed) text so gated
        # entries inside #ifdef blocks are correctly excluded.
        tmp_c_path = c_path
        results, table_count = parse_rbus_source_from_text(cleaned, tmp_c_path, local_macros, args.instances)
        all_params.extend(results)
        total_tables += table_count
        print(f"[*] {c_path.relative_to(source_dir)}: {len(results)} element(s), {table_count} table marker(s)", file=sys.stderr)

    parameters = dedupe(all_params)
    parameters = filter_prefix(parameters, args.prefix)

    print(f"[*] Total rbus elements: {len(parameters)}  (+{total_tables} table markers, not emitted as params)", file=sys.stderr)
    print(FORMATTERS[args.output](parameters))
    return 0


def parse_rbus_source_from_text(
    cleaned_text: str, c_path: Path, macros: dict[str, str], instances: int
) -> tuple[list[RbusParameter], int]:
    results: list[RbusParameter] = []
    table_count = 0
    for array_name, body in find_rbus_arrays(cleaned_text):
        for entry_text in split_top_level_entries(body):
            fields = split_args(entry_text)
            if len(fields) < 2:
                continue
            name_token, etype_token = fields[0], fields[1]
            etype = TYPE_MAP.get(etype_token.strip())
            if etype is None:
                continue
            resolved = resolve_name(name_token, macros)
            if resolved is None:
                continue
            if etype == "table":
                table_count += 1
                continue
            writable = False
            if len(fields) >= 3:
                cb_body = fields[2].strip()
                if cb_body.startswith("{") and cb_body.endswith("}"):
                    cb_body = cb_body[1:-1]
                cb_fields = split_args(cb_body)
                if len(cb_fields) >= 2:
                    set_handler = cb_fields[1].strip()
                    writable = set_handler not in ("", "NULL")
            for expanded_path in expand_instances(resolved, instances):
                param_name = expanded_path.rsplit(".", 1)[-1]
                object_path = expanded_path.rsplit(".", 1)[0] if "." in expanded_path else expanded_path
                results.append(
                    RbusParameter(
                        dm_path=expanded_path,
                        param_name=param_name,
                        data_type=etype,
                        writable=writable,
                        object_path=object_path,
                        source=f"rbus_{etype}",
                        c_file=c_path.name,
                        array_name=array_name,
                    )
                )
    return results, table_count


if __name__ == "__main__":
    sys.exit(main())
