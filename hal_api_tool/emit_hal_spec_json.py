#!/usr/bin/env python3
"""
emit_hal_spec_json.py
----------------------------------------------------------------------
Southbound HAL API extraction tool (RDK-B).

Companion to the northbound TR-181 extractor (emit_spec_json.py). Where
that tool walks TR-181 datamodel XML/C to emit the northbound parameter
surface, this tool walks a directory of rdkb-halif-<component> repos
(HAL header files under include/, e.g. cm_hal.h, wifi_hal.h, mta_hal.h)
and emits the southbound HAL C-API surface, one JSON file per component.

No third-party dependencies -- stdlib only, so it runs on any box with
python3, including build/CI hosts with no network access.

USAGE
    # per-repo output: writes rdkb-halif-cm/cm.json, rdkb-halif-wifi/wifi.json, etc.
    # straight inside each component's own folder (what you want when you `cd`
    # into the folder containing all your rdkb-halif-* checkouts and run it there)
    python3 emit_hal_spec_json.py --root . --in-place --summary

    # separate output tree instead, one subfolder per component
    python3 emit_hal_spec_json.py --root /path/to/halif/checkouts \
        --out ./hal_spec_json --summary

    # single component / single header, for quick checks
    python3 emit_hal_spec_json.py --header /path/to/cm_hal.h --out ./out

OUTPUT
    --in-place (recommended for a folder of rdkb-halif-* checkouts):
        rdkb-halif-cm/cm.json
        rdkb-halif-wifi/wifi.json
        ...one JSON written straight into each component's own repo folder.
        _summary.json / _all_apis.json (if --summary) go to --out, or the
        current directory if --out is omitted, since they span all components.

    default (--out, no --in-place):
        <out>/<component>/<component>.json   -- one FOLDER per component, e.g.
                                                 hal_spec_json/cm/cm.json,
                                                 hal_spec_json/wifi/wifi.json
        <out>/_summary.json      -- component -> header/api counts (if --summary)
        <out>/_all_apis.json     -- every API from every component, flattened (if --summary)

Each component JSON has the shape:
{
  "component": "cm",
  "repo_dir": "rdkb-halif-cm",
  "generated_at": "2026-08-28T12:00:00Z",
  "headers": [
    {
      "file": "include/cm_hal.h",
      "api_count": 63,
      "apis": [
        {
          "name": "docsis_GetDSChannel",
          "return_type": "INT",
          "params": [{"type": "PCMMGMT_CM_DS_CHANNEL *", "name": "ppinfo"}],
          "param_count": 1,
          "signature": "INT docsis_GetDSChannel(PCMMGMT_CM_DS_CHANNEL *ppinfo);",
          "brief": "Retrieves information about a downstream (DS) channel.",
          "params_doc": {"ppinfo": "Pointer to a `PCMMGMT_CM_DS_CHANNEL` structure to be populated."},
          "return_doc": "Status of the operation. RETURN_OK on success. RETURN_ERR on failure ...",
          "deprecated": false,
          "line": 562
        },
        ...
      ]
    }
  ],
  "total_api_count": 63
}

DESIGN NOTES (why regex instead of a real C parser)
    RDK-B HAL headers are hand-written to a very consistent house style:
    one prototype per physical line, ending in ");", preceded by a
    doxygen /** ... */ block. There is no libclang / pycparser guarantee
    on target build hosts, so this tool uses a small brace/paren-depth
    aware statement splitter (handles multi-line prototypes if they ever
    appear) plus a prototype regex, rather than pulling in a compiler
    front end. It has one deliberate special case: the standard
        #ifdef __cplusplus
        extern "C"{
        #endif
    wrapper is stripped before statement splitting, since otherwise it
    would make every real top-level statement look like it's nested
    one brace deep.
----------------------------------------------------------------------
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 1. Comment stripping (keeps line numbers intact; records doxygen blocks)
# ---------------------------------------------------------------------------

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_EXTERN_C_OPEN_RE = re.compile(
    r"#ifdef\s+__cplusplus\s*\r?\nextern\s+\"C\"\s*\{\s*\r?\n#endif", re.MULTILINE
)
_EXTERN_C_CLOSE_RE = re.compile(
    r"#ifdef\s+__cplusplus\s*\r?\n\}\s*\r?\n#endif", re.MULTILINE
)


def _blank_out(match):
    """Replace a match with whitespace, preserving newline count (so line
    numbers of everything after it stay correct)."""
    return "\n" * match.group(0).count("\n")


def extract_doc_comments(text):
    """Return {end_line_number: comment_body} for every /** ... */ or
    /*! ... */ doxygen-style block comment, plus the stripped text with
    all comments blanked out (newlines preserved)."""
    docs = {}

    def handle(match):
        body = match.group(0)
        end_line = text.count("\n", 0, match.end()) + 1
        if body.startswith("/**") or body.startswith("/*!"):
            docs[end_line] = body
        return _blank_out(match)

    stripped = _BLOCK_COMMENT_RE.sub(handle, text)
    stripped = _LINE_COMMENT_RE.sub(_blank_out, stripped)
    return stripped, docs


def strip_extern_c_wrapper(text):
    text = _EXTERN_C_OPEN_RE.sub(_blank_out, text)
    text = _EXTERN_C_CLOSE_RE.sub(_blank_out, text)
    return text


# ---------------------------------------------------------------------------
# 2. Doxygen comment parsing -> brief / @param / @return
# ---------------------------------------------------------------------------

_DOX_LINE_PREFIX_RE = re.compile(r"^[ \t]*\*+[ \t]?", re.MULTILINE)
_DOX_TAG_RE = re.compile(
    r"@(brief|param(?:\[\w+\])?|return|returns|retval|deprecated)\b\s*(.*)"
)


def parse_doxygen(raw_comment):
    """Parse a /** ... */ block into brief/params_doc/return_doc/deprecated."""
    body = raw_comment.strip()
    body = re.sub(r"^/\*[!*]*", "", body)
    body = re.sub(r"\*/$", "", body)
    lines = [_DOX_LINE_PREFIX_RE.sub("", ln, count=1).rstrip() for ln in body.split("\n")]

    brief_lines, return_lines = [], []
    params_doc = {}
    deprecated = False
    current = ("brief", None)  # (bucket, param_name)

    for ln in lines:
        m = _DOX_TAG_RE.match(ln.strip())
        if m:
            tag, rest = m.group(1), m.group(2).strip()
            if tag == "brief":
                current = ("brief", None)
                if rest:
                    brief_lines.append(rest)
            elif tag.startswith("param"):
                pm = re.match(r"([A-Za-z_]\w*)\s*(.*)", rest)
                if pm:
                    pname, desc = pm.group(1), pm.group(2).strip(" -")
                    params_doc[pname] = desc
                    current = ("param", pname)
            elif tag in ("return", "returns", "retval"):
                current = ("return", None)
                if rest:
                    return_lines.append(rest)
            elif tag == "deprecated":
                deprecated = True
                current = ("return", None)
                if rest:
                    return_lines.append("DEPRECATED: " + rest)
            continue
        # continuation line for whatever bucket we're in
        text = ln.strip()
        if not text:
            continue
        bucket, pname = current
        if bucket == "brief":
            brief_lines.append(text)
        elif bucket == "return":
            return_lines.append(text)
        elif bucket == "param" and pname:
            params_doc[pname] = (params_doc.get(pname, "") + " " + text).strip()

    return {
        "brief": " ".join(brief_lines).strip(),
        "params_doc": params_doc,
        "return_doc": " ".join(return_lines).strip(),
        "deprecated": deprecated,
    }


# ---------------------------------------------------------------------------
# 3. Statement splitter (brace/paren-depth aware) + prototype matcher
# ---------------------------------------------------------------------------

_PROTO_RE = re.compile(
    r"^(?P<ret>[A-Za-z_][\w ]*?[\s\*])(?P<name>[A-Za-z_]\w*)\s*\(\s*(?P<params>.*?)\s*\)$"
)

_C_KEYWORDS_NOT_FUNCS = {"if", "for", "while", "switch", "sizeof", "return"}


def split_statements(text):
    """Yield (start_line, end_line, statement_text) for each top-level
    ';'-terminated statement, skipping preprocessor lines. Depth-aware so a
    struct/enum/union body (balanced braces) is captured as one statement,
    and so a prototype whose params happen to span more than one physical
    line is still captured whole."""
    paren_depth = 0
    brace_depth = 0
    buf = []
    start_line = None
    line_no = 1
    i = 0
    n = len(text)
    at_line_start = True

    while i < n:
        ch = text[i]

        if at_line_start:
            # Preprocessor directive: skip the whole line, don't touch depth
            j = i
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] == "#":
                nl = text.find("\n", j)
                if nl == -1:
                    nl = n
                line_no += text.count("\n", i, nl)
                i = nl
                at_line_start = True
                continue
        at_line_start = False

        if ch == "\n":
            line_no += 1
            at_line_start = True
            if buf:
                buf.append(ch)
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)

        if not buf and not ch.isspace():
            start_line = line_no

        if ch == ";" and paren_depth == 0 and brace_depth == 0:
            if buf:
                yield (start_line, line_no, "".join(buf))
            buf = []
            start_line = None
            i += 1
            continue

        if buf or not ch.isspace():
            buf.append(ch)
        i += 1

    if buf and "".join(buf).strip():
        yield (start_line, line_no, "".join(buf))


def parse_params(params_str):
    """Split a parameter list string into [{'type':..,'name':..}], best
    effort. Handles simple cases (which covers all RDK-B HAL headers seen)."""
    params_str = params_str.strip()
    if params_str == "" or params_str == "void":
        return []
    parts = []
    depth = 0
    cur = []
    for ch in params_str:
        if ch == "(" :
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))

    result = []
    for p in parts:
        p = p.strip()
        if not p or p == "void":
            continue
        # function-pointer param, e.g. "void (*cb)(int)" -- keep as-is
        if "(*" in p:
            result.append({"type": p, "name": ""})
            continue
        m = re.match(r"^(.*?[\s\*])([A-Za-z_]\w*)(\s*\[\s*\d*\s*\])?$", p)
        if m:
            ptype = m.group(1).strip()
            pname = m.group(2)
            if m.group(3):
                pname += m.group(3).strip()
            result.append({"type": ptype, "name": pname})
        else:
            result.append({"type": p, "name": ""})
    return result


def extract_apis(filepath):
    """Parse one HAL header file and return a list of API dicts."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    stripped, docs = extract_doc_comments(raw)
    stripped = strip_extern_c_wrapper(stripped)

    apis = []
    for start_line, end_line, stmt in split_statements(stripped):
        stmt_norm = re.sub(r"\s+", " ", stmt).strip()
        if not stmt_norm:
            continue
        if stmt_norm.startswith("typedef"):
            continue  # function-pointer typedefs, struct typedefs, etc.
        if stmt_norm.startswith(("struct", "enum", "union")) and "{" in stmt:
            continue
        if stmt_norm.startswith(("extern ", "static ", "#")):
            continue

        m = _PROTO_RE.match(stmt_norm)
        if not m:
            continue
        name = m.group("name")
        if name in _C_KEYWORDS_NOT_FUNCS:
            continue
        ret_type = m.group("ret").strip()
        params_raw = m.group("params").strip()
        params = parse_params(params_raw)

        # find the nearest doc comment ending on or just above start_line
        doc_text = None
        for ln in range(start_line - 1, max(start_line - 6, 0), -1):
            if ln in docs:
                doc_text = docs[ln]
                break
        doc = parse_doxygen(doc_text) if doc_text else {
            "brief": "", "params_doc": {}, "return_doc": "", "deprecated": False
        }

        deprecated = doc["deprecated"] or "deprecated" in name.lower() or "DEPRECATED" in filepath.upper()

        apis.append({
            "name": name,
            "return_type": ret_type,
            "params": params,
            "param_count": len(params),
            "signature": f"{ret_type} {name}({params_raw});",
            "brief": doc["brief"],
            "params_doc": doc["params_doc"],
            "return_doc": doc["return_doc"],
            "deprecated": deprecated,
            "line": start_line,
        })

    return apis


