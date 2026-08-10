# Runtime Graph Directory

This directory is intentionally empty in the release package.

3CAN creates project-local graph files here at runtime:

- `nodes/*.json`
- `edges.json`
- `agents.json`
- `activity_log.json`
- `embeddings.npz`
- `token_usage.sqlite3`

Do not commit those runtime files. For a fresh project, run:

```bash
python neural-memory/backend/seed_nodes.py
```

or use `scripts/init-project.ps1` / `scripts/init-project.sh`, which seeds the
generic base graph and binds the package to a project-specific port and graph.
