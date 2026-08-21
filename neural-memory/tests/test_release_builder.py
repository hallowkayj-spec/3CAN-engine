from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = PACKAGE_ROOT / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("threecan_release_builder", BUILDER_PATH)
assert SPEC and SPEC.loader
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


@pytest.mark.parametrize("version", ["v0.2.0", "v0.2.0-rc.1", "v1.0.0-beta.2"])
def test_release_version_contract_accepts_supported_versions(version: str) -> None:
    assert BUILDER.validate_version(version) == version


@pytest.mark.parametrize("version", ["0.2.0", "v0.2", "latest", "v0.2.0-rc"])
def test_release_version_contract_rejects_ambiguous_versions(version: str) -> None:
    with pytest.raises(ValueError):
        BUILDER.validate_version(version)


def test_release_archive_members_must_stay_under_package_prefix() -> None:
    BUILDER.validate_archive_members(
        ["3CAN-engine-v0.2.0/README.md", "3CAN-engine-v0.2.0/scripts/build_release.py"],
        "3CAN-engine-v0.2.0/",
    )
    with pytest.raises(RuntimeError):
        BUILDER.validate_archive_members(
            ["3CAN-engine-v0.2.0/../private.txt"],
            "3CAN-engine-v0.2.0/",
        )
