# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by emailing security@sageox.com.

Please do not open a public GitHub issue for security vulnerabilities.

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial assessment**: Within 7 days
- **Resolution timeline**: Provided after assessment

We will work with you to understand and address the issue. Once resolved, we will coordinate disclosure timing with you.

## Security Considerations

This library handles CLI error detection and correction. When using frictionx:

- **Telemetry**: The library can collect friction events. Ensure telemetry endpoints are trusted and use HTTPS.
- **Secret redaction**: Built-in redactors strip sensitive data before emission. Always enable redaction in production.
- **Auto-execution**: The library can auto-execute corrected commands. Only enable auto-execute for high-confidence suggestions from trusted catalogs.
- **Catalog data**: Command catalogs may be fetched from remote servers. Validate catalog sources.

## Best Practices

When using frictionx in your applications:

1. Enable secret redaction for all friction events
2. Use HTTPS for telemetry endpoints
3. Set appropriate confidence thresholds for auto-execution
4. Keep the library updated to the latest version
5. Review catalog entries before deployment
