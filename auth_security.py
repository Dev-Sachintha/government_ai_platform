"""Account two-factor authentication and security email notifications.

Required secrets:
  AUTH_ENCRYPTION_KEY: Fernet key (generate with cryptography.fernet.Fernet.generate_key()).
  SECURITY_ALERT_EMAIL: recipient address.
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL.
"""
import hashlib
import io
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage

import pyotp
import qrcode
import streamlit as st
from cryptography.fernet import Fernet, InvalidToken


def _setting(name, default=""):
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return str(os.environ.get(name, default))


def _fernet():
    key = _setting("AUTH_ENCRYPTION_KEY").strip()
    if not key:
        raise RuntimeError("AUTH_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("AUTH_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def ensure_security_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff_security (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                totp_secret_enc TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_ip_hash TEXT NOT NULL DEFAULT '',
                last_totp_step INTEGER NOT NULL DEFAULT -1,
                recovery_email TEXT NOT NULL DEFAULT '',
                recovery_email_verified INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_security)")}
        if "last_totp_step" not in columns:
            conn.execute("ALTER TABLE staff_security ADD COLUMN last_totp_step INTEGER NOT NULL DEFAULT -1")
        if "recovery_email" not in columns:
            conn.execute("ALTER TABLE staff_security ADD COLUMN recovery_email TEXT NOT NULL DEFAULT ''")
        if "recovery_email_verified" not in columns:
            conn.execute("ALTER TABLE staff_security ADD COLUMN recovery_email_verified INTEGER NOT NULL DEFAULT 0")
        # Remove legacy phone-factor data after SMS/voice authentication was retired.
        if "phone_number" in columns:
            conn.execute("UPDATE staff_security SET phone_number=''")
        if "phone_verified" in columns:
            conn.execute("UPDATE staff_security SET phone_verified=0")

        # Enforce one verified recovery email per account (case-insensitive).
        seen_emails = set()
        rows = conn.execute(
            """SELECT username, lower(trim(recovery_email))
               FROM staff_security
               WHERE recovery_email_verified=1 AND trim(recovery_email)<>''
               ORDER BY rowid"""
        ).fetchall()
        for email_owner, normalized_email in rows:
            if normalized_email in seen_emails:
                conn.execute(
                    """UPDATE staff_security
                       SET recovery_email='', recovery_email_verified=0,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE username=? COLLATE NOCASE""",
                    (email_owner,),
                )
            else:
                seen_emails.add(normalized_email)
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS
               uq_staff_security_verified_recovery_email
               ON staff_security(lower(trim(recovery_email)))
               WHERE recovery_email_verified=1 AND trim(recovery_email)<>''"""
        )


def totp_is_enabled(db_path, username):
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT enabled FROM staff_security WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
    return bool(row and row[0])


def new_totp_secret():
    return pyotp.random_base32()


def provisioning_uri(username, secret):
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name="Government AI Platform"
    )

def provisioning_qr_code(username, secret):
    """Return a PNG QR code for scanning the TOTP provisioning URI."""
    uri = provisioning_uri(username, secret)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output



def _matching_totp_step(secret, code, now=None):
    """Return the matching RFC 6238 time-step, allowing one-step clock drift."""
    value = str(code).strip()
    if len(value) != 6 or not value.isdigit():
        return None
    now = time.time() if now is None else float(now)
    totp = pyotp.TOTP(secret)
    for offset in (0, -1, 1):
        candidate_time = now + offset * totp.interval
        if totp.verify(value, for_time=candidate_time, valid_window=0):
            return int(candidate_time // totp.interval)
    return None


def enable_totp(db_path, username, secret, code):
    step = _matching_totp_step(secret, code)
    if step is None:
        return False
    encrypted = _fernet().encrypt(secret.encode("utf-8")).decode("ascii")
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO staff_security(username, totp_secret_enc, enabled, last_totp_step)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(username) DO UPDATE SET
                totp_secret_enc=excluded.totp_secret_enc,
                enabled=1, last_totp_step=excluded.last_totp_step,
                updated_at=CURRENT_TIMESTAMP
        """, (username, encrypted, step))
    return True


