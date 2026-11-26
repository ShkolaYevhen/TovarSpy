import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    query TEXT NOT NULL,
    countries_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    total_count INTEGER NOT NULL,
    winners_count INTEGER NOT NULL,
    reports_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    product_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    target_market TEXT NOT NULL,
    opportunity_score REAL NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_channels (
    handle TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL
);
"""


@contextmanager
def _connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    """Create SQLite tables if they do not exist."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)


def normalize_tg_handle(raw: str) -> str:
    """Normalize @handle or t.me/s/handle URL to bare Telegram channel handle."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^https?://(www\.)?t\.me/(s/)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^t\.me/(s/)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.lstrip("@").split("?")[0].strip("/")
    if "/" in cleaned:
        cleaned = cleaned.split("/")[0]
    if not cleaned:
        raise ValueError("Telegram channel handle cannot be empty")
    return cleaned.lower()


def get_cache(db_path: str | Path, key: str, ttl_seconds: int = 21600) -> Any | None:
    """Fetch cached JSON payload if present and within TTL."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json, updated_at FROM cache_entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    if (time.time() - float(row["updated_at"])) > ttl_seconds:
        return None
    return json.loads(row["payload_json"])


def set_cache(db_path: str | Path, key: str, value: Any) -> None:
    """Upsert JSON-serializable payload into SQLite cache."""
    init_db(db_path)
    payload = json.dumps(value, ensure_ascii=False)
    now_ts = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cache_entries (cache_key, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (key, payload, now_ts),
        )


def save_scan(
    db_path: str | Path,
    query: str,
    countries: list[str],
    sources: list[str],
    total_count: int,
    winners_count: int,
    reports: list[dict[str, Any]],
) -> int:
    """Persist a completed scan run and return its row ID."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO scan_history (
                created_at, query, countries_json, sources_json,
                total_count, winners_count, reports_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                query,
                json.dumps(countries),
                json.dumps(sources),
                int(total_count),
                int(winners_count),
                json.dumps(reports, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def list_scans(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent scan history metadata ordered newest first."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, query, countries_json, sources_json, total_count, winners_count
            FROM scan_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "created_at": r["created_at"],
            "query": r["query"],
            "countries": json.loads(r["countries_json"]),
            "sources": json.loads(r["sources_json"]),
            "total_count": int(r["total_count"]),
            "winners_count": int(r["winners_count"]),
        }
        for r in rows
    ]


def get_scan(db_path: str | Path, scan_id: int) -> Optional[dict[str, Any]]:
    """Return full persisted scan record including reports payload."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM scan_history WHERE id = ?",
            (scan_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "query": row["query"],
        "countries": json.loads(row["countries_json"]),
        "sources": json.loads(row["sources_json"]),
        "total_count": int(row["total_count"]),
        "winners_count": int(row["winners_count"]),
        "reports": json.loads(row["reports_json"]),
    }


def upsert_watchlist(
    db_path: str | Path,
    product_key: str,
    title: str,
    target_market: str,
    opportunity_score: float,
    report: dict[str, Any],
) -> None:
    """Insert or update a product in the user's watchlist."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                product_key, title, target_market, opportunity_score, report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_key) DO UPDATE SET
                title = excluded.title,
                target_market = excluded.target_market,
                opportunity_score = excluded.opportunity_score,
                report_json = excluded.report_json
            """,
            (
                product_key,
                title,
                target_market,
                float(opportunity_score),
                json.dumps(report, ensure_ascii=False),
                now_iso,
            ),
        )


def list_watchlist(db_path: str | Path) -> list[dict[str, Any]]:
    """List saved watchlist products sorted by opportunity score descending."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY opportunity_score DESC, created_at DESC"
        ).fetchall()
    return [
        {
            "product_key": r["product_key"],
            "title": r["title"],
            "target_market": r["target_market"],
            "opportunity_score": float(r["opportunity_score"]),
            "report": json.loads(r["report_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def remove_watchlist(db_path: str | Path, product_key: str) -> bool:
    """Delete a product from the watchlist, returning True if removed."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE product_key = ?", (product_key,))
        return cur.rowcount > 0


def seed_default_channels(db_path: str | Path, handles: list[str]) -> None:
    """Seed default Telegram drop channels once without overwriting user edits."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS cnt FROM tg_channels").fetchone()
        if count_row and int(count_row["cnt"]) > 0:
            return
        for raw in handles:
            try:
                norm = normalize_tg_handle(raw)
            except ValueError:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO tg_channels (handle, label, enabled, added_at)
                VALUES (?, ?, 1, ?)
                """,
                (norm, norm, now_iso),
            )


def list_tg_channels(db_path: str | Path, enabled_only: bool = False) -> list[dict[str, Any]]:
    """List configured Ukrainian Telegram drop channels."""
    init_db(db_path)
    sql = "SELECT * FROM tg_channels"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY handle ASC"
    with _connect(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {
            "handle": r["handle"],
            "label": r["label"],
            "enabled": bool(r["enabled"]),
            "added_at": r["added_at"],
        }
        for r in rows
    ]


def upsert_tg_channel(
    db_path: str | Path,
    handle: str,
    label: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """Add or update a Telegram drop channel."""
    init_db(db_path)
    norm = normalize_tg_handle(handle)
    clean_label = label.strip() or norm
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tg_channels (handle, label, enabled, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(handle) DO UPDATE SET
                label = excluded.label,
                enabled = excluded.enabled
            """,
            (norm, clean_label, 1 if enabled else 0, now_iso),
        )
    return {
        "handle": norm,
        "label": clean_label,
        "enabled": bool(enabled),
        "added_at": now_iso,
    }


def delete_tg_channel(db_path: str | Path, handle: str) -> bool:
    """Remove a Telegram channel by handle or URL."""
    init_db(db_path)
    norm = normalize_tg_handle(handle)
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM tg_channels WHERE handle = ?", (norm,))
        return cur.rowcount > 0
