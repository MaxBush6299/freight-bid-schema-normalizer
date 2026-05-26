"""Compare LLM-priced submission values against the source LaneExport.

For every cell the writer logged, look up the original (route, field) pair in
the LaneExport workbook and verify the LLM wrote the correct value.

Usage:
    python scripts/compare_llm_to_export.py <run_dir> <lane_export.xlsx>

Run dir must contain write_report.json (produced by the rehydrate pipeline).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import openpyxl


def _load_export(path: Path) -> tuple[list[str], list[dict]]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    records = []
    for r in rows[1:]:
        if all(c is None or str(c).strip() == "" for c in r):
            continue
        records.append({headers[i]: r[i] for i in range(len(headers))})
    return headers, records


def _values_match(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-6)
    return str(a).strip() == str(b).strip()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    run_dir = Path(sys.argv[1])
    export_path = Path(sys.argv[2])
    write_report = json.loads((run_dir / "write_report.json").read_text(encoding="utf-8"))
    mapping_plan = json.loads((run_dir / "mapping_plan.json").read_text(encoding="utf-8"))
    write_log: list[dict] = write_report["write_log"]

    # Build target_column -> source_field map from mapping plan
    col_to_field = {m["target_column"]: m["source_field"] for m in mapping_plan["mappings"]}

    headers, records = _load_export(export_path)
    print(f"Export loaded: {len(records)} rows, {len(headers)} columns")
    print()

    # Build template_route -> export_record by inspecting Customer Lane ID writes
    # The writer puts the export's Customer Lane ID value into the template, so we
    # can use that value to join back to the export row.
    route_to_export: dict[str, dict] = {}
    by_clid = {str(r.get("Customer Lane ID", "")).strip(): r for r in records}
    for entry in write_log:
        if entry.get("action") != "written":
            continue
        if entry.get("field") == "Customer Lane ID":
            route = str(entry.get("route", "")).strip()
            clid = str(entry.get("value", "")).strip()
            if clid in by_clid:
                route_to_export[route] = by_clid[clid]
    print(f"Resolved {len(route_to_export)} template routes -> export rows via Customer Lane ID writes")
    print()

    total_written = 0
    matches = 0
    mismatches: list[dict] = []
    unmatched_routes: set[str] = set()
    field_no_source: dict[str, int] = {}

    for entry in write_log:
        if entry.get("action") != "written":
            continue
        total_written += 1
        route = str(entry.get("route", "")).strip()
        field = entry.get("field", "")
        written_val = entry.get("value")

        # Translate template field to export field via mapping plan
        export_field = col_to_field.get(field, field)

        rec = route_to_export.get(route)
        if rec is None:
            unmatched_routes.add(route)
            continue
        if export_field not in rec:
            # Field doesn't exist in export (likely a derived/transformed value)
            field_no_source[field] = field_no_source.get(field, 0) + 1
            continue

        source_val = rec[export_field]
        if _values_match(written_val, source_val):
            matches += 1
        else:
            mismatches.append({
                "sheet": entry["sheet"],
                "cell": f"R{entry['row']}C{entry['col']}",
                "route": route,
                "template_field": field,
                "export_field": export_field,
                "written": written_val,
                "source": source_val,
            })

    print(f"=== Summary ===")
    print(f"Total cells written  : {total_written}")
    print(f"Lanes resolved       : {len(route_to_export)}")
    print(f"Exact value matches  : {matches}")
    print(f"Value mismatches     : {len(mismatches)}")
    print(f"Routes not in export : {len(unmatched_routes)}")
    print(f"Fields not in export : {sum(field_no_source.values())} writes "
          f"across {len(field_no_source)} fields")
    if field_no_source:
        print("  Field breakdown (derived/transformed, no direct source):")
        for k, v in sorted(field_no_source.items(), key=lambda kv: -kv[1]):
            print(f"    {v:4d}x  {k}")
    if unmatched_routes:
        print(f"\nUnmatched routes (first 5): {list(unmatched_routes)[:5]}")
    if mismatches:
        print(f"\nFirst 10 mismatches:")
        for m in mismatches[:10]:
            print(f"  {m['sheet']} {m['cell']} {m['route']}/{m['template_field']}: "
                  f"wrote {m['written']!r} vs export {m['source']!r}")
    return 0 if not mismatches and not unmatched_routes else 1


if __name__ == "__main__":
    sys.exit(main())
