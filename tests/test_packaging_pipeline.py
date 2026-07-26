from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import io
import os
import shutil
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from native.generate_icon import generate_icon
from scripts.build_native import build_launcher, resolve_toolchain
from scripts.release_pipeline import (
    BUILD_MANIFEST_NAME,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    STAGING_STATE_NAME,
    HashMismatchError,
    InstallRegistry,
    LockValidationError,
    LockSet,
    PeValidationError,
    PipelineError,
    UnsafeArchiveError,
    WheelValidationError,
    _write_deterministic_zip,
    build_deterministic_zip,
    create_temporary_staging,
    evaluate_marker,
    extract_locked_model,
    fetch_exact,
    inspect_pe,
    inspect_wheel,
    install_wheel,
    inventory_tree,
    load_json_object,
    prune_cpython_console_launcher,
    prune_pyside_essentials,
    prune_setuptools_script_launchers,
    publish_staging,
    sha256_file,
    stage_application_source,
    validate_lock_set,
    validate_pe_tree,
    validate_tar_members,
    validate_windows_relative_path,
    validate_zip_archive,
    validate_zip_members,
    verify_locked_font,
    verify_python_runtime_inspected_files,
    verify_saved_staging_for_package,
    version_satisfies,
    windows_path_key,
    tree_digest,
    write_canonical_json,
    write_runtime_configuration,
    write_staging_state,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def make_wheel(
    path: Path,
    *,
    name: str = "demo",
    version: str = "1.0",
    tag: str = "py3-none-any",
    additional: dict[str, bytes] | None = None,
    requires_dist: tuple[str, ...] = (),
) -> dict[str, object]:
    dist = name.replace("-", "_")
    prefix = f"{dist}-{version}.dist-info"
    metadata = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requires_dist)
        + "\n"
    ).encode()
    contents: dict[str, bytes] = {
        f"{dist}/__init__.py": b"VALUE = 1\n",
        f"{prefix}/METADATA": metadata,
        f"{prefix}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: tests\n"
            "Root-Is-Purelib: true\n"
            f"Tag: {tag}\n\n"
        ).encode(),
    }
    contents.update(additional or {})
    record_name = f"{prefix}/RECORD"
    rows = [
        f"{member},{_record_hash(data)},{len(data)}"
        for member, data in sorted(contents.items())
    ]
    rows.append(f"{record_name},,")
    contents[record_name] = ("\n".join(rows) + "\n").encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, data in contents.items():
            archive.writestr(member, data)
    digest, size = sha256_file(path)
    return {
        "name": name,
        "version": version,
        "filename": path.name,
        "url": f"https://example.invalid/{path.name}",
        "size": size,
        "sha256": digest,
        "wheel_tags": [tag],
        "requires_python": ">=3.10",
        "selected_extras": [],
        "source": {"kind": "direct", "required_by": []},
    }


