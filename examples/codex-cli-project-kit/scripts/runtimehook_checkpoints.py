"""Bounded checkpoint documents and Jev review policy; no worker or scheduler."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path

import runtimehook_jev as jev

SPEC_SCHEMA = "3can.checkpoints/v1"
RECORD_SCHEMA = "3can.checkpoint-record/v1"
POLICY_SCHEMA = "3can.runtimehook-policy/v1"
MAX_BYTES = 64 * 1024


def read_json(path, limit=MAX_BYTES):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise jev.JevError("CHECKPOINT_NOT_DIRECT_FILE")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise jev.JevError("CHECKPOINT_TOO_LARGE")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise jev.JevError("CHECKPOINT_INVALID_JSON") from None


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def required():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not home.is_absolute():
        raise jev.JevError("INVALID_CODEX_HOME")
    path = home / "runtimehook/policy.json"
    if not os.path.lexists(path):
        return False  # A public install alone is not permission for paid uploads.
    policy = read_json(path, 4096)
    if (not isinstance(policy, dict) or set(policy) != {"schema", "jev_required"}
            or policy["schema"] != POLICY_SCHEMA or type(policy["jev_required"]) is not bool):
        raise jev.JevError("INVALID_RUNTIMEHOOK_POLICY")
    return policy["jev_required"]


def spec_from(path, intent):
    spec = read_json(path, jev.MAX_PACKET_BYTES)
    if (not isinstance(spec, dict) or set(spec) != {"schema", "final_checkpoint", "checkpoints"}
            or spec["schema"] != SPEC_SCHEMA or not isinstance(spec["checkpoints"], list)
            or not 1 <= len(spec["checkpoints"]) <= 32):
        raise jev.JevError("INVALID_CHECKPOINT_SPEC")
    acceptance = {item["id"] for item in intent["acceptance"]}
    ids = set()
    covered = set()
    for point in spec["checkpoints"]:
        if not isinstance(point, dict) or set(point) != {
            "id", "title", "criteria", "parameters", "rubric", "minimum_score",
            "minimum_confidence", "minimum_probability",
        }:
            raise jev.JevError("INVALID_CHECKPOINT_FIELDS")
        key = jev._id(point["id"])
        if key.casefold() in ids:
            raise jev.JevError("DUPLICATE_CHECKPOINT_ID")
        ids.add(key.casefold())
        jev._text(point["title"], 240)
        for name in ("criteria", "parameters"):
            values = point[name]
            if (not isinstance(values, list) or len(values) > 32
                    or any(not isinstance(v, str) or not v.strip() for v in values)
                    or len(set(values)) != len(values)):
                raise jev.JevError("INVALID_CHECKPOINT_LIST")
        if not point["criteria"] or not set(point["criteria"]) <= acceptance:
            raise jev.JevError("CHECKPOINT_CRITERION_MISMATCH")
        covered.update(point["criteria"])
        rubric = point["rubric"]
        if not isinstance(rubric, list) or not 2 <= len(rubric) <= 7:
            raise jev.JevError("INVALID_CHECKPOINT_RUBRIC")
        for item in rubric:
            jev._text(item, 1000)
        if (type(point["minimum_score"]) not in (int, float) or not math.isfinite(point["minimum_score"])
                or not 0 < point["minimum_score"] <= len(rubric) - 1):
            raise jev.JevError("INVALID_CHECKPOINT_THRESHOLD")
        for name in ("minimum_confidence", "minimum_probability"):
            number = point[name]
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= 1:
                raise jev.JevError("INVALID_CHECKPOINT_THRESHOLD")
    if (not isinstance(spec["final_checkpoint"], str)
            or spec["final_checkpoint"] not in {p["id"] for p in spec["checkpoints"]} or covered != acceptance):
        raise jev.JevError("CHECKPOINT_COVERAGE_MISSING")
    return spec


def make_packet(spec, checkpoint_id, supplied, intent):
    point = next((p for p in spec["checkpoints"] if p["id"] == checkpoint_id), None)
    if point is None:
        raise jev.JevError("UNKNOWN_CHECKPOINT")
    if not isinstance(supplied, dict) or "parameters" not in supplied:
        raise jev.JevError("CHECKPOINT_PARAMETERS_REQUIRED")
    packet = dict(supplied)
    parameters = packet.pop("parameters")
    if (not isinstance(parameters, dict) or set(parameters) != set(point["parameters"])
            or any(type(v) not in (str, int, float, bool) or
                   (type(v) is float and not math.isfinite(v)) for v in parameters.values())):
        raise jev.JevError("CHECKPOINT_PARAMETERS_MISMATCH")
    packet["checkpoint"] = {
        "id": point["id"], "title": point["title"], "criteria": point["criteria"],
        "parameters": parameters, "rubric": point["rubric"],
    }
    jev.build_request(packet, intent)
    if (not set(point["criteria"]) <= {c["criterion_id"] for c in packet["claims"]}
            or not any(e["kind"] != "agent_summary" for e in packet["evidence"])):
        raise jev.JevError("CHECKPOINT_ACTUAL_EVIDENCE_REQUIRED")
    if len(json.dumps(packet, ensure_ascii=False).encode()) > jev.MAX_PACKET_BYTES:
        raise jev.JevError("PACKET_TOO_LARGE")
    return packet


def record_path(root, checkpoint_id, scope="main"):
    if scope not in {"main", "temporary"}:
        raise jev.JevError("INVALID_CHECKPOINT_SCOPE")
    return Path(root) / ".codex/runtimehook" / ("checkpoint." + scope + "." + jev._id(checkpoint_id) + ".json")


def save(path, value):
    """Caller validates the existing dedicated state root; no arbitrary outputs."""
    path = Path(path)
    if os.path.lexists(path):
        read_json(path)
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(raw.encode()) > MAX_BYTES:
        raise jev.JevError("CHECKPOINT_TOO_LARGE")
    descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def concerns(opinion, point):
    if opinion.get("status") != "OBSERVED":
        return [opinion.get("error_code", opinion.get("status", "UNAVAILABLE"))]
    issues = []
    for key, answer in opinion["answers"].items():
        confidence = answer.get("confidence")
        if confidence is None or confidence < point["minimum_confidence"]:
            issues.append(f"{key}: LOW_OR_MISSING_CONFIDENCE")
        if answer["type"] == "score":
            acceptable = sum(p for i, p in answer["probabilities"].items()
                             if int(i) >= point["minimum_score"])
            if answer["score"] < point["minimum_score"]:
                issues.append(f"{key}: LOW_SCORE")
        else:
            positive = {"DIRECTLY_RELEVANT", "JUSTIFIED_PREREQUISITE"} if key == "next_step" else {"SUPPORTED"}
            acceptable = sum(answer["probabilities"][c] for c in positive)
            if answer["choice"] not in positive:
                issues.append(f"{key}: {answer['choice']}")
        if acceptable < point["minimum_probability"]:
            issues.append(f"{key}: LOW_SUPPORT_PROBABILITY")
    return issues
