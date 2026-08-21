#!/usr/bin/env python3
"""Build and verify an exact, portable 3CAN GitHub release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


PACKAGE_NAME = "3CAN-engine"
VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def validate_version(value: str) -> str:
    version = value.strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must look like v0.2.0 or v0.2.0-rc.1")
    return version


def validate_archive_members(names: list[str], prefix: str) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not name.startswith(prefix):
            raise RuntimeError(f"unsafe archive member: {name}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(root: Path, version: str, output_dir: Path, source_ref: str) -> dict[str, object]:
    if _git(root, "status", "--porcelain"):
        raise RuntimeError("release builds require a clean Git worktree")

    commit = _git(root, "rev-parse", f"{source_ref}^{{commit}}")
    tree = _git(root, "rev-parse", f"{commit}^{{tree}}")
    prefix = f"{PACKAGE_NAME}-{version}/"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{PACKAGE_NAME}-{version}.zip"
    checksum_path = output_dir / f"{archive_path.name}.sha256"
    receipt_path = output_dir / f"{PACKAGE_NAME}-{version}.receipt.json"
    outputs = (archive_path, checksum_path, receipt_path)
    existing = [str(path) for path in outputs if path.exists()]
    if existing:
        raise FileExistsError(f"release output already exists: {', '.join(existing)}")

    with tempfile.TemporaryDirectory(prefix=".3can-release-", dir=output_dir) as staging:
        staging_root = Path(staging)
        staged_archive = staging_root / archive_path.name
        _git(
            root,
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            f"--output={staged_archive}",
            commit,
        )

        with zipfile.ZipFile(staged_archive) as archive:
            names = archive.namelist()
            validate_archive_members(names, prefix)
            file_count = sum(not name.endswith("/") for name in names)
            extracted = staging_root / "extracted"
            archive.extractall(extracted)
            extracted_root = extracted / prefix.rstrip("/")
            result = subprocess.run(
                [
                    sys.executable,
                    str(extracted_root / "scripts" / "prerelease_scan.py"),
                    str(extracted_root),
                    "--strict",
                    "--extracted-package",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                detail = result.stdout.strip() or result.stderr.strip()
                raise RuntimeError(f"extracted package scan failed:\n{detail}")

        archive_sha256 = sha256_file(staged_archive)
        receipt: dict[str, object] = {
            "schema_version": "3can.public-release-package/v1",
            "status": "VERIFIED",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "package": PACKAGE_NAME,
            "version": version,
            "source_commit": commit,
            "source_tree": tree,
            "archive": archive_path.name,
            "archive_sha256": archive_sha256,
            "archive_bytes": staged_archive.stat().st_size,
            "file_count": file_count,
            "privacy_scan": "STRICT_EXTRACTED_PACKAGE_PASS",
            "runtime_data_included": False,
            "license": "PolyForm Noncommercial License 1.0.0",
        }
        staged_checksum = staging_root / checksum_path.name
        staged_receipt = staging_root / receipt_path.name
        staged_checksum.write_text(
            f"{archive_sha256}  {archive_path.name}\n",
            encoding="utf-8",
        )
        staged_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staged_archive.replace(archive_path)
        staged_checksum.replace(checksum_path)
        staged_receipt.replace(receipt_path)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, type=validate_version)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-ref", default="HEAD")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        receipt = build_release(root, args.version, args.output_dir.resolve(), args.source_ref)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"[3CAN release] failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
