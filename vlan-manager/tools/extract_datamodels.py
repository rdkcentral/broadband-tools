#!/usr/bin/env python3
"""
extract_datamodels.py
=====================
Generates the COMPLETE list of Device.X_RDK_WanManager.* datamodel parameters
matching exactly what a live device returns via:

    dmcli eRT getv Device.X_RDK_WanManager.

Three sources are combined:
  1. config/RdkWanManager.xml        — v1 CCSP DML (CPEInterface model)
  2. config/RdkWanManager_v2.xml     — v2 CCSP DML (Interface/VirtualInterface/Group model)
  3. Built-in rbus-only params       — registered in wanmgr_rbus_handler_apis.c via
                                       rbus_regDataElements(), NOT in any XML file

Auto-generated NumberOfEntries params (produced by the CCSP framework for every
dynamicTable/writableTable/staticTable) are also added automatically.

Usage:
    python3 tools/extract_datamodels.py [OPTIONS]

Options:
    --repo-root PATH     Path to the repo root (default: two levels above this script)
    --instances N        Number of table instances to expand (default: 2)
    --output FORMAT      text (default) | dmcli | json | csv
    --xml v1|v2|both     Which XML file(s) to parse (default: both)

Examples:
    python3 tools/extract_datamodels.py
    python3 tools/extract_datamodels.py --instances 1 --output dmcli
    python3 tools/extract_datamodels.py --xml v2 --output csv > params.csv
    python3 tools/extract_datamodels.py --output json
"""

import argparse
import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DmParameter:
    dm_path: str       # Full path e.g. Device.X_RDK_WanManager.Interface.1.Selection.Enable
    param_name: str    # Short name e.g. Enable
    data_type: str     # e.g. boolean, string, unsignedInt, int
    writable: bool     # True if writable
    object_path: str   # Parent object path
    source: str        # "xml_v1" | "xml_v2" | "rbus" | "auto"


@dataclass
class DataModelReport:
    parameters: List[DmParameter] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rbus-only parameters
# (registered in wanMgrRbusDataElements[] and wanMgrIfacePublishElements[]
#  in wanmgr_rbus_handler_apis.c — NOT declared in any XML file)
# These are expanded per-instance where {i} appears.
# ---------------------------------------------------------------------------

# Top-level rbus properties (no instance expansion needed)
RBUS_TOP_LEVEL_PARAMS = [
    ("Device.X_RDK_WanManager.CurrentActiveInterface",  "CurrentActiveInterface",  "string",  False),
    ("Device.X_RDK_WanManager.CurrentStatus",           "CurrentStatus",           "string",  False),
    ("Device.X_RDK_WanManager.CurrentStandbyInterface", "CurrentStandbyInterface", "string",  False),
    ("Device.X_RDK_WanManager.InterfaceAvailableStatus","InterfaceAvailableStatus","string",  False),
    ("Device.X_RDK_WanManager.InterfaceActiveStatus",   "InterfaceActiveStatus",   "string",  False),
    ("Device.X_RDK_WanManager.CurrentActiveDNS",        "CurrentActiveDNS",        "string",  False),
    # Events (visible via dmcli but event-type, shown as string)
    ("Device.X_RDK_WanManager.InitialScanComplete",     "InitialScanComplete",     "string",  False),
    ("Device.X_RDK_WanManager.InterfaceIpStatus",       "InterfaceIpStatus",       "string",  False),
]

# Per-interface rbus properties (expanded for each Interface.{i} instance)
# These are registered with {i} pattern in wanMgrIfacePublishElements
RBUS_PER_IFACE_PARAMS = [
    # suffix, param_name, data_type, writable
    ("Selection.Enable",                         "Enable",      "boolean", True),
    ("Alias",                                    "Alias",       "string",  True),
    ("BaseInterfaceStatus",                      "BaseInterfaceStatus", "string", False),
]

