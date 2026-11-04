#!/usr/bin/env python3
"""
Combined RDK-B datamodel extractor -- everything from the repo, nothing from
a live device.

Merges two independent sources, both parsed straight from source with real
build-flag evaluation, into one deduplicated parameter list:

  1. CCSP XML  (config/*.xml)              -- via extract_dml.py's logic
  2. rbus C registrations (source/**/*.c)  -- via extract_rbus_dml.py's logic

Why both are needed: XML describes the CCSP-managed data model; rbus C
registrations describe properties wired directly to get/set callbacks that
often never appear in any XML file (see extract_rbus_dml.py's docstring).
A component's *actual* shipped model is the union of both, filtered by
whichever build flags apply to your target.

Usage:
    # Everything, no build-flag filtering (superset / "full picture")
    python3 tools/extract_all_dml.py --repo-root . --build-flags keep --output json

    # A specific build variant (mirrors your Yocto config)
    python3 tools/extract_all_dml.py --repo-root . \\
        --enable-flag RBUS_BUILD_FLAG_ENABLE \\
        --enable-flag WAN_MANAGER_UNIFICATION_ENABLED \\
        --output json

    # Only the XML you actually ship (avoids double-counting a legacy v1
    # file that a different Yocto recipe variant would exclude)
    python3 tools/extract_all_dml.py --repo-root . --xml-file RdkWanManager_v2.xml
"""

import argparse
import csv
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import extract_dml
import extract_rbus_dml


@dataclass(frozen=True)
class UnifiedParameter:
    dm_path: str
    param_name: str
    data_type: str
    writable: bool
    object_path: str
    source: str        # xml | auto | rbus_property | rbus_event | rbus_method
    origin_file: str    # xml filename or c filename


def collect_xml(
    config_dir: Path, instances: int, build_flags_mode: str, enabled: set[str], xml_files: list[str] | None
) -> list[UnifiedParameter]:
    dml_xmls = extract_dml.discover_dml_xml_files(config_dir, build_flags_mode, enabled)
    if xml_files:
        wanted = set(xml_files)
        dml_xmls = [d for d in dml_xmls if d.path.name in wanted]

    results: list[UnifiedParameter] = []
    for dml_xml in dml_xmls:
        for p in extract_dml.parse_dml_xml(dml_xml, instances):
            results.append(
                UnifiedParameter(
                    dm_path=p.dm_path,
                    param_name=p.param_name,
                    data_type=p.data_type,
                    writable=p.writable,
                    object_path=p.object_path,
                    source=p.source,
                    origin_file=p.xml_file,
                )
            )
    return results, dml_xmls


def collect_rbus(
    source_dir: Path, instances: int, enabled: set[str], build_flags_mode: str = "strip"
) -> tuple[list[UnifiedParameter], int, list[Path]]:
    c_files = extract_rbus_dml.discover_c_files(source_dir)
    h_files = extract_rbus_dml.discover_h_files(source_dir)
    macros = extract_rbus_dml.build_macro_table(h_files, enabled, build_flags_mode)

    results: list[UnifiedParameter] = []
    total_tables = 0
    for c_path in c_files:
        cleaned, file_macros = extract_rbus_dml.preprocess_c(
            c_path.read_text(encoding="utf-8", errors="replace"), enabled, build_flags_mode
        )
        local_macros = dict(macros)
        local_macros.update(file_macros)
        rbus_params, table_count = extract_rbus_dml.parse_rbus_source_from_text(
            cleaned, c_path, local_macros, instances
        )
        total_tables += table_count
        for p in rbus_params:
            results.append(
                UnifiedParameter(
                    dm_path=p.dm_path,
                    param_name=p.param_name,
                    data_type=p.data_type,
                    writable=p.writable,
                    object_path=p.object_path,
                    source=p.source,
                    origin_file=p.c_file,
                )
            )
    return results, total_tables, c_files


def dedupe(parameters: list[UnifiedParameter]) -> list[UnifiedParameter]:
    # XML entries win over rbus entries for the same dm_path (XML carries a
    # real declared data_type; rbus-only entries just say "property").
    priority = {"xml": 0, "auto": 1}
    best: dict[str, UnifiedParameter] = {}
    for p in parameters:
        rank = priority.get(p.source, 2)
        current = best.get(p.dm_path)
        if current is None:
            best[p.dm_path] = p
        else:
            current_rank = priority.get(current.source, 2)
            if rank < current_rank:
                best[p.dm_path] = p
    return sorted(best.values(), key=lambda item: item.dm_path)


def filter_prefix(parameters: list[UnifiedParameter], prefix: str | None) -> list[UnifiedParameter]:
    if not prefix:
        return parameters
    return [p for p in parameters if p.dm_path.startswith(prefix)]


