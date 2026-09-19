"""
저장 계층 — 설계문서 8번/10번의 my_po_header + my_po_line 구조를 SQLite로 구현.
운영 규모가 커지면 이 모듈만 Postgres 등으로 교체하면 되고, 그 외 코드는
영향받지 않도록 함수 인터페이스만 앱과 계약(contract)한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# cwd(실행 위치)에 의존하지 않도록 이 파일 기준 절대경로로 계산한다.
# core/storage.py -> parent(core) -> parent(app) / data / my_po.db
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(_DATA_DIR / "my_po.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS my_po_header (
    po_no           TEXT PRIMARY KEY,
    style_no        TEXT,
    factory_code    TEXT,
    factory_name    TEXT,
    channel_type    TEXT,
    selling_channel TEXT,
    flow_type       TEXT,
    floorset        TEXT,
    total_order_units INTEGER,
    validated       INTEGER,      -- 1 = 자기검증 통과, 0 = 강제저장(override)
    confirmed_by    TEXT,
    confirmed_at    TEXT,
    source_path     TEXT,
    source_modified_at TEXT,
    file_hash       TEXT
);

CREATE TABLE IF NOT EXISTS my_po_line (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no           TEXT NOT NULL,
    color_code      TEXT,
    color_name      TEXT,
    design_color    TEXT,
    sub_channel     TEXT,
    pack_type       TEXT,
    fob             REAL,
    size_code       TEXT,
    qty             INTEGER,
    total_color_qty INTEGER,
    total_line_qty INTEGER,
    FOREIGN KEY (po_no) REFERENCES my_po_header(po_no)
);

CREATE TABLE IF NOT EXISTS my_po_validation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no TEXT NOT NULL,
    color_code TEXT,
    color_name TEXT,
    size_sum INTEGER,
    total_color_qty INTEGER,
    size_check_ok INTEGER,
    color_sum_vs_order_ok INTEGER,
    sum_of_colors INTEGER,
    order_units INTEGER,
    status TEXT,
    parse_warnings TEXT,
    info_notes TEXT,
    FOREIGN KEY (po_no) REFERENCES my_po_header(po_no)
);

CREATE TABLE IF NOT EXISTS my_po_upcharge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no TEXT NOT NULL,
    color_code TEXT,
    color_name TEXT,
    under_l_qty INTEGER,
    over_l_qty INTEGER,
    total_qty INTEGER,
    over_l_ratio REAL,
    needs_upcharge_review INTEGER,
    FOREIGN KEY (po_no) REFERENCES my_po_header(po_no)
);
"""


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(my_po_header)")}
    for name in ("source_path", "source_modified_at", "file_hash"):
        if name not in columns:
            conn.execute(f"ALTER TABLE my_po_header ADD COLUMN {name} TEXT")
    line_columns = {row[1] for row in conn.execute("PRAGMA table_info(my_po_line)")}
    if "total_line_qty" not in line_columns:
        conn.execute("ALTER TABLE my_po_line ADD COLUMN total_line_qty INTEGER")
    conn.commit()
    return conn