# Per-virtual-interface rbus properties (expanded for Interface.{i}.VirtualInterface.{j})
RBUS_PER_VIRTIF_PARAMS = [
    ("Status",      "Status",      "string", False),
    ("VlanStatus",  "VlanStatus",  "string", False),
    ("IP.IPv4Address", "IPv4Address", "string", False),
    ("IP.IPv6Address", "IPv6Address", "string", False),
    ("IP.IPv6Prefix",  "IPv6Prefix",  "string", False),
]

# CPEInterface rbus properties (v1 compatibility, per Interface.{i} instance)
RBUS_PER_CPE_PARAMS = [
    ("Phy.Status",      "Status",     "string", False),
    ("Wan.Status",      "Status",     "string", False),
    ("Wan.LinkStatus",  "LinkStatus", "string", False),
]


def build_rbus_params(instances: int) -> List[DmParameter]:
    """Build the full list of rbus-only DmParameter entries."""
    params = []

    # Top-level
    for dm_path, pname, dtype, writable in RBUS_TOP_LEVEL_PARAMS:
        params.append(DmParameter(
            dm_path=dm_path,
            param_name=pname,
            data_type=dtype,
            writable=writable,
            object_path=dm_path.rsplit(".", 1)[0],
            source="rbus",
        ))

    # Per Interface instance
    for i in range(1, instances + 1):
        iface_path = f"Device.X_RDK_WanManager.Interface.{i}"
        for suffix, pname, dtype, writable in RBUS_PER_IFACE_PARAMS:
            params.append(DmParameter(
                dm_path=f"{iface_path}.{suffix}",
                param_name=pname,
                data_type=dtype,
                writable=writable,
                object_path=iface_path if "." not in suffix else f"{iface_path}.{suffix.rsplit('.', 1)[0]}",
                source="rbus",
            ))

        # Per VirtualInterface instance under this Interface
        for j in range(1, instances + 1):
            virtif_path = f"{iface_path}.VirtualInterface.{j}"
            for suffix, pname, dtype, writable in RBUS_PER_VIRTIF_PARAMS:
                params.append(DmParameter(
                    dm_path=f"{virtif_path}.{suffix}",
                    param_name=pname,
                    data_type=dtype,
                    writable=writable,
                    object_path=virtif_path if "." not in suffix else f"{virtif_path}.{suffix.rsplit('.', 1)[0]}",
                    source="rbus",
                ))

        # CPEInterface compatibility (v1 paths)
        cpe_path = f"Device.X_RDK_WanManager.CPEInterface.{i}"
        for suffix, pname, dtype, writable in RBUS_PER_CPE_PARAMS:
            params.append(DmParameter(
                dm_path=f"{cpe_path}.{suffix}",
                param_name=pname,
                data_type=dtype,
                writable=writable,
                object_path=f"{cpe_path}.{suffix.rsplit('.', 1)[0]}",
                source="rbus",
            ))

    return params


# ---------------------------------------------------------------------------
# NumberOfEntries auto-params
# CCSP framework auto-generates these for every table object.
# ---------------------------------------------------------------------------

def build_number_of_entries_params(instances: int) -> List[DmParameter]:
    """Build the *NumberOfEntries params auto-generated by CCSP framework."""
    params = []

    base = "Device.X_RDK_WanManager"

    # Top-level table NOE
    for table_name, noe_name in [
        ("Group",         "GroupNumberOfEntries"),
        ("CPEInterface",  "CPEInterfaceNumberOfEntries"),
        ("Interface",     "InterfaceNumberOfEntries"),
    ]:
        params.append(DmParameter(
            dm_path=f"{base}.{noe_name}",
            param_name=noe_name,
            data_type="unsignedInt",
            writable=False,
            object_path=base,
            source="auto",
        ))

    # Per Interface
    for i in range(1, instances + 1):
        iface_path = f"{base}.Interface.{i}"
        for noe_name in ["MarkingNumberOfEntries", "VirtualInterfaceNumberOfEntries"]:
            params.append(DmParameter(
                dm_path=f"{iface_path}.{noe_name}",
                param_name=noe_name,
                data_type="unsignedInt",
                writable=False,
                object_path=iface_path,
                source="auto",
            ))

        # Per VirtualInterface
        for j in range(1, instances + 1):
            virtif_path = f"{iface_path}.VirtualInterface.{j}"
            for noe_name in ["MarkingNumberOfEntries", "VLANNumberOfEntries"]:
                params.append(DmParameter(
                    dm_path=f"{virtif_path}.{noe_name}",
                    param_name=noe_name,
                    data_type="unsignedInt",
                    writable=False,
                    object_path=virtif_path,
                    source="auto",
                ))

    return params