def make_pe(
    path: Path,
    *,
    imported_dll: str | None = None,
    timestamp: int = 0,
    subsystem: int = 3,
    payload_byte: int = 0,
) -> None:
    pe_offset = 0x80
    optional_size = 0xF0
    raw_pointer = 0x200
    raw_size = 0x200
    data = bytearray(raw_pointer + raw_size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH",
        data,
        pe_offset + 4,
        0x8664,
        1,
        timestamp,
        0,
        0,
        optional_size,
        0x2022,
    )
    optional = pe_offset + 24
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<H", data, optional + 68, subsystem)
    struct.pack_into("<I", data, optional + 108, 16)
    if imported_dll:
        struct.pack_into("<II", data, optional + 120, 0x1000, 40)
    section = optional + optional_size
    data[section : section + 8] = b".rdata\0\0"
    struct.pack_into(
        "<IIII",
        data,
        section + 8,
        raw_size,
        0x1000,
        raw_size,
        raw_pointer,
    )
    if imported_dll:
        struct.pack_into("<IIIII", data, raw_pointer, 0, 0, 0, 0x1030, 0)
        encoded = imported_dll.encode("ascii") + b"\0"
        data[raw_pointer + 0x30 : raw_pointer + 0x30 + len(encoded)] = encoded
    data[-1] = payload_byte
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tar_payload(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


def _tar_entry(
    name: str,
    data: bytes,
    *,
    archive_format: int = tarfile.USTAR_FORMAT,
) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info.tobuf(format=archive_format) + _tar_payload(data)


def _pax_header(
    fields: dict[str, str],
    *,
    header_type: bytes = tarfile.XHDTYPE,
) -> bytes:
    return tarfile.TarInfo("pax")._create_pax_generic_header(
        fields,
        header_type,
        "utf-8",
    )


def _gnu_longname_header(path: str) -> bytes:
    return tarfile.TarInfo("longname")._create_gnu_long_header(
        path,
        tarfile.GNUTYPE_LONGNAME,
        "utf-8",
        "surrogateescape",
    )


def _complete_tar(payload: bytes) -> bytes:
    return payload + b"\0" * (tarfile.BLOCKSIZE * 2)


class TargetAndLockTests(unittest.TestCase):
    def _copied_checked_in_locks(self) -> LockSet:
        locks = LockSet.load(REPOSITORY_ROOT / "vendor-lock")
        return LockSet(
            root=locks.root,
            wheels=copy.deepcopy(locks.wheels),
            resources=copy.deepcopy(locks.resources),
        )

    def test_checked_in_lock_has_exact_68_wheel_windows_closure_shape(self) -> None:
        report = validate_lock_set(LockSet.load(REPOSITORY_ROOT / "vendor-lock"))
        self.assertEqual(report["wheel_count"], 68)
        self.assertEqual(report["runtime_resource_count"], 4)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":"1.0.0","schema_version":"2.0.0"}')

            with self.assertRaisesRegex(
                LockValidationError, "duplicate JSON object key"
            ):
                load_json_object(path)

    def test_plan_pinned_core_version_cannot_be_rewritten_in_lock(self) -> None:
        locks = self._copied_checked_in_locks()
        numpy = next(
            artifact
            for artifact in locks.wheels["artifacts"]
            if artifact["name"] == "numpy"
        )
        numpy["version"] = "2.2.7"
        numpy["filename"] = "numpy-2.2.7-cp313-cp313-win_amd64.whl"
        locks.wheels["resolution"]["direct_requirements"][0] = "numpy==2.2.7"

        with self.assertRaisesRegex(
            LockValidationError, "plan-pinned wheel numpy must be exactly 2.2.6"
        ):
            validate_lock_set(locks)

    def test_locked_wheel_tags_must_match_filename_tags(self) -> None:
        locks = self._copied_checked_in_locks()
        numpy = next(
            artifact
            for artifact in locks.wheels["artifacts"]
            if artifact["name"] == "numpy"
        )
        numpy["wheel_tags"] = ["cp312-abi3-win_amd64"]

        with self.assertRaisesRegex(
            LockValidationError, "lock tags do not match the wheel filename tags"
        ):
            validate_lock_set(locks)

    def test_plan_pinned_resource_identity_cannot_be_rewritten(self) -> None:
        locks = self._copied_checked_in_locks()
        detector = next(
            artifact
            for artifact in locks.resources["artifacts"]
            if artifact["id"] == "pp-ocrv6-small-det-inference"
        )
        detector["version"] = "different-model"

        with self.assertRaisesRegex(
            LockValidationError,
            "resource identities or versions differ from the implementation plan",
        ):
            validate_lock_set(locks)

    def test_markers_use_explicit_target_not_host(self) -> None:
        environment = {
            "sys_platform": "win32",
            "python_version": "3.13",
            "python_full_version": "3.13.14",
            "implementation_version": "3.13.14",
            "implementation_name": "cpython",
            "os_name": "nt",
            "platform_machine": "AMD64",
            "platform_release": "11",
            "platform_system": "Windows",
            "platform_version": "11",
            "platform_python_implementation": "CPython",
            "extra": "ocr-core",
        }
        self.assertTrue(
            evaluate_marker(
                "sys_platform == 'win32' and python_version >= '3.13'",
                environment,
            )
        )
        self.assertTrue(
            evaluate_marker(
                "extra == 'ocr-core' and platform_machine == 'AMD64'",
                environment,
            )
        )
        self.assertFalse(evaluate_marker("sys_platform == 'linux'", environment))

    def test_version_specifier_bounds(self) -> None:
        self.assertTrue(version_satisfies("3.13.14", ">=3.10,<3.14"))
        self.assertFalse(version_satisfies("3.13.14", "<3.13"))
        self.assertTrue(version_satisfies("2.2.6", "==2.2.*"))

class ArchiveAndWheelTests(unittest.TestCase):
    def test_windows_archive_paths_reject_traversal_and_device_names(self) -> None:
        for unsafe in (
            "../escape",
            "C:/escape",
            "/absolute",
            "folder\\file",
            "CON.txt",
            "folder/name. ",
            "folder//file",
            "folder/./file",
            "./folder",
            "folder/.",
            "CONIN$",
            "CONOUT$.txt",
            "COM¹",
            "COM².txt",
            "COM³",
            "LPT¹.txt",
            "LPT²",
            "LPT³.txt",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(UnsafeArchiveError):
                    validate_windows_relative_path(unsafe)
        self.assertEqual(windows_path_key("Package/File.txt"), "package/file.txt")

    def test_zip_case_collision_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collision = Path(directory) / "collision.zip"
            with zipfile.ZipFile(collision, "w") as archive:
                archive.writestr("A/file.txt", b"a")
                archive.writestr("a/FILE.txt", b"b")
            with zipfile.ZipFile(collision) as archive:
                with self.assertRaises(UnsafeArchiveError):
                    validate_zip_members(archive)

    def test_zip_file_ancestor_collisions_are_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ancestor = ("Root/App", b"file")
            descendant = ("root/app/child.txt", b"child")
            for descendant_first in (False, True):
                with self.subTest(descendant_first=descendant_first):
                    path = root / f"collision-{descendant_first}.zip"
                    entries = (
                        (descendant, ancestor)
                        if descendant_first
                        else (ancestor, descendant)
                    )
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, data in entries:
                            archive.writestr(name, data)
                    with zipfile.ZipFile(path) as archive:
                        with self.assertRaisesRegex(
                            UnsafeArchiveError,
                            "non-directory member is an ancestor",
                        ):
                            validate_zip_members(archive)

            for label, names in (
                ("implicit", ("Root/App/child.txt",)),
                ("directory", ("Root/App/", "root/app/child.txt")),
            ):
                with self.subTest(valid=label):
                    path = root / f"valid-{label}.zip"
                    with zipfile.ZipFile(path, "w") as archive:
                        for name in names:
                            archive.writestr(name, b"")
                    with zipfile.ZipFile(path) as archive:
                        self.assertEqual(len(validate_zip_members(archive)), len(names))

    def test_archive_directory_members_allow_only_one_trailing_slash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_zip = root / "canonical.zip"
            with zipfile.ZipFile(canonical_zip, "w") as archive:
                archive.writestr("folder/", b"")
            with zipfile.ZipFile(canonical_zip) as archive:
                self.assertIn("folder", validate_zip_members(archive))

            for index, unsafe in enumerate(("/", "///", "folder//")):
                with self.subTest(format="zip", unsafe=unsafe):
                    path = root / f"unsafe-{index}.zip"
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(unsafe, b"")
                    with zipfile.ZipFile(path) as archive:
                        with self.assertRaises(UnsafeArchiveError):
                            validate_zip_members(archive)

            canonical_tar = root / "canonical.tar"
            with tarfile.open(canonical_tar, "w") as archive:
                directory_info = tarfile.TarInfo("folder/")
                directory_info.type = tarfile.DIRTYPE
                archive.addfile(directory_info)
            with tarfile.open(canonical_tar) as archive:
                self.assertIn("folder", validate_tar_members(archive))

            for index, unsafe in enumerate(("/", "///", "folder//")):
                with self.subTest(format="tar", unsafe=unsafe):
                    path = root / f"unsafe-{index}.tar"
                    with tarfile.open(path, "w") as archive:
                        directory_info = tarfile.TarInfo(unsafe)
                        directory_info.type = tarfile.DIRTYPE
                        archive.addfile(directory_info)
                    with tarfile.open(path) as archive:
                        with self.assertRaises(UnsafeArchiveError):
                            validate_tar_members(archive)

            symlink = Path(directory) / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(info, b"target")
            with zipfile.ZipFile(symlink) as archive:
                with self.assertRaises(UnsafeArchiveError):
                    validate_zip_members(archive)

    def test_tar_traversal_and_link_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.tar"
            with tarfile.open(path, "w") as archive:
                data = b"x"
                traversal = tarfile.TarInfo("../escape")
                traversal.size = len(data)
                archive.addfile(traversal, io.BytesIO(data))
            with tarfile.open(path) as archive:
                with self.assertRaises(UnsafeArchiveError):
                    validate_tar_members(archive)

            path = Path(directory) / "link.tar"
            with tarfile.open(path, "w") as archive:
                link = tarfile.TarInfo("model/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../escape"
                archive.addfile(link)
            with tarfile.open(path) as archive:
                with self.assertRaises(UnsafeArchiveError):
                    validate_tar_members(archive)

    def test_ustar_gnu_pax_and_gzip_paths_are_accepted(self) -> None:
        cases = (
            (
                "ustar-prefix",
                f"{'u' * 80}/{'n' * 30}",
                tarfile.USTAR_FORMAT,
            ),
            ("gnu-longname", "g" * 101, tarfile.GNU_FORMAT),
            ("pax-longname", "p" * 101, tarfile.PAX_FORMAT),
            ("pax-unicode", "模型/文件.txt", tarfile.PAX_FORMAT),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, member_name, archive_format in cases:
                for compressed in (False, True):
                    with self.subTest(label=label, compressed=compressed):
                        raw = _complete_tar(
                            _tar_entry(
                                member_name,
                                b"x",
                                archive_format=archive_format,
                            )
                        )
                        if compressed:
                            raw = gzip.compress(raw, mtime=0)
                        path = root / (
                            f"{label}.tar.gz" if compressed else f"{label}.tar"
                        )
                        path.write_bytes(raw)
                        with tarfile.open(path, "r:*") as archive:
                            members = validate_tar_members(archive)
                        self.assertIn(windows_path_key(member_name), members)

    def test_comment_pax_header_before_gnu_longname_is_accepted(self) -> None:
        member_name = "g" * 101
        raw = _complete_tar(
            _pax_header({"comment": "metadata"})
            + _tar_entry(
                member_name,
                b"x",
                archive_format=tarfile.GNU_FORMAT,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.tar"
            path.write_bytes(raw)
            with tarfile.open(path, "r:*") as archive:
                members = validate_tar_members(archive)
        self.assertIn(windows_path_key(member_name), members)

    def test_tar_path_overrides_cannot_hide_noncanonical_declarations(self) -> None:
        cases = {
            "outer-gnu": (
                _gnu_longname_header(f"{'g' * 100}//")
                + _pax_header({"path": "safe/path"})
                + _tar_entry("placeholder", b"x")
            ),
            "inner-pax": (
                _gnu_longname_header("g" * 101)
                + _pax_header({"path": "unsafe//path"})
                + _tar_entry("placeholder", b"x")
            ),
            "ordinary-header": (
                _pax_header({"path": "safe/path"})
                + _tar_entry("../escape", b"x")
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, payload in cases.items():
                with self.subTest(label=label):
                    path = root / f"{label}.tar"
                    path.write_bytes(_complete_tar(payload))
                    with tarfile.open(path, "r:*") as archive:
                        with self.assertRaises(UnsafeArchiveError):
                            validate_tar_members(archive)

    def test_pax_sparse_name_and_path_overrides_are_all_validated(self) -> None:
        cases = {
            "sparse-before-path": (
                {
                    "GNU.sparse.name": "unsafe//sparse",
                    "path": "safe/path",
                },
                "safe/path",
            ),
            "path-before-sparse": (
                {
                    "path": "unsafe//path",
                    "GNU.sparse.name": "safe/sparse",
                },
                "safe/sparse",
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, (fields, final_name) in cases.items():
                for compressed in (False, True):
                    with self.subTest(label=label, compressed=compressed):
                        raw = _complete_tar(
                            _pax_header(fields)
                            + _tar_entry("placeholder", b"x")
                        )
                        if compressed:
                            raw = gzip.compress(raw, mtime=0)
                        path = root / (
                            f"{label}.tar.gz" if compressed else f"{label}.tar"
                        )
                        path.write_bytes(raw)
                        with tarfile.open(path, "r:*") as archive:
                            self.assertEqual(archive.getmembers()[0].name, final_name)
                            with self.assertRaises(UnsafeArchiveError):
                                validate_tar_members(archive)

    def test_pax_path_directory_syntax_uses_final_member_type(self) -> None:
        raw = _complete_tar(
            _pax_header({"path": "safe/"})
            + _tar_entry("placeholder", b"x")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pax-file-with-directory-path.tar"
            path.write_bytes(raw)
            with tarfile.open(path, "r:*") as archive:
                member = archive.getmembers()[0]
                self.assertTrue(member.isreg())
                self.assertEqual(member.name, "safe")
                with self.assertRaises(UnsafeArchiveError):
                    validate_tar_members(archive)

    def test_global_pax_path_is_audited_for_each_later_member_type(self) -> None:
        directory = tarfile.TarInfo("placeholder/")
        directory.type = tarfile.DIRTYPE
        raw = _complete_tar(
            _pax_header(
                {"path": "safe/"},
                header_type=tarfile.XGLTYPE,
            )
            + directory.tobuf(format=tarfile.USTAR_FORMAT)
            + _pax_header({"path": "other/file"})
            + _tar_entry("placeholder-file", b"x")
        )
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            for compressed in (False, True):
                with self.subTest(compressed=compressed):
                    payload = gzip.compress(raw, mtime=0) if compressed else raw
                    path = root / (
                        "global-path.tar.gz" if compressed else "global-path.tar"
                    )
                    path.write_bytes(payload)
                    with tarfile.open(path, "r:*") as archive:
                        members = archive.getmembers()
                        self.assertTrue(members[0].isdir())
                        self.assertTrue(members[1].isreg())
                        self.assertEqual(
                            [member.name for member in members],
                            ["safe", "other/file"],
                        )
                        with self.assertRaises(UnsafeArchiveError):
                            validate_tar_members(archive)

    def test_tar_sparse_members_are_rejected(self) -> None:
        sparse = tarfile.TarInfo("sparse")
        sparse.type = tarfile.GNUTYPE_SPARSE
        raw = _complete_tar(sparse.tobuf(format=tarfile.GNU_FORMAT))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for compressed in (False, True):
                with self.subTest(compressed=compressed):
                    payload = gzip.compress(raw, mtime=0) if compressed else raw
                    path = root / (
                        "sparse.tar.gz" if compressed else "sparse.tar"
                    )
                    path.write_bytes(payload)
                    with tarfile.open(path, "r:*") as archive:
                        with self.assertRaisesRegex(
                            UnsafeArchiveError,
                            "sparse files are forbidden",
                        ):
                            validate_tar_members(archive)

    def test_wheel_record_and_data_spread_are_verified_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "demo-1.0-py3-none-any.whl"
            sentinel = root / "executed"
            artifact = make_wheel(
                wheel,
                additional={
                    "demo-1.0.data/data/share/demo.txt": b"data\n",
                    "setup.py": (
                        f"from pathlib import Path; "
                        f"Path({str(sentinel)!r}).write_text('bad')\n"
                    ).encode(),
                },
            )
            stage = root / "stage"
            registry = InstallRegistry()
            report = install_wheel(wheel, artifact, stage, registry)
            self.assertGreater(report["installed_file_count"], 1)
            self.assertEqual((stage / "runtime/share/demo.txt").read_bytes(), b"data\n")
            self.assertFalse(sentinel.exists())

    def test_wheel_record_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "demo-1.0-py3-none-any.whl"
            artifact = make_wheel(original)
            tampered = root / "tampered.whl"
            with (
                zipfile.ZipFile(original) as source,
                zipfile.ZipFile(tampered, "w") as target,
            ):
                for info in source.infolist():
                    data = source.read(info)
                    if info.filename == "demo/__init__.py":
                        data = b"VALUE = 2\n"
                    target.writestr(info, data)
            digest, size = sha256_file(tampered)
            artifact = {**artifact, "sha256": digest, "size": size}
            with self.assertRaises(WheelValidationError):
                inspect_wheel(tampered, artifact)

    def test_vendored_dist_info_metadata_is_not_a_second_wheel_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "demo-1.0-py3-none-any.whl"
            artifact = make_wheel(
                wheel,
                additional={
                    "demo/_vendor/helper-2.0.dist-info/WHEEL": b"vendored wheel\n",
                    "demo/_vendor/helper-2.0.dist-info/METADATA": (
                        b"Name: helper\nVersion: 2.0\n"
                    ),
                    "demo/_vendor/helper-2.0.dist-info/RECORD": b"",
                },
            )

            inspection = inspect_wheel(wheel, artifact)

            self.assertEqual(inspection.dist_info_prefix, "demo-1.0.dist-info/")

    def test_wheel_path_traversal_is_rejected_before_record_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "demo-1.0-py3-none-any.whl"
            artifact = make_wheel(wheel, additional={"../escape": b"x"})
            with self.assertRaises(UnsafeArchiveError):
                inspect_wheel(wheel, artifact)

    def test_locked_model_extracts_only_declared_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "model.tar"
            content = b"model-bytes"
            with tarfile.open(archive_path, "w") as archive:
                info = tarfile.TarInfo("source/model.bin")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
                ignored = tarfile.TarInfo("source/readme.txt")
                ignored.size = 1
                archive.addfile(ignored, io.BytesIO(b"x"))
            digest, size = sha256_file(archive_path)
            artifact = {
                "id": "model",
                "kind": "paddle_inference_model",
                "size": size,
                "sha256": digest,
                "unpack": {
                    "format": "tar",
                    "reject_unsafe_paths": True,
                    "destination_root": "models/model/",
                    "files": [
                        {
                            "source_path": "source/model.bin",
                            "destination_path": "model.bin",
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                },
            }
            stage = root / "stage"
            report = extract_locked_model(archive_path, artifact, stage)
            self.assertEqual((stage / "models/model/model.bin").read_bytes(), content)
            self.assertFalse((stage / "models/model/readme.txt").exists())
            self.assertEqual(len(report["files"]), 1)


class FetchAndPeTests(unittest.TestCase):
    class _Response(io.BytesIO):
        def geturl(self) -> str:
            return "https://cdn.example.invalid/artifact"

        def __enter__(self) -> "FetchAndPeTests._Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def test_fetch_uses_exact_hash_and_does_not_keep_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cache" / "artifact.bin"
            content = b"locked bytes"

            def opener(_request: object) -> FetchAndPeTests._Response:
                return self._Response(content)

            result = fetch_exact(
                url="https://example.invalid/artifact",
                destination=destination,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size=len(content),
                opener=opener,
            )
            self.assertFalse(result["cache_hit"])
            self.assertEqual(destination.read_bytes(), content)

            bad_destination = Path(directory) / "cache" / "bad.bin"
            with self.assertRaises(HashMismatchError):
                fetch_exact(
                    url="https://example.invalid/artifact",
                    destination=bad_destination,
                    expected_sha256="0" * 64,
                    expected_size=len(content),
                    opener=opener,
                )
            self.assertFalse(bad_destination.exists())
            self.assertEqual(list(bad_destination.parent.glob("*.part")), [])

    def test_synthetic_pe_parses_import_and_rejects_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pe = root / "module.pyd"
            make_pe(pe, imported_dll="missing.dll")
            info = inspect_pe(pe)
            self.assertEqual(info.machine, 0x8664)
            self.assertEqual(info.imports, ("missing.dll",))
            with self.assertRaises(PeValidationError):
                validate_pe_tree(root, require_launcher=False)

    def test_all_setuptools_script_launcher_templates_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site_packages = Path(directory)
            package = site_packages / "setuptools"
            package.mkdir()
            removed_names = {
                "cli-32.exe",
                "cli-64.exe",
                "cli-arm64.exe",
                "cli.exe",
                "gui-32.exe",
                "gui-64.exe",
                "gui-arm64.exe",
                "gui.exe",
            }
            for basename in removed_names:
                (package / basename).write_bytes(b"launcher")
            retained = package / "__init__.py"
            retained.write_text("", encoding="utf-8")

            report = prune_setuptools_script_launchers(site_packages)

            self.assertEqual(
                set(report["removed"]),
                {f"setuptools/{name}" for name in removed_names},
            )
            self.assertTrue(
                all(not (package / name).exists() for name in removed_names)
            )
            self.assertTrue(retained.is_file())

    def test_pyside_pruning_keeps_runtime_support_but_not_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site_packages = Path(directory)
            package = site_packages / "PySide6"
            for relative in (
                "__init__.py",
                "support/__init__.py",
                "support/deprecated.py",
                "support/generate_pyi.py",
                "typesystems/typesystem_core.xml",
                "glue/qtcore.cpp",
                "QtCore.pyd",
                "QtSql.pyd",
                "plugins/platforms/qwindows.dll",
            ):
                path = package / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")

            report = prune_pyside_essentials(site_packages)

            for relative in (
                "__init__.py",
                "support/__init__.py",
                "support/deprecated.py",
                "QtCore.pyd",
                "plugins/platforms/qwindows.dll",
            ):
                self.assertTrue((package / relative).is_file())
            for relative in (
                "support/generate_pyi.py",
                "typesystems/typesystem_core.xml",
                "glue/qtcore.cpp",
                "QtSql.pyd",
            ):
                self.assertFalse((package / relative).exists())
                self.assertIn(relative, report["removed"])

    def test_cpython_console_launcher_is_pruned_but_pythonw_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            runtime = stage / "runtime"
            runtime.mkdir()
            console = runtime / "python.exe"
            gui = runtime / "pythonw.exe"
            console.write_bytes(b"console")
            gui.write_bytes(b"gui")

            report = prune_cpython_console_launcher(stage)

            self.assertFalse(console.exists())
            self.assertEqual(gui.read_bytes(), b"gui")
            self.assertEqual(report["removed"][0]["path"], "runtime/python.exe")
            self.assertEqual(
                report["removed"][0]["sha256"],
                hashlib.sha256(b"console").hexdigest(),
            )

    def test_conflicting_msvc_runtime_is_preserved_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "runtime" / "vcruntime140.dll"
            second = (
                root
                / "runtime"
                / "Lib"
                / "site-packages"
                / "PySide6"
                / "vcruntime140.dll"
            )
            make_pe(first, payload_byte=1)
            make_pe(second, payload_byte=2)
            report = validate_pe_tree(root, require_launcher=False)
            self.assertEqual(len(report["duplicate_dlls"]), 1)
            duplicate = report["duplicate_dlls"][0]
            self.assertTrue(duplicate["hashes_differ"])
            self.assertIn("deduplication is forbidden", duplicate["reason"])

    def test_conflicting_msvc_runtime_outside_approved_locations_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pe(root / "runtime" / "vcruntime140.dll", payload_byte=1)
            make_pe(
                root
                / "runtime"
                / "Lib"
                / "site-packages"
                / "PySide6"
                / "vcruntime140.dll",
                payload_byte=2,
            )
            make_pe(root / "rogue" / "vcruntime140.dll", payload_byte=3)

            with self.assertRaisesRegex(
                PeValidationError,
                "appears outside approved runtime",
            ):
                validate_pe_tree(root, require_launcher=False)

    def test_cross_directory_dll_candidate_stays_pending_for_windows_loader(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pe(
                root / "package_a" / "importer.pyd",
                imported_dll="candidate.dll",
            )
            make_pe(root / "package_b" / "candidate.dll")

            report = validate_pe_tree(root, require_launcher=False)

            self.assertEqual(
                report["load_path_pending"],
                [
                    {
                        "path": "package_a/importer.pyd",
                        "import": "candidate.dll",
                        "candidate_paths": ["package_b/candidate.dll"],
                        "status": "pending-windows-loader-validation",
                    }
                ],
            )

    def test_duplicate_rule_applies_to_dlls_but_not_qualified_pyds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pe(root / "package_a" / "lib.cp313-win_amd64.pyd", payload_byte=1)
            make_pe(root / "package_b" / "lib.cp313-win_amd64.pyd", payload_byte=2)
            report = validate_pe_tree(root, require_launcher=False)
            self.assertEqual(report["duplicate_dlls"], [])

            make_pe(root / "package_a" / "shared.dll", payload_byte=1)
            make_pe(root / "package_b" / "shared.dll", payload_byte=2)
            with self.assertRaisesRegex(
                PeValidationError,
                "conflicting duplicate DLL basename shared.dll",
            ):
                validate_pe_tree(root, require_launcher=False)

    def test_windows_11_system_imports_are_not_required_in_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, imported in enumerate(
                ("combase.dll", "pdh.dll", "imagehlp.dll", "icuuc.dll")
            ):
                make_pe(
                    root / f"module_{index}.pyd",
                    imported_dll=imported,
                )

            report = validate_pe_tree(root, require_launcher=False)

            self.assertEqual(len(report["files"]), 4)

    def test_locked_font_and_python_members_are_reverified_in_staging(self) -> None:
        locks = LockSet.load(REPOSITORY_ROOT / "vendor-lock")
        copied = LockSet(
            root=locks.root,
            wheels=copy.deepcopy(locks.wheels),
            resources=copy.deepcopy(locks.resources),
        )
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            font_artifact = next(
                artifact
                for artifact in copied.resources["artifacts"]
                if artifact["kind"] == "font"
            )
            font_entry = font_artifact["unpack"]["selected_files"][0]
            font_data = b"locked test font"
            font_entry["size"] = len(font_data)
            font_entry["sha256"] = hashlib.sha256(font_data).hexdigest()
            font_path = stage / "assets" / font_entry["destination_path"]
            font_path.parent.mkdir(parents=True)
            font_path.write_bytes(font_data)

            python_artifact = next(
                artifact
                for artifact in copied.resources["artifacts"]
                if artifact["kind"] == "python_embeddable_runtime"
            )
            python_paths: list[Path] = []
            for inspected in python_artifact["inspected_files"]:
                data = f"locked:{inspected['path']}".encode()
                inspected["size"] = len(data)
                inspected["sha256"] = hashlib.sha256(data).hexdigest()
                path = stage / "runtime" / inspected["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                python_paths.append(path)

            self.assertEqual(len(verify_locked_font(copied, stage)), 1)
            self.assertEqual(
                len(verify_python_runtime_inspected_files(copied, stage)),
                1,
            )

            font_path.write_bytes(b"tampered")
            with self.assertRaises(HashMismatchError):
                verify_locked_font(copied, stage)
            font_path.write_bytes(font_data)
            python_paths[0].write_bytes(b"tampered")
            with self.assertRaises(HashMismatchError):
                verify_python_runtime_inspected_files(copied, stage)


class NativeAndZipTests(unittest.TestCase):
    def test_icon_generator_is_deterministic_and_contains_four_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.ico"
            second = Path(directory) / "second.ico"
            generate_icon(first)
            generate_icon(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            reserved, kind, count = struct.unpack_from("<HHH", first.read_bytes(), 0)
            self.assertEqual((reserved, kind, count), (0, 1, 4))

    def test_toolchain_resolution_prepends_private_absolute_bin_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary_directory = Path(directory) / "toolchain bin"
            binary_directory.mkdir()
            gcc_path = binary_directory / "prefix-gcc"
            windres_path = binary_directory / "prefix-windres"
            gcc_path.touch()
            windres_path.touch()
            resolved = {
                "prefix-gcc": str(gcc_path),
                "prefix-windres": str(windres_path),
            }
            with (
                mock.patch.dict(os.environ, {"PATH": "original-path"}),
                mock.patch(
                    "scripts.build_native.shutil.which",
                    side_effect=lambda executable: resolved.get(executable),
                ),
            ):
                gcc, windres, environment = resolve_toolchain("prefix-")
                self.assertEqual(os.environ["PATH"], "original-path")

            self.assertEqual(Path(gcc), gcc_path.resolve())
            self.assertEqual(Path(windres), windres_path.resolve())
            self.assertEqual(
                environment["PATH"].split(os.pathsep)[0],
                str(binary_directory.resolve()),
            )

    def test_temporary_staging_root_is_short_unique_and_in_output_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            output = parent / "TextSnapLayout"
            first = create_temporary_staging(output)
            second = create_temporary_staging(output)

            self.assertEqual(first.parent, parent)
            self.assertEqual(second.parent, parent)
            self.assertNotEqual(first, second)
            self.assertRegex(first.name, r"^\.ts-[0-9a-f]{8}$")
            self.assertLessEqual(len(first.name), len(output.name))
            longest_relative = Path(
                "runtime/Lib/site-packages/modelscope/msdatasets/dataset_cls/"
                "custom_datasets/image_quality_assessment_degradation/"
                "__pycache__/"
                "image_quality_assessment_degradation_dataset.cpython-313.pyc"
            )
            self.assertLessEqual(
                len(str(first / longest_relative)),
                len(str(output / longest_relative)),
            )

    def test_runtime_configuration_precreates_all_private_cache_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            report = write_runtime_configuration(stage)
            expected = {
                "runtime/pdx-cache",
                "runtime/pdx-cache/temp",
                "runtime/pdx-cache/func_ret",
                "runtime/pdx-cache/locks",
                "data",
            }

            self.assertEqual(set(report["precreated_directories"]), expected)
            self.assertTrue(
                all((stage / relative).is_dir() for relative in expected)
            )

    @unittest.skipUnless(
        shutil.which("x86_64-w64-mingw32-gcc")
        and shutil.which("x86_64-w64-mingw32-windres"),
        "x86_64 MinGW toolchain is not installed",
    )
    def test_native_launcher_and_tray_icon_build_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.exe"
            second = root / "second.exe"
            first_icon = root / "first.ico"
            second_icon = root / "second.ico"
            first_result = build_launcher(
                source_root=REPOSITORY_ROOT / "native",
                output=first,
                icon_output=first_icon,
            )
            second_result = build_launcher(
                source_root=REPOSITORY_ROOT / "native",
                output=second,
                icon_output=second_icon,
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_icon.read_bytes(), second_icon.read_bytes())
            info = inspect_pe(first)
            self.assertEqual(info.machine, 0x8664)
            self.assertEqual(info.optional_magic, 0x20B)
            self.assertEqual(info.subsystem, 2)
            self.assertEqual(info.timestamp, 0)
            self.assertEqual(first_result["gcc_version"], second_result["gcc_version"])
            self.assertEqual(
                first_result["windres_version"],
                second_result["windres_version"],
            )
            self.assertTrue(first_result["gcc_version"])
            self.assertTrue(first_result["windres_version"])

    def _make_saved_stage(self, root: Path, *, profile: str) -> Path:
        stage = root / "stage"
        stage.mkdir()
        (stage / "TextSnapLayout.exe").write_bytes(b"synthetic launcher")
        (stage / "empty").mkdir()
        files = inventory_tree(
            stage,
            excluded_paths=frozenset({BUILD_MANIFEST_NAME, STAGING_STATE_NAME}),
        )
        write_canonical_json(
            stage / BUILD_MANIFEST_NAME,
            {
                "product": {
                    "name": PRODUCT_NAME,
                    "version": PRODUCT_VERSION,
                    "target": "windows-x86_64",
                    "python": "3.13.14",
                },
                "profile": profile,
                "files": files,
                "files_digest": tree_digest(files),
            },
        )
        write_staging_state(stage, profile=profile)
        return stage

    def test_two_zip_builds_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            first = root / "one" / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            second = root / "two" / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            first_result = _write_deterministic_zip(stage, first)
            second_result = _write_deterministic_zip(stage, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_result["sha256"], second_result["sha256"])
            report = validate_zip_archive(first)
            self.assertGreaterEqual(report["file_count"], 2)
            with zipfile.ZipFile(first) as archive:
                self.assertFalse(
                    any(
                        member.filename.endswith(".textsnap-staging.json")
                        for member in archive.infolist()
                    )
                )

    def test_zip_builders_reject_nonfixed_product_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            output = root / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            alternate = PRODUCT_NAME.lower()

            with self.assertRaisesRegex(PipelineError, "must be exactly"):
                _write_deterministic_zip(
                    stage,
                    output,
                    product_directory=alternate,
                )
            with self.assertRaisesRegex(PipelineError, "must be exactly"):
                build_deterministic_zip(
                    stage,
                    output,
                    locks=mock.sentinel.locks,
                    product_directory=alternate,
                )

            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(output.suffix + ".sha256").exists())

    def test_zip_validation_reads_member_payloads_and_rejects_corruption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            archive_path = (
                root / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            _write_deterministic_zip(stage, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                info = archive.getinfo(
                    f"{PRODUCT_NAME}/TextSnapLayout.exe"
                )
            archive_bytes = bytearray(archive_path.read_bytes())
            name_length, extra_length = struct.unpack_from(
                "<HH", archive_bytes, info.header_offset + 26
            )
            payload_offset = (
                info.header_offset + 30 + name_length + extra_length
            )
            archive_bytes[payload_offset + info.compress_size // 2] ^= 1
            archive_path.write_bytes(archive_bytes)

            with self.assertRaisesRegex(
                PipelineError,
                "cannot read release ZIP member|invalid release ZIP archive",
            ):
                validate_zip_archive(archive_path)

    def test_zip_validation_reads_empty_directory_compressed_stream(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            archive_path = (
                root / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            _write_deterministic_zip(stage, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                info = archive.getinfo(f"{PRODUCT_NAME}/empty/")
            self.assertEqual(info.file_size, 0)
            archive_bytes = bytearray(archive_path.read_bytes())
            name_length, extra_length = struct.unpack_from(
                "<HH", archive_bytes, info.header_offset + 26
            )
            payload_offset = (
                info.header_offset + 30 + name_length + extra_length
            )
            archive_bytes[payload_offset] ^= 0x04
            archive_path.write_bytes(archive_bytes)

            with self.assertRaisesRegex(
                PipelineError,
                "cannot read release ZIP member|invalid release ZIP archive",
            ):
                validate_zip_archive(archive_path)

    def test_zip_validation_rejects_nonempty_directory_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = (
                Path(directory)
                / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in (
                    (f"{PRODUCT_NAME}/", b""),
                    (f"{PRODUCT_NAME}/empty/", b"not-empty"),
                    (f"{PRODUCT_NAME}/TextSnapLayout.exe", b"exe"),
                    (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME}", b"{}"),
                ):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    archive.writestr(info, data)

            with self.assertRaisesRegex(
                PipelineError,
                "directory member is not empty",
            ):
                validate_zip_archive(path)

    def test_zip_validation_requires_explicit_directory_product_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "missing": (),
                "ordinary-file": ((PRODUCT_NAME, b"not a directory"),),
                "wrong-case": ((f"{PRODUCT_NAME.lower()}/", b""),),
            }
            for label, root_entries in cases.items():
                with self.subTest(label=label):
                    path = root / f"{label}.zip"
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, data in (
                            *root_entries,
                            (f"{PRODUCT_NAME}/TextSnapLayout.exe", b"exe"),
                            (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME}", b"{}"),
                        ):
                            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                            archive.writestr(info, data)

                    with self.assertRaisesRegex(
                        PipelineError,
                        "product root must be an explicit directory",
                    ):
                        validate_zip_archive(path)

    def test_zip_validation_rejects_explicit_file_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = (
                Path(directory)
                / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in (
                    (f"{PRODUCT_NAME}/", b""),
                    (f"{PRODUCT_NAME}/App", b"not a directory"),
                    (f"{PRODUCT_NAME.lower()}/app/child.txt", b"child"),
                    (f"{PRODUCT_NAME}/TextSnapLayout.exe", b"exe"),
                    (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME}", b"{}"),
                ):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    archive.writestr(info, data)

            with self.assertRaisesRegex(
                PipelineError,
                "non-directory member is an ancestor",
            ):
                validate_zip_archive(path)

    def test_zip_validation_requires_exact_regular_release_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "directories": (
                    (f"{PRODUCT_NAME}/TextSnapLayout.exe/", b""),
                    (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME}/", b""),
                ),
                "launcher-case": (
                    (f"{PRODUCT_NAME}/textsnaplayout.exe", b"exe"),
                    (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME}", b"{}"),
                ),
                "manifest-case": (
                    (f"{PRODUCT_NAME}/TextSnapLayout.exe", b"exe"),
                    (f"{PRODUCT_NAME}/{BUILD_MANIFEST_NAME.lower()}", b"{}"),
                ),
            }
            for label, required_entries in cases.items():
                with self.subTest(label=label):
                    path = root / f"{label}.zip"
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, data in (
                            (f"{PRODUCT_NAME}/", b""),
                            *required_entries,
                        ):
                            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                            archive.writestr(info, data)

                    with self.assertRaisesRegex(
                        PipelineError,
                        "lacks required regular launcher or manifest",
                    ):
                        validate_zip_archive(path)

    def test_zip_validator_rejects_custom_expected_product_root(self) -> None:
        alternate = "OtherProduct"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom-root.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in (
                    (f"{alternate}/", b""),
                    (f"{alternate}/TextSnapLayout.exe", b"exe"),
                    (f"{alternate}/{BUILD_MANIFEST_NAME}", b"{}"),
                ):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    archive.writestr(info, data)

            with self.assertRaisesRegex(PipelineError, "must be exactly"):
                validate_zip_archive(
                    path,
                    expected_product_directory=alternate,
                )

    def test_private_use_saved_staging_is_package_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")

            state = verify_saved_staging_for_package(stage)

            self.assertEqual(state["profile"], "private-use")

    def test_saved_staging_rejects_added_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            (stage / "added-empty").mkdir()

            with self.assertRaisesRegex(
                PipelineError, "staging tree changed after validation"
            ):
                verify_saved_staging_for_package(stage)

    def test_saved_staging_rejects_removed_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            (stage / "empty").rmdir()

            with self.assertRaisesRegex(
                PipelineError, "staging tree changed after validation"
            ):
                verify_saved_staging_for_package(stage)

    def test_saved_staging_rejects_nested_state_named_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            application = stage / "app"
            application.mkdir()
            write_staging_state(stage, profile="private-use")
            (application / STAGING_STATE_NAME).mkdir()

            with self.assertRaisesRegex(
                PipelineError, "staging tree changed after validation"
            ):
                verify_saved_staging_for_package(stage)

    def test_manifest_exclusions_keep_nested_same_named_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / BUILD_MANIFEST_NAME).write_text("root", encoding="utf-8")
            (stage / STAGING_STATE_NAME).write_text("root", encoding="utf-8")
            nested_manifest = stage / "app" / BUILD_MANIFEST_NAME
            nested_manifest.parent.mkdir()
            nested_manifest.write_text("nested manifest", encoding="utf-8")
            nested_state = stage / "app" / STAGING_STATE_NAME
            nested_state.write_text("nested state", encoding="utf-8")

            inventory = inventory_tree(
                stage,
                excluded_paths=frozenset(
                    {BUILD_MANIFEST_NAME, STAGING_STATE_NAME}
                ),
            )

            self.assertEqual(
                [entry["path"] for entry in inventory],
                [
                    f"app/{STAGING_STATE_NAME}",
                    f"app/{BUILD_MANIFEST_NAME}",
                ],
            )

    def test_nested_state_named_file_is_packaged_but_root_state_is_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            nested_state = stage / "app" / STAGING_STATE_NAME
            nested_state.parent.mkdir()
            nested_state.write_text("application data", encoding="utf-8")
            output = root / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"

            _write_deterministic_zip(stage, output)
            validate_zip_archive(output)

            with zipfile.ZipFile(output) as archive:
                names = {member.filename for member in archive.infolist()}
            self.assertIn(
                f"{PRODUCT_NAME}/app/{STAGING_STATE_NAME}",
                names,
            )
            self.assertNotIn(
                f"{PRODUCT_NAME}/{STAGING_STATE_NAME}",
                names,
            )

    def test_zip_output_inside_staging_is_rejected_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            output_directory = stage / "release"
            output = (
                output_directory
                / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )

            with self.assertRaisesRegex(PipelineError, "outside staging root"):
                _write_deterministic_zip(stage, output)

            self.assertFalse(output_directory.exists())
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(output.suffix + ".sha256").exists())

    def test_checksum_publish_replaces_hardlink_without_touching_victim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            output = (
                root
                / "artifacts"
                / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            output.parent.mkdir()
            checksum = output.with_suffix(output.suffix + ".sha256")
            victim = stage / "TextSnapLayout.exe"
            original = victim.read_bytes()
            os.link(victim, checksum)

            result = _write_deterministic_zip(stage, output)

            self.assertEqual(victim.read_bytes(), original)
            self.assertTrue(checksum.is_file())
            self.assertFalse(checksum.is_symlink())
            self.assertFalse(os.path.samefile(victim, checksum))
            self.assertEqual(
                checksum.read_text(encoding="ascii"),
                f"{result['sha256']}  {output.name}\n",
            )

    def test_checksum_temporary_is_cleaned_when_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")
            output = (
                root
                / "artifacts"
                / f"{PRODUCT_NAME}-{PRODUCT_VERSION}-win-x64.zip"
            )
            checksum = output.with_suffix(output.suffix + ".sha256")
            real_replace = os.replace

            def replace(source: object, destination: object) -> None:
                if Path(destination) == checksum:
                    raise OSError("synthetic checksum publish failure")
                real_replace(source, destination)

            with (
                mock.patch(
                    "scripts.release_pipeline.os.replace",
                    side_effect=replace,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "synthetic checksum publish failure",
                ),
            ):
                _write_deterministic_zip(stage, output)

            self.assertFalse(checksum.exists())
            self.assertEqual(
                list(output.parent.glob(f".{checksum.name}.*.tmp")),
                [],
            )

    def test_other_staging_profile_is_not_package_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="release")

            with self.assertRaisesRegex(PipelineError, "private-use"):
                verify_saved_staging_for_package(stage)

    def test_publish_staging_moves_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / ".candidate"
            output = root / "TextSnapLayout"
            temporary.mkdir()
            (temporary / "file.txt").write_text("content", encoding="utf-8")

            publish_staging(temporary, output)

            self.assertTrue((output / "file.txt").is_file())
            self.assertFalse(temporary.exists())

    def test_entry_script_requires_bytecode_protection_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "textsnap"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            bad_entry = root / "bad.py"
            bad_entry.write_text(
                "from textsnap.bootstrap import run_application\n"
                "import sys\n"
                "sys.dont_write_bytecode = True\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "dont_write_bytecode"):
                stage_application_source(
                    package, bad_entry, root / "stage", readme=None
                )


if __name__ == "__main__":
    unittest.main()
