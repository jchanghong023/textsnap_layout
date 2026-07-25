from __future__ import annotations

import base64
import copy
import hashlib
import io
import shutil
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from native.generate_icon import generate_icon
from scripts.build_native import build_launcher
from scripts.release_pipeline import (
    BUILD_MANIFEST_NAME,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    HashMismatchError,
    InstallRegistry,
    LockValidationError,
    LockSet,
    PeValidationError,
    PipelineError,
    UnsafeArchiveError,
    WheelValidationError,
    _write_deterministic_zip,
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
    verify_saved_staging_for_package,
    version_satisfies,
    windows_path_key,
    tree_digest,
    write_canonical_json,
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
            excluded_names=frozenset({BUILD_MANIFEST_NAME, ".textsnap-staging.json"}),
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

    def test_private_use_saved_staging_is_package_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self._make_saved_stage(root, profile="private-use")

            state = verify_saved_staging_for_package(stage)

            self.assertEqual(state["profile"], "private-use")

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