# ---------------------------------------------------------------------------
# XML parser
# ---------------------------------------------------------------------------

TABLE_TYPES = {"dynamicTable", "writableTable", "staticTable"}


def parse_type(raw_type: str) -> str:
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


def parse_writable(elem) -> bool:
    w = elem.findtext("writable")
    return w is not None and w.strip().lower() == "true"


def collect_parameters(obj_elem, obj_path: str, source: str, results: List[DmParameter]):
    params_elem = obj_elem.find("parameters")
    if params_elem is None:
        return
    for param in params_elem.findall("parameter"):
        pname = (param.findtext("name") or "").strip()
        if not pname:
            continue
        raw_type = (param.findtext("type") or "string").strip()
        results.append(DmParameter(
            dm_path=f"{obj_path}.{pname}",
            param_name=pname,
            data_type=parse_type(raw_type),
            writable=parse_writable(param),
            object_path=obj_path,
            source=source,
        ))


def walk_objects(objects_elem, parent_path: str, instances: int, source: str, results: List[DmParameter]):
    if objects_elem is None:
        return
    for obj in objects_elem.findall("object"):
        obj_name = (obj.findtext("name") or "").strip()
        obj_type = (obj.findtext("objectType") or "object").strip()
        if obj_type in TABLE_TYPES:
            for idx in range(1, instances + 1):
                obj_path = f"{parent_path}.{obj_name}.{idx}"
                collect_parameters(obj, obj_path, source, results)
                walk_objects(obj.find("objects"), obj_path, instances, source, results)
        else:
            obj_path = f"{parent_path}.{obj_name}"
            collect_parameters(obj, obj_path, source, results)
            walk_objects(obj.find("objects"), obj_path, instances, source, results)


def parse_xml(xml_path: Path, instances: int, source: str) -> List[DmParameter]:
    results = []
    raw = xml_path.read_text(encoding="utf-8", errors="replace")
    cleaned = re.sub(r'<\?(?:ifdef|ifndef|else|endif)[^?]*\?>', '', raw)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        print(f"WARNING: Could not parse {xml_path.name}: {e}", file=sys.stderr)
        return results

    top_objects = root.find("objects")
    if top_objects is None:
        return results

    for top_obj in top_objects.findall("object"):
        top_name = (top_obj.findtext("name") or "").strip()
        obj_path = f"Device.{top_name}"
        collect_parameters(top_obj, obj_path, source, results)
        walk_objects(top_obj.find("objects"), obj_path, instances, source, results)

    return results


# ---------------------------------------------------------------------------
# Main extractor — combines all 3 sources
# ---------------------------------------------------------------------------

