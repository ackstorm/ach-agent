# Security Policy

## Supported Versions

We release patches for security vulnerabilities. Which versions are eligible for receiving such patches depends on the CVSS v3.0 Rating:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

The ach-agent team and community take security bugs seriously. We appreciate your efforts to responsibly disclose your findings, and will make every effort to acknowledge your contributions.

### How to Report a Security Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use GitHub's private vulnerability reporting:

**[Submit a Private Security Advisory](https://github.com/ackstorm/ach-agent/security/advisories/new)**

### What to Include

Please include the following information in your report:

- Type of issue (e.g. buffer overflow, SQL injection, cross-site scripting, etc.)
- Full paths of source file(s) related to the manifestation of the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit the issue

### Response Timeline

- **Initial Response**: We will acknowledge receipt of your vulnerability report within 48 hours
- **Status Updates**: We will send you regular updates about our progress, at least every 7 days
- **Resolution**: We aim to resolve critical vulnerabilities within 30 days of the initial report

### Security Response Process

1. **Triage**: The security team will assess the severity and impact of the vulnerability
2. **Investigation**: We will investigate and develop a fix
3. **Disclosure**: We will coordinate the release of the fix and public disclosure
4. **Recognition**: We will acknowledge your contribution (unless you prefer to remain anonymous)

### Security Best Practices

When deploying ach-agent, we recommend:

- **Principle of Least Privilege**: Run the service with minimal required permissions
- **Network Security**: Use network policies to restrict service network access
- **Regular Updates**: Keep the service and its dependencies up to date
- **Monitoring**: Monitor service logs for suspicious activity
- **Secrets Management**: Use Kubernetes secrets or external secret management systems for sensitive data

> **Note on secrets:** ach-agent treats `ek_`/`ALITELLM`-style bearer/token values as
> secrets. These are read from mounted file paths at use time (the rendered config carries
> paths, never values) and **must never be logged**. Do not echo, print, or include such
> tokens in log lines, error messages, traces, or test fixtures.

### Vulnerability Disclosure Policy

- We will provide advance notification to users about security updates when possible
- Security advisories will be published on GitHub Security Advisories
- We follow a coordinated disclosure timeline of 90 days from initial report to public disclosure
