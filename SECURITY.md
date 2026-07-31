# Security

## Sensitive data

The automation reads the local Messages database and writes plaintext exports.
Treat every generated archive as highly sensitive. Never attach an export,
diagnostics from an unfamiliar version, `local.yml`, or recovered media to a
public issue.

The repository's `.gitignore` excludes common export paths, but ignore rules are
not a substitute for reviewing `git status` before every commit.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's security advisory
feature rather than a public issue. Do not include real messages, contact
details, or attachments in a report.