def verify_totp_detailed(db_path, username, code):
    """Verify a TOTP once and explain whether it was invalid or already consumed.

    A code is intentionally accepted only once per 30-second time step. This
    prevents replay attacks while giving the login UI an actionable message.
    """
    ensure_security_table(db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT totp_secret_enc, last_totp_step FROM staff_security WHERE username=? COLLATE NOCASE AND enabled=1",
            (username,),
        ).fetchone()
        if not row:
            conn.rollback()
            return "invalid"
        try:
            secret = _fernet().decrypt(row[0].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, RuntimeError):
            conn.rollback()
            return "invalid"
        step = _matching_totp_step(secret, code)
        if step is None:
            conn.rollback()
            return "invalid"
        last_step = int(row[1] if row[1] is not None else -1)
        if step <= last_step:
            conn.rollback()
            return "already_used"
        cursor = conn.execute(
            "UPDATE staff_security SET last_totp_step=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE username=? COLLATE NOCASE AND enabled=1 AND last_totp_step < ?",
            (step, username, step),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return "already_used"
        conn.commit()
        return "valid"


def verify_totp(db_path, username, code):
    """Backward-compatible boolean TOTP verification helper."""
    return verify_totp_detailed(db_path, username, code) == "valid"


def client_ip_hash():
    try:
        address = getattr(st.context, "ip_address", None)
        if not address:
            return "unknown"
        return hashlib.sha256(("staff-login-ip:" + str(address)).encode()).hexdigest()
    except Exception:
        return "unknown"


def record_staff_ip(db_path, username, ip_hash):
    ensure_security_table(db_path)
    if not ip_hash or ip_hash == "unknown":
        return False
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_ip_hash FROM staff_security WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        previous = row[0] if row else ""
        conn.execute(
            "UPDATE staff_security SET last_ip_hash=?, updated_at=CURRENT_TIMESTAMP WHERE username=? COLLATE NOCASE",
            (ip_hash, username),
        )
    return bool(previous and previous != ip_hash)


def send_security_alert(event, username, details):
    host = _setting("SMTP_HOST").strip()
    try:
        port = int(_setting("SMTP_PORT", "465") or "465")
    except (TypeError, ValueError):
        return False
    smtp_user = _setting("SMTP_USERNAME").strip()
    smtp_password = _setting("SMTP_PASSWORD")
    sender = _setting("SMTP_FROM_EMAIL", smtp_user).strip()
    recipient = _setting("SECURITY_ALERT_EMAIL").strip()
    if not all((host, smtp_user, smtp_password, sender, recipient)):
        return False
    message = EmailMessage()
    message["Subject"] = f"[Government AI Platform] Security alert: {event}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"Security event: {event}\nAccount: {username or 'unknown'}\n"
        f"Details: {details}\n\nThis is an automated security notification."
    )
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=12) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(message)
        return True
    except Exception:
        return False


def _recovery_code_hash(code):
    return hashlib.sha256(("govai-staff-recovery:" + str(code).strip().upper()).encode("utf-8")).hexdigest()


def ensure_recovery_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                code_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                used_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_recovery_username ON staff_recovery_codes(username)")


