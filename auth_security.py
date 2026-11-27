"""Staff two-factor authentication and security email notifications.

Required secrets:
  AUTH_ENCRYPTION_KEY: Fernet key (generate with cryptography.fernet.Fernet.generate_key()).
  SECURITY_ALERT_EMAIL: recipient address.
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL.
"""
import hashlib
import os
import smtplib
import sqlite3
import ssl
from email.message import EmailMessage

import pyotp
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
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


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


def enable_totp(db_path, username, secret, code):
    if not pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1):
        return False
    encrypted = _fernet().encrypt(secret.encode("utf-8")).decode("ascii")
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO staff_security(username, totp_secret_enc, enabled)
            VALUES (?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                totp_secret_enc=excluded.totp_secret_enc,
                enabled=1, updated_at=CURRENT_TIMESTAMP
        """, (username, encrypted))
    return True


def verify_totp(db_path, username, code):
    ensure_security_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT totp_secret_enc FROM staff_security WHERE username=? COLLATE NOCASE AND enabled=1",
            (username,),
        ).fetchone()
    if not row:
        return False
    try:
        secret = _fernet().decrypt(row[0].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, RuntimeError):
        return False
    return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)


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
