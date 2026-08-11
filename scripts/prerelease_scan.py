#!/usr/bin/env python3
"""Pre-release scanner for 3CAN redistributable packages.

The default mode fails on high-confidence secrets and reports portability
findings. Use --strict when preparing a public or cross-project package; strict
mode also fails on user-profile absolute paths. Project-specific private terms
belong in an untracked release audit, not in this public scanner.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path


TEXT_EXTENSIONS = {
    ".cfg", ".cmd", ".css", ".html", ".ini", ".js", ".json", ".md", ".ps1",
    ".py", ".sh", ".toml", ".txt", ".yaml", ".yml",
}
SKIP_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__",
    "build", "dist", "logs", "node_modules", "runtime", "venv",
}
RUNTIME_GRAPH_ALLOWED = {
    "graph/README.md",
    "graph/.gitkeep",
    "neural-memory/graph/README.md",
    "neural-memory/graph/.gitkeep",
}
RUNTIME_GRAPH_PREFIXES = ("graph/", "neural-memory/graph/")
RUNTIME_STATE_PREFIXES = (
    "neural-memory/.3can-locks/",
    "examples/codex-cli-project-kit/data/_3can_pending_writeback/",
    "examples/codex-cli-project-kit/data/_3can_runtime/",
    "examples/codex-cli-project-kit/test-results/3can/",
)
RUNTIME_DB_PATTERN = re.compile(
    r"(?:^|/)[^/]+\.(?:db|sqlite|sqlite3)(?:-(?:journal|shm|wal))?$",
    re.I,
)

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\b(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{20,}[\"']", re.I),
    re.compile(r"\b(cookie|set-cookie)\s*[:=]\s*[\"'][^\"']{20,}[\"']", re.I),
]

REBINDING_PATTERNS = [
    re.compile("C:" + r"[/\\]Users[/\\]", re.I),
    re.compile("/mnt/" + r"c/Users/", re.I),
]
PACKAGE_MANIFEST = "RELEASE_PACKAGE_MANIFEST.json"
POLYFORM_HEADER = "# PolyForm Noncommercial License 1.0.0"
LICENSE_POLICY_DOCS = (
    "README.md",
    "README.en.md",
    "LICENSING.md",
    "LICENSING.en.md",
    "docs/specs/3CAN_ENGINE/LICENSING.md",
)
COMMERCIAL_BOUNDARY_PATTERNS = (
    re.compile(r"\bcommercial\s+uses?\b", re.I),
    re.compile(r"商业"),
)
LICENSE_CONFLICT_PATTERNS = (
    re.compile(r"PolyForm\s+Shield", re.I),
    re.compile(r"建议[^\n]{0,80}\bGPL-?3(?:\.0)?\b", re.I),
    re.compile(
        r"\bGPL-?3(?:\.0)?\s*/\s*MPL-?2(?:\.0)?\s*(?:候选|candidate)",
        re.I,
    ),
    re.compile(r"(?:许可证|licen[cs]e)[^\n]{0,30}(?:还没选|未决|undecided)", re.I),
    re.compile(r"(?:开源\s*)?licen[cs]e[^\n]{0,30}(?:暂缓|再定)", re.I),
    re.compile(r"许可证[^\n]{0,20}(?:暂缓|再定|选择建议)", re.I),
)
PROJECT_KIT_IGNORE_TEMPLATE = (
    "examples/codex-cli-project-kit/.gitignore.template"
)
PROJECT_KIT_RUNTIME_IGNORES = frozenset(
    {
        "data/_3can_pending_writeback/",
        "data/_3can_runtime/",
        "test-results/3can/",
    }
)
PUBLIC_BENCHMARK_FIXTURES = (
    "neural-memory/benchmark/route_benchmark_v1.json",
    "neural-memory/benchmark/substrate_bench_v1.json",
)
PRIVATE_BENCHMARK_PATTERNS = (
    re.compile(r"\b(?:SES|HO)-20\d{6}[A-Za-z0-9._-]*\b"),
    re.compile(r"\bS\d{2}[a-z]?\b", re.I),
    re.compile(r"\b(?:AutoDL|DashScope|KAIROS)\b", re.I),
    re.compile(r"DeepSeek\s+API\s+key", re.I),
    re.compile(r"\badvisor(?:-v\d+)?\s+API\b", re.I),
    re.compile(r"\bvideo\s+factory\b", re.I),
    re.compile(r"(?:情绪价值定价|鹅卵石|开光)"),
    re.compile(r"\bthe maintainer\b", re.I),
)
TRUSTED_NUMPY_LOAD_CONSUMERS = frozenset(
    {
        "neural-memory/backend/graph_engine.py",
        "neural-memory/tools/build_topic_neighbors.py",
        "neural-memory/tools/edge_inferrer.py",
    }
)


def iter_text_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & SKIP_DIRS:
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        yield path


def scan_runtime_artifacts(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        rel = relative.as_posix()
        if rel in RUNTIME_GRAPH_ALLOWED:
            continue
        if rel.startswith(RUNTIME_GRAPH_PREFIXES):
            hits.append(rel)
            continue
        if rel.startswith(RUNTIME_STATE_PREFIXES):
            hits.append(rel)
            continue
        if RUNTIME_DB_PATTERN.search(rel):
            hits.append(rel)
    return hits


def _git_tracked_files(root: Path) -> tuple[set[str] | None, str | None]:
    """Return paths tracked below ``root``, or a fail-closed diagnostic."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],
            check=False,
            capture_output=True,
            text=False,
        )
    except OSError as exc:
        return None, f"git tracking check could not start: {exc}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return None, f"package is not inside a readable Git checkout: {detail or 'git ls-files failed'}"
    tracked = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }
    return tracked, None


