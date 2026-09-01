"""Authentication and user session management for AlgoPaca."""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parent.parent / "data"
AUTH_DB_PATH = DB_DIR / "auth.db"
SECRET_KEY_PATH = DB_DIR / ".secret_key"

USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
# Upper bound accepted at sign-in. Deliberately looser than MAX_PASSWORD_LENGTH
# so nobody is locked out, while still capping PBKDF2 work per request.
MAX_LOGIN_PASSWORD_LENGTH = 1024
HASH_ITERATIONS = 100_000


def _get_master_key() -> bytes:
    """Retrieve or generate the master key for credential encryption."""
    env_key = os.getenv("ALGOPACA_SECRET_KEY", "").strip()
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    DB_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.is_file():
        try:
            return bytes.fromhex(SECRET_KEY_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            pass

    new_key = secrets.token_bytes(32)
    try:
        SECRET_KEY_PATH.write_text(new_key.hex(), encoding="utf-8")
        SECRET_KEY_PATH.chmod(0o600)
    except Exception:
        pass
    return new_key


def _encrypt_val(plaintext: str) -> str:
    """Encrypt a secret string using AES-CTR-like keystream authenticated with HMAC-SHA256."""
    if not plaintext:
        return ""
    master_key = _get_master_key()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    
    # Generate keystream from master_key and nonce
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(data):
        block = hashlib.sha256(master_key + nonce + counter.to_bytes(4, "big")).digest()
        keystream.extend(block)
        counter += 1
    
    ciphertext = bytes(b ^ k for b, k in zip(data, keystream[:len(data)]))
    tag = hmac.new(master_key, nonce + ciphertext, hashlib.sha256).digest()
    
    # Format: nonce(16) + tag(32) + ciphertext
    payload = nonce + tag + ciphertext
    return payload.hex()


def _decrypt_val(ciphertext_hex: str) -> str:
    """Decrypt and verify an encrypted secret string."""
    if not ciphertext_hex:
        return ""
    try:
        payload = bytes.fromhex(ciphertext_hex)
        if len(payload) < 48:  # 16 nonce + 32 tag
            return ""
        nonce = payload[:16]
        tag = payload[16:48]
        ciphertext = payload[48:]
        
        master_key = _get_master_key()
        expected_tag = hmac.new(master_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            return ""
            
        keystream = bytearray()
        counter = 0
        while len(keystream) < len(ciphertext):
            block = hashlib.sha256(master_key + nonce + counter.to_bytes(4, "big")).digest()
            keystream.extend(block)
            counter += 1
            
        data = bytes(b ^ k for b, k in zip(ciphertext, keystream[:len(ciphertext)]))
        return data.decode("utf-8")
    except Exception:
        return ""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _get_connection(db_path: Path = AUTH_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class AuthStore:
    def __init__(self, db_path: Path = AUTH_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with _get_connection(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    display_name TEXT,
                    role TEXT DEFAULT 'trader',
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    user_agent TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_credentials (
                    user_id INTEGER PRIMARY KEY,
                    alpaca_paper_api_key TEXT,
                    alpaca_paper_secret_key TEXT,
                    alpaca_live_api_key TEXT,
                    alpaca_live_secret_key TEXT,
                    openai_api_key TEXT,
                    gemini_api_key TEXT,
                    anthropic_api_key TEXT,
                    xai_api_key TEXT,
                    trading_mode TEXT DEFAULT 'paper',
                    allow_live INTEGER DEFAULT 0,
                    live_authorized INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    theme TEXT DEFAULT 'obsidian',
                    language TEXT DEFAULT 'en',
                    default_page TEXT DEFAULT 'auto-trade',
                    sound_alerts INTEGER DEFAULT 1,
                    confirm_orders INTEGER DEFAULT 1,
                    confirm_close_all INTEGER DEFAULT 1,
                    chart_refresh_interval INTEGER DEFAULT 20,
                    compact_mode INTEGER DEFAULT 0,
                    timezone_display TEXT DEFAULT 'local',
                    default_size_mode TEXT DEFAULT 'qty',
                    default_trade_qty REAL DEFAULT 1.0,
                    default_trade_notional REAL DEFAULT 100.0,
                    require_approval INTEGER DEFAULT 0,
                    notify_browser INTEGER DEFAULT 1,
                    notify_email INTEGER DEFAULT 0,
                    notification_email TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_id INTEGER,
                    actor_username TEXT,
                    action TEXT NOT NULL,
                    target_user_id INTEGER,
                    target_username TEXT,
                    detail TEXT
                );

                CREATE TABLE IF NOT EXISTS email_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_reset_tokens_user_id ON password_reset_tokens(user_id);
                CREATE INDEX IF NOT EXISTS idx_reset_tokens_expires_at ON password_reset_tokens(expires_at);
                CREATE INDEX IF NOT EXISTS idx_audit_created_at ON admin_audit_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_user_id);
                CREATE INDEX IF NOT EXISTS idx_email_log_created_at ON email_log(created_at);
                """
            )

            # `status` arrived after the first releases, so existing databases need
            # the column added in place rather than through CREATE TABLE.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if "status" not in existing:
                conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

            existing_creds = {r["name"] for r in conn.execute("PRAGMA table_info(user_credentials)")}
            if "anthropic_api_key" not in existing_creds:
                conn.execute("ALTER TABLE user_credentials ADD COLUMN anthropic_api_key TEXT")
            if "xai_api_key" not in existing_creds:
                conn.execute("ALTER TABLE user_credentials ADD COLUMN xai_api_key TEXT")

            existing_prefs = {r["name"] for r in conn.execute("PRAGMA table_info(user_preferences)")}
            if "require_approval" not in existing_prefs:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN require_approval INTEGER DEFAULT 0")
            if "notify_browser" not in existing_prefs:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN notify_browser INTEGER DEFAULT 1")
            if "notify_email" not in existing_prefs:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN notify_email INTEGER DEFAULT 0")
            if "notification_email" not in existing_prefs:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN notification_email TEXT DEFAULT ''")

            conn.commit()
        # Seed demo user and check for owner accounts needing env credentials
        demo_user = self.get_or_create_demo_user()
        try:
            with _get_connection(self.db_path) as conn:
                owners = conn.execute("SELECT id FROM users WHERE role = 'owner'").fetchall()
                for row in owners:
                    self.seed_env_credentials_if_empty(row["id"])
        except Exception:
            pass

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            HASH_ITERATIONS,
        ).hex()

    def register_user(
        self,
        username: str,
        email: str,
        password: str,
        display_name: Optional[str] = None,
        role: str = "trader",
    ) -> dict[str, Any]:
        username = (username or "").strip()
        email = (email or "").strip().lower()

        if not username:
            raise ValueError("Username is required")
        if not USERNAME_REGEX.match(username):
            raise ValueError("Username must be 3-30 characters (letters, numbers, underscores, dots, hyphens)")
        if not email:
            raise ValueError("Email address is required")
        if not EMAIL_REGEX.match(email):
            raise ValueError("Please provide a valid email address")
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"Password must be {MAX_PASSWORD_LENGTH} characters or fewer")

        display_name = (display_name or username).strip()
        salt = secrets.token_hex(16)
        password_hash = self._hash_password(password, salt)
        now = _now_iso()

        # Auto-promote first registered non-demo user to owner if no owners exist
        owner_env = os.getenv("OWNER_EMAIL", "").strip().lower()
        admin_env = os.getenv("ADMIN_EMAIL", "").strip().lower()
        if owner_env and email == owner_env:
            role = "owner"
        elif admin_env and email == admin_env:
            role = "admin"
        elif role == "trader" and username != "demo":
            with _get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) as count FROM users WHERE role = 'owner'")
                row = cursor.fetchone()
                if not row or row["count"] == 0:
                    role = "owner"

        try:
            with _get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO users (username, email, password_hash, salt, display_name, role, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, email, password_hash, salt, display_name, role, now),
                )
                user_id = cursor.lastrowid
                conn.commit()

            return {
                "id": user_id,
                "username": username,
                "email": email,
                "display_name": display_name,
                "role": role,
                "created_at": now,
            }
        except sqlite3.IntegrityError as exc:
            err_msg = str(exc).lower()
            if "users.username" in err_msg or "unique constraint failed: users.username" in err_msg:
                raise ValueError("An account with this username already exists") from exc
            if "users.email" in err_msg or "unique constraint failed: users.email" in err_msg:
                raise ValueError("An account with this email address already exists") from exc
            raise ValueError("Username or email already in use") from exc

    def has_owner(self) -> bool:
        """Check if at least one owner account exists."""
        try:
            with _get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM users WHERE role = 'owner' LIMIT 1")
                return cursor.fetchone() is not None
        except sqlite3.OperationalError:
            self._init_db()
            with _get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM users WHERE role = 'owner' LIMIT 1")
                return cursor.fetchone() is not None

    def needs_setup(self) -> bool:
        """Check if the desk is in a fresh/unconfigured state requiring initial setup."""
        return not self.has_owner()

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        """Retrieve user profile by ID without password hash."""
        now_str = _now_iso()
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    u.id, u.username, u.email, u.display_name, u.role, u.created_at, u.last_login_at, u.status,
                    COUNT(DISTINCT s.token) as active_sessions,
                    c.trading_mode, c.allow_live, c.live_authorized,
                    (CASE WHEN c.user_id IS NULL THEN 0 ELSE 1 END) as has_credentials,
                    (CASE WHEN c.alpaca_paper_api_key IS NOT NULL AND c.alpaca_paper_api_key != '' THEN 1 ELSE 0 END) as has_paper_key,
                    (CASE WHEN c.alpaca_live_api_key IS NOT NULL AND c.alpaca_live_api_key != '' THEN 1 ELSE 0 END) as has_live_key,
                    (CASE WHEN c.openai_api_key IS NOT NULL AND c.openai_api_key != '' THEN 1 ELSE 0 END) as has_openai_key,
                    (CASE WHEN c.gemini_api_key IS NOT NULL AND c.gemini_api_key != '' THEN 1 ELSE 0 END) as has_gemini_key,
                    (CASE WHEN c.anthropic_api_key IS NOT NULL AND c.anthropic_api_key != '' THEN 1 ELSE 0 END) as has_anthropic_key,
                    (CASE WHEN c.xai_api_key IS NOT NULL AND c.xai_api_key != '' THEN 1 ELSE 0 END) as has_xai_key
                FROM users u
                LEFT JOIN sessions s ON u.id = s.user_id AND s.expires_at >= ?
                LEFT JOIN user_credentials c ON u.id = c.user_id
                WHERE u.id = ?
                GROUP BY u.id
                """,
                (now_str, user_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"] or row["username"],
                "role": row["role"] or "trader",
                "status": row["status"] or "active",
                "created_at": row["created_at"],
                "last_login_at": row["last_login_at"],
                "active_sessions": row["active_sessions"],
                "has_credentials": bool(row["has_credentials"]),
                "trading_mode": row["trading_mode"] or "paper",
                "allow_live": bool(row["allow_live"]),
                "live_authorized": bool(row["live_authorized"]),
                "has_paper_key": bool(row["has_paper_key"]),
                "has_live_key": bool(row["has_live_key"]),
                "has_openai_key": bool(row["has_openai_key"]),
                "has_gemini_key": bool(row["has_gemini_key"]),
                "has_anthropic_key": bool(row["has_anthropic_key"]),
                "has_xai_key": bool(row["has_xai_key"]),
            }

    # Whitelist of sortable columns — the sort key arrives from the query string
    # and is interpolated into SQL, so it must never come straight from the client.
    SORT_COLUMNS = {
        "created_at": "u.created_at",
        "last_login_at": "u.last_login_at",
        "username": "LOWER(u.username)",
        "email": "LOWER(u.email)",
        "role": "LOWER(u.role)",
        "active_sessions": "active_sessions",
    }

    def list_users(
        self,
        search: str = "",
        role: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        sort: str = "created_at",
        direction: str = "desc",
    ) -> dict[str, Any]:
        """List users with active sessions count and credential configuration summary."""
        now_str = _now_iso()
        search = (search or "").strip().lower()
        role = (role or "").strip().lower()
        status = (status or "").strip().lower()
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        sort_key = (sort or "").strip()
        if sort_key not in self.SORT_COLUMNS:
            sort_key = "created_at"
        sort_sql = self.SORT_COLUMNS[sort_key]
        dir_sql = "ASC" if (direction or "").strip().lower() == "asc" else "DESC"

        where_clauses: list[str] = []
        params: list[Any] = []

        if search:
            where_clauses.append("(LOWER(u.username) LIKE ? OR LOWER(u.email) LIKE ? OR LOWER(u.display_name) LIKE ?)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        if role and role != "all":
            where_clauses.append("LOWER(u.role) = ?")
            params.append(role)

        if status and status != "all":
            where_clauses.append("LOWER(COALESCE(u.status, 'active')) = ?")
            params.append(status)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()

            # Total count for pagination
            count_sql = f"SELECT COUNT(*) as total FROM users u {where_sql}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()["total"]

            query_sql = f"""
                SELECT
                    u.id, u.username, u.email, u.display_name, u.role, u.created_at, u.last_login_at, u.status,
                    COUNT(DISTINCT s.token) as active_sessions,
                    c.trading_mode, c.allow_live, c.live_authorized,
                    (CASE WHEN c.user_id IS NULL THEN 0 ELSE 1 END) as has_credentials,
                    (CASE WHEN c.alpaca_paper_api_key IS NOT NULL AND c.alpaca_paper_api_key != '' THEN 1 ELSE 0 END) as has_paper_key,
                    (CASE WHEN c.alpaca_live_api_key IS NOT NULL AND c.alpaca_live_api_key != '' THEN 1 ELSE 0 END) as has_live_key,
                    (CASE WHEN c.openai_api_key IS NOT NULL AND c.openai_api_key != '' THEN 1 ELSE 0 END) as has_openai_key,
                    (CASE WHEN c.gemini_api_key IS NOT NULL AND c.gemini_api_key != '' THEN 1 ELSE 0 END) as has_gemini_key,
                    (CASE WHEN c.anthropic_api_key IS NOT NULL AND c.anthropic_api_key != '' THEN 1 ELSE 0 END) as has_anthropic_key,
                    (CASE WHEN c.xai_api_key IS NOT NULL AND c.xai_api_key != '' THEN 1 ELSE 0 END) as has_xai_key
                FROM users u
                LEFT JOIN sessions s ON u.id = s.user_id AND s.expires_at >= ?
                LEFT JOIN user_credentials c ON u.id = c.user_id
                {where_sql}
                GROUP BY u.id
                ORDER BY {sort_sql} {dir_sql}, u.id DESC
                LIMIT ? OFFSET ?
            """
            all_params = [now_str] + params + [limit, offset]
            cursor.execute(query_sql, all_params)
            rows = cursor.fetchall()

            users_list = []
            for r in rows:
                users_list.append({
                    "id": r["id"],
                    "username": r["username"],
                    "email": r["email"],
                    "display_name": r["display_name"] or r["username"],
                    "role": r["role"] or "trader",
                    "status": r["status"] or "active",
                    "created_at": r["created_at"],
                    "last_login_at": r["last_login_at"],
                    "active_sessions": r["active_sessions"],
                    "has_credentials": bool(r["has_credentials"]),
                    "trading_mode": r["trading_mode"] or "paper",
                    "allow_live": bool(r["allow_live"]),
                    "live_authorized": bool(r["live_authorized"]),
                    "has_paper_key": bool(r["has_paper_key"]),
                    "has_live_key": bool(r["has_live_key"]),
                    "has_openai_key": bool(r["has_openai_key"]),
                    "has_gemini_key": bool(r["has_gemini_key"]),
                    "has_anthropic_key": bool(r["has_anthropic_key"]),
                    "has_xai_key": bool(r["has_xai_key"]),
                })

            return {
                "users": users_list,
                "total": total,
                "limit": limit,
                "offset": offset,
                # Echo the resolved key, never the raw input — the client uses
                # this to paint its sort indicators.
                "sort": sort_key,
                "direction": dir_sql.lower(),
            }

    def get_user_analytics(self) -> dict[str, Any]:
        """Aggregate high-level user statistics, trading modes, and activity timeline."""
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        day_ago_str = (now - timedelta(days=1)).isoformat()
        week_ago_str = (now - timedelta(days=7)).isoformat()
        month_ago_str = (now - timedelta(days=30)).isoformat()

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()

            # Total and role counts
            cursor.execute(
                """
                SELECT 
                    COUNT(*) as total_users,
                    SUM(CASE WHEN LOWER(role) = 'owner' THEN 1 ELSE 0 END) as total_owners,
                    SUM(CASE WHEN LOWER(role) = 'admin' THEN 1 ELSE 0 END) as total_admins,
                    SUM(CASE WHEN LOWER(role) NOT IN ('owner', 'admin') THEN 1 ELSE 0 END) as total_traders,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) as signups_24h,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) as signups_7d,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) as signups_30d,
                    SUM(CASE WHEN last_login_at >= ? THEN 1 ELSE 0 END) as active_users_24h,
                    SUM(CASE WHEN last_login_at >= ? THEN 1 ELSE 0 END) as active_users_7d,
                    SUM(CASE WHEN LOWER(COALESCE(status, 'active')) = 'suspended' THEN 1 ELSE 0 END) as suspended_users
                FROM users
                """,
                (day_ago_str, week_ago_str, month_ago_str, day_ago_str, week_ago_str),
            )
            overview = dict(cursor.fetchone() or {})

            # Active sessions count and unique users online
            cursor.execute(
                """
                SELECT 
                    COUNT(*) as total_active_sessions,
                    COUNT(DISTINCT user_id) as online_users_count
                FROM sessions
                WHERE expires_at >= ?
                """,
                (now_str,),
            )
            session_stats = dict(cursor.fetchone() or {})

            # Credential and Trading Mode breakdown
            cursor.execute(
                """
                SELECT
                    SUM(CASE WHEN trading_mode = 'live' THEN 1 ELSE 0 END) as live_mode_count,
                    SUM(CASE WHEN trading_mode != 'live' OR trading_mode IS NULL THEN 1 ELSE 0 END) as paper_mode_count,
                    SUM(CASE WHEN alpaca_paper_api_key IS NOT NULL AND alpaca_paper_api_key != '' THEN 1 ELSE 0 END) as paper_keys_count,
                    SUM(CASE WHEN alpaca_live_api_key IS NOT NULL AND alpaca_live_api_key != '' THEN 1 ELSE 0 END) as live_keys_count,
                    SUM(CASE WHEN openai_api_key IS NOT NULL AND openai_api_key != '' THEN 1 ELSE 0 END) as openai_keys_count,
                    SUM(CASE WHEN gemini_api_key IS NOT NULL AND gemini_api_key != '' THEN 1 ELSE 0 END) as gemini_keys_count,
                    SUM(CASE WHEN anthropic_api_key IS NOT NULL AND anthropic_api_key != '' THEN 1 ELSE 0 END) as anthropic_keys_count,
                    SUM(CASE WHEN xai_api_key IS NOT NULL AND xai_api_key != '' THEN 1 ELSE 0 END) as xai_keys_count
                FROM user_credentials
                """
            )
            cred_stats = dict(cursor.fetchone() or {})

            # Daily signups for the last 14 days
            daily_signups: list[dict[str, Any]] = []
            for day_idx in range(13, -1, -1):
                start_dt = (now - timedelta(days=day_idx)).replace(hour=0, minute=0, second=0, microsecond=0)
                end_dt = start_dt + timedelta(days=1)
                cursor.execute(
                    "SELECT COUNT(*) as count FROM users WHERE created_at >= ? AND created_at < ?",
                    (start_dt.isoformat(), end_dt.isoformat()),
                )
                cnt = cursor.fetchone()["count"]
                daily_signups.append({
                    "date": start_dt.strftime("%Y-%m-%d"),
                    "label": start_dt.strftime("%b %d"),
                    "count": cnt,
                })

            total_users = overview.get("total_users") or 0
            paper_mode = cred_stats.get("paper_mode_count") or 0
            live_mode = cred_stats.get("live_mode_count") or 0
            # Users with no user_credentials row at all belong to neither bucket.
            # Without this third segment the card silently drops them and its
            # percentages are computed against a different total than the
            # integrations bars beside it.
            unconfigured = max(0, total_users - paper_mode - live_mode)

            return {
                "overview": {
                    "total_users": total_users,
                    "suspended_users": overview.get("suspended_users") or 0,
                    "total_owners": overview.get("total_owners") or 0,
                    "total_admins": overview.get("total_admins") or 0,
                    "total_traders": overview.get("total_traders") or 0,
                    "signups_24h": overview.get("signups_24h") or 0,
                    "signups_7d": overview.get("signups_7d") or 0,
                    "signups_30d": overview.get("signups_30d") or 0,
                    "active_users_24h": overview.get("active_users_24h") or 0,
                    "active_users_7d": overview.get("active_users_7d") or 0,
                    "active_sessions": session_stats.get("total_active_sessions") or 0,
                    "online_users": session_stats.get("online_users_count") or 0,
                },
                "trading_modes": {
                    "paper": paper_mode,
                    "live": live_mode,
                    "unconfigured": unconfigured,
                    # Every segment above is a share of this, so the card and the
                    # integration bars beside it now divide by the same number.
                    "total": total_users,
                },
                "integrations": {
                    "paper_keys": cred_stats.get("paper_keys_count") or 0,
                    "live_keys": cred_stats.get("live_keys_count") or 0,
                    "openai": cred_stats.get("openai_keys_count") or 0,
                    "gemini": cred_stats.get("gemini_keys_count") or 0,
                    "anthropic": cred_stats.get("anthropic_keys_count") or 0,
                    "xai": cred_stats.get("xai_keys_count") or 0,
                },
                "daily_signups": daily_signups,
            }

    def update_user_role(self, user_id: int, new_role: str, acting_user: dict[str, Any]) -> dict[str, Any]:
        """Update user role with strict permission validation."""
        valid_roles = {"owner", "admin", "trader"}
        new_role = (new_role or "").strip().lower()
        if new_role not in valid_roles:
            raise ValueError(f"Invalid role '{new_role}'. Allowed roles: {', '.join(sorted(valid_roles))}")

        acting_role = (acting_user.get("role") or "").lower()
        if acting_role not in {"owner", "admin"}:
            raise ValueError("Only administrators or owners can change user roles.")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, role FROM users WHERE id = ?", (user_id,))
            target = cursor.fetchone()
            if not target:
                raise ValueError("User not found.")

            target_role = (target["role"] or "trader").lower()

            # Permissions hierarchy
            if target_role == "owner" and acting_role != "owner":
                raise ValueError("Only an Owner can modify another Owner's account.")

            if new_role == "owner" and acting_role != "owner":
                raise ValueError("Only an existing Owner can grant the Owner role.")

            # Safety check: do not demote the last owner
            if target_role == "owner" and new_role != "owner":
                cursor.execute("SELECT COUNT(*) as owner_count FROM users WHERE role = 'owner'")
                owner_cnt = cursor.fetchone()["owner_count"]
                if owner_cnt <= 1:
                    raise ValueError("Cannot demote the only remaining Owner of the system.")

            cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
            conn.commit()

            return {
                "id": target["id"],
                "username": target["username"],
                "email": target["email"],
                "role": new_role,
            }

    def admin_terminate_user_sessions(self, user_id: int) -> int:
        """Revoke all active sessions for a user."""
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount

    def admin_delete_user(self, user_id: int, acting_user: dict[str, Any]) -> bool:
        """Delete user account and all associated data."""
        acting_id = acting_user.get("id")
        acting_role = (acting_user.get("role") or "").lower()

        if acting_id == user_id:
            raise ValueError("You cannot delete your own account.")

        if acting_role not in {"owner", "admin"}:
            raise ValueError("Permission denied.")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, role FROM users WHERE id = ?", (user_id,))
            target = cursor.fetchone()
            if not target:
                raise ValueError("User not found.")

            target_role = (target["role"] or "trader").lower()
            if target_role == "owner" and acting_role != "owner":
                raise ValueError("Only an Owner can delete another Owner account.")

            if target_role == "owner":
                cursor.execute("SELECT COUNT(*) as owner_count FROM users WHERE role = 'owner'")
                if cursor.fetchone()["owner_count"] <= 1:
                    raise ValueError("Cannot delete the only remaining Owner of the system.")

            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return True

    def admin_purge_expired_data(self) -> dict[str, int]:
        """Purge expired sessions and expired password reset tokens."""
        now_str = _now_iso()
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str,))
            purged_sessions = cursor.rowcount

            cursor.execute("DELETE FROM password_reset_tokens WHERE expires_at < ? OR used = 1", (now_str,))
            purged_tokens = cursor.rowcount
            conn.commit()

            return {
                "purged_sessions": purged_sessions,
                "purged_tokens": purged_tokens,
            }

    def admin_vacuum_db(self) -> dict[str, Any]:
        """Execute VACUUM and integrity check on SQLite database."""
        with _get_connection(self.db_path) as conn:
            conn.execute("VACUUM")
            cursor = conn.execute("PRAGMA integrity_check")
            integrity = cursor.fetchone()[0]

        file_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "integrity": integrity,
            "file_size_bytes": file_size,
            "file_size_mb": round(file_size / (1024 * 1024), 2),
        }

    def get_db_stats(self) -> dict[str, Any]:
        """Cheap database telemetry for the stats endpoint.

        Deliberately does no VACUUM and no integrity check — both take an
        exclusive write lock and cost O(database size), which a read endpoint
        polled on every tab switch must never pay. The System tab's explicit
        "Run Vacuum" action is where that work belongs.
        """
        file_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "db_path": str(self.db_path),
            "file_size_bytes": file_size,
            "file_size_mb": round(file_size / (1024 * 1024), 2),
        }

    # ------------------------------------------------------------------
    # Account status (suspend / reinstate)
    # ------------------------------------------------------------------

    def admin_set_user_status(
        self, user_id: int, new_status: str, acting_user: dict[str, Any]
    ) -> dict[str, Any]:
        """Suspend or reinstate an account, mirroring the role permission ladder."""
        new_status = (new_status or "").strip().lower()
        if new_status not in {"active", "suspended"}:
            raise ValueError("Status must be either 'active' or 'suspended'.")

        acting_id = acting_user.get("id")
        acting_role = (acting_user.get("role") or "").lower()
        if acting_role not in {"owner", "admin"}:
            raise ValueError("Permission denied.")
        if acting_id == user_id and new_status == "suspended":
            raise ValueError("You cannot suspend your own account.")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, email, role, status FROM users WHERE id = ?", (user_id,)
            )
            target = cursor.fetchone()
            if not target:
                raise ValueError("User not found.")

            target_role = (target["role"] or "trader").lower()
            if target_role == "owner" and acting_role != "owner":
                raise ValueError("Only an Owner can suspend another Owner's account.")

            if target_role == "owner" and new_status == "suspended":
                cursor.execute("SELECT COUNT(*) as owner_count FROM users WHERE role = 'owner' AND COALESCE(status, 'active') = 'active'")
                if cursor.fetchone()["owner_count"] <= 1:
                    raise ValueError("Cannot suspend the only remaining active Owner.")

            cursor.execute("UPDATE users SET status = ? WHERE id = ?", (new_status, user_id))
            # A suspended account keeps no foothold — drop its live sessions too.
            revoked = 0
            if new_status == "suspended":
                cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                revoked = cursor.rowcount
            conn.commit()

            return {
                "id": target["id"],
                "username": target["username"],
                "email": target["email"],
                "status": new_status,
                "revoked_sessions": revoked,
            }

    def admin_create_user(
        self,
        username: str,
        email: str,
        role: str,
        acting_user: dict[str, Any],
        display_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Provision an account on an admin's behalf with an unusable password.

        The invitee sets their own password through the reset-token flow, so no
        secret ever needs to travel between the admin and the new trader.
        """
        role = (role or "trader").strip().lower()
        if role not in {"owner", "admin", "trader"}:
            raise ValueError("Invalid role.")

        acting_role = (acting_user.get("role") or "").lower()
        if acting_role not in {"owner", "admin"}:
            raise ValueError("Permission denied.")
        if role == "owner" and acting_role != "owner":
            raise ValueError("Only an existing Owner can grant the Owner role.")

        # register_user auto-promotes the first account to owner; pin the role
        # afterwards so an invite always lands on exactly the role requested.
        created = self.register_user(
            username=username,
            email=email,
            password=secrets.token_urlsafe(32),
            display_name=display_name or username,
            role=role,
        )
        if created["role"] != role:
            with _get_connection(self.db_path) as conn:
                conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, created["id"]))
                conn.commit()
            created["role"] = role
        return created

    # ------------------------------------------------------------------
    # Session inspection
    # ------------------------------------------------------------------

    def list_user_sessions(self, user_id: int) -> list[dict[str, Any]]:
        """Active sessions for one user, newest first."""
        now_str = _now_iso()
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT token, created_at, expires_at, user_agent
                FROM sessions
                WHERE user_id = ? AND expires_at >= ?
                ORDER BY created_at DESC
                """,
                (user_id, now_str),
            )
            return [
                {
                    # Only a short prefix leaves the server — enough to tell two
                    # sessions apart in the UI, useless as a stolen credential.
                    "id": row["token"][:12],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "user_agent": row["user_agent"] or "",
                }
                for row in cursor.fetchall()
            ]

    def admin_revoke_session(self, user_id: int, session_id: str) -> bool:
        """Revoke a single session by the short id handed out by list_user_sessions."""
        session_id = (session_id or "").strip()
        if not session_id:
            return False
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token LIKE ? || '%'",
                (user_id, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def record_audit(
        self,
        actor: dict[str, Any],
        action: str,
        target: Optional[dict[str, Any]] = None,
        detail: str = "",
    ) -> None:
        """Append one line to the admin audit trail.

        Never raises: an audit write must not be able to fail the operation it
        is describing, which has already been committed by the time we get here.
        """
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO admin_audit_log
                        (created_at, actor_id, actor_username, action, target_user_id, target_username, detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now_iso(),
                        (actor or {}).get("id"),
                        (actor or {}).get("username") or "",
                        action,
                        (target or {}).get("id"),
                        (target or {}).get("username") or "",
                        detail or "",
                    ),
                )
                conn.commit()
        except Exception:
            log.exception("Failed to write admin audit entry for action %s", action)

    def list_audit_log(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Paginated admin audit trail, newest first."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            total = cursor.execute("SELECT COUNT(*) as total FROM admin_audit_log").fetchone()["total"]
            cursor.execute(
                """
                SELECT id, created_at, actor_id, actor_username, action,
                       target_user_id, target_username, detail
                FROM admin_audit_log
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            return {
                "entries": [dict(r) for r in cursor.fetchall()],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    # ------------------------------------------------------------------
    # Email delivery log
    # ------------------------------------------------------------------

    def record_email(
        self, recipient: str, kind: str, success: bool, error: str = ""
    ) -> None:
        """Record one outbound email attempt. Never raises."""
        try:
            with _get_connection(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO email_log (created_at, recipient, kind, success, error) VALUES (?, ?, ?, ?, ?)",
                    (_now_iso(), recipient or "", kind or "unknown", 1 if success else 0, error or ""),
                )
                conn.commit()
        except Exception:
            log.exception("Failed to write email log entry for %s", kind)

    def list_email_log(self, limit: int = 25) -> list[dict[str, Any]]:
        """Most recent outbound email attempts, newest first."""
        limit = max(1, min(limit, 200))
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, created_at, recipient, kind, success, error FROM email_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "recipient": r["recipient"],
                    "kind": r["kind"],
                    "success": bool(r["success"]),
                    "error": r["error"] or "",
                }
                for r in cursor.fetchall()
            ]

    def authenticate_user(self, identifier: str, password: str) -> dict[str, Any]:
        identifier = (identifier or "").strip()
        if not identifier:
            raise ValueError("Username or email is required")
        if not password:
            raise ValueError("Password is required")
        if len(password) > MAX_LOGIN_PASSWORD_LENGTH:
            # Never hash an unbounded string — that is free CPU for an attacker.
            raise ValueError("Invalid username/email or password")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username, email, password_hash, salt, display_name, role, created_at, status
                FROM users
                WHERE username = ? OR email = ?
                """,
                (identifier, identifier.lower()),
            )
            row = cursor.fetchone()
            if not row:
                # Spend the same work as a real check so response time does not
                # reveal whether the account exists.
                self._hash_password(password, secrets.token_hex(16))
                raise ValueError("Invalid username/email or password")

            user = dict(row)
            expected_hash = user["password_hash"]
            computed_hash = self._hash_password(password, user["salt"])

            if not hmac.compare_digest(expected_hash, computed_hash):
                raise ValueError("Invalid username/email or password")

            # Check the suspension only after the password verifies, so a wrong
            # password and a suspended account are indistinguishable to a guesser.
            if (user.get("status") or "active").lower() == "suspended":
                raise ValueError("This account has been suspended. Please contact an administrator.")

            now = _now_iso()
            cursor.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now, user["id"]),
            )
            conn.commit()

            return {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "display_name": user["display_name"] or user["username"],
                "role": user["role"],
                "created_at": user["created_at"],
                "last_login_at": now,
            }

    def create_session(
        self,
        user_id: int,
        remember_me: bool = False,
        user_agent: Optional[str] = None,
    ) -> tuple[str, dt.datetime]:
        token = secrets.token_hex(32)
        days = 30 if remember_me else 7
        now_dt = dt.datetime.now(dt.timezone.utc)
        expires_dt = now_dt + dt.timedelta(days=days)
        now_str = now_dt.isoformat()
        expires_str = expires_dt.isoformat()

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token, user_id, now_str, expires_str, user_agent or ""),
            )
            conn.commit()

        return token, expires_dt

    def get_user_by_session(self, token: Optional[str]) -> Optional[dict[str, Any]]:
        if not token or not isinstance(token, str):
            return None

        now_str = _now_iso()
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            # Clean up expired sessions periodically
            cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str,))
            conn.commit()

            cursor.execute(
                """
                SELECT u.id, u.username, u.email, u.display_name, u.role, u.created_at,
                       u.last_login_at, u.status, s.expires_at
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND s.expires_at >= ?
                """,
                (token, now_str),
            )
            row = cursor.fetchone()
            if not row:
                return None
            user = dict(row)
            # Suspension takes effect on the next request even if a session
            # somehow survived the revoke that accompanies it.
            if (user.get("status") or "active").lower() == "suspended":
                return None
            return user

    def delete_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return cursor.rowcount > 0

    def seed_env_credentials_if_empty(self, user_id: int) -> bool:
        """Seed credentials from environment variables if the user has none saved."""
        current = self.get_user_credentials(user_id)
        has_any_key = any(
            bool(current.get(k))
            for k in [
                "alpaca_paper_api_key",
                "alpaca_live_api_key",
                "openai_api_key",
                "gemini_api_key",
                "anthropic_api_key",
                "xai_api_key",
            ]
        )
        if has_any_key:
            return False

        paper_key = (
            os.getenv("ALPACA_PAPER_API_KEY", "").strip()
            or os.getenv("ALPACA_API_KEY", "").strip()
        )
        paper_secret = (
            os.getenv("ALPACA_PAPER_SECRET_KEY", "").strip()
            or os.getenv("ALPACA_SECRET_KEY", "").strip()
        )
        live_key = os.getenv("ALPACA_LIVE_API_KEY", "").strip()
        live_secret = os.getenv("ALPACA_LIVE_SECRET_KEY", "").strip()
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        xai_key = os.getenv("XAI_API_KEY", "").strip()

        updates = {}
        if paper_key:
            updates["alpaca_paper_api_key"] = paper_key
        if paper_secret:
            updates["alpaca_paper_secret_key"] = paper_secret
        if live_key:
            updates["alpaca_live_api_key"] = live_key
        if live_secret:
            updates["alpaca_live_secret_key"] = live_secret
        if openai_key:
            updates["openai_api_key"] = openai_key
        if gemini_key:
            updates["gemini_api_key"] = gemini_key
        if anthropic_key:
            updates["anthropic_api_key"] = anthropic_key
        if xai_key:
            updates["xai_api_key"] = xai_key

        if updates:
            self.save_user_credentials(user_id, updates)
            return True
        return False

    def get_or_create_demo_user(self) -> dict[str, Any]:
        demo = None
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, display_name, role, created_at FROM users WHERE username = 'demo'")
            row = cursor.fetchone()
            if row:
                demo = dict(row)

        if not demo:
            # Create demo account
            try:
                demo = self.register_user(
                    username="demo",
                    email="demo@algopaca.local",
                    password="AlgoPaca2026!",
                    display_name="Demo Trader",
                    role="trader",
                )
            except ValueError:
                with _get_connection(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, username, email, display_name, role, created_at FROM users WHERE username = 'demo'")
                    row = cursor.fetchone()
                    if row:
                        demo = dict(row)
                    else:
                        raise

        if demo and "id" in demo:
            self.seed_env_credentials_if_empty(demo["id"])
        return demo

    def get_user_credentials(self, user_id: int) -> dict[str, Any]:
        """Retrieve and decrypt credentials for a specific user."""
        default_creds = {
            "alpaca_paper_api_key": "",
            "alpaca_paper_secret_key": "",
            "alpaca_live_api_key": "",
            "alpaca_live_secret_key": "",
            "openai_api_key": "",
            "gemini_api_key": "",
            "anthropic_api_key": "",
            "xai_api_key": "",
            "trading_mode": "paper",
            "allow_live": False,
            "live_authorized": False,
            "updated_at": _now_iso(),
        }
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT alpaca_paper_api_key, alpaca_paper_secret_key,
                       alpaca_live_api_key, alpaca_live_secret_key,
                       openai_api_key, gemini_api_key,
                       anthropic_api_key, xai_api_key,
                       trading_mode, allow_live, live_authorized, updated_at
                FROM user_credentials
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return default_creds

            return {
                "alpaca_paper_api_key": _decrypt_val(row["alpaca_paper_api_key"] or ""),
                "alpaca_paper_secret_key": _decrypt_val(row["alpaca_paper_secret_key"] or ""),
                "alpaca_live_api_key": _decrypt_val(row["alpaca_live_api_key"] or ""),
                "alpaca_live_secret_key": _decrypt_val(row["alpaca_live_secret_key"] or ""),
                "openai_api_key": _decrypt_val(row["openai_api_key"] or ""),
                "gemini_api_key": _decrypt_val(row["gemini_api_key"] or ""),
                "anthropic_api_key": _decrypt_val(row["anthropic_api_key"] or ""),
                "xai_api_key": _decrypt_val(row["xai_api_key"] or ""),
                "trading_mode": row["trading_mode"] or "paper",
                "allow_live": bool(row["allow_live"]),
                "live_authorized": bool(row["live_authorized"]),
                "updated_at": row["updated_at"] or _now_iso(),
            }

    def save_user_credentials(self, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        """Save and encrypt credentials for a user."""
        current = self.get_user_credentials(user_id)
        
        # Merge updates
        for k, v in updates.items():
            if v is not None:
                current[k] = v

        now = _now_iso()
        enc_paper_key = _encrypt_val(current.get("alpaca_paper_api_key", ""))
        enc_paper_secret = _encrypt_val(current.get("alpaca_paper_secret_key", ""))
        enc_live_key = _encrypt_val(current.get("alpaca_live_api_key", ""))
        enc_live_secret = _encrypt_val(current.get("alpaca_live_secret_key", ""))
        enc_openai_key = _encrypt_val(current.get("openai_api_key", ""))
        enc_gemini_key = _encrypt_val(current.get("gemini_api_key", ""))
        enc_anthropic_key = _encrypt_val(current.get("anthropic_api_key", ""))
        enc_xai_key = _encrypt_val(current.get("xai_api_key", ""))
        trading_mode = current.get("trading_mode", "paper")
        allow_live = 1 if current.get("allow_live") else 0
        live_authorized = 1 if current.get("live_authorized") else 0

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_credentials (
                    user_id, alpaca_paper_api_key, alpaca_paper_secret_key,
                    alpaca_live_api_key, alpaca_live_secret_key,
                    openai_api_key, gemini_api_key,
                    anthropic_api_key, xai_api_key,
                    trading_mode, allow_live, live_authorized, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    alpaca_paper_api_key = excluded.alpaca_paper_api_key,
                    alpaca_paper_secret_key = excluded.alpaca_paper_secret_key,
                    alpaca_live_api_key = excluded.alpaca_live_api_key,
                    alpaca_live_secret_key = excluded.alpaca_live_secret_key,
                    openai_api_key = excluded.openai_api_key,
                    gemini_api_key = excluded.gemini_api_key,
                    anthropic_api_key = excluded.anthropic_api_key,
                    xai_api_key = excluded.xai_api_key,
                    trading_mode = excluded.trading_mode,
                    allow_live = excluded.allow_live,
                    live_authorized = excluded.live_authorized,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, enc_paper_key, enc_paper_secret,
                    enc_live_key, enc_live_secret,
                    enc_openai_key, enc_gemini_key,
                    enc_anthropic_key, enc_xai_key,
                    trading_mode, allow_live, live_authorized, now,
                ),
            )
            conn.commit()

        return self.get_user_credentials(user_id)

    def clear_user_credentials(self, user_id: int, environment: str = "all") -> dict[str, Any]:
        """Clear user credentials for paper, live, or all."""
        current = self.get_user_credentials(user_id)
        env_name = (environment or "all").strip().lower()
        
        if env_name in {"paper", "all"}:
            current["alpaca_paper_api_key"] = ""
            current["alpaca_paper_secret_key"] = ""
        if env_name in {"live", "all"}:
            current["alpaca_live_api_key"] = ""
            current["alpaca_live_secret_key"] = ""
            current["live_authorized"] = False
        if env_name == "ai":
            current["openai_api_key"] = ""
            current["gemini_api_key"] = ""
            current["anthropic_api_key"] = ""
            current["xai_api_key"] = ""

        if env_name == "all" or (env_name == "live" and current.get("trading_mode") == "live"):
            current["trading_mode"] = "paper"
            current["allow_live"] = False

        return self.save_user_credentials(user_id, current)

    def create_password_reset_token(self, identifier: str, expires_minutes: int = 30) -> Optional[dict[str, Any]]:
        """Generate a one-time password reset token for a user."""
        clean_id = (identifier or "").strip().lower()
        if not clean_id:
            return None

        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, username, email, display_name FROM users WHERE lower(username) = ? OR lower(email) = ?",
                (clean_id, clean_id),
            ).fetchone()
            if not row:
                return None

            user_id = row["id"]
            token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            expires_at = (now + timedelta(minutes=expires_minutes)).isoformat()
            now_iso = now.isoformat()

            # Invalidate any older unused reset tokens for this user
            conn.execute(
                "UPDATE password_reset_tokens SET used = 1 WHERE user_id = ? AND used = 0",
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO password_reset_tokens (token, user_id, created_at, expires_at, used)
                VALUES (?, ?, ?, ?, 0)
                """,
                (token, user_id, now_iso, expires_at),
            )
            conn.commit()

            return {
                "token": token,
                "user_id": user_id,
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"] or row["username"],
                "expires_at": expires_at,
            }

    def verify_and_use_reset_token(self, token: str, new_password: str) -> dict[str, Any]:
        """Verify token and update password."""
        clean_token = (token or "").strip()
        if not clean_token:
            raise ValueError("Invalid or missing reset token.")

        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        if len(new_password) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"Password cannot exceed {MAX_PASSWORD_LENGTH} characters.")

        with _get_connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT r.token, r.user_id, r.expires_at, r.used, u.username, u.email, u.display_name
                FROM password_reset_tokens r
                JOIN users u ON r.user_id = u.id
                WHERE r.token = ?
                """,
                (clean_token,),
            ).fetchone()

            if not row:
                raise ValueError("Invalid reset token.")
            if row["used"]:
                raise ValueError("This reset link has already been used.")

            now = datetime.now(timezone.utc)
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except Exception:
                expires_at = now

            if now > expires_at:
                raise ValueError("This reset link has expired. Please request a new one.")

            user_id = row["user_id"]
            new_salt = secrets.token_hex(16)
            new_hash = self._hash_password(new_password, new_salt)

            # Update password
            conn.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (new_hash, new_salt, user_id),
            )
            # Mark token as used
            conn.execute(
                "UPDATE password_reset_tokens SET used = 1 WHERE token = ?",
                (clean_token,),
            )
            # Invalidate all active sessions for security
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()

            return {
                "id": user_id,
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"] or row["username"],
            }

    def get_user_preferences(self, user_id: int) -> dict[str, Any]:
        """Retrieve UI, localization, and trading defaults for a user."""
        default_prefs: dict[str, Any] = {
            "theme": "obsidian",
            "language": "en",
            "default_page": "auto-trade",
            "sound_alerts": True,
            "confirm_orders": True,
            "confirm_close_all": True,
            "chart_refresh_interval": 20,
            "compact_mode": False,
            "timezone_display": "local",
            "default_size_mode": "qty",
            "default_trade_qty": 1.0,
            "default_trade_notional": 100.0,
            "require_approval": False,
            "notify_browser": True,
            "notify_email": False,
            "notification_email": "",
            "updated_at": _now_iso(),
        }
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT theme, language, default_page, sound_alerts, confirm_orders, confirm_close_all,
                       chart_refresh_interval, compact_mode, timezone_display,
                       default_size_mode, default_trade_qty, default_trade_notional,
                       require_approval, notify_browser, notify_email, notification_email, updated_at
                FROM user_preferences
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return default_prefs

            return {
                "theme": row["theme"] or "obsidian",
                "language": row["language"] or "en",
                "default_page": row["default_page"] or "auto-trade",
                "sound_alerts": bool(row["sound_alerts"]),
                "confirm_orders": bool(row["confirm_orders"]),
                "confirm_close_all": bool(row["confirm_close_all"]),
                "chart_refresh_interval": int(row["chart_refresh_interval"] or 20),
                "compact_mode": bool(row["compact_mode"]),
                "timezone_display": row["timezone_display"] or "local",
                "default_size_mode": row["default_size_mode"] or "qty",
                "default_trade_qty": float(row["default_trade_qty"] or 1.0),
                "default_trade_notional": float(row["default_trade_notional"] or 100.0),
                "require_approval": bool(row["require_approval"]),
                "notify_browser": bool(row["notify_browser"]),
                "notify_email": bool(row["notify_email"]),
                "notification_email": row["notification_email"] or "",
                "updated_at": row["updated_at"] or _now_iso(),
            }

    def save_user_preferences(self, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        """Save UI, localization, and trading defaults for a user."""
        current = self.get_user_preferences(user_id)
        for k, v in updates.items():
            if v is not None:
                current[k] = v

        now = _now_iso()
        theme = str(current.get("theme", "obsidian")).strip().lower()
        if theme not in {"obsidian", "midnight", "emerald", "daylight"}:
            theme = "obsidian"

        language = str(current.get("language", "en")).strip().lower()
        if language not in {"en", "bn", "es", "fr", "hi"}:
            language = "en"

        default_page = str(current.get("default_page", "auto-trade")).strip().lower()
        allowed_pages = {"auto-trade", "manual-order", "positions", "orders", "history", "backtest", "api-keys", "admin"}
        if default_page not in allowed_pages:
            default_page = "auto-trade"

        sound_alerts = 1 if current.get("sound_alerts") else 0
        confirm_orders = 1 if current.get("confirm_orders") else 0
        confirm_close_all = 1 if current.get("confirm_close_all") else 0
        chart_refresh_interval = max(5, min(int(current.get("chart_refresh_interval", 20)), 120))
        compact_mode = 1 if current.get("compact_mode") else 0
        timezone_display = str(current.get("timezone_display", "local")).strip().lower()
        if timezone_display not in {"local", "utc", "exchange"}:
            timezone_display = "local"

        default_size_mode = "notional" if str(current.get("default_size_mode", "qty")).lower() == "notional" else "qty"
        default_trade_qty = max(0.01, float(current.get("default_trade_qty", 1.0)))
        default_trade_notional = max(1.0, float(current.get("default_trade_notional", 100.0)))
        require_approval = 1 if current.get("require_approval") else 0
        notify_browser = 1 if current.get("notify_browser") else 0
        notify_email = 1 if current.get("notify_email") else 0
        notification_email = str(current.get("notification_email", "")).strip()

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_preferences (
                    user_id, theme, language, default_page, sound_alerts, confirm_orders, confirm_close_all,
                    chart_refresh_interval, compact_mode, timezone_display,
                    default_size_mode, default_trade_qty, default_trade_notional,
                    require_approval, notify_browser, notify_email, notification_email, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    theme = excluded.theme,
                    language = excluded.language,
                    default_page = excluded.default_page,
                    sound_alerts = excluded.sound_alerts,
                    confirm_orders = excluded.confirm_orders,
                    confirm_close_all = excluded.confirm_close_all,
                    chart_refresh_interval = excluded.chart_refresh_interval,
                    compact_mode = excluded.compact_mode,
                    timezone_display = excluded.timezone_display,
                    default_size_mode = excluded.default_size_mode,
                    default_trade_qty = excluded.default_trade_qty,
                    default_trade_notional = excluded.default_trade_notional,
                    require_approval = excluded.require_approval,
                    notify_browser = excluded.notify_browser,
                    notify_email = excluded.notify_email,
                    notification_email = excluded.notification_email,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, theme, language, default_page, sound_alerts, confirm_orders, confirm_close_all,
                    chart_refresh_interval, compact_mode, timezone_display,
                    default_size_mode, default_trade_qty, default_trade_notional,
                    require_approval, notify_browser, notify_email, notification_email, now,
                ),
            )
            conn.commit()

        return self.get_user_preferences(user_id)

    def update_user_profile(
        self,
        user_id: int,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update display name and/or email for the user."""
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, display_name, role, created_at FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                raise ValueError("User not found.")

            updates: list[str] = []
            params: list[Any] = []

            if display_name is not None:
                clean_dn = display_name.strip()
                if not clean_dn:
                    clean_dn = user["username"]
                if len(clean_dn) > 50:
                    raise ValueError("Display name cannot exceed 50 characters.")
                updates.append("display_name = ?")
                params.append(clean_dn)

            if email is not None:
                clean_email = email.strip().lower()
                if not clean_email:
                    raise ValueError("Email address cannot be empty.")
                if not EMAIL_REGEX.match(clean_email):
                    raise ValueError("Please provide a valid email address.")
                # Check uniqueness if changed
                if clean_email != (user["email"] or "").lower():
                    cursor.execute("SELECT id FROM users WHERE LOWER(email) = ? AND id != ?", (clean_email, user_id))
                    if cursor.fetchone():
                        raise ValueError("An account with this email address already exists.")
                updates.append("email = ?")
                params.append(clean_email)

            if not updates:
                return self.get_user_by_id(user_id) or {}

            params.append(user_id)
            query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            conn.commit()

            return self.get_user_by_id(user_id) or {}

    def change_user_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> dict[str, Any]:
        """Change user password after verifying current password."""
        if not current_password:
            raise ValueError("Current password is required.")
        if not new_password:
            raise ValueError("New password is required.")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"New password must be at least {MIN_PASSWORD_LENGTH} characters long.")
        if len(new_password) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"New password cannot exceed {MAX_PASSWORD_LENGTH} characters.")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, password_hash, salt FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                raise ValueError("User not found.")

            computed_hash = self._hash_password(current_password, user["salt"])
            if not hmac.compare_digest(user["password_hash"], computed_hash):
                raise ValueError("Incorrect current password.")

            new_salt = secrets.token_hex(16)
            new_hash = self._hash_password(new_password, new_salt)

            cursor.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (new_hash, new_salt, user_id),
            )
            conn.commit()

            return {"ok": True, "message": "Password updated successfully."}

    def get_user_sessions(self, user_id: int, current_token: Optional[str] = None) -> list[dict[str, Any]]:
        """Retrieve active sessions for a user with client/device metadata."""
        now_str = _now_iso()
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            # Clean expired
            cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now_str,))
            conn.commit()

            cursor.execute(
                """
                SELECT token, created_at, expires_at, user_agent
                FROM sessions
                WHERE user_id = ? AND expires_at >= ?
                ORDER BY created_at DESC
                """,
                (user_id, now_str),
            )
            rows = cursor.fetchall()
            sessions = []
            for r in rows:
                token_val = r["token"]
                ua = r["user_agent"] or "Unknown Device / Browser"
                # Parse simplified device/browser name
                client_name = "Web Browser"
                if "Chrome" in ua and "Edg" in ua:
                    client_name = "Microsoft Edge"
                elif "Chrome" in ua:
                    client_name = "Google Chrome"
                elif "Firefox" in ua:
                    client_name = "Mozilla Firefox"
                elif "Safari" in ua and "Chrome" not in ua:
                    client_name = "Apple Safari"
                elif "Python" in ua or "Postman" in ua:
                    client_name = "API Client"

                os_name = "Unknown OS"
                if "Macintosh" in ua or "Mac OS" in ua:
                    os_name = "macOS"
                elif "Windows" in ua:
                    os_name = "Windows"
                elif "Linux" in ua:
                    os_name = "Linux"
                elif "iPhone" in ua or "iPad" in ua:
                    os_name = "iOS"
                elif "Android" in ua:
                    os_name = "Android"

                sessions.append({
                    "token_prefix": token_val[:8] + "…" + token_val[-6:],
                    "token": token_val,
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "user_agent": ua,
                    "client_name": client_name,
                    "os_name": os_name,
                    "is_current": (token_val == current_token) if current_token else False,
                })
            return sessions

    def terminate_user_session(self, user_id: int, session_token: str) -> bool:
        """Terminate a specific session token belonging to user."""
        if not session_token:
            return False
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token = ?",
                (user_id, session_token),
            )
            conn.commit()
            return cursor.rowcount > 0

    def terminate_other_sessions(self, user_id: int, current_token: str) -> int:
        """Terminate all sessions except the currently active one."""
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                (user_id, current_token),
            )
            conn.commit()
            return cursor.rowcount

    def delete_user_account(self, user_id: int, password: str) -> bool:
        """Delete account after validating password, protecting system owner integrity."""
        if not password:
            raise ValueError("Password is required to confirm account deletion.")

        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, role, password_hash, salt FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                raise ValueError("User not found.")

            computed_hash = self._hash_password(password, user["salt"])
            if not hmac.compare_digest(user["password_hash"], computed_hash):
                raise ValueError("Incorrect password. Account was not deleted.")

            target_role = (user["role"] or "trader").lower()
            if target_role == "owner":
                cursor.execute("SELECT COUNT(*) as owner_count FROM users WHERE role = 'owner'")
                if cursor.fetchone()["owner_count"] <= 1:
                    raise ValueError("Cannot delete the only remaining Owner of the system.")

            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return True

    def export_user_account_data(self, user_id: int) -> dict[str, Any]:
        """Export comprehensive summary of user account profile, preferences, and activity."""
        user = self.get_user_by_id(user_id)
        if not user:
            raise ValueError("User not found.")

        prefs = self.get_user_preferences(user_id)
        sessions = self.get_user_sessions(user_id)

        # Do not export raw API secrets, only export integration statuses
        return {
            "version": "2.0.0",
            "exported_at": _now_iso(),
            "profile": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "display_name": user["display_name"],
                "role": user["role"],
                "created_at": user["created_at"],
                "last_login_at": user["last_login_at"],
            },
            "preferences": prefs,
            "integrations": {
                "trading_mode": user.get("trading_mode", "paper"),
                "has_paper_key": user.get("has_paper_key", False),
                "has_live_key": user.get("has_live_key", False),
                "has_openai_key": user.get("has_openai_key", False),
                "has_gemini_key": user.get("has_gemini_key", False),
                "has_anthropic_key": user.get("has_anthropic_key", False),
                "has_xai_key": user.get("has_xai_key", False),
            },
            "active_sessions": [
                {
                    "client": s["client_name"],
                    "os": s["os_name"],
                    "created_at": s["created_at"],
                    "expires_at": s["expires_at"],
                    "is_current": s["is_current"],
                }
                for s in sessions
            ],
        }



# Global singleton auth store
AUTH_STORE = AuthStore()


def is_admin_or_owner(user: Optional[dict[str, Any]]) -> bool:
    """Return True if the user has admin or owner privileges."""
    if not user or not isinstance(user, dict):
        return False
    return str(user.get("role", "")).strip().lower() in {"owner", "admin"}

