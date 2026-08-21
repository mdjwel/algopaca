# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |

---

## Reporting a Vulnerability

The security of AlgoPaca and users' financial trading desks is of paramount importance.

If you discover a security vulnerability, please **DO NOT** create a public GitHub issue. Instead, report it privately to the repository maintainers via:

- **GitHub Private Vulnerability Reporting**: Use the "Report a vulnerability" button under the **Security** tab of this repository.

Please include the following details in your report:
1. Description of the vulnerability.
2. Steps to reproduce the issue (proof-of-concept script or payload).
3. Potential impact.
4. Suggested remediation if available.

We will acknowledge receipt of your vulnerability report within 48 hours and provide regular updates until a fix is released.

---

## Best Practices for Trading Desk Security

- **Keep API Keys Confidential**: Never commit `.env` files or hardcode API keys in any code.
- **Use Paper Trading for Testing**: Never test untrusted strategies or experimental code with live money.
- **HTTPS in Production**: If deploying AlgoPaca over the public internet, always place it behind a secure reverse proxy (e.g., Nginx, Caddy, Cloudflare) with valid SSL/TLS certificates.

