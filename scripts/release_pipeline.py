"""Deterministic, cross-platform construction of the Windows portable bundle.

This module deliberately installs wheels as ZIP archives.  It never imports or
executes target packages and never asks the host interpreter to resolve target
markers.  The lock's explicit Windows marker environment is the sole source of
truth.
"""

from __future__ import annotations

import ast
import base64
import csv
import email
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import unicodedata
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Mapping, Sequence


PRODUCT_NAME = "TextSnapLayout"
PRODUCT_VERSION = "0.1.0"
EXPECTED_WHEEL_COUNT = 68
LOCK_SCHEMA_VERSION = "1.0.0"
PYTHON_VERSION = "3.13.14"
TARGET_MARKER_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": PYTHON_VERSION,
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_release": "11",
    "platform_system": "Windows",
    "platform_version": "11",
    "python_full_version": PYTHON_VERSION,
    "platform_python_implementation": "CPython",
    "python_version": "3.13",
    "sys_platform": "win32",
}
PINNED_DIRECT_REQUIREMENTS = (
    "numpy==2.2.6",
    "opencv-contrib-python==4.10.0.84",
    "paddleocr==3.7.0",
    "paddlepaddle==3.3.1",
    "paddlex[ocr-core]==3.7.2",
    "PySide6-Essentials==6.11.1",
)
PINNED_CORE_WHEEL_VERSIONS = {
    "numpy": "2.2.6",
    "opencv-contrib-python": "4.10.0.84",
    "paddleocr": "3.7.0",
    "paddlepaddle": "3.3.1",
    "paddlex": "3.7.2",
    "pyside6-essentials": "6.11.1",
    "shiboken6": "6.11.1",
}
PINNED_RESOURCE_IDENTITIES = {
    "cpython-3.13.14-embed-win-amd64": (
        "python_embeddable_runtime",
        "3.13.14",
    ),
    "pp-ocrv6-medium-det-inference": (
        "paddle_inference_model",
        "PP-OCRv6_medium_det",
    ),
    "pp-ocrv6-medium-rec-inference": (
        "paddle_inference_model",
        "PP-OCRv6_medium_rec",
    ),
    "noto-sans-mono-cjk-sc-regular-sans2.004": ("font", "Sans2.004"),
    "paddlepaddle-3.3.1-cp312-linux-aarch64-integration": (
        "integration_test_wheel",
        "3.3.1",
    ),
}
ALLOWED_RELEASE_LICENSE_STATUSES = frozenset({"verified"})
RESOLVED_EXTERNAL_EVIDENCE_STATUSES = frozenset(
    {
        "verified_content",
        "verified_content_mutable_url",
        "verified_content_tagged_url",
    }
)
AFFIRMATIVE_LICENSE_EVIDENCE_KINDS = frozenset(
    {
        "publisher_artifact_license",
        "publisher_dependency_archive_member",
        "publisher_license_terms_html_snapshot",
        "publisher_redistribution_authorization",
        "publisher_upstream_license",
        "upstream_dependency_license",
    }
)
STAGING_STATE_NAME = ".textsnap-staging.json"
BUILD_MANIFEST_NAME = "BUILD_MANIFEST.json"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_HASH_CHUNK_SIZE = 1024 * 1024
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_INVALID_WINDOWS_CHARS = frozenset('<>:"|?*')
_NAME_NORMALIZER = re.compile(r"[-_.]+")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_WHEEL_FILENAME_RE = re.compile(
    r"^(?P<distribution>.+?)-(?P<version>[^-]+)"
    r"(?:-(?P<build>[^-]+))?-(?P<python>[^-]+)-(?P<abi>[^-]+)-"
    r"(?P<platform>[^-]+)\.whl$",
    re.IGNORECASE,
)


class PipelineError(RuntimeError):
    """A reproducible build invariant was violated."""


class LockValidationError(PipelineError):
    """The checked-in lock is incomplete or inconsistent."""


class UnsafeArchiveError(PipelineError):
    """An archive contains a path or member unsafe for Windows staging."""


class HashMismatchError(PipelineError):
    """Locked content did not match its expected bytes."""


class WheelValidationError(PipelineError):
    """A wheel is malformed, incompatible, or outside the lock closure."""


class LicenseGateError(PipelineError):
    """Redistribution evidence is not complete."""

    def __init__(self, blockers: Sequence[Mapping[str, Any]]):
        self.blockers = tuple(dict(item) for item in blockers)
        summary = "; ".join(
            f"{item.get('id', item.get('component', 'unknown'))}: "
            f"{item.get('reason', item.get('status', 'failed'))}"
            for item in self.blockers
        )
        super().__init__(f"release license gate failed: {summary}")


class PeValidationError(PipelineError):
    """A retained PE file is incompatible with the Windows x64 target."""


def canonical_distribution_name(value: str) -> str:
    return _NAME_NORMALIZER.sub("-", value).lower()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def write_canonical_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    os.replace(temporary, path)


def sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = stream.read(_HASH_CHUNK_SIZE)
        if not block:
            break
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def sha256_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return sha256_stream(stream)


def verify_file(path: Path, expected_sha256: str, expected_size: int) -> None:
    actual_sha256, actual_size = sha256_file(path)
    if actual_size != expected_size:
        raise HashMismatchError(
            f"{path}: expected {expected_size} bytes, got {actual_size}"
        )
    if actual_sha256 != expected_sha256:
        raise HashMismatchError(
            f"{path}: expected sha256 {expected_sha256}, got {actual_sha256}"
        )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LockValidationError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise LockValidationError(f"{path}: root must be a JSON object")
    return result


@dataclass(frozen=True)
class LockSet:
    root: Path
    wheels: Mapping[str, Any]
    resources: Mapping[str, Any]
    licenses: Mapping[str, Any]

    @classmethod
    def load(cls, root: Path) -> "LockSet":
        root = root.resolve()
        return cls(
            root=root,
            wheels=load_json_object(root / "wheels.json"),
            resources=load_json_object(root / "resources.json"),
            licenses=load_json_object(root / "licenses.json"),
        )

    @property
    def wheel_artifacts(self) -> list[dict[str, Any]]:
        return list(self.wheels.get("artifacts", ()))

    @property
    def runtime_resources(self) -> list[dict[str, Any]]:
        return [
            artifact
            for artifact in self.resources.get("artifacts", ())
            if artifact.get("kind") != "integration_test_wheel"
            and artifact.get("target", {}).get("include_in_windows_zip", True)
        ]

    @property
    def all_runtime_artifacts(self) -> list[dict[str, Any]]:
        return [*self.wheel_artifacts, *self.runtime_resources]

    @property
    def external_evidence(self) -> list[dict[str, Any]]:
        return list(self.licenses.get("external_evidence", ()))

    def input_hashes(self) -> dict[str, str]:
        return {
            name: sha256_file(self.root / name)[0]
            for name in ("wheels.json", "resources.json", "licenses.json")
        }


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LockValidationError(f"{context}: expected object")
    return value