def extract_all(repo_root: Path, instances: int, xml_filter: str) -> DataModelReport:
    report = DataModelReport()
    seen = set()

    def add(p: DmParameter):
        if p.dm_path not in seen and p.dm_path.startswith("Device.X_RDK_WanManager"):
            seen.add(p.dm_path)
            report.parameters.append(p)

    # 1. XML sources
    xml_map = {
        "v1": (repo_root / "config" / "RdkWanManager.xml",    "xml_v1"),
        "v2": (repo_root / "config" / "RdkWanManager_v2.xml", "xml_v2"),
    }
    to_parse = []
    if xml_filter in ("v1", "both"):
        to_parse.append(xml_map["v1"])
    if xml_filter in ("v2", "both"):
        to_parse.append(xml_map["v2"])

    for xml_path, source in to_parse:
        if not xml_path.exists():
            print(f"WARNING: XML not found: {xml_path}", file=sys.stderr)
            continue
        print(f"[*] Parsing {xml_path.name} ...", file=sys.stderr)
        for p in parse_xml(xml_path, instances, source):
            add(p)

    # 2. rbus-only params (from wanmgr_rbus_handler_apis.c)
    print("[*] Adding rbus-only parameters ...", file=sys.stderr)
    for p in build_rbus_params(instances):
        add(p)

    # 3. Auto-generated NumberOfEntries params
    print("[*] Adding auto-generated NumberOfEntries params ...", file=sys.stderr)
    for p in build_number_of_entries_params(instances):
        add(p)

    # Sort alphabetically
    report.parameters.sort(key=lambda p: p.dm_path)
    return report


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_text(report: DataModelReport) -> str:
    lines = [
        f"Device.X_RDK_WanManager.* Parameters — {len(report.parameters)} total",
        "=" * 90,
    ]
    for i, p in enumerate(report.parameters, start=1):
        rw = "RW" if p.writable else "RO"
        src = f"[{p.source}]"
        lines.append(f"  [{i:>3}]  {p.dm_path}")
        lines.append(f"          type={p.data_type:<22} {rw}  {src}")
    return "\n".join(lines)


def format_dmcli(report: DataModelReport) -> str:
    """Mirror dmcli eRT getv output style."""
    lines = []
    for i, p in enumerate(report.parameters, start=1):
        lines.append(f"Parameter {i:>4} name: {p.dm_path}")
        lines.append(f"               type:     {p.data_type}")
    return "\n".join(lines)


def format_json(report: DataModelReport) -> str:
    return json.dumps(asdict(report), indent=2)


def format_csv(report: DataModelReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["index", "dm_path", "param_name", "data_type",
                     "writable", "object_path", "source"])
    for i, p in enumerate(report.parameters, start=1):
        writer.writerow([i, p.dm_path, p.param_name, p.data_type,
                         p.writable, p.object_path, p.source])
    return buf.getvalue()


FORMATTERS = {
    "text":  format_text,
    "dmcli": format_dmcli,
    "json":  format_json,
    "csv":   format_csv,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Generate the COMPLETE Device.X_RDK_WanManager.* parameter list "
            "from XML + rbus registrations (matches dmcli output end-to-end)."
        )
    )
    parser.add_argument(
        "--repo-root", default=str(default_root),
        help=f"Path to the repository root (default: {default_root})",
    )
    parser.add_argument(
        "--instances", type=int, default=2,
        help="Number of table instances to expand per table object (default: 2)",
    )
    parser.add_argument(
        "--output", choices=list(FORMATTERS.keys()), default="text",
        help="Output format: text (default), dmcli, json, csv",
    )
    parser.add_argument(
        "--xml", choices=["v1", "v2", "both"], default="both",
        help="Which XML file(s) to parse: v1, v2, or both (default: both)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()

    if not repo_root.is_dir():
        print(f"ERROR: repo root not found: {repo_root}", file=sys.stderr)
        sys.exit(1)

    report = extract_all(repo_root, args.instances, args.xml)

    # Stats by source
    from collections import Counter
    src_counts = Counter(p.source for p in report.parameters)
    print(
        f"\n[*] Total parameters: {len(report.parameters)}"
        f"\n    {src_counts.get('xml_v1', 0):>4}  from RdkWanManager.xml (v1)"
        f"\n    {src_counts.get('xml_v2', 0):>4}  from RdkWanManager_v2.xml (v2)"
        f"\n    {src_counts.get('rbus', 0):>4}  rbus-only (wanmgr_rbus_handler_apis.c)"
        f"\n    {src_counts.get('auto', 0):>4}  auto-generated NumberOfEntries",
        file=sys.stderr,
    )

    print(FORMATTERS[args.output](report))


if __name__ == "__main__":
    main()
