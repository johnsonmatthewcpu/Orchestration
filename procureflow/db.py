"""SQLite storage layer."""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    cost_center TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'employee',
    department_id INTEGER REFERENCES departments(id),
    manager_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    category TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    amount REAL NOT NULL,
    UNIQUE (department_id, category, fiscal_year)
);
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    email TEXT,
    tax_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    risk_tier TEXT NOT NULL DEFAULT 'unassessed',
    onboarding TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    vendor_id INTEGER REFERENCES vendors(id),
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    description TEXT,
    line_items TEXT NOT NULL DEFAULT '[]',
    answers TEXT NOT NULL DEFAULT '{}',
    flags TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_steps (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    stage INTEGER NOT NULL,
    name TEXT NOT NULL,
    approver_user_id INTEGER REFERENCES users(id),
    approver_role TEXT,
    status TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    comment TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS po_lines (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    received_quantity REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    po_id INTEGER REFERENCES purchase_orders(id),
    amount REAL NOT NULL,
    lines TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    match_result TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (vendor_id, number)
);
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    request_id INTEGER REFERENCES requests(id),
    title TEXT NOT NULL,
    value REAL NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    notice_days INTEGER NOT NULL DEFAULT 60,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    renewal_request_id INTEGER REFERENCES requests(id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor_id INTEGER,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS clauses (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    section TEXT,
    version_date TEXT,
    source_url TEXT
);
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '["*"]'
);
"""

JSON_COLUMNS = {"onboarding", "line_items", "answers", "flags", "lines", "match_result", "detail", "events"}


class Database:
    """Thin wrapper around a single SQLite connection guarded by a lock."""

    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        out = dict(row)
        for key in JSON_COLUMNS & out.keys():
            if isinstance(out[key], str):
                out[key] = json.loads(out[key])
        return out

    def all(self, sql, params=()):
        with self.lock:
            return [self._decode(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        with self.lock:
            return self._decode(self.conn.execute(sql, params).fetchone())

    def scalar(self, sql, params=()):
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def insert(self, table, values):
        values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
            self.conn.commit()
            return cur.lastrowid

    def update(self, table, row_id, values):
        values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))
            self.conn.commit()

    def get(self, table, row_id):
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