# ---------------------------------------------------------------------------
# 4. Repo / component discovery
# ---------------------------------------------------------------------------

def find_component_repos(root):
    """A 'component repo' is any directory under root whose name matches
    rdkb-halif-* (or, failing that, any dir directly containing an
    include/*.h or a top-level *_hal*.h)."""
    repos = []
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if not os.path.isdir(full) or entry.startswith("."):
            continue
        if entry.startswith("rdkb-halif-") or find_headers(full):
            repos.append(full)
    return repos


def find_headers(repo_dir):
    """Header files belonging to this component: include/*.h preferred,
    else any *_hal*.h / *hal_api*.h at the repo root, excluding docs."""
    headers = []
    inc = os.path.join(repo_dir, "include")
    if os.path.isdir(inc):
        for fn in sorted(os.listdir(inc)):
            if fn.endswith(".h"):
                headers.append(os.path.join(inc, fn))
    if not headers:
        for fn in sorted(os.listdir(repo_dir)):
            if fn.endswith(".h") and ("hal" in fn.lower()):
                headers.append(os.path.join(repo_dir, fn))
    return headers


def component_name(repo_dir):
    base = os.path.basename(os.path.normpath(repo_dir))
    return base[len("rdkb-halif-"):] if base.startswith("rdkb-halif-") else base


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def build_component_json(repo_dir, root):
    comp = component_name(repo_dir)
    headers = find_headers(repo_dir)
    header_entries = []
    total = 0
    for h in headers:
        apis = extract_apis(h)
        rel = os.path.relpath(h, repo_dir)
        header_entries.append({
            "file": rel,
            "api_count": len(apis),
            "apis": apis,
        })
        total += len(apis)
    return {
        "component": comp,
        "repo_dir": os.path.relpath(repo_dir, root) if root else os.path.basename(repo_dir),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headers": header_entries,
        "total_api_count": total,
    }


