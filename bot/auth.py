"""Authentication and user session management for AlgoPaca."""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Optional

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

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_reset_tokens_user_id ON password_reset_tokens(user_id);
                CREATE INDEX IF NOT EXISTS idx_reset_tokens_expires_at ON password_reset_tokens(expires_at);
                """
            )
            conn.commit()
        # Seed demo user if table is empty
        self.get_or_create_demo_user()

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
                SELECT id, username, email, password_hash, salt, display_name, role, created_at
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
                SELECT u.id, u.username, u.email, u.display_name, u.role, u.created_at, u.last_login_at, s.expires_at
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND s.expires_at >= ?
                """,
                (token, now_str),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def delete_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return cursor.rowcount > 0

    def get_or_create_demo_user(self) -> dict[str, Any]:
        with _get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, display_name, role, created_at FROM users WHERE username = 'demo'")
            row = cursor.fetchone()
            if row:
                return dict(row)

        # Create demo account
        try:
            return self.register_user(
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
                    return dict(row)
                raise

    def get_user_credentials(self, user_id: int) -> dict[str, Any]:
        """Retrieve and decrypt credentials for a specific user."""
        default_creds = {
            "alpaca_paper_api_key": "",
            "alpaca_paper_secret_key": "",
            "alpaca_live_api_key": "",
            "alpaca_live_secret_key": "",
            "openai_api_key": "",
            "gemini_api_key": "",
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
                    trading_mode, allow_live, live_authorized, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    alpaca_paper_api_key = excluded.alpaca_paper_api_key,
                    alpaca_paper_secret_key = excluded.alpaca_paper_secret_key,
                    alpaca_live_api_key = excluded.alpaca_live_api_key,
                    alpaca_live_secret_key = excluded.alpaca_live_secret_key,
                    openai_api_key = excluded.openai_api_key,
                    gemini_api_key = excluded.gemini_api_key,
                    trading_mode = excluded.trading_mode,
                    allow_live = excluded.allow_live,
                    live_authorized = excluded.live_authorized,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id, enc_paper_key, enc_paper_secret,
                    enc_live_key, enc_live_secret,
                    enc_openai_key, enc_gemini_key,
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



# Global singleton auth store
AUTH_STORE = AuthStore()
