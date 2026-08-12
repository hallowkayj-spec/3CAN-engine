# Contributing to 3CAN

Thank you for your interest. 3CAN is maintained by a small team (effectively one non-traditional developer plus AI coding agents). Every issue, PR, and piece of feedback genuinely helps.

## About the maintainer (honest upfront)

3CAN was built by a vibecoding developer , using Claude Code and other AI coding agents as the primary authoring tools. Expect to find bugs, non-idiomatic patterns, gaps in error handling, and architectural choices that a professional software engineer would not make. **Pointing these out is extremely welcome** — direct, specific, technical criticism is the single most useful form of contribution.

Please do not pull punches out of politeness. If a function is wrong, say so and explain why.

## License note

This repository is **source-available** under the [PolyForm Noncommercial License 1.0.0](./LICENSE). It is **not OSI-approved open source**. Code submitted in a pull request must be available under the same terms. Issues and discussions do not transfer copyright. See [LICENSING.md](./LICENSING.md) for the plain-language FAQ.

If a contribution is to be included in a separately licensed commercial version, the maintainer will request an explicit, lightweight contributor agreement before merge. No additional re-licensing rights are assumed silently.

## How to contribute

### Report a bug

1. Check existing issues first.
2. Open an issue with:
   - 3CAN version / commit hash
   - Python version, OS
   - Minimal reproduction (ideally curl commands against a fresh backend)
   - Expected vs actual behavior
   - Relevant log excerpts from `~/.claude/logs/3can-gate.jsonl` or `~/.claude/logs/3can-writeback-fail.jsonl` if applicable

### Suggest a feature

1. Open an issue tagged `feature` or `discussion`.
2. Describe the use case first, then the proposed mechanism.
3. Maintainer will respond within 3-5 days (may be slower during release weeks).

### Submit a pull request

1. Fork the repository.
2. Create a feature branch off `main`.
3. Keep the PR small and focused (< 500 lines changed ideally).
4. Run `ruff check` on touched files; 0 errors.
5. If touching `backend/` or `tools/`, add at least one test or repro snippet.
6. Write a clear PR description: what problem, what changed, how to verify.
7. Be patient on review cadence. Maintainer is one person.

### Hard criticism / architectural challenge

Open an issue tagged `architecture-review`. Please include:
- What you think is wrong
- Evidence (code location / benchmark data / spec reference)
- Alternative approach (optional but helpful)

We will read every one. We may disagree but we will explain why.

## Code style

- Python: `ruff` settings in `pyproject.toml`; follow existing style. No micro-nitpicks on style when the refactor improves readability.
- JavaScript hooks (`~/.claude/scripts/hooks/*.js`): plain Node.js, no bundler, no TypeScript, no external dependencies beyond Node stdlib.
- Markdown: keep line width around 120; no strict wrap.
- Chinese and English both acceptable in comments; node content in the dogfood graph is majority Chinese, that is fine.

## Before opening a PR

- [ ] `ruff check` clean on touched files
- [ ] Docs updated if behavior changes
- [ ] No `secrets.json` / `.env` / API keys accidentally staged (run `git diff --cached | grep -iE "api[_-]?key|password|secret"`)
- [ ] If you added a new LLM integration point, it defaults to "off" and has a degradation path (see LLM_POLICY.md)
- [ ] If you added a new endpoint, it has localhost-only default (see SECURITY.md)

## Communication

- Issue tracker is the primary channel
- Discussions for design questions
- No Slack / Discord / email list for the current release candidate (maintainer capacity limited)

## Response expectations

- Security issues: within 48 hours (see [SECURITY.md](./SECURITY.md))
- Bug reports: within 3-5 days initial triage
- Feature / discussion issues: within 7 days initial response
- PR review: within 1-2 weeks for small PRs; larger PRs may take longer

These are targets, not guarantees. If something is urgent, say so in the issue.

## What will not be merged

- Changes that remove or weaken the PolyForm Noncommercial license terms
- Marketing claims ("3CAN is the best", "beats X", "fastest", etc.) in code / docs / commit messages
- Unsolicited dependencies on large frameworks when a small utility suffices
- Changes that couple the core engine to a specific LLM provider (violates BYOK / provider-neutral design)
- Changes that default a server to `0.0.0.0` without an explicit security warning (see H9 in OPEN_SOURCE_CHECKLIST.md)

## Saying thanks

There is no sponsor page yet. The most meaningful thanks you can give is:
1. File a well-scoped issue.
2. Fix a real bug in a small PR.
3. Share genuine experience reports (what worked, what broke) in Discussions.

Thank you for reading this far.