def _require_list(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise LockValidationError(f"{context}: expected array")
    return value


def _validate_artifact_download_fields(
    artifact: Mapping[str, Any], context: str
) -> None:
    filename = artifact.get("filename")
    url = artifact.get("url")
    size = artifact.get("size")
    sha256 = artifact.get("sha256")
    if not isinstance(filename, str) or not filename:
        raise LockValidationError(f"{context}: filename is required")
    validate_windows_relative_path(filename)
    if "/" in filename or "\\" in filename:
        raise LockValidationError(f"{context}: filename must be a basename")
    if not isinstance(url, str) or urllib.parse.urlparse(url).scheme != "https":
        raise LockValidationError(f"{context}: exact HTTPS URL is required")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise LockValidationError(f"{context}: positive integer size is required")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise LockValidationError(f"{context}: lowercase SHA-256 is required")


def _version_key(value: str) -> tuple[tuple[int, Any], ...]:
    """A deterministic comparison key sufficient for marker/specifier versions.

    Lock creation remains responsible for full PEP 440 resolution.  This verifier
    only compares already-normalized pinned versions against ordinary dependency
    bounds and deliberately rejects opaque direct references.
    """

    result: list[tuple[int, Any]] = []
    for part in re.findall(r"\d+|[A-Za-z]+", value):
        if part.isdigit():
            result.append((1, int(part)))
        else:
            aliases = {"dev": -3, "a": -2, "alpha": -2, "b": -1, "beta": -1, "rc": 0}
            result.append((0, aliases.get(part.lower(), part.lower())))
    return tuple(result)


def _compare_values(left: str, operator: str, right: str, versioned: bool) -> bool:
    if operator in {"in", "not in"}:
        answer = left in right
        return not answer if operator == "not in" else answer
    if versioned:
        lhs: Any = _version_key(left)
        rhs: Any = _version_key(right)
    else:
        lhs = left
        rhs = right
    if operator in {"==", "==="}:
        return lhs == rhs
    if operator == "!=":
        return lhs != rhs
    if operator == "<":
        return lhs < rhs
    if operator == "<=":
        return lhs <= rhs
    if operator == ">":
        return lhs > rhs
    if operator == ">=":
        return lhs >= rhs
    if operator == "~=":
        left_key = _version_key(left)
        right_key = _version_key(right)
        numeric = [int(piece) for piece in re.findall(r"\d+", right)]
        if len(numeric) < 2:
            raise WheelValidationError(f"invalid compatible-release marker: {right}")
        upper = tuple((1, item) for item in [numeric[0] + 1])
        if len(numeric) > 2:
            upper = tuple((1, item) for item in [*numeric[:-2], numeric[-2] + 1])
        return left_key >= right_key and left_key < upper
    raise WheelValidationError(f"unsupported comparison operator {operator!r}")


_MARKER_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<string>'(?:\\.|[^'])*'|"(?:\\.|[^"])*")
      | (?P<op>===|==|!=|<=|>=|~=|<|>)
      | (?P<word>[A-Za-z_][A-Za-z0-9_.-]*)
      | (?P<lpar>\()
      | (?P<rpar>\))
    )""",
    re.VERBOSE,
)
_MARKER_VARIABLES = frozenset(
    {
        *TARGET_MARKER_ENVIRONMENT,
        "extra",
    }
)
_VERSION_MARKER_VARIABLES = frozenset(
    {"python_version", "python_full_version", "implementation_version"}
)


def _marker_tokens(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _MARKER_TOKEN_RE.match(expression, position)
        if match is None:
            raise WheelValidationError(
                f"invalid marker near {expression[position : position + 30]!r}"
            )
        kind = match.lastgroup
        assert kind is not None
        value = match.group(kind)
        if kind == "word" and value == "not":
            next_match = _MARKER_TOKEN_RE.match(expression, match.end())
            if (
                next_match is None
                or next_match.lastgroup != "word"
                or next_match.group("word") != "in"
            ):
                raise WheelValidationError("'not' is only valid as 'not in'")
            tokens.append(("op", "not in"))
            position = next_match.end()
            continue
        if kind == "word" and value == "in":
            kind = "op"
        tokens.append((kind, value))
        position = match.end()
    return tokens


class _MarkerParser:
    def __init__(self, expression: str, environment: Mapping[str, str]):
        self._tokens = _marker_tokens(expression)
        self._environment = environment
        self._position = 0

    def parse(self) -> bool:
        value = self._parse_or()
        if self._position != len(self._tokens):
            raise WheelValidationError("unexpected trailing marker tokens")
        return value

    def _peek(self, kind: str, value: str | None = None) -> bool:
        if self._position >= len(self._tokens):
            return False
        actual_kind, actual_value = self._tokens[self._position]
        return actual_kind == kind and (value is None or actual_value == value)

    def _take(self, kind: str, value: str | None = None) -> str:
        if not self._peek(kind, value):
            expected = value if value is not None else kind
            raise WheelValidationError(f"expected marker token {expected!r}")
        answer = self._tokens[self._position][1]
        self._position += 1
        return answer

    def _parse_or(self) -> bool:
        value = self._parse_and()
        while self._peek("word", "or"):
            self._take("word", "or")
            rhs = self._parse_and()
            value = value or rhs
        return value

    def _parse_and(self) -> bool:
        value = self._parse_atom()
        while self._peek("word", "and"):
            self._take("word", "and")
            rhs = self._parse_atom()
            value = value and rhs
        return value

    def _parse_atom(self) -> bool:
        if self._peek("lpar"):
            self._take("lpar")
            value = self._parse_or()
            self._take("rpar")
            return value
        left, left_variable = self._parse_value()
        operator = self._take("op")
        right, right_variable = self._parse_value()
        versioned = bool({left_variable, right_variable} & _VERSION_MARKER_VARIABLES)
        return _compare_values(left, operator, right, versioned)

    def _parse_value(self) -> tuple[str, str | None]:
        if self._peek("string"):
            raw = self._take("string")
            value = ast.literal_eval(raw)
            if not isinstance(value, str):
                raise WheelValidationError("marker literal must be text")
            return value, None
        variable = self._take("word")
        if variable not in _MARKER_VARIABLES:
            raise WheelValidationError(f"unknown marker variable {variable!r}")
        return self._environment.get(variable, ""), variable


def evaluate_marker(expression: str, environment: Mapping[str, str]) -> bool:
    return _MarkerParser(expression, environment).parse()


def _split_requirement(requirement: str) -> tuple[str, str | None]:
    quote: str | None = None
    depth = 0
    for index, character in enumerate(requirement):
        if quote:
            if character == quote and requirement[index - 1] != "\\":
                quote = None
        elif character in "'\"":
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif character == ";" and depth == 0:
            return requirement[:index].strip(), requirement[index + 1 :].strip()
    return requirement.strip(), None


@dataclass(frozen=True)
class ParsedRequirement:
    name: str
    specifier: str
    marker: str | None


def parse_requirement(requirement: str) -> ParsedRequirement:
    base, marker = _split_requirement(requirement)
    match = _REQUIREMENT_NAME_RE.match(base)
    if match is None:
        raise WheelValidationError(f"invalid requirement {requirement!r}")
    name = canonical_distribution_name(match.group(1))
    remainder = base[match.end() :].strip()
    if remainder.startswith("["):
        closing = remainder.find("]")
        if closing < 0:
            raise WheelValidationError(f"invalid extras in {requirement!r}")
        remainder = remainder[closing + 1 :].strip()
    if "@" in remainder:
        raise WheelValidationError(
            f"direct URL requirements are forbidden in wheel closure: {requirement!r}"
        )
    if remainder.startswith("(") and remainder.endswith(")"):
        remainder = remainder[1:-1].strip()
    return ParsedRequirement(name=name, specifier=remainder, marker=marker)


def requirement_is_active(
    requirement: ParsedRequirement,
    environment: Mapping[str, str],
    selected_extras: Sequence[str],
) -> bool:
    if requirement.marker is None:
        return True
    extras = list(selected_extras) or [""]
    return any(
        evaluate_marker(
            requirement.marker,
            {**environment, "extra": extra},
        )
        for extra in extras
    )


def version_satisfies(version: str, specifier: str) -> bool:
    if not specifier:
        return True
    for raw_clause in specifier.split(","):
        clause = raw_clause.strip()
        match = re.fullmatch(r"(===|==|!=|<=|>=|~=|<|>)\s*(\S+)", clause)
        if match is None:
            raise WheelValidationError(f"unsupported version specifier {clause!r}")
        operator, wanted = match.groups()
        if wanted.endswith(".*") and operator in {"==", "!="}:
            prefix = wanted[:-2]
            answer = version == prefix or version.startswith(prefix + ".")
            if operator == "!=":
                answer = not answer
        else:
            answer = _compare_values(version, operator, wanted, versioned=True)
        if not answer:
            return False
    return True


def _tag_is_safe_shape(tag: str) -> bool:
    parts = tag.split("-")
    if len(parts) != 3:
        return False
    interpreter, abi, platform = parts
    if platform not in {"any", "win_amd64"}:
        return False
    if interpreter in {"py2.py3", "py3", "py313", "py2"}:
        return abi == "none"
    cp_match = re.fullmatch(r"cp3(\d+)", interpreter)
    if cp_match is None:
        return False
    minor = int(cp_match.group(1))
    if abi == "cp313":
        return minor == 13
    return abi == "abi3" and minor <= 13


def _tag_is_target_compatible(tag: str) -> bool:
    if not _tag_is_safe_shape(tag):
        return False
    interpreter = tag.split("-", 1)[0]
    return interpreter != "py2"


def validate_lock_set(
    locks: LockSet, *, expected_wheel_count: int = EXPECTED_WHEEL_COUNT
) -> dict[str, Any]:
    for filename, document in (
        ("wheels.json", locks.wheels),
        ("resources.json", locks.resources),
        ("licenses.json", locks.licenses),
    ):
        if document.get("schema_version") != LOCK_SCHEMA_VERSION:
            raise LockValidationError(
                f"{filename}: expected schema {LOCK_SCHEMA_VERSION}"
            )

    target = _require_mapping(locks.wheels.get("target"), "wheels target")
    expected_target = {
        "os": "windows",
        "architecture": "x86_64",
        "python_version": PYTHON_VERSION,
        "implementation": "CPython",
        "implementation_tag": "cp313",
        "abi": "cp313_or_abi3",
        "platform_tag": "win_amd64_or_any",
    }
    for key, expected in expected_target.items():
        if target.get(key) != expected:
            raise LockValidationError(
                f"wheels target {key!r}: expected {expected!r}, got {target.get(key)!r}"
            )
    if target.get("marker_environment") != TARGET_MARKER_ENVIRONMENT:
        raise LockValidationError(
            "wheels target marker_environment must equal the explicit "
            "CPython 3.13.14 Windows 11 AMD64 environment"
        )

    artifacts = _require_list(locks.wheels.get("artifacts"), "wheel artifacts")
    resolution = _require_mapping(locks.wheels.get("resolution"), "resolution")
    if resolution.get("artifact_count") != len(artifacts):
        raise LockValidationError("resolution artifact_count does not match lock")
    if len(artifacts) != expected_wheel_count:
        raise LockValidationError(
            f"expected {expected_wheel_count} locked wheels, got {len(artifacts)}"
        )

    names: dict[str, Mapping[str, Any]] = {}
    filenames: set[str] = set()
    for index, raw_artifact in enumerate(artifacts):
        artifact = _require_mapping(raw_artifact, f"wheel artifact {index}")
        context = f"wheel {artifact.get('name', index)}"
        _validate_artifact_download_fields(artifact, context)
        name = artifact.get("name")
        version = artifact.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise LockValidationError(f"{context}: name and version are required")
        canonical_name = canonical_distribution_name(name)
        if canonical_name in names:
            raise LockValidationError(f"duplicate locked project {canonical_name}")
        names[canonical_name] = artifact
        filename_key = windows_path_key(str(artifact["filename"]))
        if filename_key in filenames:
            raise LockValidationError(
                f"Windows filename collision: {artifact['filename']}"
            )
        filenames.add(filename_key)
        tags = artifact.get("wheel_tags")
        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) and _tag_is_safe_shape(tag) for tag in tags)
            or not any(_tag_is_target_compatible(tag) for tag in tags)
        ):
            raise LockValidationError(
                f"{context}: wheel tags must be target-safe and include "
                "CPython 3.13 Windows x64 compatibility"
            )
        parsed_filename = _WHEEL_FILENAME_RE.match(str(artifact["filename"]))
        if parsed_filename is None:
            raise LockValidationError(f"{context}: malformed wheel filename")
        filename_name = canonical_distribution_name(
            parsed_filename.group("distribution")
        )
        if filename_name != canonical_name:
            raise LockValidationError(
                f"{context}: filename project {filename_name!r} does not match"
            )
        if parsed_filename.group("version").replace("_", "-") != version.replace(
            "_", "-"
        ):
            raise LockValidationError(f"{context}: filename version does not match")
        filename_tags = {
            f"{python_tag}-{abi_tag}-{platform_tag}"
            for python_tag in parsed_filename.group("python").split(".")
            for abi_tag in parsed_filename.group("abi").split(".")
            for platform_tag in parsed_filename.group("platform").split(".")
        }
        if set(tags) != filename_tags:
            raise LockValidationError(
                f"{context}: lock tags do not match the wheel filename tags"
            )

        license_info = _require_mapping(artifact.get("license"), f"{context} license")
        if license_info.get("status") not in {"verified", "pending", "blocked"}:
            raise LockValidationError(f"{context}: invalid license status")

    for name, expected_version in PINNED_CORE_WHEEL_VERSIONS.items():
        artifact = names.get(name)
        if artifact is None or artifact.get("version") != expected_version:
            raise LockValidationError(
                f"plan-pinned wheel {name} must be exactly {expected_version}"
            )

    direct_requirements = _require_list(
        resolution.get("direct_requirements"), "direct requirements"
    )
    if tuple(direct_requirements) != PINNED_DIRECT_REQUIREMENTS:
        raise LockValidationError(
            "direct requirements differ from the implementation plan pins"
        )
    if resolution.get("selected_extra") != "paddlex[ocr-core]":
        raise LockValidationError("only the pinned paddlex[ocr-core] extra is allowed")
    for direct in direct_requirements:
        parsed = parse_requirement(str(direct))
        locked = names.get(parsed.name)
        if locked is None:
            raise LockValidationError(f"direct requirement {parsed.name} is not locked")
        if not version_satisfies(str(locked["version"]), parsed.specifier):
            raise LockValidationError(
                f"direct requirement {direct!r} conflicts with locked version"
            )

    for artifact in artifacts:
        for required_by in artifact.get("source", {}).get("required_by", ()):
            parent = canonical_distribution_name(str(required_by.get("name", "")))
            if parent not in names:
                raise LockValidationError(
                    f"{artifact['name']}: required_by project {parent!r} is not locked"
                )

    resources = _require_list(locks.resources.get("artifacts"), "resource artifacts")
    resource_ids: set[str] = set()
    resource_identities: dict[str, tuple[object, object]] = {}
    for index, raw_artifact in enumerate(resources):
        artifact = _require_mapping(raw_artifact, f"resource artifact {index}")
        context = f"resource {artifact.get('id', index)}"
        _validate_artifact_download_fields(artifact, context)
        filename_key = windows_path_key(str(artifact["filename"]))
        if filename_key in filenames:
            raise LockValidationError(
                f"cross-lock Windows filename collision: {artifact['filename']}"
            )
        filenames.add(filename_key)
        identifier = artifact.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise LockValidationError(f"{context}: id is required")
        if identifier in resource_ids:
            raise LockValidationError(f"duplicate resource id {identifier}")
        resource_ids.add(identifier)
        kind = artifact.get("kind")
        resource_identities[identifier] = (kind, artifact.get("version"))
        if kind == "python_embeddable_runtime":
            if artifact.get("target") != {
                "os": "windows",
                "architecture": "x86_64",
                "python_version": PYTHON_VERSION,
                "distribution": "embeddable",
            }:
                raise LockValidationError(
                    f"{context}: CPython runtime target is not exact win-x64"
                )
        elif kind == "paddle_inference_model":
            target_info = artifact.get("target", {})
            if (
                target_info.get("usage") != "windows_runtime"
                or target_info.get("format") != "inference"
            ):
                raise LockValidationError(
                    f"{context}: OCR model is not locked for Windows inference"
                )
        elif kind == "font":
            if artifact.get("target", {}).get("usage") != "windows_runtime":
                raise LockValidationError(
                    f"{context}: font is not locked for Windows runtime"
                )
        elif kind == "integration_test_wheel":
            if artifact.get("target", {}).get("include_in_windows_zip") is not False:
                raise LockValidationError(
                    f"{context}: integration wheel must be excluded from Windows ZIP"
                )
        else:
            raise LockValidationError(f"{context}: unknown resource kind {kind!r}")
        license_info = _require_mapping(artifact.get("license"), f"{context} license")
        if license_info.get("status") not in {"verified", "pending", "blocked"}:
            raise LockValidationError(f"{context}: invalid license status")

    if resource_identities != PINNED_RESOURCE_IDENTITIES:
        raise LockValidationError(
            "resource identities or versions differ from the implementation plan"
        )

    evidence_ids: set[str] = set()
    for raw_evidence in _require_list(
        locks.licenses.get("external_evidence"), "external evidence"
    ):
        evidence = _require_mapping(raw_evidence, "external evidence item")
        identifier = evidence.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise LockValidationError("external evidence id is required")
        if identifier in evidence_ids:
            raise LockValidationError(f"duplicate evidence id {identifier}")
        evidence_ids.add(identifier)
        status = evidence.get("status")
        if status not in (
            RESOLVED_EXTERNAL_EVIDENCE_STATUSES
            | {"verified_content_relationship_pending"}
        ):
            raise LockValidationError(
                f"evidence {identifier}: unsupported evidence status {status!r}"
            )
        if not isinstance(evidence.get("kind"), str) or not evidence.get("kind"):
            raise LockValidationError(f"evidence {identifier}: kind is required")
        _validate_artifact_download_fields(
            {
                "filename": f"{identifier}.evidence",
                "url": evidence.get("url"),
                "size": evidence.get("size"),
                "sha256": evidence.get("sha256"),
            },
            f"evidence {identifier}",
        )
        member = evidence.get("member")
        if member is not None:
            member = _require_mapping(member, f"evidence {identifier} member")
            member_path = member.get("path")
            if not isinstance(member_path, str):
                raise LockValidationError(
                    f"evidence {identifier}: member path is required"
                )
            validate_windows_relative_path(member_path)
            member_size = member.get("size")
            if (
                not isinstance(member_size, int)
                or isinstance(member_size, bool)
                or member_size <= 0
            ):
                raise LockValidationError(
                    f"evidence {identifier}: member size must be positive"
                )
            if not _SHA256_RE.fullmatch(str(member.get("sha256", ""))):
                raise LockValidationError(
                    f"evidence {identifier}: member SHA-256 is required"
                )

    obligation_ids: set[str] = set()
    for raw_obligation in _require_list(
        locks.licenses.get("release_obligations"), "release obligations"
    ):
        obligation = _require_mapping(raw_obligation, "release obligation")
        identifier = obligation.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise LockValidationError("release obligation id is required")
        if identifier in obligation_ids:
            raise LockValidationError(f"duplicate release obligation {identifier}")
        obligation_ids.add(identifier)
        if obligation.get("status") not in {"verified", "pending", "blocked"}:
            raise LockValidationError(
                f"obligation {identifier}: invalid license status"
            )
        if obligation.get("blocks_release") is not True:
            raise LockValidationError(
                f"obligation {identifier}: every listed obligation must block release"
            )
        components = obligation.get("components")
        if (
            not isinstance(components, list)
            or not components
            or not all(isinstance(item, str) and item for item in components)
        ):
            raise LockValidationError(
                f"obligation {identifier}: components must be non-empty strings"
            )
        for evidence_id in obligation.get("evidence_ids", ()):
            if evidence_id not in evidence_ids:
                raise LockValidationError(
                    f"obligation {identifier}: evidence {evidence_id!r} is absent"
                )

    policy = _require_mapping(locks.licenses.get("policy"), "license policy")
    gate = _require_mapping(policy.get("release_gate"), "license release gate")
    if set(gate.get("allowed_statuses", ())) != ALLOWED_RELEASE_LICENSE_STATUSES:
        raise LockValidationError("license gate must allow only verified status")
    if gate.get("require_redistributable_true") is not True:
        raise LockValidationError("license gate must require redistributable=true")
    if gate.get("require_all_evidence_ids_resolved") is not True:
        raise LockValidationError("license gate must resolve every evidence id")
    if gate.get("require_external_evidence_sha256") is not True:
        raise LockValidationError("license gate must lock external evidence hashes")

    return {
        "wheel_count": len(artifacts),
        "runtime_resource_count": len(locks.runtime_resources),
        "external_evidence_count": len(evidence_ids),
        "input_hashes": locks.input_hashes(),
    }


def validate_windows_relative_path(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise UnsafeArchiveError("archive path must be non-empty text without NUL")
    if unicodedata.normalize("NFC", path) != path:
        raise UnsafeArchiveError(f"path is not NFC-normalized: {path!r}")
    if "\\" in path:
        raise UnsafeArchiveError(f"backslash is forbidden in archive path: {path!r}")
    if path.startswith("/") or path.startswith("//") or re.match(r"^[A-Za-z]:", path):
        raise UnsafeArchiveError(f"absolute or drive path is forbidden: {path!r}")
    pure = PurePosixPath(path)
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise UnsafeArchiveError(f"non-canonical archive path: {path!r}")
    for part in pure.parts:
        if part[-1:] in {" ", "."}:
            raise UnsafeArchiveError(
                f"Windows strips trailing dot/space in path component: {path!r}"
            )
        if any(ord(character) < 32 for character in part):
            raise UnsafeArchiveError(f"control character in path: {path!r}")
        if any(character in _INVALID_WINDOWS_CHARS for character in part):
            raise UnsafeArchiveError(f"Windows-invalid character in path: {path!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise UnsafeArchiveError(f"Windows device name in path: {path!r}")
        if len(part.encode("utf-16-le")) // 2 > 255:
            raise UnsafeArchiveError(f"path component too long: {path!r}")
    if len(path.encode("utf-16-le")) // 2 > 240:
        raise UnsafeArchiveError(f"portable Windows path too long: {path!r}")
    return pure


def windows_path_key(path: str) -> str:
    pure = validate_windows_relative_path(path)
    return "/".join(
        unicodedata.normalize("NFC", part).casefold() for part in pure.parts
    )


def safe_destination(root: Path, relative: str) -> Path:
    pure = validate_windows_relative_path(relative)
    destination = root.joinpath(*pure.parts)
    resolved_root = root.resolve()
    resolved_parent = destination.parent.resolve()
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeArchiveError(f"path escapes staging root: {relative!r}") from exc
    return destination


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = info.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def validate_zip_members(
    archive: zipfile.ZipFile,
    *,
    maximum_uncompressed_size: int | None = None,
) -> dict[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        name = info.filename.rstrip("/")
        if not name:
            continue
        key = windows_path_key(name)
        if key in result:
            raise UnsafeArchiveError(
                f"duplicate or Windows case-colliding ZIP member: {info.filename!r}"
            )
        if _zip_member_is_symlink(info):
            raise UnsafeArchiveError(f"ZIP symlink is forbidden: {info.filename!r}")
        if info.flag_bits & 0x1:
            raise UnsafeArchiveError(
                f"encrypted ZIP member is forbidden: {info.filename}"
            )
        total += info.file_size
        if maximum_uncompressed_size is not None and total > maximum_uncompressed_size:
            raise UnsafeArchiveError(
                "ZIP uncompressed size exceeds locked safety bound"
            )
        result[key] = info
    return result


def _tar_member_key(member: tarfile.TarInfo) -> str:
    return windows_path_key(member.name.rstrip("/"))


def validate_tar_members(
    archive: tarfile.TarFile,
    *,
    maximum_uncompressed_size: int | None = None,
) -> dict[str, tarfile.TarInfo]:
    result: dict[str, tarfile.TarInfo] = {}
    total = 0
    for member in archive.getmembers():
        name = member.name.rstrip("/")
        if not name:
            continue
        key = _tar_member_key(member)
        if key in result:
            raise UnsafeArchiveError(
                f"duplicate or Windows case-colliding TAR member: {member.name!r}"
            )
        if not (member.isdir() or member.isreg()):
            raise UnsafeArchiveError(
                f"TAR links/devices/special files are forbidden: {member.name!r}"
            )
        total += member.size if member.isreg() else 0
        if maximum_uncompressed_size is not None and total > maximum_uncompressed_size:
            raise UnsafeArchiveError(
                "TAR uncompressed size exceeds locked safety bound"
            )
        result[key] = member
    return result


def artifact_cache_path(cache_root: Path, artifact: Mapping[str, Any]) -> Path:
    return safe_destination(cache_root / "artifacts", str(artifact["filename"]))


def evidence_cache_path(cache_root: Path, evidence: Mapping[str, Any]) -> Path:
    identifier = str(evidence["id"])
    validate_windows_relative_path(identifier)
    return safe_destination(cache_root / "evidence", f"{identifier}.evidence")


def fetch_exact(
    *,
    url: str,
    destination: Path,
    expected_sha256: str,
    expected_size: int,
    opener: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    if destination.exists():
        verify_file(destination, expected_sha256, expected_size)
        return {
            "path": str(destination),
            "sha256": expected_sha256,
            "size": expected_size,
            "cache_hit": True,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"TextSnapLayout-build/{PRODUCT_VERSION}"},
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with opener(request) as response, temporary.open("xb") as output:
            final_url = response.geturl()
            if urllib.parse.urlparse(final_url).scheme != "https":
                raise PipelineError(f"download redirected outside HTTPS: {final_url}")
            while True:
                block = response.read(_HASH_CHUNK_SIZE)
                if not block:
                    break
                size += len(block)
                if size > expected_size:
                    raise HashMismatchError(
                        f"{url}: response exceeds locked size {expected_size}"
                    )
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        actual_sha256 = digest.hexdigest()
        if size != expected_size or actual_sha256 != expected_sha256:
            raise HashMismatchError(
                f"{url}: expected {expected_size}/{expected_sha256}, "
                f"got {size}/{actual_sha256}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(destination),
        "sha256": expected_sha256,
        "size": expected_size,
        "cache_hit": False,
    }


def fetch_locked_inputs(
    locks: LockSet,
    cache_root: Path,
    *,
    include_evidence: bool = True,
) -> list[dict[str, Any]]:
    validate_lock_set(locks)
    fetched: list[dict[str, Any]] = []
    for artifact in locks.all_runtime_artifacts:
        result = fetch_exact(
            url=str(artifact["url"]),
            destination=artifact_cache_path(cache_root, artifact),
            expected_sha256=str(artifact["sha256"]),
            expected_size=int(artifact["size"]),
        )
        fetched.append(
            {
                "id": artifact.get("name", artifact.get("id")),
                "kind": "artifact",
                **result,
            }
        )
    if include_evidence:
        for evidence in locks.external_evidence:
            result = fetch_exact(
                url=str(evidence["url"]),
                destination=evidence_cache_path(cache_root, evidence),
                expected_sha256=str(evidence["sha256"]),
                expected_size=int(evidence["size"]),
            )
            fetched.append({"id": evidence["id"], "kind": "license-evidence", **result})
    return fetched


@dataclass(frozen=True)
class WheelInspection:
    artifact_name: str
    artifact_version: str
    metadata_name: str
    metadata_version: str
    root_is_purelib: bool
    tags: tuple[str, ...]
    requires_python: str | None
    requires_dist: tuple[str, ...]
    dist_info_prefix: str
    members: Mapping[str, zipfile.ZipInfo]
    record_rows: Mapping[str, tuple[str, str]]


def _single_root_dist_info_prefix(
    members: Mapping[str, zipfile.ZipInfo], context: str
) -> str:
    prefixes = {
        path.parts[0]
        for info in members.values()
        if len((path := PurePosixPath(info.filename)).parts) == 2
        and path.parts[0].endswith(".dist-info")
        and path.parts[1] == "WHEEL"
    }
    if len(prefixes) != 1:
        raise WheelValidationError(
            f"{context}: expected exactly one root .dist-info/WHEEL, "
            f"got {len(prefixes)}"
        )
    return prefixes.pop() + "/"


def _required_dist_info_member(
    members: Mapping[str, zipfile.ZipInfo],
    dist_info_prefix: str,
    basename: str,
    context: str,
) -> zipfile.ZipInfo:
    member = members.get(windows_path_key(dist_info_prefix + basename))
    if member is None:
        raise WheelValidationError(
            f"{context}: root dist-info directory lacks {basename}"
        )
    return member


def _read_bounded(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int = 4 * 1024 * 1024
) -> bytes:
    if info.file_size > limit:
        raise WheelValidationError(f"{info.filename}: metadata member is too large")
    with archive.open(info, "r") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise WheelValidationError(f"{info.filename}: metadata member is too large")
    return data


def inspect_wheel(
    wheel_path: Path,
    artifact: Mapping[str, Any],
    *,
    validate_record_hashes: bool = True,
) -> WheelInspection:
    verify_file(wheel_path, str(artifact["sha256"]), int(artifact["size"]))
    context = f"{artifact['name']}=={artifact['version']}"
    with zipfile.ZipFile(wheel_path) as archive:
        members = validate_zip_members(
            archive, maximum_uncompressed_size=max(int(artifact["size"]) * 80, 1)
        )
        dist_info_prefix = _single_root_dist_info_prefix(members, context)
        wheel_info = _required_dist_info_member(
            members, dist_info_prefix, "WHEEL", context
        )
        metadata_info = _required_dist_info_member(
            members, dist_info_prefix, "METADATA", context
        )
        record_info = _required_dist_info_member(
            members, dist_info_prefix, "RECORD", context
        )

        wheel_metadata = email.message_from_bytes(_read_bounded(archive, wheel_info))
        package_metadata = email.message_from_bytes(
            _read_bounded(archive, metadata_info)
        )
        root_value = wheel_metadata.get("Root-Is-Purelib")
        if root_value not in {"true", "false"}:
            raise WheelValidationError(f"{context}: invalid Root-Is-Purelib")
        tags = tuple(wheel_metadata.get_all("Tag", ()))
        if (
            not tags
            or any(not _tag_is_safe_shape(tag) for tag in tags)
            or not any(_tag_is_target_compatible(tag) for tag in tags)
        ):
            raise WheelValidationError(f"{context}: incompatible WHEEL tag")
        locked_tags = set(str(tag) for tag in artifact.get("wheel_tags", ()))
        if not set(tags).issubset(locked_tags):
            raise WheelValidationError(
                f"{context}: WHEEL tags are not exactly represented in lock"
            )

        metadata_name = package_metadata.get("Name")
        metadata_version = package_metadata.get("Version")
        if not isinstance(metadata_name, str) or not isinstance(metadata_version, str):
            raise WheelValidationError(f"{context}: METADATA lacks Name/Version")
        if canonical_distribution_name(metadata_name) != canonical_distribution_name(
            str(artifact["name"])
        ):
            raise WheelValidationError(f"{context}: METADATA project mismatch")
        if metadata_version != artifact["version"]:
            raise WheelValidationError(f"{context}: METADATA version mismatch")

        record_bytes = _read_bounded(archive, record_info, 32 * 1024 * 1024)
        try:
            record_text = record_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WheelValidationError(f"{context}: RECORD is not UTF-8") from exc
        rows: dict[str, tuple[str, str]] = {}
        row_keys: set[str] = set()
        try:
            reader = csv.reader(io.StringIO(record_text, newline=""))
            for row in reader:
                if len(row) != 3:
                    raise WheelValidationError(
                        f"{context}: RECORD row must contain three fields"
                    )
                record_path, encoded_hash, recorded_size = row
                key = windows_path_key(record_path)
                if key in row_keys:
                    raise WheelValidationError(
                        f"{context}: duplicate/colliding RECORD path {record_path!r}"
                    )
                row_keys.add(key)
                rows[key] = (encoded_hash, recorded_size)
        except csv.Error as exc:
            raise WheelValidationError(f"{context}: invalid RECORD CSV") from exc

        archive_files = {
            key: info for key, info in members.items() if not info.is_dir()
        }
        if set(rows) != set(archive_files):
            missing = sorted(set(archive_files) - set(rows))
            extra = sorted(set(rows) - set(archive_files))
            raise WheelValidationError(
                f"{context}: RECORD/archive mismatch missing={missing[:3]} "
                f"extra={extra[:3]}"
            )

        if validate_record_hashes:
            record_key = windows_path_key(record_info.filename)
            signature_suffixes = ("/record.jws", "/record.p7s")
            for key, info in archive_files.items():
                encoded_hash, recorded_size = rows[key]
                may_be_empty = key == record_key or key.endswith(signature_suffixes)
                if not encoded_hash or not recorded_size:
                    if may_be_empty and not encoded_hash and not recorded_size:
                        continue
                    raise WheelValidationError(
                        f"{context}: missing RECORD integrity for {info.filename}"
                    )
                try:
                    numeric_size = int(recorded_size)
                except ValueError as exc:
                    raise WheelValidationError(
                        f"{context}: invalid RECORD size for {info.filename}"
                    ) from exc
                if numeric_size != info.file_size:
                    raise WheelValidationError(
                        f"{context}: RECORD size mismatch for {info.filename}"
                    )
                if "=" not in encoded_hash:
                    raise WheelValidationError(
                        f"{context}: malformed RECORD hash for {info.filename}"
                    )
                algorithm, encoded_digest = encoded_hash.split("=", 1)
                if algorithm != "sha256":
                    raise WheelValidationError(
                        f"{context}: only sha256 RECORD hashes are accepted"
                    )
                padding = "=" * (-len(encoded_digest) % 4)
                try:
                    expected_digest = base64.urlsafe_b64decode(encoded_digest + padding)
                except (ValueError, base64.binascii.Error) as exc:
                    raise WheelValidationError(
                        f"{context}: invalid RECORD digest for {info.filename}"
                    ) from exc
                with archive.open(info, "r") as stream:
                    actual_digest = bytes.fromhex(sha256_stream(stream)[0])
                if actual_digest != expected_digest:
                    raise WheelValidationError(
                        f"{context}: RECORD hash mismatch for {info.filename}"
                    )

        return WheelInspection(
            artifact_name=str(artifact["name"]),
            artifact_version=str(artifact["version"]),
            metadata_name=metadata_name,
            metadata_version=metadata_version,
            root_is_purelib=root_value == "true",
            tags=tags,
            requires_python=package_metadata.get("Requires-Python"),
            requires_dist=tuple(package_metadata.get_all("Requires-Dist", ())),
            dist_info_prefix=dist_info_prefix,
            members=dict(members),
            record_rows=dict(rows),
        )


def _wheel_member_destination(
    member_name: str,
    inspection: WheelInspection,
) -> str:
    pure = validate_windows_relative_path(member_name)
    parts = pure.parts
    data_index = next(
        (index for index, part in enumerate(parts) if part.lower().endswith(".data")),
        None,
    )
    if data_index is not None:
        if data_index != 0 or len(parts) < 3:
            raise WheelValidationError(f"malformed wheel .data path {member_name!r}")
        scheme = parts[1].lower()
        tail = "/".join(parts[2:])
        if scheme in {"purelib", "platlib"}:
            destination = f"runtime/Lib/site-packages/{tail}"
        elif scheme == "data":
            destination = f"runtime/{tail}"
        elif scheme == "scripts":
            destination = f"runtime/Scripts/{tail}"
        elif scheme == "headers":
            project = canonical_distribution_name(inspection.metadata_name)
            destination = f"runtime/Include/{project}/{tail}"
        else:
            raise WheelValidationError(
                f"unknown wheel .data scheme {scheme!r} in {member_name!r}"
            )
    else:
        destination = f"runtime/Lib/site-packages/{member_name}"
    validate_windows_relative_path(destination)
    return destination


@dataclass
class InstallRegistry:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def reserve(
        self,
        *,
        destination: str,
        source: str,
        sha256: str,
        size: int,
    ) -> None:
        key = windows_path_key(destination)
        previous = self.entries.get(key)
        if previous is not None:
            raise WheelValidationError(
                "wheel staging collision at "
                f"{destination!r}: {previous['source']} vs {source}"
            )
        self.entries[key] = {
            "path": destination,
            "source": source,
            "sha256": sha256,
            "size": size,
        }


def install_wheel(
    wheel_path: Path,
    artifact: Mapping[str, Any],
    stage_root: Path,
    registry: InstallRegistry,
) -> dict[str, Any]:
    inspection = inspect_wheel(wheel_path, artifact)
    installed: list[str] = []
    with zipfile.ZipFile(wheel_path) as archive:
        for info in sorted(
            inspection.members.values(),
            key=lambda item: windows_path_key(item.filename.rstrip("/")),
        ):
            if info.is_dir():
                continue
            destination_relative = _wheel_member_destination(info.filename, inspection)
            with archive.open(info, "r") as stream:
                data = stream.read()
            digest = hashlib.sha256(data).hexdigest()
            registry.reserve(
                destination=destination_relative,
                source=f"{artifact['filename']}!/{info.filename}",
                sha256=digest,
                size=len(data),
            )
            destination = safe_destination(stage_root, destination_relative)
            if destination.exists():
                raise WheelValidationError(
                    f"wheel destination collides with pre-staged runtime file: "
                    f"{destination_relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            installed.append(destination_relative)
    return {
        "name": artifact["name"],
        "version": artifact["version"],
        "filename": artifact["filename"],
        "sha256": artifact["sha256"],
        "installed_file_count": len(installed),
        "dist_info_prefix": inspection.dist_info_prefix,
        "tags": list(inspection.tags),
    }


def _python_version_matches(version: str, requires_python: str | None) -> bool:
    if not requires_python:
        return True
    return version_satisfies(version, requires_python)


def validate_wheel_closure(
    locks: LockSet,
    cache_root: Path,
) -> dict[str, Any]:
    validate_lock_set(locks)
    by_name = {
        canonical_distribution_name(str(artifact["name"])): artifact
        for artifact in locks.wheel_artifacts
    }
    dependencies: dict[str, set[str]] = {}
    active_edges: list[dict[str, str]] = []
    for name, artifact in by_name.items():
        wheel_path = artifact_cache_path(cache_root, artifact)
        inspection = inspect_wheel(wheel_path, artifact)
        if not _python_version_matches(PYTHON_VERSION, inspection.requires_python):
            raise WheelValidationError(
                f"{name}=={artifact['version']} excludes Python {PYTHON_VERSION}"
            )
        selected_extras = [str(item) for item in artifact.get("selected_extras", ())]
        active: set[str] = set()
        for raw_requirement in inspection.requires_dist:
            requirement = parse_requirement(raw_requirement)
            if not requirement_is_active(
                requirement, TARGET_MARKER_ENVIRONMENT, selected_extras
            ):
                continue
            dependency = by_name.get(requirement.name)
            if dependency is None:
                raise WheelValidationError(
                    f"{name}: active dependency {requirement.name!r} is not locked "
                    f"for Windows marker environment"
                )
            if not version_satisfies(str(dependency["version"]), requirement.specifier):
                raise WheelValidationError(
                    f"{name}: locked {requirement.name}=={dependency['version']} "
                    f"does not satisfy {requirement.specifier!r}"
                )
            active.add(requirement.name)
            active_edges.append(
                {
                    "from": name,
                    "to": requirement.name,
                    "requirement": raw_requirement,
                }
            )
        dependencies[name] = active

    roots = {
        parse_requirement(str(requirement)).name
        for requirement in locks.wheels["resolution"]["direct_requirements"]
    }
    reachable: set[str] = set()
    pending = list(sorted(roots))
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(sorted(dependencies[name] - reachable))
    unreferenced = sorted(set(by_name) - reachable)
    if unreferenced:
        raise WheelValidationError(
            f"lock is not the exact direct-requirement closure: {unreferenced}"
        )
    expected_edges = locks.wheels["resolution"].get("active_requires_dist_edges")
    if isinstance(expected_edges, int) and expected_edges != len(active_edges):
        raise WheelValidationError(
            f"expected {expected_edges} active Requires-Dist edges, "
            f"validated {len(active_edges)}"
        )
    return {
        "artifact_count": len(by_name),
        "active_edge_count": len(active_edges),
        "roots": sorted(roots),
        "edges": sorted(
            active_edges,
            key=lambda item: (item["from"], item["to"], item["requirement"]),
        ),
    }


_PYSIDE_KEEP_PYTHON = frozenset(
    {
        "__init__.py",
        "_config.py",
        "_git_pyside_version.py",
        "_utils.py",
        "py.typed",
    }
)
_PYSIDE_KEEP_SUPPORT = frozenset(
    {
        "support/__init__.py",
        "support/deprecated.py",
    }
)
_PYSIDE_KEEP_MODULES = frozenset(
    {
        "QtCore.pyd",
        "QtGui.pyd",
        "QtNetwork.pyd",
        "QtWidgets.pyd",
        "pyside6.abi3.dll",
    }
)
_PYSIDE_KEEP_QT_DLLS = frozenset(
    {
        "Qt6Core.dll",
        "Qt6Gui.dll",
        "Qt6Network.dll",
        "Qt6Widgets.dll",
        "D3Dcompiler_47.dll",
        "opengl32sw.dll",
    }
)
_PYSIDE_KEEP_PLUGIN_PATHS = frozenset(
    {
        "plugins/platforms/qwindows.dll",
        "plugins/styles/qmodernwindowsstyle.dll",
        "plugins/imageformats/qico.dll",
    }
)
_MSVC_DLL_RE = re.compile(
    r"(?i)^(?:vcruntime140(?:_1)?|msvcp140(?:_1|_2|_atomic_wait|_codecvt_ids)?)\.dll$"
)
_ICU_DLL_RE = re.compile(r"(?i)^icu(?:dt|in|uc)\d+\.dll$")
_SETUPTOOLS_SCRIPT_LAUNCHERS = (
    "cli-32.exe",
    "cli-64.exe",
    "cli-arm64.exe",
    "cli.exe",
    "gui-32.exe",
    "gui-64.exe",
    "gui-arm64.exe",
    "gui.exe",
)


def prune_setuptools_script_launchers(site_packages: Path) -> dict[str, Any]:
    package = site_packages / "setuptools"
    if not package.is_dir():
        raise PipelineError("setuptools was locked but is not staged")
    removed: list[str] = []
    for basename in _SETUPTOOLS_SCRIPT_LAUNCHERS:
        path = package / basename
        if not path.is_file():
            raise PipelineError(
                f"locked setuptools script launcher is absent: {basename}"
            )
        path.unlink()
        removed.append(f"setuptools/{basename}")
    return {
        "policy": "remove locked setuptools script launcher templates",
        "removed": removed,
    }


def prune_pyside_essentials(site_packages: Path) -> dict[str, Any]:
    package = site_packages / "PySide6"
    if not package.is_dir():
        raise PipelineError("PySide6-Essentials was locked but PySide6 is not staged")
    removed: list[str] = []
    kept: list[str] = []
    for path in sorted(package.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_dir():
            continue
        relative = path.relative_to(package).as_posix()
        basename = path.name
        keep = (
            relative in _PYSIDE_KEEP_PYTHON
            or relative in _PYSIDE_KEEP_SUPPORT
            or basename in _PYSIDE_KEEP_MODULES
            or basename in _PYSIDE_KEEP_QT_DLLS
            or _MSVC_DLL_RE.fullmatch(basename) is not None
            or _ICU_DLL_RE.fullmatch(basename) is not None
            or relative in _PYSIDE_KEEP_PLUGIN_PATHS
        )
        if keep:
            kept.append(relative)
        else:
            path.unlink()
            removed.append(relative)
    for directory in sorted(
        (path for path in package.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "policy": "PySide6-Essentials QtCore/Gui/Network/Widgets whitelist",
        "kept": kept,
        "removed": removed,
    }


def remove_wheel_bytecode(stage_root: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(stage_root.rglob("*.pyc")):
        if path.is_file():
            removed.append(path.relative_to(stage_root).as_posix())
            path.unlink()
    for directory in sorted(
        (path for path in stage_root.rglob("__pycache__") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def _copy_verified_stream(
    source: BinaryIO,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            while True:
                block = source.read(_HASH_CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                if size > expected_size:
                    raise HashMismatchError(
                        f"{destination}: extracted content exceeds locked size"
                    )
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        actual_sha256 = digest.hexdigest()
        if size != expected_size or actual_sha256 != expected_sha256:
            raise HashMismatchError(
                f"{destination}: expected {expected_size}/{expected_sha256}, "
                f"got {size}/{actual_sha256}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": destination.as_posix(),
        "sha256": expected_sha256,
        "size": expected_size,
    }


def extract_python_embeddable(
    artifact_path: Path,
    artifact: Mapping[str, Any],
    stage_root: Path,
) -> dict[str, Any]:
    verify_file(artifact_path, str(artifact["sha256"]), int(artifact["size"]))
    runtime = stage_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    with zipfile.ZipFile(artifact_path) as archive:
        members = validate_zip_members(
            archive, maximum_uncompressed_size=int(artifact["size"]) * 10
        )
        for info in sorted(
            members.values(),
            key=lambda item: windows_path_key(item.filename.rstrip("/")),
        ):
            if info.is_dir():
                continue
            relative = validate_windows_relative_path(info.filename).as_posix()
            destination = safe_destination(runtime, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, _HASH_CHUNK_SIZE)
            extracted.append(f"runtime/{relative}")

    for inspected in artifact.get("inspected_files", ()):
        relative = validate_windows_relative_path(str(inspected["path"])).as_posix()
        verify_file(
            safe_destination(runtime, relative),
            str(inspected["sha256"]),
            int(inspected["size"]),
        )
    required = ("pythonw.exe", "python313.dll", "python313.zip")
    missing = [name for name in required if not (runtime / name).is_file()]
    if missing:
        raise PipelineError(f"CPython embeddable archive lacks {missing}")
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "sha256": artifact["sha256"],
        "extracted_file_count": len(extracted),
    }


def extract_locked_model(
    artifact_path: Path,
    artifact: Mapping[str, Any],
    stage_root: Path,
) -> dict[str, Any]:
    verify_file(artifact_path, str(artifact["sha256"]), int(artifact["size"]))
    unpack = _require_mapping(artifact.get("unpack"), f"{artifact['id']} unpack")
    if unpack.get("format") != "tar" or unpack.get("reject_unsafe_paths") is not True:
        raise LockValidationError(f"{artifact['id']}: safe TAR policy is required")
    declared = _require_list(unpack.get("files"), f"{artifact['id']} unpack files")
    destination_root = validate_windows_relative_path(
        str(unpack["destination_root"]).rstrip("/")
    ).as_posix()
    output: list[dict[str, Any]] = []
    with tarfile.open(artifact_path, "r:*") as archive:
        members = validate_tar_members(
            archive, maximum_uncompressed_size=int(artifact["size"]) * 2
        )
        for entry in declared:
            source_path = validate_windows_relative_path(
                str(entry["source_path"])
            ).as_posix()
            source_key = windows_path_key(source_path)
            member = members.get(source_key)
            if member is None or not member.isreg():
                raise HashMismatchError(
                    f"{artifact['id']}: locked model member missing: {source_path}"
                )
            destination_tail = validate_windows_relative_path(
                str(entry["destination_path"])
            ).as_posix()
            relative = f"{destination_root}/{destination_tail}"
            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(f"cannot read TAR member {source_path}")
            with source:
                details = _copy_verified_stream(
                    source,
                    safe_destination(stage_root, relative),
                    expected_sha256=str(entry["sha256"]),
                    expected_size=int(entry["size"]),
                )
            details["path"] = relative
            output.append(details)
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "sha256": artifact["sha256"],
        "files": output,
    }


def extract_locked_font(
    artifact_path: Path,
    artifact: Mapping[str, Any],
    stage_root: Path,
) -> dict[str, Any]:
    verify_file(artifact_path, str(artifact["sha256"]), int(artifact["size"]))
    unpack = _require_mapping(artifact.get("unpack"), f"{artifact['id']} unpack")
    if unpack.get("format") != "zip" or unpack.get("reject_unsafe_paths") is not True:
        raise LockValidationError(f"{artifact['id']}: safe ZIP policy is required")
    selected = _require_list(
        unpack.get("selected_files"), f"{artifact['id']} selected files"
    )
    output: list[dict[str, Any]] = []
    with zipfile.ZipFile(artifact_path) as archive:
        members = validate_zip_members(
            archive, maximum_uncompressed_size=int(artifact["size"]) * 4
        )
        for entry in selected:
            source_path = validate_windows_relative_path(
                str(entry["source_path"])
            ).as_posix()
            info = members.get(windows_path_key(source_path))
            if info is None or info.is_dir():
                raise HashMismatchError(
                    f"{artifact['id']}: locked font member missing: {source_path}"
                )
            locked_destination = validate_windows_relative_path(
                str(entry["destination_path"])
            ).as_posix()
            if locked_destination.startswith("fonts/"):
                destination = f"assets/{locked_destination}"
            elif locked_destination.startswith("licenses/"):
                destination = (
                    "LICENSES/resources/" + locked_destination[len("licenses/") :]
                )
            else:
                raise LockValidationError(
                    f"{artifact['id']}: unsupported font destination "
                    f"{locked_destination!r}"
                )
            with archive.open(info, "r") as source:
                details = _copy_verified_stream(
                    source,
                    safe_destination(stage_root, destination),
                    expected_sha256=str(entry["sha256"]),
                    expected_size=int(entry["size"]),
                )
            details["path"] = destination
            output.append(details)
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "sha256": artifact["sha256"],
        "files": output,
    }


def stage_runtime_resources(
    locks: LockSet,
    cache_root: Path,
    stage_root: Path,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_kinds: dict[str, int] = {}
    for artifact in locks.runtime_resources:
        kind = str(artifact["kind"])
        seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
        path = artifact_cache_path(cache_root, artifact)
        if kind == "python_embeddable_runtime":
            output.append(extract_python_embeddable(path, artifact, stage_root))
        elif kind == "paddle_inference_model":
            output.append(extract_locked_model(path, artifact, stage_root))
        elif kind == "font":
            output.append(extract_locked_font(path, artifact, stage_root))
        else:
            raise LockValidationError(f"unsupported runtime resource kind {kind!r}")
    if seen_kinds.get("python_embeddable_runtime") != 1:
        raise LockValidationError("exactly one CPython embeddable runtime is required")
    if seen_kinds.get("paddle_inference_model") != 2:
        raise LockValidationError("exactly two locked OCR model archives are required")
    if seen_kinds.get("font") != 1:
        raise LockValidationError("exactly one locked application font is required")
    return output


def prune_cpython_console_launcher(stage_root: Path) -> dict[str, Any]:
    console_launcher = stage_root / "runtime" / "python.exe"
    gui_launcher = stage_root / "runtime" / "pythonw.exe"
    if not console_launcher.is_file() or not gui_launcher.is_file():
        raise PipelineError(
            "CPython embeddable runtime must contain python.exe and pythonw.exe"
        )
    digest, size = sha256_file(console_launcher)
    console_launcher.unlink()
    return {
        "id": "cpython-console-launcher-pruning",
        "kind": "runtime-pruning",
        "policy": "retain only pythonw.exe for the GUI application runtime",
        "removed": [
            {
                "path": "runtime/python.exe",
                "sha256": digest,
                "size": size,
            }
        ],
    }


def _copy_source_tree(source: Path, destination: Path) -> list[str]:
    if not source.is_dir():
        raise PipelineError(f"application source directory not found: {source}")
    copied: list[str] = []
    source = source.resolve()
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(source).as_posix()
        if any(
            part in {"__pycache__", ".git"} for part in path.relative_to(source).parts
        ):
            continue
        if path.is_symlink():
            raise PipelineError(f"application source symlink is forbidden: {path}")
        if path.is_dir():
            continue
        if path.suffix == ".pyc":
            continue
        validate_windows_relative_path(relative)
        target = safe_destination(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(relative)
    return copied


def stage_application_source(
    source_package: Path,
    entry_script: Path,
    stage_root: Path,
    *,
    readme: Path | None,
) -> dict[str, Any]:
    app_root = stage_root / "app"
    package_target = app_root / "textsnap"
    copied = _copy_source_tree(source_package, package_target)
    if not entry_script.is_file():
        raise PipelineError(f"application entry script not found: {entry_script}")
    entry_text = entry_script.read_text(encoding="utf-8")
    entry_tree = ast.parse(entry_text, filename=str(entry_script))
    textsnap_import_index = next(
        (
            index
            for index, node in enumerate(entry_tree.body)
            if (
                isinstance(node, ast.ImportFrom)
                and isinstance(node.module, str)
                and (node.module == "textsnap" or node.module.startswith("textsnap."))
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "textsnap" or alias.name.startswith("textsnap.")
                    for alias in node.names
                )
            )
        ),
        None,
    )
    protection_index = next(
        (
            index
            for index, node in enumerate(entry_tree.body)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "sys"
                        and target.attr == "dont_write_bytecode"
                        for target in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                )
                or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Attribute)
                    and isinstance(node.target.value, ast.Name)
                    and node.target.value.id == "sys"
                    and node.target.attr == "dont_write_bytecode"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                )
            )
        ),
        None,
    )
    if (
        textsnap_import_index is None
        or protection_index is None
        or protection_index >= textsnap_import_index
    ):
        raise PipelineError(
            "app/main.py must set sys.dont_write_bytecode = True before "
            "importing textsnap"
        )
    app_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry_script, app_root / "main.py")
    if readme is not None:
        if not readme.is_file():
            raise PipelineError(f"release README not found: {readme}")
        shutil.copyfile(readme, stage_root / "README.zh-CN.md")
    return {"source_file_count": len(copied), "entry_point": "app/main.py"}


def write_runtime_configuration(stage_root: Path) -> dict[str, Any]:
    runtime = stage_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    pth = runtime / "python313._pth"
    pth.write_text(
        "python313.zip\n.\nLib/site-packages\n../app\n",
        encoding="utf-8",
        newline="\n",
    )
    if "import site" in pth.read_text(encoding="utf-8").lower():
        raise PipelineError("python313._pth must not import site")
    (runtime / "qt.conf").write_text(
        "[Paths]\nPrefix = Lib/site-packages/PySide6\nPlugins = plugins\n",
        encoding="utf-8",
        newline="\n",
    )
    for relative in ("runtime/pdx-cache", "runtime/pdx-cache/temp", "data"):
        (stage_root / relative).mkdir(parents=True, exist_ok=True)
    return {
        "pth": "runtime/python313._pth",
        "qt_conf": "runtime/qt.conf",
        "precreated_directories": [
            "runtime/pdx-cache",
            "runtime/pdx-cache/temp",
            "data",
        ],
    }


def _runtime_stdlib_magic(stage_root: Path) -> bytes:
    stdlib = stage_root / "runtime" / "python313.zip"
    if not stdlib.is_file():
        raise PipelineError("runtime/python313.zip is missing")
    with zipfile.ZipFile(stdlib) as archive:
        candidates = sorted(
            (
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.endswith(".pyc")
            ),
            key=lambda info: info.filename,
        )
        if not candidates:
            raise PipelineError("python313.zip contains no bytecode magic sample")
        with archive.open(candidates[0], "r") as stream:
            magic = stream.read(4)
    if len(magic) != 4:
        raise PipelineError("invalid bytecode sample in python313.zip")
    return magic


def precompile_checked_hash_bytecode(
    python_executable: Path,
    stage_root: Path,
) -> dict[str, Any]:
    executable = python_executable.resolve()
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-B",
            "-c",
            (
                "import importlib.util,json,platform,sys;"
                "print(json.dumps({'version':list(sys.version_info[:3]),"
                "'implementation':platform.python_implementation(),"
                "'magic':importlib.util.MAGIC_NUMBER.hex()}))"
            ),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        details = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("bytecode Python probe returned invalid JSON") from exc
    if (
        details.get("version") != [3, 13, 14]
        or details.get("implementation") != "CPython"
    ):
        raise PipelineError(
            f"bytecode compilation requires exact CPython 3.13.14; got {details!r}"
        )
    runtime_magic = _runtime_stdlib_magic(stage_root)
    if details.get("magic") != runtime_magic.hex():
        raise PipelineError(
            "bytecode compiler magic does not match locked embeddable runtime"
        )
    roots = [
        stage_root / "app",
        stage_root / "runtime" / "Lib" / "site-packages",
    ]
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = "315532800"
    subprocess.run(
        [
            str(executable),
            "-I",
            "-B",
            "-m",
            "compileall",
            "-q",
            "-f",
            "-j",
            "1",
            "--invalidation-mode",
            "checked-hash",
            "-s",
            str(stage_root),
            "-p",
            ".",
            *(str(root) for root in roots),
        ],
        check=True,
        env=environment,
    )
    compiled: list[str] = []
    for path in sorted(stage_root.rglob("*.pyc")):
        header = path.read_bytes()[:8]
        if len(header) != 8 or header[:4] != runtime_magic:
            raise PipelineError(f"invalid generated pyc header: {path}")
        flags = int.from_bytes(header[4:8], "little")
        if flags != 3:
            raise PipelineError(
                f"generated pyc is not checked-hash mode: {path} flags={flags}"
            )
        compiled.append(path.relative_to(stage_root).as_posix())
    if not compiled:
        raise PipelineError("bytecode compilation produced no .pyc files")
    return {
        "version": "3.13.14",
        "magic": runtime_magic.hex(),
        "invalidation_mode": "checked-hash",
        "file_count": len(compiled),
    }


def _copy_license_bytes(
    destination: Path,
    data: bytes,
    *,
    source: str,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PipelineError(f"license destination collision: {destination}")
    destination.write_bytes(data)
    return {
        "path": destination.as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "source": source,
    }


def _artifact_license_identifier(artifact: Mapping[str, Any]) -> str:
    if "name" in artifact:
        return (
            f"{canonical_distribution_name(str(artifact['name']))}"
            f"=={artifact['version']}"
        )
    return str(artifact["id"])


def _declared_evidence_ids(artifact: Mapping[str, Any]) -> list[str]:
    license_info = artifact.get("license", {})
    if "evidence_ids" in license_info:
        return [str(item) for item in license_info.get("evidence_ids", ())]
    evidence = license_info.get("evidence", {})
    return [str(item) for item in evidence.get("external_evidence_ids", ())]


def _external_evidence_is_affirmative(
    evidence: Mapping[str, Any],
    *,
    artifact: Mapping[str, Any],
) -> bool:
    if evidence.get("status") not in RESOLVED_EXTERNAL_EVIDENCE_STATUSES:
        return False
    if evidence.get("kind") not in AFFIRMATIVE_LICENSE_EVIDENCE_KINDS:
        return False
    expression = evidence.get("expression")
    artifact_expression = artifact.get("license", {}).get("expression")
    if (
        not isinstance(expression, str)
        or not expression
        or expression.strip().upper() == "UNKNOWN"
        or expression != artifact_expression
    ):
        return False
    if evidence.get("version") != artifact.get("version"):
        return False
    artifact_name = artifact.get("name")
    if isinstance(artifact_name, str):
        component = evidence.get("component")
        if not isinstance(component, str) or canonical_distribution_name(
            component
        ) != canonical_distribution_name(artifact_name):
            return False
    return bool(_SHA256_RE.fullmatch(str(evidence.get("sha256", ""))))


def _verified_artifact_evidence_reasons(
    artifact: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    license_info = artifact.get("license", {})
    local_evidence = license_info.get("evidence", {})
    if not isinstance(local_evidence, Mapping):
        local_evidence = {}
    embedded_paths = local_evidence.get("embedded_paths", ())
    has_embedded = (
        local_evidence.get("status") == "embedded"
        and isinstance(embedded_paths, list)
        and bool(embedded_paths)
    )
    declared_ids = _declared_evidence_ids(artifact)
    affirmative_ids = [
        evidence_id
        for evidence_id in declared_ids
        if evidence_id in evidence_by_id
        and _external_evidence_is_affirmative(
            evidence_by_id[evidence_id],
            artifact=artifact,
        )
    ]
    has_external = bool(affirmative_ids) and (
        local_evidence.get("status") == "external_locked"
        or "evidence_ids" in license_info
        or has_embedded
    )
    reasons: list[str] = []
    if not has_embedded and not has_external:
        reasons.append(
            "verified artifact lacks embedded or affirmative external license evidence"
        )
    for evidence_id in declared_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is not None and not _external_evidence_is_affirmative(
            evidence,
            artifact=artifact,
        ):
            reasons.append(
                f"evidence {evidence_id!r} does not affirm this exact "
                "component/version/license"
            )
    return reasons


def _verified_obligation_resolution_reasons(
    obligation: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    resolution = obligation.get("resolution")
    if not isinstance(resolution, Mapping):
        return ["verified obligation lacks a structured resolution record"]
    if resolution.get("status") != "verified":
        return ["obligation resolution status is not verified"]
    method = resolution.get("method")
    if not isinstance(method, str) or not method.strip():
        return ["obligation resolution method is absent"]
    resolution_ids = resolution.get("evidence_ids")
    if (
        not isinstance(resolution_ids, list)
        or not resolution_ids
        or not all(isinstance(item, str) and item for item in resolution_ids)
    ):
        return ["verified obligation lacks resolution evidence ids"]

    reasons: list[str] = []
    evidence_items: list[Mapping[str, Any]] = []
    for evidence_id in resolution_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            reasons.append(f"resolution evidence {evidence_id!r} is absent")
            continue
        evidence_items.append(evidence)
        if evidence.get("status") not in RESOLVED_EXTERNAL_EVIDENCE_STATUSES:
            reasons.append(f"resolution evidence {evidence_id!r} remains unresolved")
        if evidence.get("kind") not in AFFIRMATIVE_LICENSE_EVIDENCE_KINDS:
            reasons.append(
                f"resolution evidence {evidence_id!r} is not affirmative license evidence"
            )
        if not _SHA256_RE.fullmatch(str(evidence.get("sha256", ""))):
            reasons.append(f"resolution evidence {evidence_id!r} lacks locked SHA-256")

    identifier = str(obligation.get("id", ""))
    if identifier == "aistudio-sdk-license-missing" and not any(
        item.get("kind") == "publisher_redistribution_authorization"
        and item.get("component") == "aistudio-sdk"
        and item.get("version") == "0.3.8"
        for item in evidence_items
    ):
        reasons.append(
            "aistudio-sdk requires publisher redistribution authorization "
            "for version 0.3.8"
        )
    if identifier == "bos-model-license-relationship" and not all(
        any(
            item.get("kind") == "publisher_artifact_license"
            and item.get("component") == model_name
            for item in evidence_items
        )
        for model_name in ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
    ):
        reasons.append(
            "both locked BOS models require publisher artifact-specific licenses"
        )
    return reasons


def evaluate_license_gate(locks: LockSet) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    evidence_by_id = {str(item["id"]): item for item in locks.external_evidence}
    for artifact in locks.all_runtime_artifacts:
        component = _artifact_license_identifier(artifact)
        license_info = artifact.get("license", {})
        status = license_info.get("status")
        expression = str(license_info.get("expression", "UNKNOWN"))
        redistributable = license_info.get("redistributable")
        reasons: list[str] = []
        if status not in ALLOWED_RELEASE_LICENSE_STATUSES:
            reasons.append(f"status is {status!r}, not verified")
        if redistributable is not True:
            reasons.append("redistributable is not explicitly true")
        if expression.strip().upper() == "UNKNOWN":
            reasons.append("license expression is UNKNOWN")
        for evidence_id in _declared_evidence_ids(artifact):
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                reasons.append(f"evidence {evidence_id!r} is absent")
            elif evidence.get("status") not in RESOLVED_EXTERNAL_EVIDENCE_STATUSES:
                reasons.append(
                    f"evidence {evidence_id!r} status is {evidence.get('status')!r}"
                )
            elif not _SHA256_RE.fullmatch(str(evidence.get("sha256", ""))):
                reasons.append(f"evidence {evidence_id!r} lacks locked SHA-256")
        if status in ALLOWED_RELEASE_LICENSE_STATUSES:
            reasons.extend(
                _verified_artifact_evidence_reasons(artifact, evidence_by_id)
            )
        if reasons:
            blocker_id = component.replace("==", "-").replace(" ", "-")
            blockers.append(
                {
                    "id": f"artifact-{blocker_id}",
                    "component": component,
                    "status": status,
                    "reason": "; ".join(reasons),
                }
            )

    for obligation in locks.licenses.get("release_obligations", ()):
        if obligation.get("blocks_release") is not True:
            continue
        status = obligation.get("status")
        reasons: list[str] = []
        if status not in ALLOWED_RELEASE_LICENSE_STATUSES:
            reasons.append(str(obligation.get("reason", "unresolved obligation")))
        else:
            reasons.extend(
                _verified_obligation_resolution_reasons(
                    obligation,
                    evidence_by_id,
                )
            )
        if not reasons:
            continue
        blockers.append(
            {
                "id": str(obligation.get("id", "unnamed-obligation")),
                "component": ", ".join(
                    str(item) for item in obligation.get("components", ())
                ),
                "status": status,
                "reason": "; ".join(reasons),
                "resolution_required": obligation.get("resolution_required"),
            }
        )
    return sorted(blockers, key=lambda item: str(item["id"]))


def _locked_external_evidence_payload(
    cached: Path,
    evidence: Mapping[str, Any],
) -> tuple[bytes, str, str]:
    member = evidence.get("member")
    source_url = str(evidence["url"])
    if member is None:
        suffix = PurePosixPath(urllib.parse.urlparse(source_url).path).suffix
        return cached.read_bytes(), suffix, source_url

    if not isinstance(member, Mapping):
        raise LockValidationError("external evidence member must be an object")
    member_path = str(member["path"])
    expected_size = int(member["size"])
    expected_sha256 = str(member["sha256"])
    try:
        with zipfile.ZipFile(cached) as archive:
            members = validate_zip_members(archive)
            info = members.get(windows_path_key(member_path))
            if info is None or info.is_dir() or info.filename != member_path:
                raise HashMismatchError(
                    f"{cached}: locked evidence member {member_path!r} is absent"
                )
            with archive.open(info, "r") as stream:
                data = stream.read(expected_size + 1)
    except zipfile.BadZipFile as exc:
        raise HashMismatchError(f"{cached}: evidence archive is not a ZIP") from exc
    if (
        len(data) != expected_size
        or hashlib.sha256(data).hexdigest() != expected_sha256
    ):
        raise HashMismatchError(
            f"{cached}: locked evidence member {member_path!r} does not match"
        )
    suffix = PurePosixPath(member_path).suffix
    return data, suffix, f"{source_url}!/{member_path}"


def collect_licenses(
    locks: LockSet,
    cache_root: Path,
    stage_root: Path,
    *,
    enforce_release_gate: bool,
) -> dict[str, Any]:
    license_root = stage_root / "LICENSES"
    license_root.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, Any]] = []
    output_keys: set[str] = set()

    def remember(details: dict[str, Any]) -> dict[str, Any]:
        absolute = Path(details["path"])
        relative = absolute.relative_to(stage_root).as_posix()
        key = windows_path_key(relative)
        if key in output_keys:
            raise PipelineError(f"duplicate collected license path {relative}")
        output_keys.add(key)
        return {**details, "path": relative}

    for artifact in locks.wheel_artifacts:
        identifier = _artifact_license_identifier(artifact)
        license_info = artifact["license"]
        evidence = license_info.get("evidence", {})
        paths: list[dict[str, Any]] = []
        embedded = [str(path) for path in evidence.get("embedded_paths", ())]
        if embedded:
            wheel_path = artifact_cache_path(cache_root, artifact)
            inspection = inspect_wheel(wheel_path, artifact)
            with zipfile.ZipFile(wheel_path) as archive:
                members = inspection.members
                for source_path in embedded:
                    source_key = windows_path_key(source_path)
                    info = members.get(source_key)
                    if info is None or info.is_dir():
                        raise LicenseGateError(
                            [
                                {
                                    "id": f"{identifier}-embedded-license-missing",
                                    "component": identifier,
                                    "reason": (
                                        f"declared embedded license {source_path!r} "
                                        "is absent from locked wheel"
                                    ),
                                }
                            ]
                        )
                    with archive.open(info, "r") as source:
                        data = source.read()
                    relative_tail = PurePosixPath(source_path).as_posix()
                    validate_windows_relative_path(relative_tail)
                    destination = (
                        license_root
                        / "python"
                        / f"{canonical_distribution_name(str(artifact['name']))}"
                        f"-{artifact['version']}"
                        / Path(*PurePosixPath(relative_tail).parts)
                    )
                    details = _copy_license_bytes(
                        destination,
                        data,
                        source=f"{artifact['filename']}!/{source_path}",
                    )
                    paths.append(remember(details))
        components.append(
            {
                "component": identifier,
                "expression": license_info.get("expression"),
                "status": license_info.get("status"),
                "redistributable": license_info.get("redistributable"),
                "evidence_ids": _declared_evidence_ids(artifact),
                "files": paths,
            }
        )

    for artifact in locks.runtime_resources:
        identifier = _artifact_license_identifier(artifact)
        paths: list[dict[str, Any]] = []
        if artifact.get("kind") == "python_embeddable_runtime":
            source = stage_root / "runtime" / "LICENSE.txt"
            if source.is_file():
                data = source.read_bytes()
                details = _copy_license_bytes(
                    license_root
                    / "resources"
                    / f"CPython-{artifact['version']}"
                    / "LICENSE.txt",
                    data,
                    source="runtime/LICENSE.txt",
                )
                paths.append(remember(details))
        elif artifact.get("kind") == "font":
            for selected in artifact.get("unpack", {}).get("selected_files", ()):
                locked_destination = str(selected.get("destination_path", ""))
                if not locked_destination.startswith("licenses/"):
                    continue
                staged = (
                    license_root / "resources" / locked_destination[len("licenses/") :]
                )
                if not staged.is_file():
                    raise LicenseGateError(
                        [
                            {
                                "id": f"{identifier}-license-missing",
                                "component": identifier,
                                "reason": f"staged font license is absent: {staged}",
                            }
                        ]
                    )
                digest, size = sha256_file(staged)
                if digest != selected["sha256"] or size != selected["size"]:
                    raise HashMismatchError(f"staged font license mismatch: {staged}")
                paths.append(
                    remember(
                        {
                            "path": staged.as_posix(),
                            "sha256": digest,
                            "size": size,
                            "source": (
                                f"{artifact['filename']}!/{selected['source_path']}"
                            ),
                        }
                    )
                )
        license_info = artifact["license"]
        components.append(
            {
                "component": identifier,
                "expression": license_info.get("expression"),
                "status": license_info.get("status"),
                "redistributable": license_info.get("redistributable"),
                "evidence_ids": _declared_evidence_ids(artifact),
                "files": paths,
            }
        )

    evidence_by_id = {str(item["id"]): item for item in locks.external_evidence}
    referenced_evidence = sorted(
        {
            evidence_id
            for artifact in locks.all_runtime_artifacts
            for evidence_id in _declared_evidence_ids(artifact)
        }
        | {
            str(evidence_id)
            for obligation in locks.licenses.get("release_obligations", ())
            if obligation.get("blocks_release") is True
            for evidence_id in obligation.get("evidence_ids", ())
        }
    )
    collected_evidence: list[dict[str, Any]] = []
    for identifier in referenced_evidence:
        evidence = evidence_by_id.get(identifier)
        if evidence is None:
            raise LockValidationError(
                f"referenced external evidence is not locked: {identifier}"
            )
        cached = evidence_cache_path(cache_root, evidence)
        verify_file(cached, str(evidence["sha256"]), int(evidence["size"]))
        data, suffix, source = _locked_external_evidence_payload(cached, evidence)
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            suffix = ".evidence"
        destination = license_root / "evidence" / f"{identifier}{suffix.lower()}"
        details = _copy_license_bytes(
            destination,
            data,
            source=source,
        )
        collected_evidence.append(
            {
                **remember(details),
                "id": identifier,
                "status": evidence.get("status"),
                "relationship": evidence.get("relationship"),
            }
        )

    blockers = evaluate_license_gate(locks)
    files_on_disk = sorted(
        path.relative_to(stage_root).as_posix()
        for path in license_root.rglob("*")
        if path.is_file() and path.name != "INDEX.json"
    )
    indexed_files = sorted(
        [file["path"] for component in components for file in component["files"]]
        + [item["path"] for item in collected_evidence]
    )
    if files_on_disk != indexed_files:
        raise PipelineError(
            "license collection is not bidirectional: filesystem and index differ"
        )
    index = {
        "schema_version": "1.0.0",
        "scope": locks.licenses.get("policy", {}).get("scope"),
        "release_gate_passed": not blockers,
        "components": components,
        "external_evidence": collected_evidence,
        "files": indexed_files,
        "blockers": blockers,
    }
    write_canonical_json(license_root / "INDEX.json", index)
    if enforce_release_gate and blockers:
        raise LicenseGateError(blockers)
    return {
        "passed": not blockers,
        "component_count": len(components),
        "file_count": len(indexed_files),
        "blockers": blockers,
    }


@dataclass(frozen=True)
class PeInfo:
    path: str
    machine: int
    timestamp: int
    optional_magic: int
    subsystem: int
    characteristics: int
    imports: tuple[str, ...]


def _read_c_string(data: bytes, offset: int, *, limit: int = 4096) -> str:
    if offset < 0 or offset >= len(data):
        raise PeValidationError(f"PE string offset outside file: {offset}")
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end < 0:
        raise PeValidationError("unterminated PE import name")
    try:
        return data[offset:end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PeValidationError("PE import name is not ASCII") from exc


def inspect_pe(path: Path, *, display_path: str | None = None) -> PeInfo:
    data = path.read_bytes()
    label = display_path or path.as_posix()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PeValidationError(f"{label}: missing DOS MZ header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset < 0x40 or pe_offset + 24 > len(data):
        raise PeValidationError(f"{label}: invalid PE header offset")
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PeValidationError(f"{label}: missing PE signature")
    file_header = pe_offset + 4
    (
        machine,
        number_of_sections,
        timestamp,
        _symbol_table,
        _number_of_symbols,
        optional_size,
        characteristics,
    ) = struct.unpack_from("<HHIIIHH", data, file_header)
    optional = file_header + 20
    if optional + optional_size > len(data) or optional_size < 112:
        raise PeValidationError(f"{label}: truncated optional header")
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic != 0x20B:
        raise PeValidationError(
            f"{label}: expected PE32+ optional magic 0x20b, got 0x{magic:x}"
        )
    subsystem = struct.unpack_from("<H", data, optional + 68)[0]
    image_base = struct.unpack_from("<Q", data, optional + 24)[0]
    rva_count = struct.unpack_from("<I", data, optional + 108)[0]
    section_table = optional + optional_size
    if section_table + number_of_sections * 40 > len(data):
        raise PeValidationError(f"{label}: truncated section table")
    sections: list[tuple[int, int, int, int]] = []
    for index in range(number_of_sections):
        section = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<IIII", data, section + 8
        )
        if raw_pointer + raw_size > len(data):
            raise PeValidationError(f"{label}: section raw data escapes file")
        sections.append(
            (virtual_address, max(virtual_size, raw_size), raw_pointer, raw_size)
        )

    def rva_to_offset(rva: int) -> int:
        for virtual_address, span, raw_pointer, raw_size in sections:
            if virtual_address <= rva < virtual_address + span:
                delta = rva - virtual_address
                if delta >= raw_size:
                    raise PeValidationError(
                        f"{label}: RVA points into virtual-only section data"
                    )
                return raw_pointer + delta
        if rva < section_table:
            return rva
        raise PeValidationError(f"{label}: RVA 0x{rva:x} is unmapped")

    imports: set[str] = set()
    if rva_count > 1 and optional_size >= 128:
        import_rva, import_size = struct.unpack_from("<II", data, optional + 120)
        if import_rva:
            descriptor = rva_to_offset(import_rva)
            maximum = min(len(data), descriptor + max(import_size, 20))
            while descriptor + 20 <= maximum:
                fields = struct.unpack_from("<IIIII", data, descriptor)
                if fields == (0, 0, 0, 0, 0):
                    break
                name_rva = fields[3]
                imports.add(_read_c_string(data, rva_to_offset(name_rva)).lower())
                descriptor += 20
            else:
                raise PeValidationError(f"{label}: unterminated import directory")
    if rva_count > 13 and optional_size >= 224:
        delay_rva, delay_size = struct.unpack_from("<II", data, optional + 216)
        if delay_rva:
            descriptor = rva_to_offset(delay_rva)
            maximum = min(len(data), descriptor + max(delay_size, 32))
            while descriptor + 32 <= maximum:
                fields = struct.unpack_from("<IIIIIIII", data, descriptor)
                if fields == (0, 0, 0, 0, 0, 0, 0, 0):
                    break
                attributes, name_address = fields[:2]
                name_rva = name_address if attributes & 1 else name_address - image_base
                if name_rva <= 0:
                    raise PeValidationError(
                        f"{label}: invalid delay-import name address"
                    )
                imports.add(_read_c_string(data, rva_to_offset(name_rva)).lower())
                descriptor += 32
            else:
                raise PeValidationError(f"{label}: unterminated delay-import directory")
    return PeInfo(
        path=label,
        machine=machine,
        timestamp=timestamp,
        optional_magic=magic,
        subsystem=subsystem,
        characteristics=characteristics,
        imports=tuple(sorted(imports)),
    )


_SYSTEM_DLLS = frozenset(
    {
        "advapi32.dll",
        "authz.dll",
        "avrt.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "cabinet.dll",
        "cfgmgr32.dll",
        "combase.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "cryptbase.dll",
        "crypt32.dll",
        "d2d1.dll",
        "d3d9.dll",
        "d3d11.dll",
        "d3d12.dll",
        "d3dcompiler_47.dll",
        "dcomp.dll",
        "dbghelp.dll",
        "dnsapi.dll",
        "dsound.dll",
        "dwrite.dll",
        "dwmapi.dll",
        "dxcore.dll",
        "dxgi.dll",
        "gdi32.dll",
        "hid.dll",
        "icuuc.dll",
        "imagehlp.dll",
        "imm32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "ksuser.dll",
        "mf.dll",
        "mfplat.dll",
        "mfreadwrite.dll",
        "mpr.dll",
        "msacm32.dll",
        "mswsock.dll",
        "msimg32.dll",
        "msvcrt.dll",
        "ncrypt.dll",
        "netapi32.dll",
        "normaliz.dll",
        "ntdll.dll",
        "oleacc.dll",
        "ole32.dll",
        "oleaut32.dll",
        "opengl32.dll",
        "pdh.dll",
        "powrprof.dll",
        "propsys.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "sechost.dll",
        "secur32.dll",
        "setupapi.dll",
        "shcore.dll",
        "shell32.dll",
        "shlwapi.dll",
        "urlmon.dll",
        "user32.dll",
        "userenv.dll",
        "uiautomationcore.dll",
        "usp10.dll",
        "uxtheme.dll",
        "version.dll",
        "windowscodecs.dll",
        "wininet.dll",
        "winhttp.dll",
        "winmm.dll",
        "winspool.drv",
        "wintrust.dll",
        "wldap32.dll",
        "ws2_32.dll",
        "wsock32.dll",
        "wtsapi32.dll",
    }
)
_ALLOWED_CONFLICTING_MSVC_DLLS = frozenset(
    {
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll",
        "msvcp140_codecvt_ids.dll",
    }
)


def _is_system_import(name: str) -> bool:
    return (
        name in _SYSTEM_DLLS
        or name.startswith("api-ms-win-")
        or name.startswith("ext-ms-win-")
    )


def _allowed_conflicting_msvc_location(relative: str, basename: str) -> bool:
    normalized = relative.casefold()
    allowed = {
        f"runtime/{basename}",
        f"runtime/lib/site-packages/pyside6/{basename}",
        f"runtime/lib/site-packages/shiboken6/{basename}",
    }
    return normalized in allowed


def _import_is_statically_local(
    importer: str,
    occurrences: Sequence[tuple[Path, str]],
    stage_root: Path,
) -> bool:
    importer_directory = PurePosixPath(importer).parent
    allowed_directories = {importer_directory}
    if importer == "TextSnapLayout.exe":
        allowed_directories.add(PurePosixPath("."))
    if PurePosixPath(importer).parts[:1] == ("runtime",):
        allowed_directories.add(PurePosixPath("runtime"))
    return any(
        PurePosixPath(path.relative_to(stage_root).as_posix()).parent
        in allowed_directories
        for path, _digest in occurrences
    )


def validate_pe_tree(
    stage_root: Path,
    *,
    require_launcher: bool = True,
) -> dict[str, Any]:
    pe_paths = sorted(
        (
            path
            for path in stage_root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"}
        ),
        key=lambda path: path.relative_to(stage_root).as_posix().casefold(),
    )
    if not pe_paths:
        raise PeValidationError("staging contains no PE files")
    infos: list[PeInfo] = []
    by_basename: dict[str, list[tuple[Path, str]]] = {}
    for path in pe_paths:
        relative = path.relative_to(stage_root).as_posix()
        info = inspect_pe(path, display_path=relative)
        if info.machine != 0x8664:
            raise PeValidationError(
                f"{relative}: expected AMD64 machine 0x8664, got 0x{info.machine:x}"
            )
        if info.optional_magic != 0x20B:
            raise PeValidationError(f"{relative}: expected PE32+")
        if path.suffix.lower() == ".exe" and info.subsystem != 2:
            raise PeValidationError(
                f"{relative}: executable must use Windows GUI subsystem"
            )
        digest = sha256_file(path)[0]
        by_basename.setdefault(path.name.casefold(), []).append((path, digest))
        infos.append(info)

    launcher = stage_root / "TextSnapLayout.exe"
    if require_launcher:
        if not launcher.is_file():
            raise PeValidationError("TextSnapLayout.exe is missing")
        launcher_info = next(
            info for info in infos if info.path == "TextSnapLayout.exe"
        )
        if launcher_info.timestamp != 0:
            raise PeValidationError("launcher COFF timestamp must be zero")

    duplicates: list[dict[str, Any]] = []
    for basename, occurrences in sorted(by_basename.items()):
        if not basename.endswith(".dll"):
            continue
        if len(occurrences) < 2:
            continue
        hashes = {digest for _, digest in occurrences}
        details = {
            "basename": basename,
            "files": [
                {
                    "path": path.relative_to(stage_root).as_posix(),
                    "sha256": digest,
                }
                for path, digest in occurrences
            ],
            "hashes_differ": len(hashes) > 1,
        }
        if len(hashes) > 1 and basename not in _ALLOWED_CONFLICTING_MSVC_DLLS:
            raise PeValidationError(
                f"conflicting duplicate DLL basename {basename}: "
                f"{[item['path'] for item in details['files']]}"
            )
        if len(hashes) > 1:
            paths = [str(item["path"]).casefold() for item in details["files"]]
            if not all(
                _allowed_conflicting_msvc_location(path, basename) for path in paths
            ):
                raise PeValidationError(
                    f"conflicting {basename} appears outside approved runtime, "
                    "PySide6, or shiboken6 locations"
                )
            details["reason"] = (
                "retained in vendor-provided relative directories; basename-only "
                "deduplication is forbidden"
            )
        else:
            details["reason"] = "byte-identical duplicate retained and recorded"
        duplicates.append(details)

    unresolved: list[dict[str, str]] = []
    load_path_pending: list[dict[str, Any]] = []
    for info in infos:
        for imported in info.imports:
            if _is_system_import(imported):
                continue
            occurrences = by_basename.get(imported)
            if occurrences is None:
                unresolved.append({"path": info.path, "import": imported})
                continue
            if not _import_is_statically_local(info.path, occurrences, stage_root):
                load_path_pending.append(
                    {
                        "path": info.path,
                        "import": imported,
                        "candidate_paths": [
                            path.relative_to(stage_root).as_posix()
                            for path, _digest in occurrences
                        ],
                        "status": "pending-windows-loader-validation",
                    }
                )
    if unresolved:
        raise PeValidationError(f"unresolved PE imports: {unresolved[:20]}")
    return {
        "pe_count": len(infos),
        "files": [
            {
                "path": info.path,
                "machine": f"0x{info.machine:04x}",
                "optional_magic": f"0x{info.optional_magic:03x}",
                "subsystem": info.subsystem,
                "timestamp": info.timestamp,
                "imports": list(info.imports),
            }
            for info in infos
        ],
        "duplicate_dlls": duplicates,
        "load_path_pending": load_path_pending,
    }


def verify_locked_models(
    locks: LockSet,
    stage_root: Path,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for artifact in locks.runtime_resources:
        if artifact.get("kind") != "paddle_inference_model":
            continue
        unpack = artifact["unpack"]
        destination_root = validate_windows_relative_path(
            str(unpack["destination_root"]).rstrip("/")
        ).as_posix()
        files: list[dict[str, Any]] = []
        for entry in unpack["files"]:
            relative = (
                f"{destination_root}/"
                f"{validate_windows_relative_path(str(entry['destination_path'])).as_posix()}"
            )
            path = safe_destination(stage_root, relative)
            verify_file(path, str(entry["sha256"]), int(entry["size"]))
            files.append(
                {
                    "path": relative,
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                }
            )
        output.append({"id": artifact["id"], "files": files})
    if len(output) != 2:
        raise PipelineError("exactly two staged OCR models must be verified")
    return output


def _iter_tree_entries(
    root: Path,
    *,
    excluded_names: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, Path]]:
    for path in sorted(
        root.rglob("*"),
        key=lambda item: (
            item.relative_to(root).as_posix().casefold(),
            item.relative_to(root).as_posix(),
        ),
    ):
        relative = path.relative_to(root).as_posix()
        if path.name in excluded_names:
            continue
        if path.is_symlink():
            raise PipelineError(f"staging symlink is forbidden: {relative}")
        validate_windows_relative_path(relative)
        yield relative, path


def inventory_tree(
    root: Path,
    *,
    excluded_names: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    keys: set[str] = set()
    for relative, path in _iter_tree_entries(root, excluded_names=excluded_names):
        key = windows_path_key(relative)
        if key in keys:
            raise PipelineError(f"Windows staging path collision: {relative}")
        keys.add(key)
        if path.is_dir():
            continue
        digest, size = sha256_file(path)
        output.append({"path": relative, "sha256": digest, "size": size})
    return output


def tree_digest(files: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(files))).hexdigest()


_ALLOWED_TOP_LEVEL = frozenset(
    {
        "TextSnapLayout.exe",
        "app",
        "runtime",
        "models",
        "assets",
        "data",
        "LICENSES",
        "README.zh-CN.md",
        BUILD_MANIFEST_NAME,
        STAGING_STATE_NAME,
    }
)


def validate_staging_paths(stage_root: Path) -> dict[str, Any]:
    entries = list(_iter_tree_entries(stage_root))
    top_level = {relative.split("/", 1)[0] for relative, _ in entries}
    unexpected = sorted(top_level - _ALLOWED_TOP_LEVEL)
    if unexpected:
        raise PipelineError(f"unexpected top-level staging paths: {unexpected}")
    for required in (
        "TextSnapLayout.exe",
        "app/main.py",
        "runtime/pythonw.exe",
        "runtime/python313._pth",
        "runtime/qt.conf",
        "assets/fonts",
        "models/PP-OCRv6_medium_det",
        "models/PP-OCRv6_medium_rec",
        "data",
        "runtime/pdx-cache",
        "runtime/pdx-cache/temp",
        "LICENSES/INDEX.json",
    ):
        path = safe_destination(stage_root, required)
        if not path.exists():
            raise PipelineError(f"required staged path is absent: {required}")
    pth_text = (stage_root / "runtime" / "python313._pth").read_text(encoding="utf-8")
    if "import site" in pth_text.lower():
        raise PipelineError("staged python313._pth enables site")
    return {"entry_count": len(entries), "top_level": sorted(top_level)}


def create_build_manifest(
    *,
    stage_root: Path,
    locks: LockSet,
    profile: str,
    wheel_closure: Mapping[str, Any],
    wheel_installs: Sequence[Mapping[str, Any]],
    resources: Sequence[Mapping[str, Any]],
    bytecode: Mapping[str, Any],
    native: Mapping[str, Any],
    licenses: Mapping[str, Any],
    pe_validation: Mapping[str, Any],
) -> dict[str, Any]:
    files = inventory_tree(
        stage_root,
        excluded_names=frozenset({BUILD_MANIFEST_NAME, STAGING_STATE_NAME}),
    )
    manifest = {
        "schema_version": "1.0.0",
        "product": {
            "name": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "target": "windows-x86_64",
            "python": PYTHON_VERSION,
        },
        "profile": profile,
        "lock_inputs": locks.input_hashes(),
        "wheel_closure": {
            "artifact_count": wheel_closure["artifact_count"],
            "active_edge_count": wheel_closure["active_edge_count"],
            "roots": wheel_closure["roots"],
        },
        "wheels": list(wheel_installs),
        "resources": list(resources),
        "bytecode": dict(bytecode),
        "native": dict(native),
        "licenses": dict(licenses),
        "pe_validation": {
            "pe_count": pe_validation["pe_count"],
            "duplicate_dlls": pe_validation["duplicate_dlls"],
            "load_path_pending": pe_validation["load_path_pending"],
        },
        "files": files,
        "files_digest": tree_digest(files),
        "reproducibility": {
            "zip_timestamp": "1980-01-01T00:00:00",
            "zip_compression": "deflate-level-9",
            "sorted_paths": True,
            "scope": (
                "identical source, lock bytes, CPython builder, zlib, and "
                "recorded MinGW toolchain"
            ),
            "builder_python": (
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
            "builder_zlib": zlib.ZLIB_RUNTIME_VERSION,
            "native_toolchain": {
                "target": native.get("toolchain"),
                "gcc_version": native.get("gcc_version"),
                "windres_version": native.get("windres_version"),
            },
        },
        "windows_validation": {
            "status": "pending",
            "reason": (
                "Windows 11 x64 behavior and non-local DLL search paths cannot "
                "be proven on ARM64 Linux"
            ),
            "pe_load_path_pending_count": len(pe_validation["load_path_pending"]),
        },
    }
    write_canonical_json(stage_root / BUILD_MANIFEST_NAME, manifest)
    return manifest


def write_staging_state(
    stage_root: Path,
    *,
    profile: str,
    licenses_passed: bool,
) -> dict[str, Any]:
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if not manifest_path.is_file():
        raise PipelineError("cannot save staging state without BUILD_MANIFEST.json")
    inventory = inventory_tree(
        stage_root, excluded_names=frozenset({STAGING_STATE_NAME})
    )
    state = {
        "schema_version": "1.0.0",
        "profile": profile,
        "licenses_passed": licenses_passed,
        "manifest_sha256": sha256_file(manifest_path)[0],
        "tree_digest": tree_digest(inventory),
    }
    write_canonical_json(stage_root / STAGING_STATE_NAME, state)
    return state


def verify_saved_staging_for_release(stage_root: Path) -> dict[str, Any]:
    state_path = stage_root / STAGING_STATE_NAME
    if not state_path.is_file():
        raise PipelineError("release packaging requires a saved staging state")
    state = load_json_object(state_path)
    if state.get("profile") != "release" or state.get("licenses_passed") is not True:
        raise LicenseGateError(
            [
                {
                    "id": "saved-staging-not-redistributable",
                    "component": str(stage_root),
                    "reason": (
                        "only profile=release staging with licenses_passed=true "
                        "may be packaged"
                    ),
                }
            ]
        )
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if sha256_file(manifest_path)[0] != state.get("manifest_sha256"):
        raise PipelineError("BUILD_MANIFEST changed after staging was saved")
    inventory = inventory_tree(
        stage_root, excluded_names=frozenset({STAGING_STATE_NAME})
    )
    if tree_digest(inventory) != state.get("tree_digest"):
        raise PipelineError("staging tree changed after validation")
    manifest = validate_build_manifest(stage_root)
    if manifest.get("licenses", {}).get("passed") is not True:
        raise LicenseGateError(
            [
                {
                    "id": "manifest-license-gate-failed",
                    "component": BUILD_MANIFEST_NAME,
                    "reason": "manifest does not attest a passed license gate",
                }
            ]
        )
    return state


def _zip_info(name: str, *, is_dir: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o40755 if is_dir else 0o100644
    info.external_attr = (mode << 16) | (0x10 if is_dir else 0)
    info.flag_bits |= 0x800
    return info


def _write_deterministic_zip(
    stage_root: Path,
    output_zip: Path,
    *,
    product_directory: str = PRODUCT_NAME,
) -> dict[str, Any]:
    validate_windows_relative_path(product_directory)
    if "/" in product_directory:
        raise PipelineError("ZIP product directory must be a basename")
    expected_name = f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
    if output_zip.name != expected_name:
        raise PipelineError(
            f"release ZIP filename must be {expected_name}, got {output_zip.name}"
        )
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_zip.with_name(f".{output_zip.name}.{uuid.uuid4().hex}.tmp")
    entries = [
        (relative, path)
        for relative, path in _iter_tree_entries(
            stage_root, excluded_names=frozenset({STAGING_STATE_NAME})
        )
    ]
    try:
        with zipfile.ZipFile(
            temporary,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            root_info = _zip_info(f"{product_directory}/", is_dir=True)
            archive.writestr(root_info, b"", compresslevel=9)
            for relative, path in entries:
                archive_name = f"{product_directory}/{relative}"
                if path.is_dir():
                    info = _zip_info(archive_name + "/", is_dir=True)
                    archive.writestr(info, b"", compresslevel=9)
                    continue
                info = _zip_info(archive_name, is_dir=False)
                with path.open("rb") as source, archive.open(info, "w") as target:
                    shutil.copyfileobj(source, target, _HASH_CHUNK_SIZE)
        os.replace(temporary, output_zip)
    finally:
        if temporary.exists():
            temporary.unlink()
    digest, size = sha256_file(output_zip)
    checksum_path = output_zip.with_suffix(output_zip.suffix + ".sha256")
    checksum_path.write_text(
        f"{digest}  {output_zip.name}\n",
        encoding="ascii",
        newline="\n",
    )
    return {
        "path": str(output_zip),
        "sha256": digest,
        "size": size,
        "sha256_file": str(checksum_path),
    }


def build_deterministic_zip(
    stage_root: Path,
    output_zip: Path,
    *,
    locks: LockSet,
    product_directory: str = PRODUCT_NAME,
) -> dict[str, Any]:
    """Package only a current-lock release staging that passes every gate."""

    validate_lock_set(locks)
    blockers = evaluate_license_gate(locks)
    if blockers:
        raise LicenseGateError(blockers)
    static_verify_staging(locks=locks, stage_root=stage_root)
    verify_saved_staging_for_release(stage_root)
    return _write_deterministic_zip(
        stage_root,
        output_zip,
        product_directory=product_directory,
    )


def validate_zip_archive(
    zip_path: Path,
    *,
    expected_product_directory: str = PRODUCT_NAME,
) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as archive:
        members = validate_zip_members(archive)
        prefix = f"{windows_path_key(expected_product_directory)}/"
        root_key = windows_path_key(expected_product_directory)
        file_count = 0
        for key, info in members.items():
            if key != root_key and not key.startswith(prefix):
                raise PipelineError(
                    f"ZIP member is outside product directory: {info.filename}"
                )
            if key.endswith(f"/{STAGING_STATE_NAME.casefold()}"):
                raise PipelineError("private staging state leaked into release ZIP")
            if not info.is_dir():
                file_count += 1
            if info.date_time != ZIP_EPOCH:
                raise PipelineError(f"non-deterministic ZIP timestamp: {info.filename}")
        required = {
            windows_path_key(f"{expected_product_directory}/TextSnapLayout.exe"),
            windows_path_key(f"{expected_product_directory}/{BUILD_MANIFEST_NAME}"),
        }
        if not required.issubset(members):
            raise PipelineError("ZIP lacks required launcher or manifest")
    return {"member_count": len(members), "file_count": file_count}


def validate_build_manifest(stage_root: Path) -> dict[str, Any]:
    manifest = load_json_object(stage_root / BUILD_MANIFEST_NAME)
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        raise PipelineError("BUILD_MANIFEST files must be an array")
    actual_files = inventory_tree(
        stage_root,
        excluded_names=frozenset({BUILD_MANIFEST_NAME, STAGING_STATE_NAME}),
    )
    if actual_files != expected_files:
        raise PipelineError("staged files differ from BUILD_MANIFEST inventory")
    if tree_digest(actual_files) != manifest.get("files_digest"):
        raise PipelineError("BUILD_MANIFEST files_digest is invalid")
    if manifest.get("product") != {
        "name": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "target": "windows-x86_64",
        "python": PYTHON_VERSION,
    }:
        raise PipelineError("BUILD_MANIFEST product identity is invalid")
    return manifest


def verify_saved_staging(stage_root: Path) -> dict[str, Any]:
    state_path = stage_root / STAGING_STATE_NAME
    state = load_json_object(state_path)
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if sha256_file(manifest_path)[0] != state.get("manifest_sha256"):
        raise PipelineError("saved staging manifest hash is invalid")
    inventory = inventory_tree(
        stage_root, excluded_names=frozenset({STAGING_STATE_NAME})
    )
    if tree_digest(inventory) != state.get("tree_digest"):
        raise PipelineError("saved staging tree digest is invalid")
    validate_build_manifest(stage_root)
    return state


def publish_staging(
    temporary: Path,
    output_stage: Path,
    *,
    profile: str,
    license_report: Mapping[str, Any],
) -> None:
    if profile == "release" and license_report.get("passed") is not True:
        raise LicenseGateError(license_report.get("blockers", ()))
    os.replace(temporary, output_stage)


def stage_portable_tree(
    *,
    locks: LockSet,
    cache_root: Path,
    output_stage: Path,
    source_package: Path,
    entry_script: Path,
    readme: Path | None,
    python_for_bytecode: Path,
    native_source: Path,
    toolchain_prefix: str,
    profile: str,
) -> dict[str, Any]:
    if profile not in {"release", "nonredistributable-test"}:
        raise PipelineError(f"unknown staging profile {profile!r}")
    if output_stage.exists():
        raise PipelineError(f"staging destination already exists: {output_stage}")
    output_stage.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_stage.parent / (
        f".{output_stage.name}.{uuid.uuid4().hex}.staging"
    )
    temporary.mkdir()
    completed = False
    try:
        lock_report = validate_lock_set(locks)
        closure = validate_wheel_closure(locks, cache_root)
        resources = stage_runtime_resources(locks, cache_root, temporary)
        resources.append(prune_cpython_console_launcher(temporary))
        registry = InstallRegistry()
        wheel_installs = [
            install_wheel(
                artifact_cache_path(cache_root, artifact),
                artifact,
                temporary,
                registry,
            )
            for artifact in locks.wheel_artifacts
        ]
        site_packages = temporary / "runtime" / "Lib" / "site-packages"
        pyside_pruning = prune_pyside_essentials(site_packages)
        setuptools_launcher_pruning = prune_setuptools_script_launchers(site_packages)
        removed_wheel_bytecode = remove_wheel_bytecode(temporary)
        application = stage_application_source(
            source_package, entry_script, temporary, readme=readme
        )
        runtime_configuration = write_runtime_configuration(temporary)
        bytecode = precompile_checked_hash_bytecode(python_for_bytecode, temporary)

        from scripts.build_native import build_launcher

        native = build_launcher(
            source_root=native_source,
            output=temporary / "TextSnapLayout.exe",
            icon_output=temporary / "assets" / "icons" / "textsnap.ico",
            toolchain_prefix=toolchain_prefix,
        )
        if native.get("tray_icon"):
            native["tray_icon"]["path"] = "assets/icons/textsnap.ico"
        licenses = collect_licenses(
            locks,
            cache_root,
            temporary,
            enforce_release_gate=False,
        )
        if profile == "release" and not licenses["passed"]:
            raise LicenseGateError(licenses["blockers"])
        paths = validate_staging_paths(temporary)
        models = verify_locked_models(locks, temporary)
        pe_validation = validate_pe_tree(temporary)
        native = {
            **native,
            "pyside_pruning": pyside_pruning,
            "setuptools_launcher_pruning": setuptools_launcher_pruning,
            "removed_wheel_bytecode_count": len(removed_wheel_bytecode),
        }
        resources = [
            *resources,
            {
                "kind": "application",
                **application,
            },
            {
                "kind": "runtime-configuration",
                **runtime_configuration,
            },
        ]
        create_build_manifest(
            stage_root=temporary,
            locks=locks,
            profile=profile,
            wheel_closure=closure,
            wheel_installs=wheel_installs,
            resources=resources,
            bytecode=bytecode,
            native=native,
            licenses=licenses,
            pe_validation=pe_validation,
        )
        write_staging_state(
            temporary,
            profile=profile,
            licenses_passed=bool(licenses["passed"]),
        )
        verify_saved_staging(temporary)
        publish_staging(
            temporary,
            output_stage,
            profile=profile,
            license_report=licenses,
        )
        completed = True
        result = {
            "profile": profile,
            "stage": str(output_stage),
            "lock": lock_report,
            "wheel_closure": {
                "artifact_count": closure["artifact_count"],
                "active_edge_count": closure["active_edge_count"],
            },
            "paths": paths,
            "models": models,
            "pe_count": pe_validation["pe_count"],
            "duplicate_dlls": pe_validation["duplicate_dlls"],
            "load_path_pending": pe_validation["load_path_pending"],
            "licenses": licenses,
        }
        return result
    finally:
        if not completed and temporary.exists():
            shutil.rmtree(temporary)


def static_verify_staging(
    *,
    locks: LockSet,
    stage_root: Path,
) -> dict[str, Any]:
    validate_lock_set(locks)
    state = verify_saved_staging(stage_root)
    paths = validate_staging_paths(stage_root)
    models = verify_locked_models(locks, stage_root)
    pe = validate_pe_tree(stage_root)
    manifest = validate_build_manifest(stage_root)
    if manifest.get("lock_inputs") != locks.input_hashes():
        raise PipelineError("staging was built from different lock bytes")
    index = load_json_object(stage_root / "LICENSES" / "INDEX.json")
    blockers = evaluate_license_gate(locks)
    expected_passed = not blockers
    if index.get("blockers") != blockers:
        raise PipelineError("license index blockers differ from the current lock")
    if index.get("release_gate_passed") is not expected_passed:
        raise PipelineError("license index gate differs from the current lock")
    manifest_licenses = manifest.get("licenses", {})
    if (
        manifest_licenses.get("passed") is not expected_passed
        or manifest_licenses.get("blockers") != blockers
        or state.get("licenses_passed") is not expected_passed
    ):
        raise PipelineError(
            "saved state, build manifest, and current license gate disagree"
        )
    return {
        "profile": state.get("profile"),
        "licenses_passed": state.get("licenses_passed"),
        "paths": paths,
        "model_count": len(models),
        "pe_count": pe["pe_count"],
        "duplicate_dlls": pe["duplicate_dlls"],
        "load_path_pending": pe["load_path_pending"],
    }
