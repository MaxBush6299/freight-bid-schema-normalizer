"""xls_converter.py

TD-001 / TD-006 — Pre-convert legacy .xls files to a temporary .xlsx so that
the rest of the pipeline can use openpyxl throughout.

Key responsibilities:
- Detect .xls by magic bytes (not just file extension).
- Convert every sheet using xlrd → openpyxl, preserving cell values.
- Fix the xlrd float-to-ZIP problem: integer-valued floats in columns whose
  name contains zip/postal/zip_code are coerced back to zero-padded strings
  (TD-006 ZIP fix).
- Return the path to the temp .xlsx; the caller is responsible for cleanup
  (use with tempfile.TemporaryDirectory or call cleanup() on the result).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

XLS_MAGIC = b"\xd0\xcf\x11\xe0"  # OLE2 / Compound Document signature

ZIP_COLUMN_PATTERN = re.compile(r"zip|postal", re.IGNORECASE)


def is_xls(path: str | Path) -> bool:
    """Return True if the file starts with the OLE2 magic bytes."""
    file_path = Path(path)
    if not file_path.exists():
        return False
    with file_path.open("rb") as fh:
        return fh.read(4) == XLS_MAGIC


def _fix_zip_value(value: Any, column_name: str) -> Any:
    """Coerce float ZIPs back to zero-padded strings.

    xlrd reads numeric cells as Python floats, so ZIP 06851 becomes 6851.0.
    When the column name looks like a ZIP/postal field and the value is a
    whole-number float, we cast to int then re-pad to 5 digits.
    """
    if not isinstance(value, float):
        return value
    if not ZIP_COLUMN_PATTERN.search(column_name):
        return value
    int_val = int(value)
    if float(int_val) == value:
        return str(int_val).zfill(5)
    return value


def convert_xls_to_xlsx(xls_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Convert an .xls file to .xlsx and return the output path.

    Parameters
    ----------
    xls_path:
        Path to the source .xls file.
    output_path:
        Optional explicit destination. When omitted, a sibling file with
        the same stem and the ``.xlsx`` extension is written inside a new
        temporary directory that the caller must clean up.  Pass an explicit
        path inside a ``tempfile.TemporaryDirectory`` to control cleanup.

    Returns
    -------
    Path
        Absolute path to the newly created .xlsx file.
    """
    try:
        import xlrd  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "xlrd is required to convert .xls files. "
            "Add 'xlrd>=2.0,<3.0' to requirements.txt and reinstall."
        ) from exc

    from openpyxl import Workbook  # local import to mirror existing style

    xls_path = Path(xls_path)

    if output_path is None:
        tmp_dir = Path(tempfile.mkdtemp())
        output_path = tmp_dir / (xls_path.stem + ".xlsx")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb_xls = xlrd.open_workbook(str(xls_path), formatting_info=False)
    wb_xlsx = Workbook()
    wb_xlsx.remove(wb_xlsx.active)  # type: ignore[arg-type]

    for sheet_index in range(wb_xls.nsheets):
        ws_xls = wb_xls.sheet_by_index(sheet_index)
        ws_xlsx = wb_xlsx.create_sheet(title=ws_xls.name)

        # Read header row to identify ZIP-like columns by name
        header_names: list[str] = []
        if ws_xls.nrows > 0:
            header_names = [
                str(ws_xls.cell_value(0, col_idx) or "")
                for col_idx in range(ws_xls.ncols)
            ]

        for row_idx in range(ws_xls.nrows):
            for col_idx in range(ws_xls.ncols):
                raw = ws_xls.cell_value(row_idx, col_idx)
                col_name = header_names[col_idx] if col_idx < len(header_names) else ""
                value = _fix_zip_value(raw, col_name)
                # xlrd returns whole-number floats for integer cells — cast them
                if isinstance(value, float) and value == int(value):
                    value = int(value)
                ws_xlsx.cell(row=row_idx + 1, column=col_idx + 1, value=value)

    wb_xlsx.save(str(output_path))
    return output_path


def ensure_xlsx(path: str | Path) -> tuple[Path, bool]:
    """Return an .xlsx-compatible path for the given workbook.

    If the file is already .xlsx (or .xlsm/etc.), returns ``(Path(path), False)``.
    If the file is .xls (detected by magic bytes), converts it and returns
    ``(converted_path, True)``.  The caller must delete the converted file
    when done (it lives in a sibling temp directory).
    """
    path = Path(path)
    if is_xls(path):
        converted = convert_xls_to_xlsx(path)
        return converted, True
    return path, False
