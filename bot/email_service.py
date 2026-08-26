"""SMTP Email Dispatcher for AlgoPaca."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("algopaca-email")

# Load .env if not loaded
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.is_file():
        load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass


from bot.env_store import upsert_env_values


def get_smtp_config(masked: bool = False) -> dict[str, Any]:
    """Retrieve SMTP configuration from environment."""
    host = os.getenv("SMTP_HOST", "").strip()
    port_raw = os.getenv("SMTP_PORT", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM_EMAIL", "").strip() or username
    sender_name = os.getenv("SMTP_SENDER_NAME", "").strip() or "AlgoPaca"
    use_ssl_str = os.getenv("SMTP_USE_SSL", "").strip().lower()

    port = int(port_raw) if port_raw.isdigit() else 587
    # An explicit saved choice always wins. Deriving it from the port would
    # silently re-check the box every time someone unticks SSL on port 465.
    if use_ssl_str:
        use_ssl = use_ssl_str in {"true", "1", "yes"}
    else:
        use_ssl = port == 465
    is_configured = bool(host and (username or port == 25))

    masked_password = ("••••••••" if password else "") if masked else password

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": masked_password,
        "has_password": bool(password),
        "from_email": from_email,
        "sender_name": sender_name,
        "use_ssl": use_ssl,
        "configured": is_configured,
    }


def save_smtp_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Save SMTP configuration updates to .env and sync process."""
    current_pass = os.getenv("SMTP_PASSWORD", "").strip()
    new_pass = str(updates.get("password", "")).strip()

    # If the new password is empty or was sent as mask, keep current
    if not new_pass or new_pass.startswith("••••"):
        final_pass = current_pass
    else:
        final_pass = new_pass

    host = str(updates.get("host", "")).strip()
    port = str(updates.get("port", "587")).strip()
    username = str(updates.get("username", "")).strip()
    from_email = str(updates.get("from_email", "")).strip() or username
    sender_name = str(updates.get("sender_name", "")).strip() or "AlgoPaca"
    use_ssl = bool(updates.get("use_ssl", False))

    env_map = {
        "SMTP_HOST": host,
        "SMTP_PORT": port,
        "SMTP_USERNAME": username,
        "SMTP_PASSWORD": final_pass,
        "SMTP_FROM_EMAIL": from_email,
        "SMTP_SENDER_NAME": sender_name,
        "SMTP_USE_SSL": "true" if use_ssl else "false",
    }

    upsert_env_values(env_map)
    return get_smtp_config(masked=True)


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    timeout: int = 15,
) -> bool:
    """Send an email using configured SMTP settings."""
    config = get_smtp_config()
    if not config["configured"]:
        logger.warning("SMTP is not configured. Email will not be sent.")
        raise ValueError("SMTP email server is not configured on this desk.")

    sender_name = config.get("sender_name") or "AlgoPaca"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{config['from_email']}>"
    msg["To"] = to_email

    # Plaintext fallback
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    # HTML body if provided
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        if config["use_ssl"]:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=timeout)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=timeout)
            server.ehlo()
            if server.has_extn("STARTTLS"):
                server.starttls()
                server.ehlo()

        if config["username"] and config["password"]:
            server.login(config["username"], config["password"])

        server.sendmail(config["from_email"], [to_email], msg.as_string())
        server.quit()
        logger.info("Successfully sent email to %s (subject: %s)", to_email, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        raise ValueError(f"Failed to send email: {exc}") from exc


def render_test_email(to_email: str, host: str, lang: str = "en") -> tuple[str, str, str]:
    """Generate subject, plain text, and HTML body for SMTP test message."""
    is_bn = str(lang).strip().lower() == "bn"
    timestamp = os.getenv("TZ", "") or "UTC"
    import datetime as _dt
    now_str = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if is_bn:
        subject = "AlgoPaca — SMTP টেস্ট ইমেইল সফল হয়েছে"
        body_text = f"""নমস্কার,

আপনার AlgoPaca ট্রেডিং ডেস্কে SMTP কনফিগারেশন সফলভাবে সম্পন্ন হয়েছে।

টেস্ট রিপোর্ট:
- রিসিভার: {to_email}
- হোস্ট: {host}
- সময়: {now_str}

এই ইমেইলটি নির্দেশ করে যে আপনার সার্ভার স্বয়ংক্রিয় পাসওয়ার্ড রিসেট ও ট্রেডিং নোটিফিকেশন পাঠাতে সম্পূর্ণ প্রস্তুত।

ধন্যবাদান্তে,
AlgoPaca Trading Team
"""
        body_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>AlgoPaca SMTP Test</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f17; color: #e2e8f0; margin: 0; padding: 40px 20px;">
  <div style="max-width: 560px; margin: 0 auto; background: #131b2e; border: 1px solid #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
    <div style="background: linear-gradient(135deg, #0d9488 0%, #2563eb 100%); padding: 24px; text-align: center;">
      <h1 style="color: #ffffff; margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0.5px;">AlgoPaca · SMTP টেস্ট সফল</h1>
    </div>
    <div style="padding: 28px;">
      <p style="font-size: 15px; line-height: 1.6; color: #cbd5e1; margin-top: 0;">
        আপনার AlgoPaca ট্রেডিং ডেস্কে SMTP কনফিগারেশন সফলভাবে যাচাই করা হয়েছে।
      </p>
      <div style="background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin: 20px 0; font-family: monospace; font-size: 13px;">
        <div style="color: #38bdf8; margin-bottom: 6px;"><strong>রিসিভার:</strong> {html.escape(to_email)}</div>
        <div style="color: #38bdf8; margin-bottom: 6px;"><strong>এসএমটিপি হোস্ট:</strong> {html.escape(host)}</div>
        <div style="color: #94a3b8;"><strong>সময়:</strong> {html.escape(now_str)}</div>
      </div>
      <p style="font-size: 13px; color: #94a3b8; line-height: 1.5; margin-bottom: 0;">
        স্বয়ংক্রিয় পাসওয়ার্ড রিসেট ও ট্রেডিং এলার্ট নোটিফিকেশন সফলভাবে কাজ করবে।
      </p>
    </div>
    <div style="border-top: 1px solid #1e293b; padding: 14px 28px; font-size: 11px; color: #64748b; text-align: center;">
      AlgoPaca Autonomous Algorithmic Trading Systems
    </div>
  </div>
</body>
</html>"""
    else:
        subject = "AlgoPaca — SMTP Test Connection Successful"
        body_text = f"""Hello,

This is a test email confirming that your AlgoPaca trading desk SMTP configuration is working correctly.

Diagnostics:
- Recipient: {to_email}
- SMTP Host: {host}
- Dispatched At: {now_str}

Your AlgoPaca instance is now ready to dispatch automated security alerts and password reset notifications.

Best regards,
AlgoPaca Team
"""
        body_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>AlgoPaca SMTP Test</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f17; color: #e2e8f0; margin: 0; padding: 40px 20px;">
  <div style="max-width: 560px; margin: 0 auto; background: #131b2e; border: 1px solid #1e293b; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
    <div style="background: linear-gradient(135deg, #0d9488 0%, #2563eb 100%); padding: 24px; text-align: center;">
      <h1 style="color: #ffffff; margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0.5px;">AlgoPaca · SMTP Test Successful</h1>
    </div>
    <div style="padding: 28px;">
      <p style="font-size: 15px; line-height: 1.6; color: #cbd5e1; margin-top: 0;">
        Your SMTP settings for the AlgoPaca trading desk have been successfully verified.
      </p>
      <div style="background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin: 20px 0; font-family: monospace; font-size: 13px;">
        <div style="color: #38bdf8; margin-bottom: 6px;"><strong>Recipient:</strong> {html.escape(to_email)}</div>
        <div style="color: #38bdf8; margin-bottom: 6px;"><strong>SMTP Host:</strong> {html.escape(host)}</div>
        <div style="color: #94a3b8;"><strong>Timestamp:</strong> {html.escape(now_str)}</div>
      </div>
      <p style="font-size: 13px; color: #94a3b8; line-height: 1.5; margin-bottom: 0;">
        Your desk can now dispatch one-time password reset links and trading notifications securely.
      </p>
    </div>
    <div style="border-top: 1px solid #1e293b; padding: 14px 28px; font-size: 11px; color: #64748b; text-align: center;">
      AlgoPaca Autonomous Algorithmic Trading Systems
    </div>
  </div>
</body>
</html>"""

    return subject, body_text, body_html


def test_smtp_connection(
    test_to_email: str,
    custom_config: Optional[dict[str, Any]] = None,
    lang: str = "en",
    timeout: int = 12,
) -> dict[str, Any]:
    """Perform a step-by-step diagnostic test of SMTP connection and send a test message."""
    logs: list[dict[str, Any]] = []

    def _log(step: str, status: str, detail: str) -> None:
        logs.append({"step": step, "status": status, "detail": detail})

    config = get_smtp_config()
    if custom_config:
        # Merge custom params if provided
        for k in ("host", "port", "username", "from_email", "sender_name", "use_ssl"):
            if k in custom_config:
                config[k] = custom_config[k]
        if "password" in custom_config and custom_config["password"] and not custom_config["password"].startswith("••••"):
            config["password"] = custom_config["password"]

    host = config.get("host", "").strip()
    port = int(config.get("port", 587))
    use_ssl = bool(config.get("use_ssl", False))
    username = config.get("username", "").strip()
    password = config.get("password", "")
    from_email = config.get("from_email", "").strip() or username
    sender_name = config.get("sender_name", "") or "AlgoPaca"

    _log("init", "info", f"Beginning SMTP diagnostics for host: {host or '(empty)'}:{port}")

    if not host:
        _log("validate", "error", "SMTP Host cannot be empty.")
        return {"ok": False, "error": "SMTP Host is required.", "logs": logs}

    if not test_to_email:
        _log("validate", "error", "Test recipient email address is required.")
        return {"ok": False, "error": "Recipient email is required.", "logs": logs}

    server = None
    try:
        # Step 1: Connect
        if use_ssl:
            _log("connect", "info", f"Establishing SSL socket connection to {host}:{port}...")
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
            _log("connect", "ok", f"SSL connection established with {host}:{port}")
        else:
            _log("connect", "info", f"Connecting to {host}:{port}...")
            server = smtplib.SMTP(host, port, timeout=timeout)
            _log("connect", "ok", f"TCP socket connected to {host}:{port}")

            _log("ehlo", "info", "Sending EHLO greeting...")
            server.ehlo()
            _log("ehlo", "ok", "EHLO greeting accepted")

            if server.has_extn("STARTTLS"):
                _log("tls", "info", "Server supports STARTTLS. Initiating TLS handshake...")
                server.starttls()
                server.ehlo()
                _log("tls", "ok", "TLS encryption active")
            else:
                _log("tls", "warn", "Server does not advertise STARTTLS (plain connection).")

        # Step 2: Authenticate
        if username and password:
            _log("auth", "info", f"Authenticating as {username}...")
            server.login(username, password)
            _log("auth", "ok", f"Authentication successful for user {username}")
        elif username and not password:
            _log("auth", "warn", f"Username specified ({username}) but no password provided.")
        else:
            _log("auth", "info", "No credentials provided. Proceeding without authentication.")

        # Step 3: Dispatch Test Email
        _log("send", "info", f"Rendering test message to {test_to_email}...")
        subject, body_text, body_html = render_test_email(test_to_email, host, lang=lang)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{sender_name} <{from_email}>"
        msg["To"] = test_to_email
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        _log("send", "info", f"Sending test envelope from {from_email} to {test_to_email}...")
        server.sendmail(from_email, [test_to_email], msg.as_string())
        _log("send", "ok", f"Message accepted for delivery by {host}")

        server.quit()
        _log("finish", "ok", "SMTP diagnostic test completed successfully!")
        return {"ok": True, "message": "Test email sent successfully!", "logs": logs}

    except smtplib.SMTPAuthenticationError as exc:
        _log("auth", "error", f"Authentication failed: {exc.smtp_error.decode('utf-8', errors='ignore') if isinstance(exc.smtp_error, bytes) else exc.smtp_error}")
        return {"ok": False, "error": "SMTP Authentication Failed. Check username and password.", "logs": logs}
    except smtplib.SMTPConnectError as exc:
        _log("connect", "error", f"Connection error: {exc}")
        return {"ok": False, "error": f"Failed to connect to SMTP server: {exc}", "logs": logs}
    except smtplib.SMTPServerDisconnected as exc:
        _log("disconnect", "error", f"Server disconnected unexpectedly: {exc}")
        return {"ok": False, "error": "Server disconnected unexpectedly.", "logs": logs}
    except Exception as exc:
        _log("error", "error", f"SMTP error: {str(exc)}")
        return {"ok": False, "error": f"SMTP test failed: {exc}", "logs": logs}
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass


test_smtp_connection.__test__ = False


import html


def render_password_reset_email(
    username: str,
    reset_url: str,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Generate (subject, body_text, body_html) for password reset email."""
    is_bn = str(lang).strip().lower() == "bn"
    safe_username = html.escape(str(username or "").strip())
    safe_url = html.escape(str(reset_url or "").strip())

    if is_bn:
        subject = "AlgoPaca - পাসওয়ার্ড রিসেট লিংক"
        body_text = f"""প্রিয় {username},

আমরা আপনার AlgoPaca ট্রেডিং ডেস্ক অ্যাকাউন্টের পাসওয়ার্ড রিসেট করার একটি অনুরোধ পেয়েছি।

নতুন পাসওয়ার্ড নির্ধারণ করতে নিচের লিঙ্কে ক্লিক করুন:
{reset_url}

অথবা এই লিঙ্কটি কপি করে ব্রাউজারে পেস্ট করুন:
{reset_url}

⚠️ নিরাপত্তা বিজ্ঞপ্তি: এই লিঙ্কটির মেয়াদ ৩০ মিনিট। আপনি যদি পাসওয়ার্ড রিসেটের অনুরোধ না করে থাকেন, তবে নির্দ্বিধায় এই ইমেইলটি উপেক্ষা করুন — আপনার অ্যাকাউন্ট সম্পূর্ণ সুরক্ষিত রয়েছে।

ধন্যবাদ,
AlgoPaca Automated Quantitative Trading Desk
"""
        body_html = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="bn">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="dark" />
  <meta name="supported-color-schemes" content="dark" />
  <title>AlgoPaca - পাসওয়ার্ড রিসেট অনুরোধ</title>
  <style type="text/css">
    body {{ margin: 0 !important; padding: 0 !important; background-color: #080d14 !important; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif !important; color: #cbd5e1 !important; -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table {{ border-collapse: collapse !important; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    a {{ color: #d4894c; text-decoration: none; }}
    @media only screen and (max-width: 600px) {{
      .email-card {{ width: 100% !important; border-radius: 10px !important; }}
      .email-card-inner {{ padding: 24px 18px !important; }}
      .email-title {{ font-size: 19px !important; }}
      .email-btn {{ width: 100% !important; text-align: center !important; padding: 14px 18px !important; box-sizing: border-box !important; }}
    }}
  </style>
</head>
<body style="margin: 0; padding: 0; background-color: #080d14; color: #cbd5e1; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <!-- Canvas Table -->
  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #080d14; min-height: 100%; padding: 40px 16px 48px 16px; margin: 0;">
    <tr>
      <td align="center" valign="top">
        <!-- Main Card Container -->
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" class="email-card" style="width: 100%; max-width: 540px; background-color: #141c28; border: 1px solid #223044; border-radius: 14px; overflow: hidden; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);">
          <!-- Top Accent Gradient Line -->
          <tr>
            <td style="height: 4px; background: linear-gradient(90deg, #f59e0b 0%, #d4894c 50%, #10b981 100%); background-color: #d4894c; font-size: 1px; line-height: 1px;">&nbsp;</td>
          </tr>
          <!-- Card Content Body -->
          <tr>
            <td class="email-card-inner" style="padding: 34px 32px 32px 32px;">
              <!-- Brand Header Table -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 28px; border-bottom: 1px solid #1f2c3e; padding-bottom: 22px;">
                <tr>
                  <td valign="middle" style="width: 48px; padding-right: 14px;">
                    <div style="width: 44px; height: 44px; border-radius: 11px; overflow: hidden; box-shadow: 0 0 14px rgba(212, 137, 76, 0.28); line-height: 0;">
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="44" height="44" style="display: block; width: 44px; height: 44px;">
                        <defs>
                          <linearGradient id="favBg_bn" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#141C2B"/>
                            <stop offset="100%" stop-color="#080C14"/>
                          </linearGradient>
                          <linearGradient id="favBorder_bn" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#F59E0B"/>
                            <stop offset="100%" stop-color="#10B981"/>
                          </linearGradient>
                          <linearGradient id="favGold_bn" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#FEF08A"/>
                            <stop offset="100%" stop-color="#D97706"/>
                          </linearGradient>
                          <linearGradient id="favGreen_bn" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#6EE7B7"/>
                            <stop offset="100%" stop-color="#059669"/>
                          </linearGradient>
                        </defs>
                        <rect x="2" y="2" width="60" height="60" rx="14" fill="url(#favBg_bn)" stroke="url(#favBorder_bn)" stroke-width="1.5"/>
                        <g transform="translate(1, 0)">
                          <polygon points="25,23 26,10 32,20 29,26" fill="#B45309"/>
                          <polygon points="30,22 32,9 38,19 35,25" fill="url(#favGold_bn)"/>
                          <polygon points="25,24 35,24 43,28 47,33 42,37 36,33 33,38 25,34" fill="url(#favGold_bn)"/>
                          <polygon points="43,28 49,32 46,36 42,37" fill="#78350F"/>
                          <circle cx="39" cy="30" r="1.8" fill="#38BDF8"/>
                          <circle cx="39" cy="30" r="0.8" fill="#FFFFFF"/>
                          <polygon points="25,34 33,38 31,52 23,52" fill="#D97706"/>
                          <polygon points="33,38 41,40 38,52 31,52" fill="url(#favGreen_bn)"/>
                          <polygon points="17,40 25,34 23,52 16,52" fill="#78350F"/>
                          <polyline points="14,48 24,42 32,45 44,32 51,24" fill="none" stroke="#FDE047" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                          <circle cx="51" cy="24" r="2.2" fill="#F59E0B" stroke="#FEF08A" stroke-width="1"/>
                        </g>
                      </svg>
                    </div>
                  </td>
                  <td valign="middle">
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 23px; font-weight: 800; line-height: 1.15; letter-spacing: -0.5px;">
                      <span style="color: #ffffff;">Algo</span><span style="color: #f59e0b;">Paca</span>
                    </div>
                    <div style="font-family: 'IBM Plex Mono', 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 9.5px; font-weight: 600; color: #94a3b8; letter-spacing: 1.4px; text-transform: uppercase; margin-top: 3px;">
                      QUANTITATIVE TRADING DESK <span style="color: #10b981; font-size: 10px;">●</span> <span style="color: #34d399;">ALPACA</span>
                    </div>
                  </td>
                </tr>
              </table>

              <!-- Main Heading -->
              <h1 class="email-title" style="margin: 0 0 16px 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 20px; font-weight: 700; color: #f8fafc; letter-spacing: -0.3px; line-height: 1.35;">
                পাসওয়ার্ড রিসেট অনুরোধ
              </h1>

              <!-- Greeting & Body -->
              <p style="margin: 0 0 14px 0; font-size: 14.5px; color: #cbd5e1; line-height: 1.6;">
                প্রিয় <strong style="color: #ffffff; font-weight: 600;">{safe_username}</strong>,
              </p>
              <p style="margin: 0 0 24px 0; font-size: 14px; color: #94a3b8; line-height: 1.65;">
                আমরা আপনার AlgoPaca অ্যাকাউন্টের পাসওয়ার্ড পরিবর্তনের একটি অনুরোধ পেয়েছি। আপনার অ্যাকাউন্টের জন্য একটি নতুন ও শক্তিশালী পাসওয়ার্ড সেট করতে নিচের বোতামে ক্লিক করুন:
              </p>

              <!-- Call To Action Button (Bulletproof Table) -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin: 26px 0;">
                <tr>
                  <td align="center" style="border-radius: 8px; background-color: #d4894c; background: linear-gradient(135deg, #f59e0b 0%, #d4894c 55%, #b56a32 100%); box-shadow: 0 4px 16px rgba(212, 137, 76, 0.38);">
                    <a href="{safe_url}" target="_blank" rel="noopener noreferrer" class="email-btn" style="display: inline-block; padding: 13px 30px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 14px; font-weight: 700; color: #120b05 !important; text-decoration: none; border-radius: 8px; letter-spacing: 0.2px; text-transform: uppercase;">
                      পাসওয়ার্ড রিসেট করুন &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Fallback Direct Link Container -->
              <div style="margin-top: 26px; padding-top: 20px; border-top: 1px solid #1e2a3b;">
                <p style="margin: 0 0 8px 0; font-size: 12.5px; color: #94a3b8; line-height: 1.4;">
                  বোতামে সমস্যা হলে এই সরাসরি লিঙ্কটি কপি করে আপনার ব্রাউজারে পেস্ট করুন:
                </p>
                <div style="background-color: #0b1118; border: 1px solid #1f2c3d; border-radius: 6px; padding: 11px 13px; word-break: break-all;">
                  <a href="{safe_url}" target="_blank" rel="noopener noreferrer" style="font-family: 'IBM Plex Mono', 'SFMono-Regular', Consolas, Menlo, Monaco, monospace; font-size: 11.5px; color: #38bdf8; text-decoration: none; line-height: 1.5;">
                    {safe_url}
                  </a>
                </div>
              </div>

              <!-- Security Callout Notice -->
              <div style="margin-top: 22px; background-color: #121820; border: 1px solid #2e2619; border-left: 4px solid #f59e0b; border-radius: 0 8px 8px 0; padding: 13px 15px;">
                <div style="font-size: 11.5px; font-weight: 700; color: #f59e0b; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 3px;">
                  ⚠️ নিরাপত্তা বিজ্ঞপ্তি
                </div>
                <p style="margin: 0; font-size: 12.5px; color: #cbd5e1; line-height: 1.55;">
                  এই লিঙ্কটির মেয়াদ <strong>৩০ মিনিট</strong>। আপনি যদি পাসওয়ার্ড রিসেটের অনুরোধ না করে থাকেন, তবে নির্দ্বিধায় এই ইমেইলটি উপেক্ষা করুন &mdash; আপনার অ্যাকাউন্ট সম্পূর্ণ সুরক্ষিত রয়েছে।
                </p>
              </div>

              <!-- Footer Section with Proper Separator Spacing -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-top: 36px;">
                <tr>
                  <td style="border-top: 1px solid #1f2c3e; height: 1px; font-size: 1px; line-height: 1px;">&nbsp;</td>
                </tr>
                <tr>
                  <td align="center" style="padding-top: 26px; padding-bottom: 4px;">
                    <div style="font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; color: #cbd5e1; letter-spacing: 0.4px; margin-bottom: 6px;">
                      AlgoPaca Automated Quantitative Trading Desk
                    </div>
                    <div style="font-size: 11px; color: #64748b; line-height: 1.5; margin-bottom: 6px;">
                      অ্যালগরিদমিক এক্সিকিউশন &bull; রিয়েল-টাইম মার্কেট টেলিমেট্রি &bull; ঝুঁকি ব্যবস্থাপনা
                    </div>
                    <div style="font-size: 10px; color: #475569; line-height: 1.4;">
                      এটি একটি স্বয়ংক্রিয় সিস্টেম বিজ্ঞপ্তি। &bull; &copy; ২০২৬ AlgoPaca। সর্বস্বত্ব সংরক্ষিত।
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    else:
        subject = "AlgoPaca - Password Reset Request"
        body_text = f"""Hello {username},

We received a request to reset the password for your AlgoPaca trading desk account.

Click the link below to choose a new password:
{reset_url}

Or copy and paste this link into your browser:
{reset_url}

⚠️ Security Notice: This link is valid for 30 minutes. If you did not request a password reset, you can safely ignore this email — your account credentials remain unchanged.

Best regards,
AlgoPaca Automated Quantitative Trading Desk
"""
        body_html = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="dark" />
  <meta name="supported-color-schemes" content="dark" />
  <title>AlgoPaca - Password Reset Request</title>
  <style type="text/css">
    body {{ margin: 0 !important; padding: 0 !important; background-color: #080d14 !important; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif !important; color: #cbd5e1 !important; -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table {{ border-collapse: collapse !important; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
    a {{ color: #d4894c; text-decoration: none; }}
    @media only screen and (max-width: 600px) {{
      .email-card {{ width: 100% !important; border-radius: 10px !important; }}
      .email-card-inner {{ padding: 24px 18px !important; }}
      .email-title {{ font-size: 19px !important; }}
      .email-btn {{ width: 100% !important; text-align: center !important; padding: 14px 18px !important; box-sizing: border-box !important; }}
    }}
  </style>
</head>
<body style="margin: 0; padding: 0; background-color: #080d14; color: #cbd5e1; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <!-- Canvas Table -->
  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #080d14; min-height: 100%; padding: 40px 16px 48px 16px; margin: 0;">
    <tr>
      <td align="center" valign="top">
        <!-- Main Card Container -->
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" class="email-card" style="width: 100%; max-width: 540px; background-color: #141c28; border: 1px solid #223044; border-radius: 14px; overflow: hidden; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);">
          <!-- Top Accent Gradient Line -->
          <tr>
            <td style="height: 4px; background: linear-gradient(90deg, #f59e0b 0%, #d4894c 50%, #10b981 100%); background-color: #d4894c; font-size: 1px; line-height: 1px;">&nbsp;</td>
          </tr>
          <!-- Card Content Body -->
          <tr>
            <td class="email-card-inner" style="padding: 34px 32px 32px 32px;">
              <!-- Brand Header Table -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 28px; border-bottom: 1px solid #1f2c3e; padding-bottom: 22px;">
                <tr>
                  <td valign="middle" style="width: 48px; padding-right: 14px;">
                    <div style="width: 44px; height: 44px; border-radius: 11px; overflow: hidden; box-shadow: 0 0 14px rgba(212, 137, 76, 0.28); line-height: 0;">
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="44" height="44" style="display: block; width: 44px; height: 44px;">
                        <defs>
                          <linearGradient id="favBg_en" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#141C2B"/>
                            <stop offset="100%" stop-color="#080C14"/>
                          </linearGradient>
                          <linearGradient id="favBorder_en" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#F59E0B"/>
                            <stop offset="100%" stop-color="#10B981"/>
                          </linearGradient>
                          <linearGradient id="favGold_en" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#FEF08A"/>
                            <stop offset="100%" stop-color="#D97706"/>
                          </linearGradient>
                          <linearGradient id="favGreen_en" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#6EE7B7"/>
                            <stop offset="100%" stop-color="#059669"/>
                          </linearGradient>
                        </defs>
                        <rect x="2" y="2" width="60" height="60" rx="14" fill="url(#favBg_en)" stroke="url(#favBorder_en)" stroke-width="1.5"/>
                        <g transform="translate(1, 0)">
                          <polygon points="25,23 26,10 32,20 29,26" fill="#B45309"/>
                          <polygon points="30,22 32,9 38,19 35,25" fill="url(#favGold_en)"/>
                          <polygon points="25,24 35,24 43,28 47,33 42,37 36,33 33,38 25,34" fill="url(#favGold_en)"/>
                          <polygon points="43,28 49,32 46,36 42,37" fill="#78350F"/>
                          <circle cx="39" cy="30" r="1.8" fill="#38BDF8"/>
                          <circle cx="39" cy="30" r="0.8" fill="#FFFFFF"/>
                          <polygon points="25,34 33,38 31,52 23,52" fill="#D97706"/>
                          <polygon points="33,38 41,40 38,52 31,52" fill="url(#favGreen_en)"/>
                          <polygon points="17,40 25,34 23,52 16,52" fill="#78350F"/>
                          <polyline points="14,48 24,42 32,45 44,32 51,24" fill="none" stroke="#FDE047" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                          <circle cx="51" cy="24" r="2.2" fill="#F59E0B" stroke="#FEF08A" stroke-width="1"/>
                        </g>
                      </svg>
                    </div>
                  </td>
                  <td valign="middle">
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 23px; font-weight: 800; line-height: 1.15; letter-spacing: -0.5px;">
                      <span style="color: #ffffff;">Algo</span><span style="color: #f59e0b;">Paca</span>
                    </div>
                    <div style="font-family: 'IBM Plex Mono', 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 9.5px; font-weight: 600; color: #94a3b8; letter-spacing: 1.4px; text-transform: uppercase; margin-top: 3px;">
                      QUANTITATIVE TRADING DESK <span style="color: #10b981; font-size: 10px;">●</span> <span style="color: #34d399;">ALPACA</span>
                    </div>
                  </td>
                </tr>
              </table>

              <!-- Main Heading -->
              <h1 class="email-title" style="margin: 0 0 16px 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 20px; font-weight: 700; color: #f8fafc; letter-spacing: -0.3px; line-height: 1.35;">
                Password Reset Request
              </h1>

              <!-- Greeting & Body -->
              <p style="margin: 0 0 14px 0; font-size: 14.5px; color: #cbd5e1; line-height: 1.6;">
                Hello <strong style="color: #ffffff; font-weight: 600;">{safe_username}</strong>,
              </p>
              <p style="margin: 0 0 24px 0; font-size: 14px; color: #94a3b8; line-height: 1.65;">
                We received a request to reset the password for your AlgoPaca trading desk account. Click the button below to choose a new secure password:
              </p>

              <!-- Call To Action Button (Bulletproof Table) -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin: 26px 0;">
                <tr>
                  <td align="center" style="border-radius: 8px; background-color: #d4894c; background: linear-gradient(135deg, #f59e0b 0%, #d4894c 55%, #b56a32 100%); box-shadow: 0 4px 16px rgba(212, 137, 76, 0.38);">
                    <a href="{safe_url}" target="_blank" rel="noopener noreferrer" class="email-btn" style="display: inline-block; padding: 13px 30px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 14px; font-weight: 700; color: #120b05 !important; text-decoration: none; border-radius: 8px; letter-spacing: 0.2px; text-transform: uppercase;">
                      Reset Password &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Fallback Direct Link Container -->
              <div style="margin-top: 26px; padding-top: 20px; border-top: 1px solid #1e2a3b;">
                <p style="margin: 0 0 8px 0; font-size: 12.5px; color: #94a3b8; line-height: 1.4;">
                  Or copy and paste this URL into your browser:
                </p>
                <div style="background-color: #0b1118; border: 1px solid #1f2c3d; border-radius: 6px; padding: 11px 13px; word-break: break-all;">
                  <a href="{safe_url}" target="_blank" rel="noopener noreferrer" style="font-family: 'IBM Plex Mono', 'SFMono-Regular', Consolas, Menlo, Monaco, monospace; font-size: 11.5px; color: #38bdf8; text-decoration: none; line-height: 1.5;">
                    {safe_url}
                  </a>
                </div>
              </div>

              <!-- Security Callout Notice -->
              <div style="margin-top: 22px; background-color: #121820; border: 1px solid #2e2619; border-left: 4px solid #f59e0b; border-radius: 0 8px 8px 0; padding: 13px 15px;">
                <div style="font-size: 11.5px; font-weight: 700; color: #f59e0b; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 3px;">
                  ⚠️ Security Notice
                </div>
                <p style="margin: 0; font-size: 12.5px; color: #cbd5e1; line-height: 1.55;">
                  This link expires in <strong>30 minutes</strong>. If you did not request a password reset, you can safely ignore this email &mdash; your account credentials remain unchanged.
                </p>
              </div>

              <!-- Footer Section with Proper Separator Spacing -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-top: 36px;">
                <tr>
                  <td style="border-top: 1px solid #1f2c3e; height: 1px; font-size: 1px; line-height: 1px;">&nbsp;</td>
                </tr>
                <tr>
                  <td align="center" style="padding-top: 26px; padding-bottom: 4px;">
                    <div style="font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; color: #cbd5e1; letter-spacing: 0.4px; margin-bottom: 6px;">
                      AlgoPaca Automated Quantitative Trading Desk
                    </div>
                    <div style="font-size: 11px; color: #64748b; line-height: 1.5; margin-bottom: 6px;">
                      Algorithmic execution &bull; Real-time telemetry &bull; Risk management
                    </div>
                    <div style="font-size: 10px; color: #475569; line-height: 1.4;">
                      This is an automated security notification. &bull; &copy; 2026 AlgoPaca. All rights reserved.
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return subject, body_text, body_html


def send_password_reset_email(
    to_email: str,
    username: str,
    reset_url: str,
    lang: str = "en",
) -> bool:
    """Send password reset email with branded template."""
    subject, body_text, body_html = render_password_reset_email(
        username=username,
        reset_url=reset_url,
        lang=lang,
    )

    return send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )


