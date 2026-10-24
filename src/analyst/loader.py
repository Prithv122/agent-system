"""Turn a spreadsheet into a DuckDB file the agent can be asked about.

Loading is the owner's act, not the agent's, so it is the one place that opens a WRITABLE
connection with external access on (DuckDB must read the CSV). It writes a brand-new file and
refuses to touch an existing one; the agent only ever sees the result through
`executor.open_readonly`, where file and network access are off again.

The table name is the one thing that ends up inside SQL text (identifiers cannot be bound as
parameters). It is defaulted from the file stem, forced into ``[a-z_][a-z0-9_]*``, then checked by
equality against that shape and double-quoted, so a hostile file name cannot become SQL. The CSV
path itself is bound as a parameter, never formatted in.

XLSX needs ``openpyxl`` (optional extra ``excel``). The first sheet is read; the first row is the
header; values are inserted as typed Python values and DuckDB infers the column types from them.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import duckdb

_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")
_MAX_NAME = 63
CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
XLSX_SUFFIXES = {".xlsx", ".xlsm"}


def sanitize_table_name(raw: str) -> str:
    """Lower-case, replace every run of other characters with ``_``, avoid a leading digit."""
    name = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not name:
        name = "data"
    if name[0].isdigit():
        name = "t_" + name
    return name[:_MAX_NAME]


def validated_table_name(name: str) -> str:
    """Return ``name`` only if it is exactly a safe identifier; otherwise raise ``ValueError``."""
    if len(name) > _MAX_NAME or _NAME_RE.fullmatch(name) is None:
        raise ValueError(
            f"invalid table name {name!r}: use lower-case letters, digits and underscores, "
            "not starting with a digit"
        )
    return name


def _quote(identifier: str) -> str:
    return '"' + validated_table_name(identifier) + '"'


def _clean_header(values: list[Any]) -> list[str]:
    """Header cells -> unique non-empty text names (the database copes, the agent reads them)."""
    names: list[str] = []
    seen: dict[str, int] = {}
    for i, value in enumerate(values, 1):
        base = str(value).strip() if value is not None and str(value).strip() else f"column{i}"
        count = seen.get(base.lower(), 0)
        seen[base.lower()] = count + 1
        names.append(base if count == 0 else f"{base}_{count + 1}")
    return names


def _read_xlsx(source: Path) -> tuple[list[str], list[tuple[Any, ...]]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            "reading .xlsx needs openpyxl; install with `uv sync --extra excel`"
        ) from exc
    book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
    finally:
        book.close()
    rows = [r for r in rows if any(v is not None for v in r)]
    if not rows:
        raise ValueError(f"{source} has no data rows")
    header = _clean_header(list(rows[0]))
    width = len(header)
    body = [tuple((r + (None,) * width)[:width]) for r in rows[1:]]
    return header, body


def load(source: Path, db: Path, table: str | None = None) -> dict[str, Any]:
    """Load a CSV or XLSX file into a new DuckDB file. Returns ``{table, rows, columns}``.

    Raises `FileNotFoundError` for a missing source, `FileExistsError` if ``db`` already exists
    (never overwritten), `ValueError` for a bad table name, an unsupported file type or a file
    with no data rows, and `RuntimeError` when XLSX is requested without openpyxl.
    """
    source, db = Path(source), Path(db)
    if not source.is_file():
        raise FileNotFoundError(f"no such file: {source}")
    suffix = source.suffix.lower()
    if suffix not in CSV_SUFFIXES | XLSX_SUFFIXES:
        raise ValueError(f"unsupported file type {suffix!r}; expected .csv or .xlsx")
    name = validated_table_name(table) if table is not None else sanitize_table_name(source.stem)
    name = validated_table_name(name)
    if db.exists():
        raise FileExistsError(f"{db} already exists; refusing to overwrite it")
    header_rows: tuple[list[str], list[tuple[Any, ...]]] | None = None
    if suffix in XLSX_SUFFIXES:
        header_rows = _read_xlsx(source)  # before touching disk: a missing extra leaves no file

    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    try:
        quoted = _quote(name)
        if header_rows is None:
            con.execute(
                f"CREATE TABLE {quoted} AS SELECT * FROM read_csv_auto(?, header = true)",
                [str(source)],
            )
        else:
            header, body = header_rows
            _create_from_rows(con, quoted, header, body)
        rows = int(con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])  # type: ignore[index]
        columns = [r[0] for r in con.execute(f"DESCRIBE {quoted}").fetchall()]
        if rows == 0:
            raise ValueError(f"{source} has no data rows")
    except Exception:
        con.close()
        db.unlink(missing_ok=True)
        Path(str(db) + ".wal").unlink(missing_ok=True)
        raise
    con.close()
    return {"table": name, "rows": rows, "columns": columns}


def _column_type(values: list[Any]) -> str:
    """The narrowest DuckDB type that holds every non-empty value; anything mixed is text."""
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return "VARCHAR"
    if kinds <= {bool}:
        return "BOOLEAN"
    if kinds <= {int}:
        return "BIGINT"
    if kinds <= {int, float}:
        return "DOUBLE"
    if kinds <= {dt.datetime}:
        midnight = all(v.time() == dt.time(0) for v in values if v is not None)
        return "DATE" if midnight else "TIMESTAMP"
    if kinds <= {dt.date}:
        return "DATE"
    return "VARCHAR"


def _create_from_rows(
    con: duckdb.DuckDBPyConnection, quoted: str, header: list[str], body: list[tuple[Any, ...]]
) -> None:
    """Create the table from in-memory rows, typing each column from its values."""
    types: list[str] = []
    columns: list[list[Any]] = []
    for i in range(len(header)):
        col = [row[i] for row in body]
        kind = _column_type(col)
        if kind == "DATE":
            col = [v.date() if isinstance(v, dt.datetime) else v for v in col]
        elif kind == "VARCHAR":
            col = [None if v is None else str(v) for v in col]
        types.append(kind)
        columns.append(col)
    defs = ", ".join(
        '"' + name.replace('"', '""') + '" ' + kind
        for name, kind in zip(header, types, strict=True)
    )
    con.execute(f"CREATE TABLE {quoted} ({defs})")
    if body:
        marks = ", ".join("?" for _ in header)
        con.executemany(f"INSERT INTO {quoted} VALUES ({marks})", list(zip(*columns, strict=True)))
