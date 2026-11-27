"""SMS and voice-call OTP helpers for staff 2FA (Twilio-compatible)."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

import requests
import streamlit as st


def setting(key):
    try:
        value = st.secrets.get(key, "")
    except Exception:
        value = ""
    return str(value or os.environ.get(key, "")).strip()


def ensure_table(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff_phone_2fa (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                phone TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                otp_hash TEXT NOT NULL DEFAULT '',
                otp_expires INTEGER NOT NULL DEFAULT 0,
                last_sent INTEGER NOT NULL DEFAULT 0
            )
        """)


def phone_for_user(db_path, username):
    ensure_table(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT phone FROM staff_phone_2fa WHERE username=? COLLATE NOCASE AND verified=1",
            (username,),
        ).fetchone()
    return row[0] if row else ""


def send_code(db_path, username, phone, channel="sms", enroll=False):
    channel = str(channel).lower()
    if channel not in ("sms", "call"):
        return False, "Choose SMS or Voice Call."
    if not re.fullmatch(r"\+[1-9]\d{7,14}", str(phone).strip()):
        return False, "Use international phone format, e.g. +94771234567."
    sid, token, from_number = (setting(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    if not all((sid, token, from_number)):
        return False, "Twilio is not configured. Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER to Streamlit Secrets."
    ensure_table(db_path)
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT last_sent FROM staff_phone_2fa WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if row and now - int(row[0] or 0) < 60:
            return False, "Wait 60 seconds before requesting another code."
        conn.execute("INSERT OR IGNORE INTO staff_phone_2fa(username,phone) VALUES (?,?)", (username, phone.strip()))
        conn.execute("UPDATE staff_phone_2fa SET phone=? WHERE username=? COLLATE NOCASE", (phone.strip(), username))
    code = f"{secrets.randbelow(1_000_000):06d}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/"
    if channel == "sms":
        endpoint = url + "Messages.json"
        data = {"To": phone.strip(), "From": from_number, "Body": f"Government AI Platform code: {code}. Expires in 5 minutes. Do not share."}
    else:
        endpoint = url + "Calls.json"
        data = {"To": phone.strip(), "From": from_number, "Twiml": f"<Response><Say voice='alice'>Your Government AI Platform code is {' '.join(code)}. It expires in five minutes.</Say></Response>"}
    try:
        response = requests.post(endpoint, data=data, auth=(sid, token), timeout=15)
        response.raise_for_status()
    except Exception:
        return False, "Could not send OTP. Check Twilio configuration, verified destination and account balance."
    digest = hashlib.sha256(("govai-phone-otp:" + username.lower() + ":" + code).encode()).hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE staff_phone_2fa SET otp_hash=?,otp_expires=?,last_sent=? WHERE username=? COLLATE NOCASE",
                     (digest, now + 300, now, username))
    return True, "Verification code sent."


def verify_code(db_path, username, code, enable=False):
    if not re.fullmatch(r"\d{6}", str(code).strip()):
        return False
    digest = hashlib.sha256(("govai-phone-otp:" + username.lower() + ":" + str(code).strip()).encode()).hexdigest()
    now = int(time.time())
    ensure_table(db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT otp_hash,otp_expires FROM staff_phone_2fa WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not row or now > int(row[1] or 0) or not hmac.compare_digest(row[0] or "", digest):
            conn.rollback()
            return False
        conn.execute("UPDATE staff_phone_2fa SET verified=?,otp_hash='',otp_expires=0 WHERE username=? COLLATE NOCASE",
                     (1 if enable else row[0] != "", username))
        conn.commit()
        return True
