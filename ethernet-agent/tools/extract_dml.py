#!/usr/bin/env python3
"""
extract_dml.py
==============
Generic RDK DML extractor — works with ANY RDK component repository.

Discovers and expands CCSP XML datamodel files found in the config/ directory.
Auto-detects the DM prefix from the XML root object name.

Key difference from extract_datamodels.py (WanManager-specific):
  - Zero hardcoding of component names, XML filenames, or DM paths
  - Works with any repo: ethernet-agent, vlan-manager, RdkWanManager, etc.
  - Supports --build-flags to control how <?ifdef?> / <?ifndef?> blocks are handled

Usage:
    python3 tools/extract_dml.py [OPTIONS]

Options:
    --repo-root PATH        Path to repo root (default: two levels above this script)
    --config-dir PATH       Path to config dir with XML files (default: <repo-root>/config)
    --instances N           Number of table instances to expand (default: 2)
    --output FORMAT         text | dmcli | json | csv (default: text)
    --prefix FILTER         Only include paths starting with this prefix (optional)
    --build-flags MODE      How to handle <?ifdef?> blocks:
                              strip  - remove all conditional blocks (default)
                                       → only unconditional params shown
                              keep   - keep ALL content regardless of build flags
                                       → full picture of everything in the XML

Examples:
    # All params in this repo (unconditional only)
    python3 tools/extract_dml.py --output json > dml.json

    # ALL params including ifdef-gated ones (full picture)
    python3 tools/extract_dml.py --build-flags keep --output json > dml_full.json

    # dmcli style output
    python3 tools/extract_dml.py --build-flags keep --output dmcli

    # From a different repo
    python3 tools/extract_dml.py --repo-root /path/to/vlan-manager --output json

    # Filter to a subtree
    python3 tools/extract_dml.py --build-flags keep --prefix Device.Ethernet --output json

    # 1 instance only
    python3 tools/extract_dml.py --build-flags keep --instances 1 --output csv > dml.csv
"""

import argparse
import csv
import io
import json
import re
import signal
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


TABLE_TYPES = {"dynamicTable", "writableTable", "staticTable"}

# Matches <?ifdef ...?>, <?ifndef ...?>, <?else?>, <?endif?>
_PI_STRIP = re.compile(r"<\?(?:ifdef|ifndef|else|endif)\b[^?]*\?>", re.IGNORECASE)