def ensure_encrypted_recovery_backup_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff_recovery_code_backups (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                encrypted_codes BLOB NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


def generate_recovery_codes(db_path, username, count=10):
    """Generate one-time codes and keep an encrypted copy for secure Drive backup."""
    ensure_recovery_table(db_path)
    ensure_encrypted_recovery_backup_table(db_path)
    codes = set()
    while len(codes) < count:
        codes.add(f"{secrets.randbelow(100000000):08d}")
    codes = sorted(codes)
    encrypted_codes = _fernet().encrypt(json.dumps(codes).encode("utf-8"))
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM staff_recovery_codes WHERE username=? COLLATE NOCASE", (username,))
        conn.executemany(
            "INSERT INTO staff_recovery_codes(username, code_hash) VALUES (?, ?)",
            [(username, _recovery_code_hash(code)) for code in codes],
        )
        conn.execute(
            """INSERT INTO staff_recovery_code_backups(username, encrypted_codes)
               VALUES (?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 encrypted_codes=excluded.encrypted_codes,
                 updated_at=CURRENT_TIMESTAMP""",
            (username, encrypted_codes),
        )
    return codes


def _google_drive_oauth_config():
    raw = _setting("GOOGLE_DRIVE_OAUTH_CLIENT_JSON").strip()
    if not raw:
        raise RuntimeError("Google Drive OAuth is not configured. Add GOOGLE_DRIVE_OAUTH_CLIENT_JSON to Streamlit Secrets.")
    try:
        config = json.loads(raw)
        section = config.get("web") or config.get("installed")
        if not section or not section.get("client_id") or not section.get("client_secret"):
            raise ValueError("OAuth client JSON must contain client_id and client_secret.")
        return config
    except Exception as exc:
        raise RuntimeError("GOOGLE_DRIVE_OAUTH_CLIENT_JSON is not valid Google OAuth client JSON.") from exc


def _google_drive_redirect_uri():
    value = _setting("GOOGLE_DRIVE_OAUTH_REDIRECT_URI").strip()
    if not value:
        raise RuntimeError("Set GOOGLE_DRIVE_OAUTH_REDIRECT_URI to your Streamlit app URL.")
    return value.rstrip("/")


def ensure_google_drive_oauth_table(db_path):
    with sqlite3.connect(db_path) as conn:
        # Per-account OAuth grants: each staff/user account connects its own Drive.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_drive_oauth_tokens_per_user (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                refresh_token_enc BLOB NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_drive_oauth_pending (
                state_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE,
                code_verifier_enc TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # One-time migration: retain the old global grant for its recorded owner only.
        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='google_drive_oauth_tokens'"
        ).fetchone()
        if legacy_exists:
            legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(google_drive_oauth_tokens)")}
            if {"refresh_token_enc", "connected_by"}.issubset(legacy_columns):
                conn.execute("""
                    INSERT OR IGNORE INTO google_drive_oauth_tokens_per_user(username, refresh_token_enc)
                    SELECT connected_by, refresh_token_enc
                    FROM google_drive_oauth_tokens
                    WHERE id=1 AND trim(connected_by)<>''
                """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(google_drive_oauth_pending)")}
        if "code_verifier_enc" not in columns:
            conn.execute(
                "ALTER TABLE google_drive_oauth_pending ADD COLUMN code_verifier_enc TEXT NOT NULL DEFAULT ''"
            )


def google_drive_oauth_authorization_url(db_path, username):
    """Create a one-use OAuth authorization URL and keep its CSRF state in session."""
    from google_auth_oauthlib.flow import Flow
    # Keep the PKCE verifier across Streamlit reruns and the Google redirect.
    # It is encrypted at rest because the OAuth code exchange requires this secret.
    code_verifier = secrets.token_urlsafe(64)
    flow = Flow.from_client_config(
        _google_drive_oauth_config(),
        scopes=["https://www.googleapis.com/auth/drive.file"],
        code_verifier=code_verifier,
    )
    flow.redirect_uri = _google_drive_redirect_uri()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    ensure_google_drive_oauth_table(db_path)
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    verifier_enc = _fernet().encrypt(code_verifier.encode("utf-8")).decode("ascii")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM google_drive_oauth_pending WHERE created_at < datetime('now', '-15 minutes')")
        conn.execute(
            """INSERT INTO google_drive_oauth_pending(state_hash, username, code_verifier_enc)
               VALUES (?, ?, ?)""",
            (state_hash, str(username), verifier_enc),
        )
    return url


def complete_google_drive_oauth(db_path, code, state, username):
    """Validate OAuth state, exchange the code, and encrypt the refresh token at rest."""
    from google_auth_oauthlib.flow import Flow
    if not code or not state:
        raise RuntimeError("Google did not return the required authorization code and state.")
    ensure_google_drive_oauth_table(db_path)
    state_hash = hashlib.sha256(str(state).encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as conn:
        pending = conn.execute(
            """SELECT username, code_verifier_enc FROM google_drive_oauth_pending
               WHERE state_hash=? AND created_at >= datetime('now', '-15 minutes')""",
            (state_hash,),
        ).fetchone()
    if not pending:
        raise RuntimeError("Google authorization state expired or was not recognized. Please connect again.")
    if str(pending[0]).casefold() != str(username).casefold():
        raise RuntimeError("Sign in with the same account that started Google Drive connection.")
    if not pending[1]:
        raise RuntimeError("The PKCE verifier was not saved. Start Google Drive connection again.")
    try:
        code_verifier = _fernet().decrypt(str(pending[1]).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("The saved PKCE verifier could not be decrypted. Start Google Drive connection again.") from exc
    flow = Flow.from_client_config(
        _google_drive_oauth_config(),
        scopes=["https://www.googleapis.com/auth/drive.file"],
        state=str(state),
        code_verifier=code_verifier,
    )
    flow.redirect_uri = _google_drive_redirect_uri()
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        raise RuntimeError("Google did not return a refresh token. Reconnect and approve access again.")
    encrypted = _fernet().encrypt(refresh_token.encode("utf-8"))
    ensure_google_drive_oauth_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO google_drive_oauth_tokens_per_user(username, refresh_token_enc)
               VALUES (?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 refresh_token_enc=excluded.refresh_token_enc,
                 updated_at=CURRENT_TIMESTAMP""",
            (str(username), encrypted),
        )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM google_drive_oauth_pending WHERE state_hash=?", (state_hash,))
    return True


def google_drive_oauth_connected(db_path, username):
    """Return whether this account has connected its own Google Drive."""
    ensure_google_drive_oauth_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM google_drive_oauth_tokens_per_user WHERE username=? COLLATE NOCASE",
            (str(username),),
        ).fetchone()
    return bool(row)


def _decrypt_and_validate_recovery_backup(encrypted_blob, expected_count=10):
    """Decrypt a backup and validate its one-time recovery-code payload."""
    try:
        decoded = _fernet().decrypt(bytes(encrypted_blob)).decode("utf-8")
        codes = json.loads(decoded)
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("Encrypted recovery backup could not be decrypted or parsed.") from exc

    if (
        not isinstance(codes, list)
        or len(codes) != expected_count
        or any(not isinstance(code, str) or re.fullmatch(r"\d{8}", code) is None for code in codes)
        or len(set(codes)) != expected_count
    ):
        raise ValueError(
            f"Recovery backup must contain exactly {expected_count} unique 8-digit codes."
        )
    return codes


def verify_local_recovery_backup(db_path, username):
    """Return the number of valid codes in this account's encrypted local backup."""
    ensure_encrypted_recovery_backup_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT encrypted_codes FROM staff_recovery_code_backups WHERE username=? COLLATE NOCASE",
            (str(username),),
        ).fetchone()
    if not row:
        raise RuntimeError("No encrypted recovery-code backup exists for this account.")
    return len(_decrypt_and_validate_recovery_backup(row[0], expected_count=10))


def sync_recovery_codes_to_drive(db_path, username):
    """Upload the encrypted backup, download it again, and verify all ten codes."""
    # Each user's backup lives in their own Drive root; no shared folder ID needed.
    ensure_encrypted_recovery_backup_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT encrypted_codes FROM staff_recovery_code_backups WHERE username=? COLLATE NOCASE",
            (str(username),),
        ).fetchone()
    if not row:
        return False

    # Validate locally before sending anything to Drive. Plaintext is never uploaded.
    local_codes = _decrypt_and_validate_recovery_backup(row[0], expected_count=10)

    ensure_google_drive_oauth_table(db_path)
    with sqlite3.connect(db_path) as conn:
        token_row = conn.execute(
            "SELECT refresh_token_enc FROM google_drive_oauth_tokens_per_user WHERE username=? COLLATE NOCASE",
            (str(username),),
        ).fetchone()
    if not token_row:
        return False
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

        refresh_token = _fernet().decrypt(bytes(token_row[0])).decode("utf-8")
        config = _google_drive_oauth_config()
        section = config.get("web") or config.get("installed")
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=section["client_id"],
            client_secret=section["client_secret"],
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        credentials.refresh(Request())
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        opaque_name = hashlib.sha256(
            ("govai-recovery-backup:" + str(username).casefold()).encode("utf-8")
        ).hexdigest() + ".enc"
        query = f"name='{opaque_name}' and trashed=false"
        found = drive.files().list(
            q=query, fields="files(id,name)", pageSize=5
        ).execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(bytes(row[0])), mimetype="application/octet-stream",
            resumable=False
        )
        if found:
            file_id = found[0]["id"]
            drive.files().update(fileId=file_id, media_body=media).execute()
        else:
            created = drive.files().create(
                body={"name": opaque_name},
                media_body=media, fields="id"
            ).execute()
            file_id = created["id"]

        # Read back the exact Drive object and verify its encrypted payload.
        request = drive.files().get_media(fileId=file_id)
        downloaded = io.BytesIO()
        downloader = MediaIoBaseDownload(downloaded, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        remote_codes = _decrypt_and_validate_recovery_backup(
            downloaded.getvalue(), expected_count=10
        )
        if set(remote_codes) != set(local_codes):
            raise ValueError("The uploaded Drive backup does not match the local recovery-code set.")
        return True
    except Exception as exc:
        logging.warning("Encrypted recovery-code Google Drive upload/verification failed: %s", exc)
        raise RuntimeError(
            f"Google Drive backup upload or verification failed ({type(exc).__name__})."
        ) from exc



def consume_drive_backup_code(db_path, username, code):
    """Restore an account's encrypted Drive recovery list and consume one code exactly once.

    Drive stores only the Fernet-encrypted recovery-code list. Codes are imported
    into the local one-time hash table (without replacing already-used rows),
    then the submitted code is consumed and removed from the encrypted list.
    """
    value = str(code).strip()
    if len(value) != 8 or not value.isdigit():
        return False

    ensure_google_drive_oauth_table(db_path)
    with sqlite3.connect(db_path) as conn:
        token_row = conn.execute(
            "SELECT refresh_token_enc FROM google_drive_oauth_tokens_per_user WHERE username=? COLLATE NOCASE",
            (str(username),),
        ).fetchone()
    if not token_row:
        raise RuntimeError("Connect your own Google Drive from the account sidebar before using Drive recovery.")

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        refresh_token = _fernet().decrypt(bytes(token_row[0])).decode("utf-8")
        config = _google_drive_oauth_config()
        section = config.get("web") or config.get("installed")
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=section["client_id"],
            client_secret=section["client_secret"],
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        credentials.refresh(Request())
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        opaque_name = hashlib.sha256(
            ("govai-recovery-backup:" + str(username).casefold()).encode("utf-8")
        ).hexdigest() + ".enc"
        query = f"name='{opaque_name}' and trashed=false"
        found = drive.files().list(
            q=query, fields="files(id)", pageSize=1
        ).execute().get("files", [])
        if not found:
            return False

        request = drive.files().get_media(fileId=found[0]["id"])
        output = io.BytesIO()
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        encrypted_blob = output.getvalue()
        codes = json.loads(_fernet().decrypt(encrypted_blob).decode("utf-8"))
        if not isinstance(codes, list) or not all(
            isinstance(item, str) and len(item) == 8 and item.isdigit()
            for item in codes
        ):
            raise ValueError("The encrypted Drive backup has an invalid recovery-code format.")
    except Exception as exc:
        logging.warning("Encrypted recovery-code Google Drive download failed: %s", exc)
        raise RuntimeError(
            f"Google Drive recovery backup could not be read ({type(exc).__name__})."
        ) from exc

    if not any(secrets.compare_digest(item, value) for item in codes):
        return False

    ensure_recovery_table(db_path)
    ensure_encrypted_recovery_backup_table(db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        # INSERT OR IGNORE preserves any existing used_at marker for this code.
        conn.executemany(
            "INSERT OR IGNORE INTO staff_recovery_codes(username, code_hash) VALUES (?, ?)",
            [(username, _recovery_code_hash(item)) for item in codes],
        )
    if not consume_backup_code(db_path, username, value):
        return False

    remaining_codes = [item for item in codes if item != value]
    encrypted_remaining = _fernet().encrypt(
        json.dumps(remaining_codes).encode("utf-8")
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO staff_recovery_code_backups(username, encrypted_codes)
               VALUES (?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 encrypted_codes=excluded.encrypted_codes,
                 updated_at=CURRENT_TIMESTAMP""",
            (username, encrypted_remaining),
        )
    return True


def identify_recovery_code(db_path, code):
    ensure_recovery_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT r.username, u.role FROM staff_recovery_codes r
               JOIN app_users u ON u.username = r.username COLLATE NOCASE
               WHERE r.code_hash=? AND r.used_at IS NULL AND u.active=1
                 AND u.role IN ('admin','analyst') LIMIT 1""",
            (_recovery_code_hash(code),),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def consume_recovery_code_and_update(db_path, code, new_username, password_hash, password_salt):
    """Atomically consume a valid recovery code and update the same active staff account."""
    ensure_recovery_table(db_path)
    digest = _recovery_code_hash(code)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT r.id, r.username FROM staff_recovery_codes r
               JOIN app_users u ON u.username = r.username COLLATE NOCASE
               WHERE r.code_hash=? AND r.used_at IS NULL AND u.active=1
                 AND u.role IN ('admin','analyst') LIMIT 1""",
            (digest,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "Recovery code is invalid or has already been used."
        old_username = row[1]
        try:
            conn.execute(
                "UPDATE app_users SET username=?, password_hash=?, password_salt=? WHERE username=? COLLATE NOCASE AND active=1 AND role IN ('admin','analyst')",
                (new_username, password_hash, password_salt, old_username),
            )
            if conn.total_changes < 1:
                conn.rollback()
                return False, "The staff account could not be updated."
            conn.execute("UPDATE staff_security SET username=?, updated_at=CURRENT_TIMESTAMP WHERE username=? COLLATE NOCASE", (new_username, old_username))
            conn.execute("UPDATE staff_recovery_codes SET used_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
            conn.execute("DELETE FROM staff_recovery_codes WHERE username=? COLLATE NOCASE AND id<>?", (old_username, row[0]))
            conn.commit()
            return True, old_username
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "That username is already taken."


def is_recovery_email_available(db_path, email, username=None):
    """Return True when no other account owns this verified recovery email."""
    ensure_security_table(db_path)
    normalized = str(email).strip().lower()
    if not normalized:
        return False
    with sqlite3.connect(db_path) as conn:
        query = """SELECT 1 FROM staff_security
                   WHERE lower(trim(recovery_email))=?
                     AND recovery_email_verified=1
                     AND trim(recovery_email)<>''"""
        params = [normalized]
        if username:
            query += " AND username<>? COLLATE NOCASE"
            params.append(username)
        return conn.execute(query + " LIMIT 1", params).fetchone() is None


def set_recovery_email(db_path, username, email):
    """Save a verified recovery email only if it is unique across accounts."""
    ensure_security_table(db_path)
    normalized = str(email).strip().lower()
    if not normalized or not is_recovery_email_available(db_path, normalized, username):
        return False
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.execute(
                """UPDATE staff_security
                   SET recovery_email=?, recovery_email_verified=1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE username=? COLLATE NOCASE AND enabled=1""",
                (normalized, username),
            )
            return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        return False


def get_staff_by_recovery_email(db_path, email):
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT s.username, u.role FROM staff_security s
               JOIN app_users u ON u.username=s.username COLLATE NOCASE
               WHERE lower(s.recovery_email)=lower(?) AND s.recovery_email_verified=1
                 AND s.enabled=1 AND u.active=1 AND u.role IN ('admin','analyst','user') LIMIT 1""",
            (str(email).strip(),),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _send_configured_smtp_message(message):
    """Send through Gmail-compatible implicit TLS (465) or STARTTLS (587)."""
    host = _setting("SMTP_HOST").strip()
    smtp_user = _setting("SMTP_USERNAME").strip()
    smtp_password = _setting("SMTP_PASSWORD").strip()
    sender = _setting("SMTP_FROM_EMAIL", smtp_user).strip()
    missing = [
        name for name, value in (
            ("SMTP_HOST", host), ("SMTP_USERNAME", smtp_user),
            ("SMTP_PASSWORD", smtp_password), ("SMTP_FROM_EMAIL", sender),
        ) if not value
    ]
    if missing:
        logging.getLogger(__name__).error(
            "SMTP delivery not configured; missing settings: %s", ", ".join(missing)
        )
        return False
    try:
        port = int(_setting("SMTP_PORT", "465") or "465")
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        elif port == 587:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            logging.getLogger(__name__).error(
                "SMTP delivery failed: unsupported SMTP port %s (use 465 or 587).", port
            )
            return False
        return True
    except (smtplib.SMTPException, OSError, ssl.SSLError, ValueError) as exc:
        # Log only the exception class; never log credentials or message contents.
        logging.getLogger(__name__).error(
            "SMTP delivery failed (%s). Check host, port, Gmail App Password and sender settings.",
            type(exc).__name__,
        )
        return False


def send_recovery_email_code(email, code):
    """Send a short-lived sign-in/enrollment verification code using configured SMTP."""
    message = EmailMessage()
    message["Subject"] = "Government AI Platform - verification code"
    message["From"] = _setting("SMTP_FROM_EMAIL", _setting("SMTP_USERNAME")).strip()
    message["To"] = str(email).strip()
    message.set_content(
        f"Your Government AI Platform verification code is: {code}\n\n"
        "This code expires in 10 minutes. If you did not request it, ignore this email. "
        "Never share this code with anyone."
    )
    return _send_configured_smtp_message(message)


def send_smtp_test_email(email):
    """Send a harmless SMTP diagnostic message to an admin-supplied address."""
    message = EmailMessage()
    message["Subject"] = "Government AI Platform - SMTP test"
    message["From"] = _setting("SMTP_FROM_EMAIL", _setting("SMTP_USERNAME")).strip()
    message["To"] = str(email).strip()
    message.set_content(
        "SMTP test successful. Your Government AI Platform email delivery settings are working."
    )
    return _send_configured_smtp_message(message)



def consume_backup_code(db_path, username, code):
    """Atomically consume one 8-digit code; returns True only once."""
    ensure_recovery_table(db_path)
    value = str(code).strip()
    if len(value) != 8 or not value.isdigit():
        return False
    digest = _recovery_code_hash(value)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM staff_recovery_codes WHERE username=? COLLATE NOCASE AND code_hash=? AND used_at IS NULL LIMIT 1",
            (username, digest),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE staff_recovery_codes SET used_at=CURRENT_TIMESTAMP WHERE id=? AND used_at IS NULL",
            (row[0],),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()

    # Keep the encrypted Drive payload in sync so a consumed code is not
    # reintroduced if the account's local database is later restored.
    try:
        ensure_encrypted_recovery_backup_table(db_path)
        with sqlite3.connect(db_path) as conn:
            backup_row = conn.execute(
                "SELECT encrypted_codes FROM staff_recovery_code_backups WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
        if backup_row:
            saved_codes = json.loads(
                _fernet().decrypt(bytes(backup_row[0])).decode("utf-8")
            )
            remaining_codes = [item for item in saved_codes if item != value]
            encrypted_remaining = _fernet().encrypt(
                json.dumps(remaining_codes).encode("utf-8")
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE staff_recovery_code_backups SET encrypted_codes=?, updated_at=CURRENT_TIMESTAMP WHERE username=? COLLATE NOCASE",
                    (encrypted_remaining, username),
                )
    except Exception as exc:
        logging.warning("Could not update local encrypted recovery-code backup after use: %s", type(exc).__name__)
    return True

