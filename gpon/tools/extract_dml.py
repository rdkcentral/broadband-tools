#!/usr/bin/env python3
"""
Generic RDK DML extractor.

This script discovers and expands CCSP XML datamodel files for any RDK
component repository. It is the generic counterpart to
tools/extract_datamodels.py, which remains the WanManager-specific version.

Build-flag handling
--------------------
CCSP XML files use processing-instruction style conditionals to gate
parameters behind compile-time feature flags:

    <?ifdef FEATURE_RDKB_WAN_MANAGER?>
      ... gated object/parameter ...
    <?else?>
      ... fallback object/parameter ...
    <?endif?>

Two modes are supported via --build-flags:

  strip (default)  Evaluate the conditionals. A block is kept only if its
                    flag is enabled via --enable-flag (ifdef) or NOT enabled
                    (ifndef). With no --enable-flag given, every ifdef block
                    is dropped and every ifndef block is kept, i.e. you get
                    the "unconditional" parameter set.

  keep              Don't evaluate anything -- strip the PI markers only and
                    keep all enclosed content, gated or not. This is the
                    "full picture" mode.
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
PI_TOKEN_PATTERN = re.compile(r"<\?(ifdef|ifndef|else|endif)\b([^?]*)\?>", re.IGNORECASE)


@dataclass(frozen=True)
class DmParameter:
    dm_path: str
    param_name: str
    data_type: str
    writable: bool
    object_path: str
    source: str
    xml_file: str


@dataclass
class DmlXml:
    path: Path
    top_obj: ET.Element
    prefix: str


def evaluate_build_flags(raw_xml: str, mode: str, enabled_flags: set[str]) -> str:
    """Resolve <?ifdef?>/<?ifndef?>/<?else?>/<?endif?> conditionals.

    mode == "keep": only strip the PI markers, keep all enclosed content
    regardless of flag state (legacy / "full picture" behavior).

    mode == "strip": actually evaluate each block against enabled_flags and
    drop the content of branches that aren't taken.
    """
    if mode == "keep":
        return PI_TOKEN_PATTERN.sub("", raw_xml)

    out: list[str] = []
    stack: list[dict] = []  # each frame: {"emit": bool, "taken": bool, "flag": str}
    pos = 0

    def visible() -> bool:
        return all(frame["emit"] for frame in stack)

    for match in PI_TOKEN_PATTERN.finditer(raw_xml):
        start, end = match.span()
        if visible():
            out.append(raw_xml[pos:start])
        pos = end

        kind = match.group(1).lower()
        arg = match.group(2).strip()
        flag = arg.split()[0] if arg.split() else ""

        if kind in ("ifdef", "ifndef"):
            cond = (flag in enabled_flags) if kind == "ifdef" else (flag not in enabled_flags)
            stack.append({"emit": cond, "taken": cond, "flag": flag})
        elif kind == "else":
            if stack:
                frame = stack[-1]
                frame["emit"] = not frame["taken"]
                frame["taken"] = frame["taken"] or frame["emit"]
            # unmatched <?else?> outside any ifdef/ifndef -- ignore, malformed input
        elif kind == "endif":
            if stack:
                stack.pop()
            # unmatched <?endif?> -- ignore, malformed input

    if visible():
        out.append(raw_xml[pos:])

    return "".join(out)


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


def parse_writable(elem: ET.Element) -> bool:
    writable = elem.findtext("writable")
    return writable is not None and writable.strip().lower() == "true"


def collect_parameters(
    obj_elem: ET.Element,
    obj_path: str,
    xml_file: str,
    results: list[DmParameter],
) -> None:
    params_elem = obj_elem.find("parameters")
    if params_elem is None:
        return

    for param in params_elem.findall("parameter"):
        name = (param.findtext("name") or "").strip()
        if not name:
            continue
        results.append(
            DmParameter(
                dm_path=f"{obj_path}.{name}",
                param_name=name,
                data_type=parse_type(param.findtext("type")),
                writable=parse_writable(param),
                object_path=obj_path,
                source="xml",
                xml_file=xml_file,
            )
        )


def add_number_of_entries(
    parent_path: str,
    table_name: str,
    xml_file: str,
    results: list[DmParameter],
) -> None:
    param_name = f"{table_name}NumberOfEntries"
    results.append(
        DmParameter(
            dm_path=f"{parent_path}.{param_name}",
            param_name=param_name,
            data_type="unsignedInt",
            writable=False,
            object_path=parent_path,
            source="auto",
            xml_file=xml_file,
        )
    )


def walk_object(
    obj_elem: ET.Element,
    parent_path: str,
    instances: int,
    xml_file: str,
    results: list[DmParameter],
) -> None:
    obj_name = (obj_elem.findtext("name") or "").strip()
    if not obj_name:
        return

    obj_type = (obj_elem.findtext("objectType") or "object").strip()

    if obj_type in TABLE_TYPES:
        add_number_of_entries(parent_path, obj_name, xml_file, results)
        for index in range(1, instances + 1):
            obj_path = f"{parent_path}.{obj_name}.{index}"
            collect_parameters(obj_elem, obj_path, xml_file, results)
            child_objects = obj_elem.find("objects")
            if child_objects is not None:
                for child in child_objects.findall("object"):
                    walk_object(child, obj_path, instances, xml_file, results)
        return

    obj_path = f"{parent_path}.{obj_name}"
    collect_parameters(obj_elem, obj_path, xml_file, results)
    child_objects = obj_elem.find("objects")
    if child_objects is not None:
        for child in child_objects.findall("object"):
            walk_object(child, obj_path, instances, xml_file, results)


def parse_dml_xml(dml_xml: DmlXml, instances: int) -> list[DmParameter]:
    results: list[DmParameter] = []
    top_path = dml_xml.prefix
    collect_parameters(dml_xml.top_obj, top_path, dml_xml.path.name, results)
    child_objects = dml_xml.top_obj.find("objects")
    if child_objects is not None:
        for child in child_objects.findall("object"):
            walk_object(child, top_path, instances, dml_xml.path.name, results)

    return results


def discover_dml_xml_files(
    config_dir: Path, build_flags_mode: str, enabled_flags: set[str]
) -> list[DmlXml]:
    discovered: list[DmlXml] = []

    for xml_path in sorted(config_dir.glob("*.xml")):
        try:
            raw = xml_path.read_text(encoding="utf-8", errors="replace")
            cleaned = evaluate_build_flags(raw, build_flags_mode, enabled_flags)
            root = ET.fromstring(cleaned)
        except (OSError, ET.ParseError) as error:
            print(f"WARNING: Skipping {xml_path.name}: {error}", file=sys.stderr)
            continue

        if root.tag != "dataModelInfo":
            continue

        top_objects = root.find("objects")
        if top_objects is None:
            continue

        # A single XML file can declare more than one top-level object.
        # Capture all of them, not just the first.
        for top_obj in top_objects.findall("object"):
            top_name = (top_obj.findtext("name") or "").strip()
            if top_name:
                discovered.append(
                    DmlXml(path=xml_path, top_obj=top_obj, prefix=f"Device.{top_name}")
                )

    return discovered


def dedupe_parameters(parameters: list[DmParameter]) -> list[DmParameter]:
    unique: dict[str, DmParameter] = {}
    for param in parameters:
        unique.setdefault(param.dm_path, param)
    return sorted(unique.values(), key=lambda item: item.dm_path)


def filter_parameters(parameters: list[DmParameter], prefix: str | None) -> list[DmParameter]:
    if not prefix:
        return parameters
    return [param for param in parameters if param.dm_path.startswith(prefix)]


def format_text(parameters: list[DmParameter]) -> str:
    lines = [f"Parameters: {len(parameters)}", "=" * 100]
    for index, param in enumerate(parameters, start=1):
        access = "RW" if param.writable else "RO"
        source = param.xml_file if param.source == "xml" else f"{param.xml_file} ({param.source})"
        lines.append(f"[{index:>4}] {param.dm_path}")
        lines.append(f"       type={param.data_type:<18} {access}  file={source}")
    return "\n".join(lines)


def format_dmcli(parameters: list[DmParameter]) -> str:
    lines = []
    for index, param in enumerate(parameters, start=1):
        lines.append(f"Parameter {index:>4} name: {param.dm_path}")
        lines.append(f"               type:     {param.data_type}")
    return "\n".join(lines)


def format_json(parameters: list[DmParameter]) -> str:
    return json.dumps([asdict(param) for param in parameters], indent=2)


def format_csv(parameters: list[DmParameter]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["index", "dm_path", "param_name", "data_type", "writable", "object_path", "source", "xml_file"]
    )
    for index, param in enumerate(parameters, start=1):
        writer.writerow(
            [
                index,
                param.dm_path,
                param.param_name,
                param.data_type,
                param.writable,
                param.object_path,
                param.source,
                param.xml_file,
            ]
        )
    return output.getvalue()


FORMATTERS = {
    "text": format_text,
    "dmcli": format_dmcli,
    "json": format_json,
    "csv": format_csv,
}


def parse_args() -> argparse.Namespace:
    default_repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Discover and extract generic RDK CCSP XML datamodel parameters."
    )
    parser.add_argument(
        "--repo-root",
        default=str(default_repo_root),
        help=f"Path to the repo root (default: {default_repo_root})",
    )
    parser.add_argument(
        "--config-dir",
        help="Path to the config directory containing XML files (default: <repo-root>/config)",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=2,
        help="Number of table instances to expand (default: 2)",
    )
    parser.add_argument(
        "--output",
        choices=sorted(FORMATTERS),
        default="text",
        help="Output format: text | dmcli | json | csv (default: text)",
    )
    parser.add_argument(
        "--prefix",
        help="Only include paths starting with this prefix",
    )
    parser.add_argument(
        "--build-flags",
        choices=["strip", "keep"],
        default="strip",
        help=(
            "strip (default): evaluate <?ifdef?>/<?ifndef?>/<?else?>/<?endif?> blocks "
            "against --enable-flag; with no flags enabled this yields the unconditional "
            "parameter set. keep: ignore conditionals entirely and include everything "
            "(full picture)."
        ),
    )
    parser.add_argument(
        "--enable-flag",
        action="append",
        default=[],
        metavar="FLAG",
        help=(
            "Treat FLAG as defined/enabled when evaluating <?ifdef?>/<?ifndef?> blocks "
            "under --build-flags strip. Repeatable, e.g. "
            "--enable-flag FEATURE_RDKB_WAN_MANAGER --enable-flag FEATURE_X. "
            "Ignored under --build-flags keep."
        ),
    )
    return parser.parse_args()


def print_stats(parameters: list[DmParameter], dml_xmls: list[DmlXml], config_dir: Path) -> None:
    print(f"[*] Found {len(dml_xmls)} DML XML file(s) in {config_dir}", file=sys.stderr)
    for dml_xml in dml_xmls:
        print(f"[*] Parsing {dml_xml.path.name} ... \u2192 prefix: {dml_xml.prefix}", file=sys.stderr)

    print(f"[*] Total parameters: {len(parameters)}", file=sys.stderr)
    counts = Counter(param.xml_file for param in parameters if param.source == "xml")
    for xml_file in sorted(counts):
        print(f"{counts[xml_file]:>8}  from {xml_file}", file=sys.stderr)

    auto_count = sum(1 for param in parameters if param.source == "auto")
    print(f"{auto_count:>8}  auto-generated NumberOfEntries", file=sys.stderr)


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

    enabled_flags = set(args.enable_flag)
    if args.build_flags == "keep" and enabled_flags:
        print(
            "WARNING: --enable-flag has no effect under --build-flags keep "
            "(all conditionals are included unconditionally)",
            file=sys.stderr,
        )

    dml_xmls = discover_dml_xml_files(config_dir, args.build_flags, enabled_flags)
    parameters: list[DmParameter] = []
    for dml_xml in dml_xmls:
        parameters.extend(parse_dml_xml(dml_xml, args.instances))

    parameters = dedupe_parameters(parameters)
    parameters = filter_parameters(parameters, args.prefix)
    print_stats(parameters, dml_xmls, config_dir)
    try:
        print(FORMATTERS[args.output](parameters))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