def format_text(parameters: list[UnifiedParameter]) -> str:
    lines = [f"Parameters: {len(parameters)}", "=" * 100]
    for index, p in enumerate(parameters, start=1):
        access = "RW" if p.writable else "RO"
        lines.append(f"[{index:>4}] {p.dm_path}")
        lines.append(f"       type={p.data_type:<18} {access}  source={p.source:<14} file={p.origin_file}")
    return "\n".join(lines)


def format_json(parameters: list[UnifiedParameter]) -> str:
    return json.dumps([asdict(p) for p in parameters], indent=2)


def format_csv(parameters: list[UnifiedParameter]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["index", "dm_path", "param_name", "data_type", "writable", "object_path", "source", "origin_file"])
    for index, p in enumerate(parameters, start=1):
        writer.writerow([index, p.dm_path, p.param_name, p.data_type, p.writable, p.object_path, p.source, p.origin_file])
    return output.getvalue()


FORMATTERS = {"text": format_text, "json": format_json, "csv": format_csv}


def parse_args() -> argparse.Namespace:
    default_repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Extract the complete RDK-B datamodel (XML + rbus C registrations) directly from a repo."
    )
    parser.add_argument("--repo-root", default=str(default_repo_root))
    parser.add_argument("--config-dir", help="XML config dir (default: <repo-root>/config)")
    parser.add_argument("--source-dir", help="C/C++ source dir (default: <repo-root>/source)")
    parser.add_argument("--instances", type=int, default=2)
    parser.add_argument("--output", choices=sorted(FORMATTERS), default="text")
    parser.add_argument("--prefix")
    parser.add_argument(
        "--build-flags",
        choices=["strip", "keep"],
        default="strip",
        help="Conditional handling for BOTH XML <?ifdef?> and C #ifdef/#if defined(): "
        "strip (default) evaluates against --enable-flag -- reproduces one specific build "
        "variant, and a file/component whose registration sits entirely behind an un-enabled "
        "flag will correctly show 0 elements. keep ignores all conditionals on both sides "
        "(full picture / union of every branch).",
    )
    parser.add_argument("--enable-flag", action="append", default=[], metavar="FLAG")
    parser.add_argument(
        "--xml-file",
        action="append",
        default=None,
        metavar="FILENAME",
        help="Restrict XML parsing to specific file(s) in --config-dir (repeatable). "
        "Default: all *.xml files found. Use this to avoid mixing multiple build variants "
        "(e.g. a legacy v1 model your target doesn't actually ship) into one report.",
    )
    parser.add_argument("--skip-xml", action="store_true", help="Only extract rbus C registrations.")
    parser.add_argument("--skip-rbus", action="store_true", help="Only extract XML.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    config_dir = Path(args.config_dir).resolve() if args.config_dir else repo_root / "config"
    source_dir = Path(args.source_dir).resolve() if args.source_dir else repo_root / "source"
    enabled = set(args.enable_flag)

    all_params: list[UnifiedParameter] = []

    if not args.skip_xml:
        if not config_dir.is_dir():
            print(f"WARNING: config dir not found, skipping XML: {config_dir}", file=sys.stderr)
        else:
            xml_params, dml_xmls = collect_xml(config_dir, args.instances, args.build_flags, enabled, args.xml_file)
            print(f"[*] XML: {len(dml_xmls)} top-level object(s) parsed -> {len(xml_params)} parameter(s)", file=sys.stderr)
            for d in dml_xmls:
                print(f"      {d.path.name} -> {d.prefix}", file=sys.stderr)
            all_params.extend(xml_params)

    if not args.skip_rbus:
        if not source_dir.is_dir():
            print(f"WARNING: source dir not found, skipping rbus: {source_dir}", file=sys.stderr)
        else:
            rbus_params, table_count, c_files = collect_rbus(source_dir, args.instances, enabled, args.build_flags)
            print(f"[*] rbus: {len(c_files)} C file(s) with registration tables -> {len(rbus_params)} element(s), {table_count} table marker(s)", file=sys.stderr)
            all_params.extend(rbus_params)

    parameters = dedupe(all_params)
    parameters = filter_prefix(parameters, args.prefix)

    xml_only = sum(1 for p in parameters if p.source in ("xml", "auto"))
    rbus_only = len(parameters) - xml_only
    print(f"[*] Merged total: {len(parameters)}  (XML: {xml_only}, rbus-only: {rbus_only})", file=sys.stderr)

    print(FORMATTERS[args.output](parameters))
    return 0


if __name__ == "__main__":
    sys.exit(main())
