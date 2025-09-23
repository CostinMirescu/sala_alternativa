from __future__ import annotations
import csv
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from flask import current_app

ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"  # ex: 2025-09-23T10:00:00+0200


def _db_path_from_url(db_url: str) -> Path:
    # Acceptăm doar sqlite:///path
    if not db_url.startswith("sqlite:///"):
        raise ValueError("Only sqlite:/// URLs are supported in pilot")
    rel = db_url[len("sqlite///"):]
    root = Path(current_app.root_path).parent  # repo root
    return (root / rel).resolve()


def get_connection() -> sqlite3.Connection:
    db_url = current_app.config.get("DATABASE_URL", "sqlite:///instance/sala.db")
    db_path = _db_path_from_url(db_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS class (
            id TEXT PRIMARY KEY
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS authorized_code (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL REFERENCES class(id) ON DELETE CASCADE,
            code4_hash TEXT NOT NULL,
            UNIQUE(class_id, code4_hash)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL REFERENCES class(id),
            starts_at TEXT NOT NULL, -- ISO with TZ
            ends_at   TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
            class_id TEXT NOT NULL,
            code4_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('neconfirmat','prezent','întârziat')),
            check_in_at TEXT,
            UNIQUE(session_id, code4_hash)
        );
        """
    )

    conn.commit()
    conn.close()


def _hash_code(class_id: str, code4: str, salt: Optional[str] = None) -> str:
    salt = salt or os.getenv("SALT_APP", "")
    data = f"{salt}|{class_id}|{code4}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass
class ImportResult:
    inserted: int
    skipped_duplicates: int


def import_codes(csv_path: Path) -> ImportResult:
    conn = get_connection()
    cur = conn.cursor()
    inserted = 0
    skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"class_id", "code4"}
        if set(reader.fieldnames or []) != required:
            raise ValueError(f"CSV header must be exactly: {sorted(required)}")
        for row in reader:
            class_id = row["class_id"].strip()
            code4 = row["code4"].strip()
            if not class_id:
                raise ValueError("class_id cannot be empty")
            if not (code4.isdigit() and len(code4) == 4):
                raise ValueError(f"Invalid code4 '{code4}' (must be exactly 4 digits)")

            # ensure class exists
            cur.execute("INSERT OR IGNORE INTO class(id) VALUES (?)", (class_id,))

            code_hash = _hash_code(class_id, code4)
            try:
                cur.execute(
                    "INSERT INTO authorized_code(class_id, code4_hash) VALUES (?, ?)",
                    (class_id, code_hash),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1

    conn.commit()
    conn.close()
    return ImportResult(inserted=inserted, skipped_duplicates=skipped)


@dataclass
class SessionSeed:
    id: int
    class_id: str
    starts_at: str
    ends_at: str


def seed_session(class_id: str, starts_at_iso: str, ends_at_iso: str) -> SessionSeed:
    # Normalize ISO (accept both "+02:00" and "+0200")
    def _normalize(ts: str) -> str:
        # Remove colon in TZ for SQLite consistency
        if len(ts) >= 5 and ts[-3] == ":":
            return ts[:-3] + ts[-2:]
        return ts

    starts_norm = _normalize(starts_at_iso)
    ends_norm = _normalize(ends_at_iso)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO class(id) VALUES (?)", (class_id,))
    cur.execute(
        "INSERT INTO session(class_id, starts_at, ends_at) VALUES (?,?,?)",
        (class_id, starts_norm, ends_norm),
    )
    session_id = cur.lastrowid
    conn.commit()
    conn.close()

    return SessionSeed(id=session_id, class_id=class_id, starts_at=starts_norm, ends_at=ends_norm)