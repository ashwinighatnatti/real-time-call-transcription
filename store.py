"""Local SQLite storage + Excel export. New fields in fields.py become columns automatically."""
import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

from fields import PROPERTY_FIELDS, PARCEL_FIELDS, AGENCY_FIELDS, INSTALLMENT_FIELDS

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "calls.db"))


def csv_path():
    """agencies.csv sits next to the database file."""
    return os.path.join(os.path.dirname(DB_PATH) or ".", "agencies.csv")


def _ensure_table(con, name, base_cols, fields):
    con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({base_cols})")
    existing = {r[1] for r in con.execute(f"PRAGMA table_info({name})")}
    for f in fields:  # keys come from fields.py, not user input
        if f["key"] not in existing:
            con.execute(f'ALTER TABLE {name} ADD COLUMN "{f["key"]}" TEXT')


def _connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    _ensure_table(con, "calls", "call_id TEXT PRIMARY KEY, saved_at TEXT, transcript TEXT", PROPERTY_FIELDS)
    _ensure_table(con, "parcels", "call_id TEXT, parcel_id TEXT", PARCEL_FIELDS)
    _ensure_table(con, "agencies", "call_id TEXT, parcel_id TEXT, agency_id TEXT", AGENCY_FIELDS)
    _ensure_table(con, "installments", "call_id TEXT, agency_id TEXT, installment_id TEXT", INSTALLMENT_FIELDS)
    return con


def _insert(con, table, row):
    cols = ", ".join(f'"{c}"' for c in row)
    marks = ", ".join("?" for _ in row)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def save_call(call_id, state, transcript):
    """Insert or replace a call (safe to call repeatedly for the same call_id)."""
    with closing(_connect()) as con, con:
        for table in ("installments", "agencies", "parcels", "calls"):
            con.execute(f"DELETE FROM {table} WHERE call_id = ?", (call_id,))
        _insert(con, "calls", {"call_id": call_id, "saved_at": datetime.now().isoformat(timespec="seconds"),
                               "transcript": transcript, **state["property"]})
        for p in state["parcels"]:
            _insert(con, "parcels", {"call_id": call_id, "parcel_id": p["id"],
                                     **{f["key"]: p[f["key"]] for f in PARCEL_FIELDS}})
            for a in p["agencies"]:
                _insert(con, "agencies", {"call_id": call_id, "parcel_id": p["id"], "agency_id": a["id"],
                                          **{f["key"]: a[f["key"]] for f in AGENCY_FIELDS}})
                for i in a["installments"]:
                    _insert(con, "installments", {"call_id": call_id, "agency_id": a["id"], "installment_id": i["id"],
                                                  **{f["key"]: i[f["key"]] for f in INSTALLMENT_FIELDS}})
    try:
        write_agencies_csv()
    except OSError as e:          # e.g. the CSV is open in Excel; the database save already succeeded
        print(f"agencies.csv not updated ({e})")


def export_xlsx():
    """One row per installment, with its agency, parcel and property fields repeated."""
    cols = ['c.call_id AS "Call ID"', 'c.saved_at AS "Saved At"']
    cols += [f'c."{f["key"]}" AS "{f["label"]}"' for f in PROPERTY_FIELDS]
    cols += [f'p."{f["key"]}" AS "{f["label"]}"' for f in PARCEL_FIELDS]
    cols += [f'a."{f["key"]}" AS "{f["label"]}"' for f in AGENCY_FIELDS]
    cols += [f'i."{f["key"]}" AS "{f["label"]}"' for f in INSTALLMENT_FIELDS]
    sql = (f"SELECT {', '.join(cols)} FROM calls c "
           "LEFT JOIN parcels p ON p.call_id = c.call_id "
           "LEFT JOIN agencies a ON a.call_id = p.call_id AND a.parcel_id = p.parcel_id "
           "LEFT JOIN installments i ON i.call_id = a.call_id AND i.agency_id = a.agency_id "
           "ORDER BY c.saved_at, p.rowid, a.rowid, i.rowid")
    with closing(_connect()) as con:
        df = pd.read_sql_query(sql, con)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    return buf


def agencies_df():
    """One row per agency with its parcel and property details (installments are not included)."""
    cols = ['c.call_id AS "Call ID"', 'c.saved_at AS "Saved At"']
    cols += [f'c."{f["key"]}" AS "{f["label"]}"' for f in PROPERTY_FIELDS]
    cols += [f'p."{f["key"]}" AS "{f["label"]}"' for f in PARCEL_FIELDS]
    cols += [f'a."{f["key"]}" AS "{f["label"]}"' for f in AGENCY_FIELDS]
    sql = (f"SELECT {', '.join(cols)} FROM agencies a "
           "JOIN parcels p ON p.call_id = a.call_id AND p.parcel_id = a.parcel_id "
           "JOIN calls c ON c.call_id = a.call_id "
           "ORDER BY c.saved_at, p.rowid, a.rowid")
    with closing(_connect()) as con:
        return pd.read_sql_query(sql, con)


def write_agencies_csv():
    """Rebuild agencies.csv from the database (so repeated saves never duplicate rows)."""
    agencies_df().to_csv(csv_path(), index=False, encoding="utf-8-sig")   # utf-8-sig: opens cleanly in Excel
    return csv_path()
