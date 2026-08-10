# Security Policy

## Scope

3CAN is a **source-available prototype**, not a hardened production system. This document describes the current v0.2 release-candidate threat model and how to report security issues.

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.**

Use a GitHub Private Vulnerability Report: open the repository's **Security**
tab, select **Advisories**, then **Report a vulnerability**. If that control is
temporarily unavailable, ask the maintainer through the GitHub profile to
establish private contact. Do not disclose vulnerability details in a public
issue.

Please include:
- Affected version / commit
- Reproduction steps
- Impact assessment (what data exposed, what action becomes possible)
- Any suggested fix (optional)

## Disclosure window

- **Initial acknowledgement**: within 48 hours
- **Triage + severity classification**: within 7 days
- **Fix target**: 30 days for high-severity, 90 days for medium/low
- **Public disclosure**: coordinated with reporter, typically after fix lands in a release; reporter credited unless they prefer anonymity

## Threat model (what the current candidate does and does not protect)

### What is protected

- **Localhost-only default binding**: backend (`app.py`) and proxy (`server.py`) both default `--host 127.0.0.1`. Passing `--host 0.0.0.0` prints an explicit security warning on startup.
- **Hash-chain audit trail**: `activity_log` entries are SHA-256 chained; `/api/audit/verify` detects tampering within the current 500-entry window.
- **Sentinel bootstrap pattern is logged**: every bypass of the PreToolUse gate (when `~/.claude/logs/3can-gate-bootstrap` sentinel file exists) is appended to `~/.claude/logs/3can-gate.jsonl`. You can audit any bypass event after the fact.

### What is NOT protected

1. **No authentication on the HTTP API.** Once a client reaches the port, it can read and write the entire knowledge graph. This is why localhost-only is the default.
2. **No authorization / RBAC.** All agents with API access have the same rights.
3. **No encryption at rest.** Node JSON files in `graph/nodes/` are plain text. Do not store secrets in node content.
4. **Hash chain is window-limited.** Entries older than 500 events are truncated; the chain cannot detect tampering before the current window.
5. **No input sanitization on node content.** Assume node content is trusted (you wrote it yourself or your agent wrote it).
6. **Behavioral Gate Stage 2 relies on an external LLM.** If the LLM is compromised or misused, gate content-judgment can be bypassed.
7. **Sentinel bootstrap mechanism is a documented security tradeoff.** Presence of `~/.claude/logs/3can-gate-bootstrap` bypasses PreToolUse gate entirely. You must actively delete this file after initial setup. See [DEPLOYMENT.md §1.7](./DEPLOYMENT.md).

### What users MUST do

- Never commit `~/.claude/secrets.json` to git. The `.gitignore` excludes it by default.
- Never place API keys or passwords in node content (`content.description`, `content.notes`, `activation_keywords`). Use `SEC-*` prefix nodes with only a reference name, not the secret itself.
- Keep `--host 127.0.0.1` unless on a trusted isolated network. If you must expose, run behind a reverse proxy with authentication (e.g. Caddy + basic auth + TLS).
- After any sentinel bootstrap event, **verify `~/.claude/logs/3can-gate-bootstrap` is deleted**. Consider adding a cron / systemd check.
- Review `~/.claude/logs/3can-gate.jsonl` periodically for unexpected bootstrap-bypass entries.

### Known by maintainer, not yet fixed

- No rate limiting on the HTTP API (trivially DoS-able if exposed).
- Optional local `secrets.json` files are plaintext; prefer environment variables or an OS secret store and never commit the file.
- No sandboxing on LLM tool subprocess calls (they run with the same user privileges).

## Supported versions

Until the first public tag, only the latest commit on `main` is supported. The
v0.2 candidate is unreleased; older internal v9.x snapshots are unsupported and
were never public releases.

## Out of scope

- Bugs in dependencies (BGE-M3, bge-reranker, FastAPI, uvicorn, etc.) should be reported upstream. We will track them in our issue tracker but not fix them here.
- Social engineering or physical-access threats are explicitly out of scope for a local-first tool.

## Thank you

We take security reports seriously and appreciate every one. Even if a reported issue turns out to be documented / expected behavior, the report still helps us improve documentation clarity. All constructive reports will be acknowledged.
