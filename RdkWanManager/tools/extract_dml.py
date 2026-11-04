#!/usr/bin/env python3
"""
Generic RDK DML extractor.

This script discovers and expands CCSP XML datamodel files for any RDK
component repository. It is the generic counterpart to
tools/extract_datamodels.py, which remains the WanManager-specific version.
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
PI_PATTERN = re.compile(r"<\?(?:ifdef|ifndef|else|endif)\b[^?]*\?>", re.IGNORECASE)


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
    root: ET.Element
    prefix: str


def clean_xml(raw_xml: str) -> str:
    return PI_PATTERN.sub("", raw_xml)


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
    top_objects = dml_xml.root.find("objects")
    if top_objects is None:
        return results

    top_obj = top_objects.find("object")
    if top_obj is None:
        return results

    top_path = dml_xml.prefix
    collect_parameters(top_obj, top_path, dml_xml.path.name, results)
    child_objects = top_obj.find("objects")
    if child_objects is not None:
        for child in child_objects.findall("object"):
            walk_object(child, top_path, instances, dml_xml.path.name, results)

    return results


def discover_dml_xml_files(config_dir: Path) -> list[DmlXml]:
    discovered: list[DmlXml] = []

    for xml_path in sorted(config_dir.glob("*.xml")):
        try:
            cleaned = clean_xml(xml_path.read_text(encoding="utf-8", errors="replace"))
            root = ET.fromstring(cleaned)
        except (OSError, ET.ParseError) as error:
            print(f"WARNING: Skipping {xml_path.name}: {error}", file=sys.stderr)
            continue

        if root.tag != "dataModelInfo":
            continue

        top_objects = root.find("objects")
        if top_objects is None:
            continue

        top_obj = top_objects.find("object")
        if top_obj is None:
            continue

        top_name = (top_obj.findtext("name") or "").strip()
        if top_name:
            discovered.append(DmlXml(path=xml_path, root=root, prefix=f"Device.{top_name}"))

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

    dml_xmls = discover_dml_xml_files(config_dir)
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
