"""Account two-factor authentication and security email notifications.

Required secrets:
  AUTH_ENCRYPTION_KEY: Fernet key (generate with cryptography.fernet.Fernet.generate_key()).
  SECURITY_ALERT_EMAIL: recipient address.
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL.
"""
import hashlib
import io
import logging
import os
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


def generate_recovery_codes(db_path, username, count=10):
    """Replace a staff member's previous recovery codes and return new plaintext codes once."""
    import secrets
    ensure_recovery_table(db_path)
    codes = [secrets.token_hex(16).upper() for _ in range(count)]
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM staff_recovery_codes WHERE username=? COLLATE NOCASE", (username,))
        conn.executemany(
            "INSERT INTO staff_recovery_codes(username, code_hash) VALUES (?, ?)",
            [(username, _recovery_code_hash(code)) for code in codes],
        )
    return codes


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


def set_recovery_email(db_path, username, email):
    """Save an email address only after the caller has verified its one-time code."""
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE staff_security SET recovery_email=?, recovery_email_verified=1, updated_at=CURRENT_TIMESTAMP WHERE username=? COLLATE NOCASE AND enabled=1",
            (str(email).strip().lower(), username),
        )


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
