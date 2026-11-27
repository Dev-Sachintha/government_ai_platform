# Staff 2FA and Security Email Alerts

Admin and Analyst accounts must enroll in an authenticator app the next time they sign in after this feature is deployed. Ordinary User accounts are unchanged.

## 1. Configure encrypted 2FA storage

Generate a Fernet encryption key locally:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the generated value to Streamlit Cloud → App → Settings → Secrets:

```toml
AUTH_ENCRYPTION_KEY = "PASTE_THE_GENERATED_FERNET_KEY_HERE"
```

Keep this key private and back it up securely. Do not rotate it after staff enroll unless you have a migration plan: existing encrypted authenticator secrets cannot be decrypted with a different key.

## 2. Configure suspicious-login email alerts

Add SMTP settings to the same Streamlit Secrets:

```toml
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = "465"
SMTP_USERNAME = "your-alert-mailbox@gmail.com"
SMTP_PASSWORD = "YOUR_GOOGLE_APP_PASSWORD"
SMTP_FROM_EMAIL = "your-alert-mailbox@gmail.com"
SECURITY_ALERT_EMAIL = "security-recipient@example.com"
```

For Gmail, use a Google App Password (with 2-Step Verification enabled), not your normal Google account password. Use a dedicated mailbox where possible.

## 3. Staff enrollment

After deployment:
1. Admin/Analyst enters the normal username and password.
2. On first login, the platform displays a private setup key and authenticator setup URI.
3. Add the account to Google Authenticator, Microsoft Authenticator, or another TOTP-compatible app.
4. Enter the current six-digit code to finish enrollment.
5. Future staff logins require both password and a current six-digit code.

Save the setup key securely. Do not share it with anyone.

## 4. Alerts generated

Email alerts are attempted for:
- Three invalid authenticator codes during a sign-in.
- Repeated failed staff passwords (from the third failure onward).
- A staff account reaching the temporary login-block threshold.
- A successful staff login from a previously recorded different client IP.

Client IP information depends on what Streamlit exposes to the app. Email delivery also depends on valid SMTP settings and the mail provider accepting the connection. Failed email delivery does not prevent login.

## 5. Security notes

- TOTP secrets are encrypted in SQLite using `AUTH_ENCRYPTION_KEY`.
- Existing Admin and Analyst accounts are required to enroll before they can complete their next login.
- Ordinary User login behavior and the existing login rate limits remain unchanged.
- Back up `AUTH_ENCRYPTION_KEY` separately from the database. Losing it makes stored TOTP secrets unusable.
