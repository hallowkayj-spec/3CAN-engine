# GitHub PR Harness

This harness turns a repeated GitHub PR failure into an enforceable local
workflow instead of another activity-log note.

## Problem

Some agent shells try `gh pr create` or a GitHub connector even when:

- the GitHub CLI is not installed,
- a private repository returns connector `404`,
- the correct path is already documented as local Git Credential Manager or
  token-backed REST API creation.

A documentation node alone is not enough. The project graph needs a recallable
`ERR-*` node, and hooks need to stop the wrong action before it repeats.

## Files

- `examples/codex-cli-project-kit/scripts/3can_pr_harness.py`
- `examples/codex-cli-project-kit/.codex/hooks.json`
- `neural-memory/backend/seed_nodes.py`

Seed node:

- `ERR-20260508-github-pr-local-rest-fallback-required`

## Standard Flow

Run from the target project root after copying the Codex project kit:

```powershell
python scripts\3can_pr_harness.py check
git push -u origin <branch>
python scripts\3can_pr_harness.py create-pr `
  --approval-id <user-approval-id> `
  --title "PR title" `
  --body-file .\PR_BODY.md `
  --base main
```

The harness reads `GITHUB_TOKEN`, `GH_TOKEN`, or Git Credential Manager/wincred
in memory. It does not print, write, or persist token values.

## Hook Behavior

The Codex `PreToolUse` hook:

- blocks GitHub connector PR creation because that path has repeatedly failed
  on private repositories,
- blocks `gh pr create` when `gh` is unavailable,
- reminds the agent after `git push` to create the PR through the local harness,
- keeps manual browser PR links as the fallback after local REST creation fails,
  not as the first answer.

PR creation requires `--approval-id` because it is an external publish action.

## Verification

The release package includes tests for:

- HTTPS and SSH GitHub remote parsing,
- approval before token lookup,
- no-token fallback URL,
- existing PR reuse,
- connector and missing-`gh` hook blocks,
- post-push guidance,
- ERR node payload shape.

Run:

```bash
cd neural-memory
python -m pytest tests/test_3can_pr_harness.py -q
```