def main():
    ap = argparse.ArgumentParser(description="Extract RDK-B southbound HAL APIs into per-component JSON.")
    ap.add_argument("--root", help="Directory containing one or more rdkb-halif-<component> repo checkouts")
    ap.add_argument("--header", help="Parse a single header file instead of a whole tree (quick check)")
    ap.add_argument("--out", help="Output directory for JSON files (default: current directory). "
                                   "Ignored per-component when --in-place is set.")
    ap.add_argument("--in-place", action="store_true",
                     help="Write each component's JSON directly inside that component's own repo "
                          "folder (e.g. rdkb-halif-cm/cm.json) instead of a separate --out tree. "
                          "--summary outputs (_summary.json/_all_apis.json) still go to --out "
                          "(default: current directory) since they span all components.")
    ap.add_argument("--summary", action="store_true", help="Also emit _summary.json and _all_apis.json")
    ap.add_argument("--pretty", action="store_true", default=True, help="Pretty-print JSON (default on)")
    args = ap.parse_args()

    if not args.root and not args.header:
        ap.error("one of --root or --header is required")
    if not args.in_place and not args.out:
        ap.error("--out is required unless --in-place is set")

    summary_dir = args.out or "."
    os.makedirs(summary_dir, exist_ok=True)
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    # list of (doc, repo_dir_abs_path_or_None)
    component_docs = []

    if args.header:
        apis = extract_apis(args.header)
        doc = {
            "component": os.path.basename(args.header).rsplit(".", 1)[0],
            "repo_dir": None,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "headers": [{"file": os.path.basename(args.header), "api_count": len(apis), "apis": apis}],
            "total_api_count": len(apis),
        }
        component_docs.append((doc, os.path.dirname(os.path.abspath(args.header))))
    else:
        repos = find_component_repos(args.root)
        if not repos:
            print(f"No component repos found under {args.root}", file=sys.stderr)
            sys.exit(1)
        for repo in repos:
            doc = build_component_json(repo, args.root)
            component_docs.append((doc, repo))

    for doc, repo_dir in component_docs:
        if args.in_place:
            dest_dir = repo_dir  # write straight into the component's own repo folder
        else:
            dest_dir = os.path.join(args.out, doc["component"])
            os.makedirs(dest_dir, exist_ok=True)
        out_path = os.path.join(dest_dir, f"{doc['component']}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        print(f"  {doc['component']:20s} {doc['total_api_count']:4d} APIs  -> {out_path}")

    if args.summary:
        docs_only = [d for d, _ in component_docs]
        summary = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component_count": len(docs_only),
            "total_api_count": sum(d["total_api_count"] for d in docs_only),
            "components": [
                {
                    "component": d["component"],
                    "repo_dir": d["repo_dir"],
                    "header_count": len(d["headers"]),
                    "api_count": d["total_api_count"],
                    "headers": [{"file": h["file"], "api_count": h["api_count"]} for h in d["headers"]],
                }
                for d in sorted(docs_only, key=lambda d: d["component"])
            ],
        }
        with open(os.path.join(summary_dir, "_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        all_apis = []
        for d in docs_only:
            for h in d["headers"]:
                for api in h["apis"]:
                    entry = dict(api)
                    entry["component"] = d["component"]
                    entry["header"] = h["file"]
                    all_apis.append(entry)
        with open(os.path.join(summary_dir, "_all_apis.json"), "w", encoding="utf-8") as f:
            json.dump(all_apis, f, indent=2)

        print(f"\nTotal: {summary['component_count']} components, {summary['total_api_count']} HAL APIs")
        print(f"  {os.path.join(summary_dir, '_summary.json')}")
        print(f"  {os.path.join(summary_dir, '_all_apis.json')}")


if __name__ == "__main__":
    main()
