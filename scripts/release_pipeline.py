"""Deterministic native Windows construction of the portable bundle.

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
EXPECTED_WHEEL_COUNT = 70
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
    "onnxruntime==1.28.0",
    "opencv-contrib-python==4.10.0.84",
    "paddleocr==3.7.0",
    "paddlepaddle==3.2.2",
    "paddlex[ocr-core]==3.7.2",
    "PySide6-Essentials==6.11.1",
)
PINNED_CORE_WHEEL_VERSIONS = {
    "numpy": "2.2.6",
    "onnxruntime": "1.28.0",
    "flatbuffers": "25.12.19",
    "opencv-contrib-python": "4.10.0.84",
    "paddleocr": "3.7.0",
    "paddlepaddle": "3.2.2",
    "paddlex": "3.7.2",
    "pyside6-essentials": "6.11.1",
    "shiboken6": "6.11.1",
}
PINNED_RESOURCE_IDENTITIES = {
    "cpython-3.13.14-embed-win-amd64": (
        "python_embeddable_runtime",
        "3.13.14",
    ),
    "pp-ocrv6-small-det-inference": (
        "paddle_inference_model",
        "PP-OCRv6_small_det",
    ),
    "pp-ocrv6-small-rec-inference": (
        "paddle_inference_model",
        "PP-OCRv6_small_rec",
    ),
    "noto-sans-mono-cjk-sc-regular-sans2.004": ("font", "Sans2.004"),
}
STAGING_STATE_NAME = ".textsnap-staging.json"
BUILD_MANIFEST_NAME = "BUILD_MANIFEST.json"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
STAGING_STATE_DIGEST_SCOPE = "files-and-directories"

_HASH_CHUNK_SIZE = 1024 * 1024
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
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

    @classmethod
    def load(cls, root: Path) -> "LockSet":
        root = root.resolve()
        return cls(
            root=root,
            wheels=load_json_object(root / "wheels.json"),
            resources=load_json_object(root / "resources.json"),
        )

    @property
    def wheel_artifacts(self) -> list[dict[str, Any]]:
        return list(self.wheels.get("artifacts", ()))

    @property
    def runtime_resources(self) -> list[dict[str, Any]]:
        return list(self.resources.get("artifacts", ()))

    @property
    def derived_model_files(self) -> list[dict[str, Any]]:
        derived = self.resources.get("derived_models", {})
        return list(derived.get("files", ())) if isinstance(derived, Mapping) else []

    @property
    def all_runtime_artifacts(self) -> list[dict[str, Any]]:
        return [*self.wheel_artifacts, *self.runtime_resources]

    def input_hashes(self) -> dict[str, str]:
        return {
            name: sha256_file(self.root / name)[0]
            for name in ("wheels.json", "resources.json")
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
        else:
            raise LockValidationError(f"{context}: unknown resource kind {kind!r}")
    if resource_identities != PINNED_RESOURCE_IDENTITIES:
        raise LockValidationError(
            "resource identities or versions differ from the implementation plan"
        )

    derived = _require_mapping(
        locks.resources.get("derived_models"), "derived ONNX models"
    )
    if derived.get("format") != "onnx" or derived.get("opset_version") != 11:
        raise LockValidationError("derived models must be ONNX opset 11")
    derived_files = _require_list(derived.get("files"), "derived model files")
    expected_model_names = {"PP-OCRv6_small_det", "PP-OCRv6_small_rec"}
    actual_model_names: set[str] = set()
    for index, raw_entry in enumerate(derived_files):
        entry = _require_mapping(raw_entry, f"derived model {index}")
        model_name = str(entry.get("model_name", ""))
        source_path = validate_windows_relative_path(str(entry.get("source_path", "")))
        destination_path = validate_windows_relative_path(
            str(entry.get("destination_path", ""))
        )
        if (
            model_name not in expected_model_names
            or source_path.as_posix() != f"{model_name}/inference.onnx"
            or destination_path.as_posix() != f"models/{model_name}/inference.onnx"
            or not isinstance(entry.get("size"), int)
            or int(entry["size"]) <= 0
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
        ):
            raise LockValidationError(f"derived model {index}: invalid locked identity")
        actual_model_names.add(model_name)
    if actual_model_names != expected_model_names or len(derived_files) != 2:
        raise LockValidationError("exactly the two PP-OCRv6 small ONNX models are required")

    return {
        "wheel_count": len(artifacts),
        "runtime_resource_count": len(locks.runtime_resources),
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
    if pure.as_posix() != path:
        raise UnsafeArchiveError(f"non-canonical archive path: {path!r}")
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


def _canonical_archive_member_path(
    name: str,
    *,
    is_directory: bool,
) -> str:
    if name.endswith("/"):
        if not is_directory or name.endswith("//"):
            raise UnsafeArchiveError(
                f"non-canonical archive directory path: {name!r}"
            )
        name = name[:-1]
    return validate_windows_relative_path(name).as_posix()


def validate_zip_members(
    archive: zipfile.ZipFile,
    *,
    maximum_uncompressed_size: int | None = None,
) -> dict[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        name = _canonical_archive_member_path(
            info.filename,
            is_directory=info.is_dir(),
        )
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
    for key in result:
        parts = key.split("/")
        for index in range(1, len(parts)):
            ancestor_key = "/".join(parts[:index])
            ancestor = result.get(ancestor_key)
            if ancestor is not None and not ancestor.is_dir():
                raise UnsafeArchiveError(
                    "ZIP non-directory member is an ancestor of another "
                    f"member: {ancestor.filename!r}"
                )
    return result


def _read_tar_archive_bytes(
    archive: tarfile.TarFile,
    offset: int,
    size: int,
) -> bytes:
    stream = archive.fileobj
    try:
        original_position = stream.tell()
        stream.seek(offset)
        data = stream.read(size)
    except (AttributeError, OSError) as exc:
        raise UnsafeArchiveError("TAR stream must support safe random access") from exc
    finally:
        try:
            stream.seek(original_position)
        except (AttributeError, OSError, UnboundLocalError):
            pass
    if len(data) != size:
        raise UnsafeArchiveError("truncated TAR member header")
    return data


def _tar_header_path(
    archive: tarfile.TarFile,
    header: bytes,
) -> tuple[str, bytes]:
    member_type = header[156:157]
    name = tarfile.nts(header[0:100], archive.encoding, archive.errors)
    prefix = tarfile.nts(header[345:500], archive.encoding, archive.errors)
    if prefix and member_type not in tarfile.GNU_TYPES:
        name = f"{prefix}/{name}"
    return name, member_type


def _tar_header_size(header: bytes, context: str) -> int:
    try:
        size = tarfile.nti(header[124:136])
    except (ValueError, tarfile.HeaderError) as exc:
        raise UnsafeArchiveError(f"invalid TAR size for {context}") from exc
    if size < 0:
        raise UnsafeArchiveError(f"negative TAR size for {context}")
    return size


def _tar_padded_size(size: int) -> int:
    return (size + tarfile.BLOCKSIZE - 1) & ~(tarfile.BLOCKSIZE - 1)


def _decode_pax_field(
    value: bytes,
    *,
    encoding: str,
    archive: tarfile.TarFile,
    fallback_encoding: str | None = None,
) -> str:
    try:
        return value.decode(encoding, "strict")
    except UnicodeDecodeError:
        return value.decode(
            fallback_encoding or archive.encoding,
            archive.errors,
        )


def _tar_pax_path_declarations(
    archive: tarfile.TarFile,
    data: bytes,
) -> list[tuple[str, str]]:
    raw_fields: list[tuple[bytes, bytes]] = []
    position = 0
    path_encoding: str | None = None
    while position < len(data) and data[position] != 0:
        separator = data.find(b" ", position)
        if separator < 0 or not data[position:separator].isdigit():
            raise UnsafeArchiveError("invalid PAX TAR record length")
        try:
            length = int(data[position:separator])
        except ValueError as exc:
            raise UnsafeArchiveError("invalid PAX TAR record length") from exc
        end = position + length
        if length < 5 or end > len(data) or data[end - 1 : end] != b"\n":
            raise UnsafeArchiveError("invalid PAX TAR record framing")
        raw_key, equals, raw_value = data[separator + 1 : end - 1].partition(b"=")
        if not raw_key or equals != b"=":
            raise UnsafeArchiveError("invalid PAX TAR record")
        raw_fields.append((raw_key, raw_value))
        if raw_key == b"hdrcharset" and path_encoding is None:
            path_encoding = (
                archive.encoding if raw_value == b"BINARY" else "utf-8"
            )
        position = end

    if path_encoding is None:
        path_encoding = "utf-8"
    paths: list[tuple[str, str]] = []
    for raw_key, raw_value in raw_fields:
        key = _decode_pax_field(
            raw_key,
            encoding="utf-8",
            archive=archive,
            fallback_encoding="utf-8",
        )
        if key in {"path", "GNU.sparse.name"}:
            encoding = path_encoding if key == "path" else "utf-8"
            paths.append(
                (
                    key,
                    _decode_pax_field(
                        raw_value,
                        encoding=encoding,
                        archive=archive,
                        fallback_encoding=None if key == "path" else "utf-8",
                    ),
                )
            )
    return paths


def _tar_header_is_declared_path_placeholder(
    archive: tarfile.TarFile,
    header: bytes,
    declarations: Sequence[str],
) -> bool:
    raw_name = header[0:100].split(b"\0", 1)[0]
    raw_prefix = header[345:500].split(b"\0", 1)[0]
    if raw_prefix:
        return False
    for declaration in declarations:
        candidates: list[bytes] = []
        try:
            candidates.append(declaration.encode("ascii", "replace")[:100])
            candidates.append(
                declaration.encode(archive.encoding, archive.errors)[:100]
            )
        except UnicodeError:
            continue
        if raw_name in candidates:
            return True
    return False


def _validate_tar_path_declaration(
    path: str,
    *,
    is_directory: bool,
) -> None:
    try:
        _canonical_archive_member_path(path, is_directory=is_directory)
    except UnicodeError as exc:
        raise UnsafeArchiveError("TAR path cannot be represented safely") from exc


def _scan_tar_path_declarations(
    archive: tarfile.TarFile,
    members: Sequence[tarfile.TarInfo],
) -> None:
    actual_headers = {
        member.offset_data - tarfile.BLOCKSIZE: member for member in members
    }
    actual_offsets = sorted(actual_headers)
    visited_actual_headers: set[int] = set()
    global_declarations: dict[str, str] = {}
    pending_declarations: list[str] = []
    extension_types = {
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
    }
    position = 0
    while True:
        header = _read_tar_archive_bytes(
            archive, position, tarfile.BLOCKSIZE
        )
        if header == tarfile.NUL * tarfile.BLOCKSIZE:
            break
        member_type = header[156:157]
        size = _tar_header_size(header, f"header at offset {position}")
        member = actual_headers.get(position)
        if member is not None:
            for path in [
                *global_declarations.values(),
                *pending_declarations,
            ]:
                _validate_tar_path_declaration(
                    path,
                    is_directory=member.isdir(),
                )
            raw_path, _raw_type = _tar_header_path(archive, header)
            try:
                _validate_tar_path_declaration(
                    raw_path,
                    is_directory=member.isdir(),
                )
            except UnsafeArchiveError:
                if not _tar_header_is_declared_path_placeholder(
                    archive,
                    header,
                    [
                        *global_declarations.values(),
                        *pending_declarations,
                    ],
                ):
                    raise
            visited_actual_headers.add(position)
            pending_declarations.clear()
            payload_size = member.size if member.isreg() else 0
            position = member.offset_data + _tar_padded_size(payload_size)
            continue
        if member_type not in extension_types:
            raise UnsafeArchiveError(
                f"unmatched TAR header at offset {position}"
            )

        payload = _read_tar_archive_bytes(
            archive,
            position + tarfile.BLOCKSIZE,
            size,
        )
        next_member = next(
            (
                actual_headers[offset]
                for offset in actual_offsets
                if offset > position
            ),
            None,
        )
        if member_type == tarfile.GNUTYPE_LONGNAME:
            path = tarfile.nts(payload, archive.encoding, archive.errors)
            _validate_tar_path_declaration(
                path,
                is_directory=bool(next_member and next_member.isdir()),
            )
            pending_declarations.append(path)
        elif member_type in {
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.SOLARIS_XHDTYPE,
        }:
            declarations = _tar_pax_path_declarations(archive, payload)
            for _key, path in declarations:
                _validate_tar_path_declaration(
                    path,
                    is_directory=bool(next_member and next_member.isdir()),
                )
            if member_type == tarfile.XGLTYPE:
                for key, path in declarations:
                    global_declarations[key] = path
            else:
                pending_declarations.extend(path for _key, path in declarations)
        position += tarfile.BLOCKSIZE + _tar_padded_size(size)

    if visited_actual_headers != set(actual_headers):
        raise UnsafeArchiveError("TAR path scan did not cover every member")


def validate_tar_members(
    archive: tarfile.TarFile,
    *,
    maximum_uncompressed_size: int | None = None,
) -> dict[str, tarfile.TarInfo]:
    result: dict[str, tarfile.TarInfo] = {}
    total = 0
    members = archive.getmembers()
    for member in members:
        if member.type == tarfile.GNUTYPE_SPARSE or member.sparse is not None:
            raise UnsafeArchiveError(
                f"TAR sparse files are forbidden: {member.name!r}"
            )
        if not (member.isdir() or member.isreg()):
            raise UnsafeArchiveError(
                f"TAR links/devices/special files are forbidden: {member.name!r}"
            )
        final_name = _canonical_archive_member_path(
            member.name,
            is_directory=member.isdir(),
        )
        key = windows_path_key(final_name)
        if key in result:
            raise UnsafeArchiveError(
                f"duplicate or Windows case-colliding TAR member: {member.name!r}"
            )
        total += member.size if member.isreg() else 0
        if maximum_uncompressed_size is not None and total > maximum_uncompressed_size:
            raise UnsafeArchiveError(
                "TAR uncompressed size exceeds locked safety bound"
            )
        result[key] = member
    _scan_tar_path_declarations(archive, members)
    return result


def artifact_cache_path(cache_root: Path, artifact: Mapping[str, Any]) -> Path:
    return safe_destination(cache_root / "artifacts", str(artifact["filename"]))


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
                if destination_tail not in {
                    "inference.json",
                    "inference.pdmodel",
                    "inference.pdiparams",
                }:
                    details = _copy_verified_stream(
                        source,
                        safe_destination(stage_root, relative),
                        expected_sha256=str(entry["sha256"]),
                        expected_size=int(entry["size"]),
                    )
                    details["path"] = relative
                    output.append(details)
                else:
                    digest = hashlib.sha256()
                    size = 0
                    while block := source.read(_HASH_CHUNK_SIZE):
                        digest.update(block)
                        size += len(block)
                    if size != int(entry["size"]) or digest.hexdigest() != str(
                        entry["sha256"]
                    ):
                        raise HashMismatchError(
                            f"{artifact['id']}: locked source model member mismatch"
                        )
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
            if not locked_destination.startswith("fonts/"):
                raise LockValidationError(
                    f"{artifact['id']}: unsupported font destination "
                    f"{locked_destination!r}"
                )
            destination = f"assets/{locked_destination}"
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


def stage_derived_onnx_models(
    locks: LockSet,
    model_root: Path,
    stage_root: Path,
) -> dict[str, Any]:
    if not model_root.is_dir():
        raise PipelineError(f"derived ONNX model root not found: {model_root}")
    files: list[dict[str, Any]] = []
    for entry in locks.derived_model_files:
        source_relative = validate_windows_relative_path(str(entry["source_path"]))
        destination_relative = validate_windows_relative_path(
            str(entry["destination_path"])
        )
        source = safe_destination(model_root, source_relative.as_posix())
        verify_file(source, str(entry["sha256"]), int(entry["size"]))
        with source.open("rb") as stream:
            details = _copy_verified_stream(
                stream,
                safe_destination(stage_root, destination_relative.as_posix()),
                expected_sha256=str(entry["sha256"]),
                expected_size=int(entry["size"]),
            )
        details["path"] = destination_relative.as_posix()
        files.append(details)
    return {"id": "derived-onnx-models", "kind": "derived-models", "files": files}


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
    precreated_directories = (
        "runtime/pdx-cache",
        "runtime/pdx-cache/temp",
        "runtime/pdx-cache/func_ret",
        "runtime/pdx-cache/locks",
        "data",
    )
    for relative in precreated_directories:
        (stage_root / relative).mkdir(parents=True, exist_ok=True)
    return {
        "pth": "runtime/python313._pth",
        "qt_conf": "runtime/qt.conf",
        "precreated_directories": list(precreated_directories),
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
            if entry["destination_path"] != "inference.yml":
                continue
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
    by_id = {entry["id"]: entry for entry in output}
    for entry in locks.derived_model_files:
        model_name = str(entry["model_name"])
        artifact_id = (
            "pp-ocrv6-small-det-inference"
            if model_name.endswith("_det")
            else "pp-ocrv6-small-rec-inference"
        )
        relative = validate_windows_relative_path(
            str(entry["destination_path"])
        ).as_posix()
        verify_file(
            safe_destination(stage_root, relative),
            str(entry["sha256"]),
            int(entry["size"]),
        )
        by_id[artifact_id]["files"].append(
            {"path": relative, "sha256": entry["sha256"], "size": entry["size"]}
        )
        model_directory = safe_destination(stage_root, f"models/{model_name}")
        for forbidden_name in ("inference.json", "inference.pdmodel", "inference.pdiparams"):
            if (model_directory / forbidden_name).exists():
                raise PipelineError(
                    f"Paddle runtime model file must not be staged: {model_name}/{forbidden_name}"
                )
    if len(output) != 2:
        raise PipelineError("exactly two staged OCR models must be verified")
    if any(len(model["files"]) != 2 for model in output):
        raise PipelineError("each staged OCR model must contain ONNX and YAML files")
    return output


def verify_locked_font(
    locks: LockSet,
    stage_root: Path,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for artifact in locks.runtime_resources:
        if artifact.get("kind") != "font":
            continue
        unpack = _require_mapping(artifact.get("unpack"), f"{artifact['id']} unpack")
        selected = _require_list(
            unpack.get("selected_files"), f"{artifact['id']} selected files"
        )
        files: list[dict[str, Any]] = []
        for entry in selected:
            locked_destination = validate_windows_relative_path(
                str(entry["destination_path"])
            ).as_posix()
            if not locked_destination.startswith("fonts/"):
                raise LockValidationError(
                    f"{artifact['id']}: unsupported font destination "
                    f"{locked_destination!r}"
                )
            relative = f"assets/{locked_destination}"
            verify_file(
                safe_destination(stage_root, relative),
                str(entry["sha256"]),
                int(entry["size"]),
            )
            files.append(
                {
                    "path": relative,
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                }
            )
        output.append({"id": artifact["id"], "files": files})
    if len(output) != 1:
        raise PipelineError("exactly one staged application font must be verified")
    return output


def verify_python_runtime_inspected_files(
    locks: LockSet,
    stage_root: Path,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for artifact in locks.runtime_resources:
        if artifact.get("kind") != "python_embeddable_runtime":
            continue
        files: list[dict[str, Any]] = []
        for inspected in artifact.get("inspected_files", ()):
            runtime_relative = validate_windows_relative_path(
                str(inspected["path"])
            ).as_posix()
            relative = f"runtime/{runtime_relative}"
            verify_file(
                safe_destination(stage_root, relative),
                str(inspected["sha256"]),
                int(inspected["size"]),
            )
            files.append(
                {
                    "path": relative,
                    "sha256": inspected["sha256"],
                    "size": inspected["size"],
                }
            )
        output.append({"id": artifact["id"], "files": files})
    if len(output) != 1:
        raise PipelineError("exactly one staged CPython runtime must be verified")
    return output


def _iter_tree_entries(
    root: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, Path]]:
    for path in sorted(
        root.rglob("*"),
        key=lambda item: (
            item.relative_to(root).as_posix().casefold(),
            item.relative_to(root).as_posix(),
        ),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in excluded_paths:
            continue
        if path.is_symlink():
            raise PipelineError(f"staging symlink is forbidden: {relative}")
        validate_windows_relative_path(relative)
        yield relative, path


def inventory_tree(
    root: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    keys: set[str] = set()
    for relative, path in _iter_tree_entries(root, excluded_paths=excluded_paths):
        key = windows_path_key(relative)
        if key in keys:
            raise PipelineError(f"Windows staging path collision: {relative}")
        keys.add(key)
        if path.is_dir():
            continue
        digest, size = sha256_file(path)
        output.append({"path": relative, "sha256": digest, "size": size})
    return output


def staging_state_inventory(
    root: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    keys: set[str] = set()
    for relative, path in _iter_tree_entries(root, excluded_paths=excluded_paths):
        key = windows_path_key(relative)
        if key in keys:
            raise PipelineError(f"Windows staging path collision: {relative}")
        keys.add(key)
        if path.is_dir():
            output.append({"path": relative, "kind": "directory"})
            continue
        digest, size = sha256_file(path)
        output.append(
            {
                "path": relative,
                "kind": "file",
                "sha256": digest,
                "size": size,
            }
        )
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
    ):
        path = safe_destination(stage_root, required)
        if not path.is_file():
            raise PipelineError(f"required staged file is absent: {required}")
    for required in (
        "assets/fonts",
        "models/PP-OCRv6_small_det",
        "models/PP-OCRv6_small_rec",
        "data",
        "runtime/pdx-cache",
        "runtime/pdx-cache/temp",
        "runtime/pdx-cache/func_ret",
        "runtime/pdx-cache/locks",
    ):
        path = safe_destination(stage_root, required)
        if not path.is_dir():
            raise PipelineError(f"required staged directory is absent: {required}")
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
    pe_validation: Mapping[str, Any],
) -> dict[str, Any]:
    files = inventory_tree(
        stage_root,
        excluded_paths=frozenset({BUILD_MANIFEST_NAME, STAGING_STATE_NAME}),
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
                "interactive Windows 11 x64 behavior and non-local DLL search "
                "paths require extracted-bundle acceptance"
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
) -> dict[str, Any]:
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if not manifest_path.is_file():
        raise PipelineError("cannot save staging state without BUILD_MANIFEST.json")
    inventory = staging_state_inventory(
        stage_root, excluded_paths=frozenset({STAGING_STATE_NAME})
    )
    state = {
        "schema_version": "1.0.0",
        "profile": profile,
        "manifest_sha256": sha256_file(manifest_path)[0],
        "tree_digest_scope": STAGING_STATE_DIGEST_SCOPE,
        "tree_digest": tree_digest(inventory),
    }
    write_canonical_json(stage_root / STAGING_STATE_NAME, state)
    return state


def verify_saved_staging_for_package(stage_root: Path) -> dict[str, Any]:
    state_path = stage_root / STAGING_STATE_NAME
    if not state_path.is_file():
        raise PipelineError("packaging requires a saved staging state")
    state = load_json_object(state_path)
    if state.get("tree_digest_scope") != STAGING_STATE_DIGEST_SCOPE:
        raise PipelineError("saved staging tree digest scope is invalid")
    if state.get("profile") != "private-use":
        raise PipelineError(
            "only profile=private-use staging may be packaged"
        )
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if sha256_file(manifest_path)[0] != state.get("manifest_sha256"):
        raise PipelineError("BUILD_MANIFEST changed after staging was saved")
    inventory = staging_state_inventory(
        stage_root, excluded_paths=frozenset({STAGING_STATE_NAME})
    )
    if tree_digest(inventory) != state.get("tree_digest"):
        raise PipelineError("staging tree changed after validation")
    manifest = validate_build_manifest(stage_root)
    if manifest.get("profile") != "private-use":
        raise PipelineError("BUILD_MANIFEST profile is not private-use")
    return state


def _zip_info(name: str, *, is_dir: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o40755 if is_dir else 0o100644
    info.external_attr = (mode << 16) | (0x10 if is_dir else 0)
    info.flag_bits |= 0x800
    return info


def _require_fixed_product_directory(product_directory: str) -> None:
    if product_directory != PRODUCT_NAME:
        raise PipelineError(
            f"ZIP product directory must be exactly {PRODUCT_NAME!r}"
        )


def _write_deterministic_zip(
    stage_root: Path,
    output_zip: Path,
    *,
    product_directory: str = PRODUCT_NAME,
) -> dict[str, Any]:
    _require_fixed_product_directory(product_directory)
    validate_windows_relative_path(product_directory)
    if "/" in product_directory:
        raise PipelineError("ZIP product directory must be a basename")
    expected_name = f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
    if output_zip.name != expected_name:
        raise PipelineError(
            f"release ZIP filename must be {expected_name}, got {output_zip.name}"
        )
    resolved_stage = stage_root.resolve()
    checksum_path = output_zip.with_suffix(output_zip.suffix + ".sha256")
    output_paths = (
        output_zip.parent.resolve() / output_zip.name,
        checksum_path.parent.resolve() / checksum_path.name,
    )
    if any(
        candidate == resolved_stage or candidate.is_relative_to(resolved_stage)
        for candidate in output_paths
    ):
        raise PipelineError("release ZIP and checksum must be outside staging root")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_zip.with_name(f".{output_zip.name}.{uuid.uuid4().hex}.tmp")
    entries = [
        (relative, path)
        for relative, path in _iter_tree_entries(
            stage_root, excluded_paths=frozenset({STAGING_STATE_NAME})
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
    checksum_temporary = checksum_path.with_name(
        f".{checksum_path.name}.{uuid.uuid4().hex}.tmp"
    )
    checksum_temporary_created = False
    try:
        checksum_output = checksum_temporary.open("xb")
        checksum_temporary_created = True
        with checksum_output:
            checksum_output.write(
                f"{digest}  {output_zip.name}\n".encode("ascii")
            )
            checksum_output.flush()
            os.fsync(checksum_output.fileno())
        os.replace(checksum_temporary, checksum_path)
    finally:
        if checksum_temporary_created:
            checksum_temporary.unlink(missing_ok=True)
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
    """Package only a current-lock private-use staging."""

    _require_fixed_product_directory(product_directory)
    validate_lock_set(locks)
    static_verify_staging(locks=locks, stage_root=stage_root)
    verify_saved_staging_for_package(stage_root)
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
    _require_fixed_product_directory(expected_product_directory)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            expected_root_name = f"{expected_product_directory}/"
            root_info = next(
                (
                    info
                    for info in archive.infolist()
                    if info.filename == expected_root_name
                ),
                None,
            )
            if root_info is None or not root_info.is_dir():
                raise PipelineError(
                    "ZIP product root must be an explicit directory member"
                )
            members = validate_zip_members(archive)
            prefix = f"{windows_path_key(expected_product_directory)}/"
            root_key = windows_path_key(expected_product_directory)
            root_info = members.get(root_key)
            if (
                root_info is None
                or not root_info.is_dir()
                or root_info.filename != f"{expected_product_directory}/"
            ):
                raise PipelineError(
                    "ZIP product root must be an explicit directory member"
                )
            private_state_key = windows_path_key(
                f"{expected_product_directory}/{STAGING_STATE_NAME}"
            )
            file_count = 0
            for key, info in members.items():
                if key != root_key and not key.startswith(prefix):
                    raise PipelineError(
                        f"ZIP member is outside product directory: {info.filename}"
                    )
                if key == private_state_key:
                    raise PipelineError("private staging state leaked into release ZIP")
                if info.is_dir() and info.file_size != 0:
                    raise PipelineError(
                        f"release ZIP directory member is not empty: {info.filename!r}"
                    )
                if not info.is_dir():
                    file_count += 1
                try:
                    with archive.open(info, "r") as stream:
                        _digest, extracted_size = sha256_stream(stream)
                except (
                    OSError,
                    EOFError,
                    RuntimeError,
                    NotImplementedError,
                    zipfile.BadZipFile,
                    zlib.error,
                ) as exc:
                    raise PipelineError(
                        f"cannot read release ZIP member {info.filename!r}"
                    ) from exc
                if extracted_size != info.file_size:
                    raise PipelineError(
                        f"release ZIP member size mismatch: {info.filename!r}"
                    )
                if info.date_time != ZIP_EPOCH:
                    raise PipelineError(
                        f"non-deterministic ZIP timestamp: {info.filename}"
                    )
            required = {
                windows_path_key(
                    f"{expected_product_directory}/TextSnapLayout.exe"
                ): f"{expected_product_directory}/TextSnapLayout.exe",
                windows_path_key(
                    f"{expected_product_directory}/{BUILD_MANIFEST_NAME}"
                ): f"{expected_product_directory}/{BUILD_MANIFEST_NAME}",
            }
            for key, expected_name in required.items():
                info = members.get(key)
                if (
                    info is None
                    or info.is_dir()
                    or info.filename != expected_name
                ):
                    raise PipelineError(
                        "ZIP lacks required regular launcher or manifest"
                    )
        return {"member_count": len(members), "file_count": file_count}
    except PipelineError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise PipelineError(f"invalid release ZIP archive: {zip_path.name}") from exc


def validate_build_manifest(stage_root: Path) -> dict[str, Any]:
    manifest = load_json_object(stage_root / BUILD_MANIFEST_NAME)
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        raise PipelineError("BUILD_MANIFEST files must be an array")
    actual_files = inventory_tree(
        stage_root,
        excluded_paths=frozenset({BUILD_MANIFEST_NAME, STAGING_STATE_NAME}),
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
    if state.get("tree_digest_scope") != STAGING_STATE_DIGEST_SCOPE:
        raise PipelineError("saved staging tree digest scope is invalid")
    manifest_path = stage_root / BUILD_MANIFEST_NAME
    if sha256_file(manifest_path)[0] != state.get("manifest_sha256"):
        raise PipelineError("saved staging manifest hash is invalid")
    inventory = staging_state_inventory(
        stage_root, excluded_paths=frozenset({STAGING_STATE_NAME})
    )
    if tree_digest(inventory) != state.get("tree_digest"):
        raise PipelineError("saved staging tree digest is invalid")
    validate_build_manifest(stage_root)
    return state


def publish_staging(
    temporary: Path,
    output_stage: Path,
) -> None:
    os.replace(temporary, output_stage)


def create_temporary_staging(output_stage: Path) -> Path:
    parent = output_stage.parent
    for _attempt in range(128):
        temporary = parent / f".ts-{uuid.uuid4().hex[:8]}"
        try:
            temporary.mkdir()
        except FileExistsError:
            continue
        return temporary
    raise PipelineError("cannot allocate a unique temporary staging directory")


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
    onnx_model_root: Path,
    toolchain_prefix: str,
    profile: str,
) -> dict[str, Any]:
    if profile != "private-use":
        raise PipelineError(f"unknown staging profile {profile!r}")
    if output_stage.exists():
        raise PipelineError(f"staging destination already exists: {output_stage}")
    output_stage.parent.mkdir(parents=True, exist_ok=True)
    temporary = create_temporary_staging(output_stage)
    completed = False
    try:
        lock_report = validate_lock_set(locks)
        closure = validate_wheel_closure(locks, cache_root)
        resources = stage_runtime_resources(locks, cache_root, temporary)
        resources.append(stage_derived_onnx_models(locks, onnx_model_root, temporary))
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
            pe_validation=pe_validation,
        )
        write_staging_state(
            temporary,
            profile=profile,
        )
        verify_saved_staging(temporary)
        publish_staging(temporary, output_stage)
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
    fonts = verify_locked_font(locks, stage_root)
    python_runtime = verify_python_runtime_inspected_files(locks, stage_root)
    pe = validate_pe_tree(stage_root)
    manifest = validate_build_manifest(stage_root)
    if manifest.get("lock_inputs") != locks.input_hashes():
        raise PipelineError("staging was built from different lock bytes")
    return {
        "profile": state.get("profile"),
        "paths": paths,
        "model_count": len(models),
        "font_count": len(fonts),
        "python_inspected_file_count": sum(
            len(resource["files"]) for resource in python_runtime
        ),
        "pe_count": pe["pe_count"],
        "duplicate_dlls": pe["duplicate_dlls"],
        "load_path_pending": pe["load_path_pending"],
    }
