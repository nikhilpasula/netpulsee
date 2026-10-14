# database.py
"""SQLite persistence layer for NetPulse.

The database is the ONLY shared state between the Streamlit UI and the
background monitor thread. Every function opens a short-lived connection so
that the worker and the UI never fight over a single cursor.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

DEFAULT_DB = "network_monitor.db"
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    """Local time as a sortable 'YYYY-MM-DD HH:MM:SS' string."""
    return datetime.now().strftime(TIMESTAMP_FMT)


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with a Row factory, 10s lock timeout and thread safety."""
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # lets the UI read while the worker writes
    return conn


def init_db(db_path: str = DEFAULT_DB) -> None:
    """Create tables/indexes and seed two default devices if the table is empty."""
    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    name             TEXT    NOT NULL,
                    host             TEXT    NOT NULL UNIQUE,
                    interval_seconds INTEGER NOT NULL DEFAULT 15,
                    enabled          INTEGER NOT NULL DEFAULT 1,
                    created_at       TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS samples (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id         INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                    device_name       TEXT,
                    host              TEXT,
                    status            TEXT    NOT NULL,
                    latency_ms        REAL,
                    interface         TEXT,
                    rx_bytes_per_sec  REAL    NOT NULL DEFAULT 0,
                    tx_bytes_per_sec  REAL    NOT NULL DEFAULT 0,
                    created_at        TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    severity   TEXT NOT NULL,
                    title      TEXT NOT NULL,
                    message    TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_samples_created_at
                    ON samples(created_at);
                CREATE INDEX IF NOT EXISTS idx_samples_device_created
                    ON samples(device_id, created_at);
                """
            )

            count = conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
            if count == 0:
                now = _now_str()
                conn.executemany(
                    """INSERT INTO devices (name, host, interval_seconds, enabled, created_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    [
                        ("Google DNS", "8.8.8.8", 15, now),
                        ("Cloudflare DNS", "1.1.1.1", 15, now),
                    ],
                )
    finally:
        conn.close()


def get_devices(db_path: str = DEFAULT_DB):
    """Enabled devices ordered by id."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM devices WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def add_device(db_path: str, name: str, host: str, interval_seconds: int = 15) -> None:
    """Insert a device. Raises ValueError when the host already exists (case-insensitive)."""
    conn = connect(db_path)
    try:
        with conn:
            existing = conn.execute(
                "SELECT id FROM devices WHERE LOWER(host) = LOWER(?)", (host,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"A device with host '{host}' already exists.")

            conn.execute(
                """INSERT INTO devices (name, host, interval_seconds, enabled, created_at)
                   VALUES (?, ?, ?, 1, ?)""",
                (name, host, int(interval_seconds), _now_str()),
            )
    finally:
        conn.close()


def delete_device(db_path: str, device_id: int) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM devices WHERE id = ?", (int(device_id),))
    finally:
        conn.close()


def insert_sample(
    db_path: str,
    device_id=None,
    device_name=None,
    host=None,
    status: str = "OK",
    latency_ms=None,
    interface=None,
    rx_bytes_per_sec: float = 0,
    tx_bytes_per_sec: float = 0,
    created_at=None,
) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO samples
                       (device_id, device_name, host, status, latency_ms, interface,
                        rx_bytes_per_sec, tx_bytes_per_sec, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device_id,
                    device_name,
                    host,
                    status,
                    latency_ms,
                    interface,
                    float(rx_bytes_per_sec or 0),
                    float(tx_bytes_per_sec or 0),
                    created_at or _now_str(),
                ),
            )
    finally:
        conn.close()


def insert_alert(db_path: str, severity: str, title: str, message: str) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO alerts (severity, title, message, created_at)
                   VALUES (?, ?, ?, ?)""",
                (severity, title, message, _now_str()),
            )
    finally:
        conn.close()


def get_latest_samples(db_path: str = DEFAULT_DB):
    """Latest sample per device_id (device samples only; host-traffic rows have NULL device_id)."""
    conn = connect(db_path)
    try:
        return conn.execute(
            """
            SELECT s.*
            FROM samples AS s
            INNER JOIN (
                SELECT device_id, MAX(id) AS max_id
                FROM samples
                WHERE device_id IS NOT NULL
                GROUP BY device_id
            ) AS latest ON s.id = latest.max_id
            """
        ).fetchall()
    finally:
        conn.close()


def get_samples(db_path: str = DEFAULT_DB, since=None):
    """All samples (optionally since a timestamp), joined to devices for a fallback name."""
    conn = connect(db_path)
    try:
        sql = """
            SELECT s.*, COALESCE(d.name, s.device_name) AS display_name
            FROM samples AS s
            LEFT JOIN devices AS d ON d.id = s.device_id
        """
        params = ()
        if since is not None:
            sql += " WHERE s.created_at >= ?"
            params = (since,)
        sql += " ORDER BY s.created_at ASC, s.id ASC"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def get_alerts(db_path: str = DEFAULT_DB, limit: int = 100):
    """Newest alerts first."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    finally:
        conn.close()


def get_interface_names():
    """Sorted list of interface names known to psutil (empty list on failure)."""
    try:
        import psutil  # local import keeps this module importable without psutil

        return sorted(psutil.net_io_counters(pernic=True).keys())
    except Exception:
        return []