def scan_package_manifest(
    root: Path,
    *,
    verify_git_tracking: bool = True,
) -> list[str]:
    """Fail closed when required release files are absent, unsafe, or untracked."""

    manifest_path = root / PACKAGE_MANIFEST
    if not manifest_path.is_file():
        return [f"{PACKAGE_MANIFEST}: required package manifest is missing"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{PACKAGE_MANIFEST}: invalid JSON: {exc}"]
    required = payload.get("required_paths") if isinstance(payload, dict) else None
    if not isinstance(required, list) or not required:
        return [f"{PACKAGE_MANIFEST}: required_paths must be a non-empty list"]

    findings: list[str] = []
    seen: set[str] = set()
    resolved_root = root.resolve()
    tracked: set[str] | None = None
    if verify_git_tracking:
        tracked, git_error = _git_tracked_files(root)
        if git_error:
            findings.append(f"{PACKAGE_MANIFEST}: {git_error}")
    for raw in required:
        rel = str(raw or "").strip().replace("\\", "/")
        if not rel or rel in seen:
            findings.append(f"{PACKAGE_MANIFEST}: blank or duplicate required path: {rel!r}")
            continue
        seen.add(rel)
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            findings.append(f"{PACKAGE_MANIFEST}: required path escapes package: {rel}")
            continue
        if not candidate.is_file():
            findings.append(f"{PACKAGE_MANIFEST}: required file is missing: {rel}")
            continue
        if tracked is not None and rel not in tracked:
            findings.append(f"{PACKAGE_MANIFEST}: required file is not tracked by Git: {rel}")
    if tracked is not None and PACKAGE_MANIFEST not in tracked:
        findings.append(f"{PACKAGE_MANIFEST}: package manifest is not tracked by Git")
    return findings


def scan_license_policy(root: Path) -> list[str]:
    """Enforce the release's selected source-available license vocabulary."""

    findings: list[str] = []
    license_path = root / "LICENSE"
    if not license_path.is_file():
        findings.append("LICENSE: required PolyForm Noncommercial license text is missing")
    else:
        first_line = license_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()[:1]
        if first_line != [POLYFORM_HEADER]:
            findings.append(
                "LICENSE: expected PolyForm Noncommercial License 1.0.0 header"
            )

    for rel in LICENSE_POLICY_DOCS:
        path = root / rel
        if not path.is_file():
            findings.append(f"{rel}: required license policy document is missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "PolyForm Noncommercial" not in text:
            findings.append(f"{rel}: PolyForm Noncommercial policy is not stated")
        if not any(pattern.search(text) for pattern in COMMERCIAL_BOUNDARY_PATTERNS):
            findings.append(f"{rel}: commercial-use boundary is not stated")
        if "source-available" not in text:
            findings.append(f"{rel}: source-available classification is not stated")
        if "OSI" not in text:
            findings.append(f"{rel}: non-OSI distinction is not stated")

    for path in iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for idx, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in LICENSE_CONFLICT_PATTERNS):
                findings.append(
                    f"{rel}:{idx}: conflicting current-license wording: {masked(line)}"
                )
    return findings


def scan_project_kit_runtime_ignores(root: Path) -> list[str]:
    """Require copied project kits to disclose their local runtime ignores."""

    path = root / PROJECT_KIT_IGNORE_TEMPLATE
    if not path.is_file():
        return [f"{PROJECT_KIT_IGNORE_TEMPLATE}: required ignore template is missing"]
    entries = {
        line.strip().replace("\\", "/")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return [
        f"{PROJECT_KIT_IGNORE_TEMPLATE}: missing runtime ignore: {entry}"
        for entry in sorted(PROJECT_KIT_RUNTIME_IGNORES - entries)
    ]


def scan_public_benchmark_fixtures(root: Path) -> list[str]:
    """Reject public fixtures derived from private graph prompts or identifiers."""

    findings: list[str] = []
    for rel in PUBLIC_BENCHMARK_FIXTURES:
        path = root / rel
        if not path.is_file():
            findings.append(f"{rel}: required synthetic public fixture is missing")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            findings.append(f"{rel}: invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            findings.append(f"{rel}: fixture root must be an object")
            continue
        evidence = payload.get("release_evidence")
        if payload.get("fixture_scope") != "synthetic_public":
            findings.append(f"{rel}: fixture_scope must be synthetic_public")
        if not isinstance(evidence, dict):
            findings.append(f"{rel}: release_evidence must be an object")
        else:
            if evidence.get("status") != "synthetic_public_fixture":
                findings.append(
                    f"{rel}: release_evidence.status must be synthetic_public_fixture"
                )
            if evidence.get("requires_excluded_runtime_graph") is not False:
                findings.append(
                    f"{rel}: public fixture must not require an excluded runtime graph"
                )
        binding = payload.get("graph_binding")
        if not isinstance(binding, dict):
            findings.append(f"{rel}: graph_binding must be an object")
        else:
            if (
                binding.get("schema_version")
                != "3can.benchmark-graph-binding/v1"
            ):
                findings.append(f"{rel}: graph_binding schema is invalid")
            required = binding.get("required_node_ids")
            if not isinstance(required, list) or not required:
                findings.append(
                    f"{rel}: graph_binding.required_node_ids must be non-empty"
                )
            elif len(required) != len(set(map(str, required))):
                findings.append(
                    f"{rel}: graph_binding.required_node_ids contains duplicates"
                )
            queries = payload.get("queries") or payload.get("cases")
            if isinstance(queries, list) and isinstance(required, list):
                expected = {
                    str(node_id)
                    for query in queries
                    if isinstance(query, dict)
                    for node_id in [
                        query.get("expected_top1"),
                        *(
                            query.get("expected_any3")
                            or query.get("expected_top3")
                            or []
                        ),
                    ]
                    if node_id
                }
                if set(map(str, required)) != expected:
                    findings.append(
                        f"{rel}: graph binding does not match query expectations"
                    )
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for pattern in PRIVATE_BENCHMARK_PATTERNS:
            match = pattern.search(serialized)
            if match:
                findings.append(
                    f"{rel}: private-derived benchmark content: {masked(match.group(0))}"
                )
    return findings


def scan_numpy_load_policy(root: Path) -> list[str]:
    """Allow only reviewed production ``numpy.load`` calls that disable pickle."""

    findings: list[str] = []
    consumers_seen: set[str] = set()
    for path in iter_text_files(root):
        relative = path.relative_to(root)
        if path.suffix.lower() != ".py" or "tests" in relative.parts:
            continue
        rel = relative.as_posix()
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8-sig", errors="replace"),
                filename=rel,
            )
        except (OSError, SyntaxError) as exc:
            findings.append(f"{rel}: cannot audit numpy.load calls: {exc}")
            continue

        numpy_aliases: set[str] = set()
        numpy_load_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                numpy_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "numpy"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "numpy":
                numpy_load_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "load"
                )

        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_attribute_call = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in numpy_aliases
            )
            is_direct_call = (
                isinstance(node.func, ast.Name)
                and node.func.id in numpy_load_aliases
            )
            if is_attribute_call or is_direct_call:
                calls.append(node)
        if not calls:
            continue
        if rel not in TRUSTED_NUMPY_LOAD_CONSUMERS:
            findings.append(
                f"{rel}: production numpy.load consumer is not in the reviewed allowlist"
            )
        else:
            consumers_seen.add(rel)
        for call in calls:
            allow_pickle = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "allow_pickle"
                ),
                None,
            )
            if not (
                isinstance(allow_pickle, ast.Constant)
                and allow_pickle.value is False
            ):
                findings.append(
                    f"{rel}:{call.lineno}: numpy.load must set allow_pickle=False"
                )

    for rel in sorted(TRUSTED_NUMPY_LOAD_CONSUMERS - consumers_seen):
        findings.append(
            f"{rel}: reviewed numpy.load consumer is missing or has no auditable call"
        )
    return findings