# In "strip" mode: remove the PI tags AND everything between ifdef/ifndef and endif
_PI_BLOCK_STRIP = re.compile(
    r"<\?(?:ifdef|ifndef)\b[^?]*\?>.*?<\?endif\?>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class DmParameter:
    dm_path: str
    param_name: str
    data_type: str
    writable: bool
    object_path: str
    source: str        # "xml" | "auto"
    xml_file: str
    build_flag: str    # "" = unconditional, else the flag name


@dataclass
class DmlXml:
    path: Path
    root: ET.Element
    prefix: str


# ---------------------------------------------------------------------------
# XML cleaning
# ---------------------------------------------------------------------------

def clean_xml_strip(raw: str) -> str:
    """Remove all <?ifdef?>...<?endif?> blocks AND their PI tags."""
    cleaned = _PI_BLOCK_STRIP.sub("", raw)
    return _PI_STRIP.sub("", cleaned)


def clean_xml_keep(raw: str) -> str:
    """Remove only the PI tags, keep all content inside ifdef blocks."""
    return _PI_STRIP.sub("", raw)


# ---------------------------------------------------------------------------
# Type / writable helpers
# ---------------------------------------------------------------------------

def parse_type(raw_type) -> str:
    if not raw_type:
        return "string"
    raw = raw_type.strip()
    if raw.startswith("boolean"):
        return "boolean"
    if raw.startswith("unsignedInt") or raw.startswith("uint"):
        return "unsignedInt"
    if raw == "int" or raw.startswith("int["):
        return "int"
    if raw.startswith("string"):
        return "string(mapped)" if (":" in raw or "/" in raw) else "string"
    if ":" in raw:
        return "string(mapped)"
    return raw.split(":")[0].split("[")[0].strip()


def parse_writable(elem: ET.Element) -> bool:
    val = elem.findtext("writable")
    return val is not None and val.strip().lower() == "true"


# ---------------------------------------------------------------------------
# Parameter collection
# ---------------------------------------------------------------------------

def collect_parameters(obj_elem, obj_path, xml_file, results):
    params_elem = obj_elem.find("parameters")
    if params_elem is None:
        return
    for param in params_elem.findall("parameter"):
        name = (param.findtext("name") or "").strip()
        if not name:
            continue
        results.append(DmParameter(
            dm_path=f"{obj_path}.{name}",
            param_name=name,
            data_type=parse_type(param.findtext("type")),
            writable=parse_writable(param),
            object_path=obj_path,
            source="xml",
            xml_file=xml_file,
            build_flag="",
        ))


def add_number_of_entries(parent_path, table_name, xml_file, results):
    param_name = f"{table_name}NumberOfEntries"
    results.append(DmParameter(
        dm_path=f"{parent_path}.{param_name}",
        param_name=param_name,
        data_type="unsignedInt",
        writable=False,
        object_path=parent_path,
        source="auto",
        xml_file=xml_file,
        build_flag="",
    ))


# ---------------------------------------------------------------------------
# Recursive object walker
# ---------------------------------------------------------------------------

def walk_object(obj_elem, parent_path, instances, xml_file, results):
    obj_name = (obj_elem.findtext("name") or "").strip()
    if not obj_name:
        return

    obj_type = (obj_elem.findtext("objectType") or "object").strip()

    if obj_type in TABLE_TYPES:
        add_number_of_entries(parent_path, obj_name, xml_file, results)
        for idx in range(1, instances + 1):
            obj_path = f"{parent_path}.{obj_name}.{idx}"
            collect_parameters(obj_elem, obj_path, xml_file, results)
            child_objects = obj_elem.find("objects")
            if child_objects is not None:
                for child in child_objects.findall("object"):
                    walk_object(child, obj_path, instances, xml_file, results)
    else:
        obj_path = f"{parent_path}.{obj_name}"
        collect_parameters(obj_elem, obj_path, xml_file, results)
        child_objects = obj_elem.find("objects")
        if child_objects is not None:
            for child in child_objects.findall("object"):
                walk_object(child, obj_path, instances, xml_file, results)


# ---------------------------------------------------------------------------
# Per-XML parser
# ---------------------------------------------------------------------------

def parse_dml_xml(dml_xml: DmlXml, instances: int) -> list:
    results = []
    top_objects = dml_xml.root.find("objects")
    if top_objects is None:
        return results

    for top_obj in top_objects.findall("object"):
        top_name = (top_obj.findtext("name") or "").strip()
        if not top_name:
            continue
        top_path = f"Device.{top_name}"
        collect_parameters(top_obj, top_path, dml_xml.path.name, results)
        child_objects = top_obj.find("objects")
        if child_objects is not None:
            for child in child_objects.findall("object"):
                walk_object(child, top_path, instances, dml_xml.path.name, results)

    return results


# ---------------------------------------------------------------------------
# XML discovery
# ---------------------------------------------------------------------------

def discover_dml_xml_files(config_dir: Path, build_flags_mode: str) -> list:
    discovered = []
    cleaner = clean_xml_keep if build_flags_mode == "keep" else clean_xml_strip

    for xml_path in sorted(config_dir.glob("*.xml")):
        try:
            raw = xml_path.read_text(encoding="utf-8", errors="replace")
            cleaned = cleaner(raw)
            root = ET.fromstring(cleaned)
        except (OSError, ET.ParseError) as e:
            print(f"WARNING: Skipping {xml_path.name}: {e}", file=sys.stderr)
            continue

        if root.tag != "dataModelInfo":
            continue

        top_objects = root.find("objects")
        if top_objects is None:
            continue

        prefixes = []
        for top_obj in top_objects.findall("object"):
            top_name = (top_obj.findtext("name") or "").strip()
            if top_name:
                prefixes.append(f"Device.{top_name}")

        if prefixes:
            discovered.append(DmlXml(
                path=xml_path,
                root=root,
                prefix=", ".join(prefixes),
            ))

    return discovered


# ---------------------------------------------------------------------------
# Dedup & filter
# ---------------------------------------------------------------------------

def dedupe_parameters(parameters: list) -> list:
    seen = {}
    for p in parameters:
        seen.setdefault(p.dm_path, p)
    return sorted(seen.values(), key=lambda p: p.dm_path)


def filter_parameters(parameters: list, prefix) -> list:
    if not prefix:
        return parameters
    return [p for p in parameters if p.dm_path.startswith(prefix)]


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_text(parameters: list) -> str:
    lines = [f"Parameters: {len(parameters)}", "=" * 100]
    for i, p in enumerate(parameters, 1):
        access = "RW" if p.writable else "RO"
        src = p.xml_file if p.source == "xml" else f"{p.xml_file} ({p.source})"
        lines.append(f"[{i:>4}] {p.dm_path}")
        lines.append(f"       type={p.data_type:<20} {access}  file={src}")
    return "\n".join(lines)


def format_dmcli(parameters: list) -> str:
    lines = []
    for i, p in enumerate(parameters, 1):
        lines.append(f"Parameter {i:>4} name: {p.dm_path}")
        lines.append(f"               type:     {p.data_type}")
    return "\n".join(lines)


def format_json(parameters: list) -> str:
    return json.dumps([asdict(p) for p in parameters], indent=2)


def format_csv(parameters: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["index", "dm_path", "param_name", "data_type",
                     "writable", "object_path", "source", "xml_file", "build_flag"])
    for i, p in enumerate(parameters, 1):
        writer.writerow([i, p.dm_path, p.param_name, p.data_type,
                         p.writable, p.object_path, p.source, p.xml_file, p.build_flag])
    return buf.getvalue()


FORMATTERS = {
    "text":  format_text,
    "dmcli": format_dmcli,
    "json":  format_json,
    "csv":   format_csv,
}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(parameters, dml_xmls, config_dir, build_flags_mode):
    print(f"[*] Found {len(dml_xmls)} DML XML file(s) in {config_dir}", file=sys.stderr)
    for dml_xml in dml_xmls:
        print(f"[*] Parsing {dml_xml.path.name} ... → prefix: {dml_xml.prefix}", file=sys.stderr)
    mode_desc = "all content including ifdef blocks" if build_flags_mode == "keep" else "unconditional params only"
    print(f"[*] Build flags mode: {build_flags_mode} ({mode_desc})", file=sys.stderr)
    print(f"[*] Total parameters: {len(parameters)}", file=sys.stderr)
    counts = Counter(p.xml_file for p in parameters if p.source == "xml")
    for xml_file in sorted(counts):
        print(f"{counts[xml_file]:>8}  from {xml_file}", file=sys.stderr)
    auto_count = sum(1 for p in parameters if p.source == "auto")
    print(f"{auto_count:>8}  auto-generated NumberOfEntries", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Generic RDK CCSP XML DML extractor — works with any RDK component repo."
    )
    parser.add_argument(
        "--repo-root", default=str(default_root),
        help=f"Path to the repo root (default: {default_root})",
    )
    parser.add_argument(
        "--config-dir",
        help="Path to config dir with XML files (default: <repo-root>/config)",
    )
    parser.add_argument(
        "--instances", type=int, default=2,
        help="Number of table instances to expand (default: 2)",
    )
    parser.add_argument(
        "--output", choices=sorted(FORMATTERS), default="text",
        help="Output format: text | dmcli | json | csv (default: text)",
    )
    parser.add_argument(
        "--prefix",
        help="Only include DM paths starting with this prefix",
    )
    parser.add_argument(
        "--build-flags", choices=["strip", "keep"], default="strip",
        dest="build_flags",
        help=(
            "How to handle <?ifdef?> blocks: "
            "strip = remove conditional blocks (default, unconditional params only); "
            "keep  = keep all content (full picture of everything in the XML)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    config_dir = Path(args.config_dir).resolve() if args.config_dir else repo_root / "config"

    if not repo_root.is_dir():
        print(f"ERROR: repo root not found: {repo_root}", file=sys.stderr)
        return 1
    if not config_dir.is_dir():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return 1
    if args.instances < 1:
        print("ERROR: --instances must be >= 1", file=sys.stderr)
        return 1

    dml_xmls = discover_dml_xml_files(config_dir, args.build_flags)
    if not dml_xmls:
        print("WARNING: No DML XML files found.", file=sys.stderr)
        return 1

    parameters = []
    for dml_xml in dml_xmls:
        parameters.extend(parse_dml_xml(dml_xml, args.instances))

    parameters = dedupe_parameters(parameters)
    parameters = filter_parameters(parameters, args.prefix)
    print_stats(parameters, dml_xmls, config_dir, args.build_flags)

    try:
        print(FORMATTERS[args.output](parameters))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
