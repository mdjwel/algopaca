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


def get_smtp_config() -> dict[str, Any]:
    """Retrieve SMTP configuration from environment."""
    host = os.getenv("SMTP_HOST", "").strip()
    port_raw = os.getenv("SMTP_PORT", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM_EMAIL", "").strip() or username
    use_ssl_str = os.getenv("SMTP_USE_SSL", "").strip().lower()

    port = int(port_raw) if port_raw.isdigit() else 587
    use_ssl = use_ssl_str in {"true", "1", "yes"} or port == 465

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_email": from_email,
        "use_ssl": use_ssl,
        "configured": bool(host and (username or port == 25)),
    }


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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AlgoPaca <{config['from_email']}>"
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
        raise ValueError("Failed to send email. Please check your SMTP settings.") from exc


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