def masked(line: str) -> str:
    line = line.strip()
    if len(line) > 220:
        line = line[:220] + "..."
    line = re.sub(r"(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+", r"\1...", line)
    line = re.sub(r"(gh[pousr]_[A-Za-z0-9_]{8})[A-Za-z0-9_]+", r"\1...", line)
    return line


def scan(root: Path) -> tuple[list[str], list[str]]:
    secret_hits: list[str] = []
    rebinding_hits: list[str] = []
    for path in iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            rebinding_hits.append(f"{path}: read failed: {exc}")
            continue
        rel = path.relative_to(root)
        for idx, line in enumerate(text.splitlines(), 1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    secret_hits.append(f"{rel}:{idx}: {masked(line)}")
                    break
            for pattern in REBINDING_PATTERNS:
                if pattern.search(line):
                    rebinding_hits.append(f"{rel}:{idx}: {masked(line)}")
                    break
    return secret_hits, rebinding_hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--strict", action="store_true", help="fail on portability/rebinding findings too")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    manifest_hits = scan_package_manifest(root)
    runtime_hits = scan_runtime_artifacts(root)
    license_hits = scan_license_policy(root)
    project_kit_ignore_hits = scan_project_kit_runtime_ignores(root)
    benchmark_privacy_hits = scan_public_benchmark_fixtures(root)
    numpy_load_hits = scan_numpy_load_policy(root)
    secret_hits, rebinding_hits = scan(root)

    if manifest_hits:
        print("[pre-release scan] package manifest findings:")
        for hit in manifest_hits:
            print(f"  {hit}")
    if runtime_hits:
        print("[pre-release scan] runtime graph artifacts found:")
        for hit in runtime_hits[:80]:
            print(f"  {hit}")
        if len(runtime_hits) > 80:
            print(f"  ... {len(runtime_hits) - 80} more")
    if license_hits:
        print("[pre-release scan] license-policy findings:")
        for hit in license_hits[:80]:
            print(f"  {hit}")
        if len(license_hits) > 80:
            print(f"  ... {len(license_hits) - 80} more")
    if project_kit_ignore_hits:
        print("[pre-release scan] project-kit runtime-ignore findings:")
        for hit in project_kit_ignore_hits:
            print(f"  {hit}")
    if benchmark_privacy_hits:
        print("[pre-release scan] public benchmark privacy findings:")
        for hit in benchmark_privacy_hits:
            print(f"  {hit}")
    if numpy_load_hits:
        print("[pre-release scan] numpy.load safety findings:")
        for hit in numpy_load_hits:
            print(f"  {hit}")
    if secret_hits:
        print("[pre-release scan] high-confidence secret findings:")
        for hit in secret_hits[:50]:
            print(f"  {hit}")
        if len(secret_hits) > 50:
            print(f"  ... {len(secret_hits) - 50} more")
    if rebinding_hits:
        print("[pre-release scan] portability/rebinding findings:")
        for hit in rebinding_hits[:80]:
            print(f"  {hit}")
        if len(rebinding_hits) > 80:
            print(f"  ... {len(rebinding_hits) - 80} more")

    if (
        manifest_hits
        or runtime_hits
        or license_hits
        or project_kit_ignore_hits
        or benchmark_privacy_hits
        or numpy_load_hits
        or secret_hits
        or (args.strict and rebinding_hits)
    ):
        print("[pre-release scan] failed")
        return 1

    if rebinding_hits:
        print("[pre-release scan] passed with portability warnings; run --strict before public release")
    else:
        print("[pre-release scan] clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