def save_po(
    df_line: pd.DataFrame,
    header_fields: dict,
    validated: bool,
    confirmed_by: str = "auto",
    source_path: str | None = None,
    source_modified_at: str | None = None,
    file_hash: str | None = None,
    validation_result=None,
    upcharge_df: pd.DataFrame | None = None,
    db_path: str = DB_PATH,
) -> str:
    """PO 1건을 저장하고, 동일/오래된 원본은 기존 최신 데이터를 유지한다."""
    conn = get_connection(db_path)
    try:
        po_no = header_fields["po_no"]
        existing = conn.execute(
            """SELECT file_hash, source_modified_at, channel_type, selling_channel
               FROM my_po_header WHERE po_no = ?""",
            (po_no,),
        ).fetchone()
        if (
            existing
            and source_modified_at
            and existing[1]
            and source_modified_at < existing[1]
        ):
            return "old"
        incomplete_lines = conn.execute(
            """SELECT 1 FROM my_po_line
               WHERE po_no = ?
                 AND (sub_channel IS NULL OR pack_type IS NULL OR fob IS NULL)
               LIMIT 1""",
            (po_no,),
        ).fetchone()
        details_exist = conn.execute(
            "SELECT 1 FROM my_po_validation WHERE po_no = ? LIMIT 1", (po_no,)
        ).fetchone()
        stored_line_count, stored_pack_types, missing_line_totals = conn.execute(
            """SELECT COUNT(*),
                      GROUP_CONCAT(DISTINCT COALESCE(pack_type, '')),
                      SUM(CASE WHEN total_line_qty IS NULL THEN 1 ELSE 0 END)
               FROM my_po_line
               WHERE po_no = ?""",
            (po_no,),
        ).fetchone()
        incoming_pack_types = {
            str(value)
            for value in df_line.get("pack_type", pd.Series(dtype=object)).dropna()
        }
        stored_pack_type_set = {
            value for value in (stored_pack_types or "").split(",") if value
        }
        line_shape_needs_refresh = existing and (
            stored_line_count != len(df_line)
            or stored_pack_type_set != incoming_pack_types
            or (
                missing_line_totals
                and df_line["total_line_qty"].notna().any()
            )
        )
        header_needs_refresh = existing and (
            existing[2] != header_fields.get("channel_type")
            or existing[3] != header_fields.get("selling_channel")
        )
        if (
            existing
            and existing[0] == file_hash
            and details_exist
            and not incomplete_lines
            and not line_shape_needs_refresh
            and not header_needs_refresh
        ):
            return "unchanged"
        conn.execute("DELETE FROM my_po_header WHERE po_no = ?", (po_no,))
        conn.execute("DELETE FROM my_po_line WHERE po_no = ?", (po_no,))
        conn.execute("DELETE FROM my_po_validation WHERE po_no = ?", (po_no,))
        conn.execute("DELETE FROM my_po_upcharge WHERE po_no = ?", (po_no,))

        conn.execute(
            """INSERT INTO my_po_header
               (po_no, style_no, factory_code, factory_name, channel_type,
                selling_channel, flow_type, floorset, total_order_units,
                validated, confirmed_by, confirmed_at, source_path,
                source_modified_at, file_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                po_no,
                header_fields.get("style_no"),
                header_fields.get("factory_code"),
                header_fields.get("factory_name"),
                header_fields.get("channel_type"),
                header_fields.get("selling_channel"),
                header_fields.get("flow_type"),
                header_fields.get("floorset"),
                header_fields.get("total_order_units"),
                int(validated),
                confirmed_by,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source_path,
                source_modified_at,
                file_hash,
            ),
        )

        line_cols = ["color_code", "color_name", "design_color", "sub_channel",
                     "pack_type", "fob", "size_code", "qty", "total_color_qty",
                     "total_line_qty"]
        for _, r in df_line.iterrows():
            conn.execute(
                f"""INSERT INTO my_po_line (po_no, {", ".join(line_cols)})
                    VALUES ({", ".join(["?"] * (len(line_cols) + 1))})""",
                (po_no, *[r[c] for c in line_cols]),
            )
        if validation_result is not None:
            checks = validation_result.color_checks or [None]
            for check in checks:
                conn.execute(
                    """INSERT INTO my_po_validation
                       (po_no, color_code, color_name, size_sum, total_color_qty,
                        size_check_ok, color_sum_vs_order_ok, sum_of_colors,
                        order_units, status, parse_warnings, info_notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        po_no,
                        check.color_code if check else None,
                        check.color_name if check else None,
                        check.size_sum if check else None,
                        check.total_color_qty if check else None,
                        int(check.ok) if check else None,
                        validation_result.color_sum_vs_order_ok,
                        validation_result.sum_of_colors,
                        validation_result.order_units,
                        validation_result.status,
                        "\n".join(validation_result.parse_warnings),
                        "\n".join(validation_result.info_notes),
                    ),
                )
        if upcharge_df is not None and not upcharge_df.empty:
            for _, r in upcharge_df.iterrows():
                conn.execute(
                    """INSERT INTO my_po_upcharge
                       (po_no, color_code, color_name, under_l_qty, over_l_qty,
                        total_qty, over_l_ratio, needs_upcharge_review)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        po_no, r["color_code"], r["color_name"], r["under_l_qty"],
                        r["over_l_qty"], r["total_qty"], r["over_l_ratio"],
                        int(r["needs_upcharge_review"]),
                    ),
                )
        conn.commit()
        return "saved"
    finally:
        conn.close()


def load_all_headers(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query("SELECT * FROM my_po_header ORDER BY confirmed_at DESC", conn)
    finally:
        conn.close()


def load_lines(po_no: str, db_path: str = DB_PATH) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(
            "SELECT * FROM my_po_line WHERE po_no = ?", conn, params=(po_no,)
        )
    finally:
        conn.close()


def load_all_lines(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(
            "SELECT * FROM my_po_line ORDER BY po_no, id", conn
        )
    finally:
        conn.close()


def load_all_validation(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query("SELECT * FROM my_po_validation ORDER BY po_no, id", conn)
    finally:
        conn.close()


def load_all_upcharge(db_path: str = DB_PATH) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query("SELECT * FROM my_po_upcharge ORDER BY po_no, id", conn)
    finally:
        conn.close()
