from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Mapping

from textsnap.domain import Success
from textsnap.layout import display_cell_width
from textsnap.ocr import OcrEngine
from textsnap.paths import BundlePaths
from textsnap.privacy import OfflineGuard, offline_guard_active
from textsnap.tiling import generate_tiles


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "html"
MANIFEST_PATH = FIXTURE_ROOT / "corpus-manifest.json"
CHARACTER_ERROR_RATE_LIMIT = 0.02
SMALL_CODE_PROBE = (
    "@0|__init__.py|TARGET_AST_UNAVAILABLE;"
    "M1|__init__.py::module|UNAVAILABLE|;"
    "@0|__main__.py|TARGET_AST_UNAVAILABLE;"
    "M1|__mai"
)


def _snapshot(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                "directory" if path.is_dir() else "file",
                0 if path.is_dir() else path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
        )
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _body_characters(text: str) -> str:
    """Exclude layout whitespace from the body-text error-rate denominator."""

    return "".join(character for character in text if not character.isspace())


def _character_error_rate(expected: str, actual: str) -> float:
    expected_body = _body_characters(expected)
    actual_body = _body_characters(actual)
    if not expected_body:
        return 0.0 if not actual_body else 1.0
    return _levenshtein_distance(expected_body, actual_body) / len(expected_body)


def _anchor_position(text: str, anchor: str) -> tuple[int, int]:
    if text.count(anchor) != 1:
        raise ValueError(f"anchor must occur exactly once: {anchor!r}")
    offset = text.index(anchor)
    preceding = text[:offset]
    row = preceding.count("\n")
    prefix = preceding.rsplit("\n", 1)[-1]
    return row, display_cell_width(prefix)


def _same_output_line(text: str, left_anchor: str, right_anchor: str) -> bool:
    return _anchor_position(text, left_anchor)[0] == _anchor_position(
        text,
        right_anchor,
    )[0]


def _load_fixture() -> tuple[dict[str, Any], str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    page = manifest["pages"][0]
    expected = (FIXTURE_ROOT / page["expected"]).read_text(
        encoding="utf-8"
    ).removesuffix("\n")
    return manifest, expected


def _line_segments(line: str) -> tuple[tuple[int, str], ...]:
    """Separate visual columns while preserving code indentation as text."""

    marker = "RIGHT-COLUMN"
    if marker not in line:
        return ((0, line),)
    right_offset = line.index(marker)
    prefix = line[:right_offset]
    segments: list[tuple[int, str]] = []
    left = prefix.rstrip(" ")
    if left:
        segments.append((0, left))
    segments.append((display_cell_width(prefix), line[right_offset:]))
    return tuple(segments)


@dataclass(frozen=True)
class _RenderedCorpus:
    case_id: str
    image_bgr: Any
    pixel_sha256: str
    png_sha256: str
    line_bounds: Mapping[str, tuple[int, int, int, int]]


def _render_corpus(
    *,
    expected: str,
    rendering: Mapping[str, Any],
    case: Mapping[str, Any],
    font_file: Path,
    output_path: Path,
) -> _RenderedCorpus:
    """Render checked-in ground truth locally, without a browser or network."""

    if not offline_guard_active():
        raise RuntimeError("controlled corpus rendering requires OfflineGuard")
    import numpy
    from PIL import Image, ImageDraw, ImageFont

    width = int(case["width"])
    height = int(case["height"])
    origin_x = int(case["origin_x"])
    origin_y = int(case["origin_y"])
    row_step = int(case["row_step"])
    lines = expected.splitlines()
    styles = tuple(str(style) for style in rendering["line_styles"])
    anchors = tuple(str(anchor) for anchor in rendering["line_anchors"])
    if len(lines) != len(styles) or len(lines) != len(anchors):
        raise ValueError("fixture lines, styles, and anchors must have equal length")

    layout_engine = ImageFont.Layout.BASIC
    fonts = {
        str(style): ImageFont.truetype(
            str(font_file),
            int(size),
            layout_engine=layout_engine,
        )
        for style, size in rendering["font_sizes"].items()
    }
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    line_bounds: dict[str, tuple[int, int, int, int]] = {}

    for row, (line, style, anchor) in enumerate(zip(lines, styles, anchors)):
        font = fonts[style]
        y = origin_y + row * row_step
        cell_advance = float(font.getlength("0"))
        positioned_segments = [
            (
                origin_x + cell_column * cell_advance,
                segment,
            )
            for cell_column, segment in _line_segments(line)
        ]
        bounds = [
            draw.textbbox(
                (x, y),
                segment,
                font=font,
                anchor="lt",
            )
            for x, segment in positioned_segments
        ]
        left = min(bound[0] for bound in bounds)
        top = min(bound[1] for bound in bounds)
        right = max(bound[2] for bound in bounds)
        bottom = max(bound[3] for bound in bounds)
        if style == "inverse":
            draw.rectangle(
                (left - 12, top - 6, right + 12, bottom + 6),
                fill=(17, 17, 17),
            )
            fill = (255, 255, 255)
        else:
            fill = (17, 17, 17)
        for x, segment in positioned_segments:
            draw.text(
                (x, y),
                segment,
                font=font,
                fill=fill,
                anchor="lt",
            )
        line_bounds[anchor] = (left, top, right, bottom)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output_path,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    pixels = image.tobytes()
    rgb = numpy.asarray(image, dtype=numpy.uint8)
    image_bgr = numpy.ascontiguousarray(rgb[:, :, ::-1])
    return _RenderedCorpus(
        case_id=str(case["id"]),
        image_bgr=image_bgr,
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        png_sha256=_sha256_file(output_path),
        line_bounds=line_bounds,
    )


def _render_small_code_probe(*, font_file: Path) -> Any:
    if not offline_guard_active():
        raise RuntimeError("small-code rendering requires OfflineGuard")
    import numpy
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(
        str(font_file),
        12,
        layout_engine=ImageFont.Layout.BASIC,
    )
    bounds = font.getbbox(SMALL_CODE_PROBE)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    image = Image.new("RGB", (width + 4, height + 4), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text(
        (2 - bounds[0], 2 - bounds[1]),
        SMALL_CODE_PROBE,
        font=font,
        fill=(17, 17, 17),
    )
    rgb = numpy.asarray(image, dtype=numpy.uint8)
    return numpy.ascontiguousarray(rgb[:, :, ::-1])


class _GuardedImportProbeComplete(Exception):
    pass


def _run_guarded_render_probe() -> None:
    import builtins
    from types import ModuleType

    if "numpy" in sys.modules or "PIL" in sys.modules:
        raise AssertionError("renderer dependencies were imported before the probe")

    real_import = builtins.__import__
    observed: list[str] = []

    def guarded_import(
        name: str,
        globals: Mapping[str, Any] | None = None,
        locals: Mapping[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "numpy":
            if not offline_guard_active():
                raise AssertionError("NumPy import occurred without OfflineGuard")
            if "numpy" in sys.modules or "PIL" in sys.modules:
                raise AssertionError("renderer dependency was imported prematurely")
            observed.append(name)
            return ModuleType("numpy")
        if name == "PIL":
            if not offline_guard_active():
                raise AssertionError("Pillow import occurred without OfflineGuard")
            if "numpy" in sys.modules or "PIL" in sys.modules:
                raise AssertionError("renderer dependency was imported prematurely")
            observed.append(name)
            raise _GuardedImportProbeComplete
        return real_import(name, globals, locals, fromlist, level)

    with tempfile.TemporaryDirectory(prefix="textsnap-render-probe-") as temporary:
        root = Path(temporary)
        cache = root / "cache"
        (cache / "temp").mkdir(parents=True)
        font_file = root / "font.otf"
        font_file.write_bytes(b"controlled import-order probe")
        guard = OfflineGuard(cache_home=cache.resolve(), font_file=font_file.resolve())
        with guard:
            if not offline_guard_active():
                raise AssertionError("OfflineGuard is inactive during rendering")
            builtins.__import__ = guarded_import
            try:
                _render_corpus(
                    expected="",
                    rendering={},
                    case={},
                    font_file=font_file,
                    output_path=root / "unused.png",
                )
            except _GuardedImportProbeComplete:
                pass
            finally:
                builtins.__import__ = real_import

    if observed != ["numpy", "PIL"]:
        raise AssertionError(f"unexpected renderer import order: {observed!r}")


class CorpusMetricTests(unittest.TestCase):
    def test_character_error_rate_ignores_layout_but_not_body_errors(self) -> None:
        self.assertEqual(_character_error_rate("甲 A\nB", "甲A B"), 0.0)
        self.assertAlmostEqual(_character_error_rate("ABCD", "ABXD"), 0.25)

    def test_anchor_position_uses_output_rows_and_half_width_grid(self) -> None:
        text = "甲 A\n    RIGHT"
        self.assertEqual(_anchor_position(text, "A"), (0, 3))
        self.assertEqual(_anchor_position(text, "RIGHT"), (1, 4))
        with self.assertRaises(ValueError):
            _anchor_position("DUP DUP", "DUP")

    def test_column_segmentation_preserves_declared_grid_position(self) -> None:
        line = "LEFT" + " " * 12 + "RIGHT-COLUMN-A"
        self.assertEqual(
            _line_segments(line),
            ((0, "LEFT"), (16, "RIGHT-COLUMN-A")),
        )

    def test_renderer_dependency_imports_begin_under_offline_guard(self) -> None:
        test_file = Path(__file__).resolve()
        source_root = test_file.parents[2] / "src"
        code = (
            "import runpy,sys;"
            "source,test=sys.argv[1:3];"
            "sys.path.insert(0,source);"
            "sys.argv=[test,'--guarded-render-probe'];"
            "runpy.run_path(test,run_name='__main__')"
        )
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(source_root),
                str(test_file),
            ),
            cwd=test_file.parents[2],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                "guarded renderer import probe failed\n"
                f"stdout:\n{result.stdout or '<empty>'}\n"
                f"stderr:\n{result.stderr or '<empty>'}"
            )


@unittest.skipUnless(
    os.environ.get("TEXTSNAP_RUN_REAL_OCR") == "1",
    "set TEXTSNAP_RUN_REAL_OCR=1 with the pinned native runtime and models",
)
class RealOcrRegressionTests(unittest.TestCase):
    def _require_exact_dependencies(
        self,
        expected_versions: Mapping[str, str],
    ) -> dict[str, str]:
        actual_versions: dict[str, str] = {}
        problems: list[str] = []
        for distribution, expected_version in sorted(expected_versions.items()):
            try:
                actual_version = metadata.version(distribution)
            except metadata.PackageNotFoundError:
                problems.append(f"{distribution}=missing")
                continue
            actual_versions[distribution] = actual_version
            if actual_version != expected_version:
                problems.append(
                    f"{distribution}={actual_version} "
                    f"(expected {expected_version})"
                )
        if problems:
            self.skipTest(
                "real OCR environment does not match the checked-in lock: "
                + "; ".join(problems)
            )
        return actual_versions

    def _require_paths(
        self,
        rendering: Mapping[str, Any],
    ) -> tuple[Path, Path]:
        model_root_value = os.environ.get("TEXTSNAP_TEST_MODEL_ROOT")
        font_value = os.environ.get("TEXTSNAP_TEST_FONT")
        if not model_root_value or not font_value:
            self.skipTest(
                "TEXTSNAP_TEST_MODEL_ROOT and TEXTSNAP_TEST_FONT are required"
            )
        try:
            model_root = Path(model_root_value).resolve(strict=True)
            font_file = Path(font_value).resolve(strict=True)
        except OSError:
            self.skipTest("configured model root or font does not exist")
        if not model_root.is_dir() or not font_file.is_file():
            self.skipTest("configured model root or font has the wrong file type")

        expected_font = rendering["font"]
        if (
            font_file.name != expected_font["filename"]
            or font_file.stat().st_size != int(expected_font["size"])
            or _sha256_file(font_file) != expected_font["sha256"]
        ):
            self.skipTest(
                "TEXTSNAP_TEST_FONT is not the font selected by the checked-in lock"
            )
        return model_root, font_file

    def _assert_fixture_outcome(
        self,
        *,
        case_id: str,
        expected: str,
        actual: str,
        rendering: Mapping[str, Any],
    ) -> float:
        expected_nonempty = [line for line in expected.splitlines() if line]
        actual_nonempty = [line for line in actual.splitlines() if line]
        self.assertEqual(
            len(actual_nonempty),
            len(expected_nonempty),
            f"{case_id}: missing, merged, or extra OCR output line",
        )

        for anchor in rendering["line_anchors"]:
            with self.subTest(case=case_id, line_anchor=anchor):
                self.assertEqual(
                    actual.count(anchor),
                    1,
                    f"{case_id}: line anchor must occur exactly once",
                )

        for anchor in rendering["recognition_anchors"]:
            with self.subTest(case=case_id, recognition_anchor=anchor):
                self.assertEqual(
                    actual.count(anchor),
                    1,
                    f"{case_id}: recognition anchor must occur exactly once",
                )

        for anchor in rendering["geometry_anchors"]:
            with self.subTest(case=case_id, geometry_anchor=anchor):
                expected_row, expected_column = _anchor_position(expected, anchor)
                actual_row, actual_column = _anchor_position(actual, anchor)
                self.assertLessEqual(
                    abs(actual_row - expected_row),
                    1,
                    f"{case_id}: anchor row differs by more than one line",
                )
                self.assertLessEqual(
                    abs(actual_column - expected_column),
                    1,
                    f"{case_id}: anchor column differs by more than one cell",
                )

        cer = _character_error_rate(expected, actual)
        self.assertLessEqual(
            cer,
            CHARACTER_ERROR_RATE_LIMIT,
            f"{case_id}: body CER exceeds {CHARACTER_ERROR_RATE_LIMIT:.0%}",
        )
        return cer

    def test_controlled_corpus_offline_at_normal_and_4k_resolution(self) -> None:
        manifest, expected = _load_fixture()
        rendering = manifest["rendering"]
        actual_versions = self._require_exact_dependencies(
            rendering["dependency_versions"]
        )
        model_root, font_file = self._require_paths(rendering)

        engine_config: dict[str, object] = {}

        before_models = _snapshot(model_root)
        before_font = (
            font_file.stat().st_size,
            font_file.stat().st_mtime_ns,
            _sha256_file(font_file),
        )
        with tempfile.TemporaryDirectory(prefix="textsnap-real-ocr-") as temporary:
            temporary_root = Path(temporary)
            bundle = BundlePaths((temporary_root / "integration-bundle").resolve())
            detection_spec, recognition_spec = bundle.model_specs()
            detection_spec = type(detection_spec)(
                detection_spec.model_name,
                model_root / detection_spec.model_name,
                detection_spec.files_sha256,
            )
            recognition_spec = type(recognition_spec)(
                recognition_spec.model_name,
                model_root / recognition_spec.model_name,
                recognition_spec.files_sha256,
            )
            missing_model_files = [
                f"{spec.model_name}/{filename}"
                for spec in (detection_spec, recognition_spec)
                for filename in spec.files_sha256
                if not (spec.directory / filename).is_file()
            ]
            if missing_model_files:
                self.skipTest(
                    "locked model files are unavailable: "
                    + ", ".join(missing_model_files)
                )

            rendered_cases: list[_RenderedCorpus] = []
            render_records: dict[str, dict[str, object]] = {}
            cache = temporary_root / "pdx-cache"
            for relative in ("temp", "func_ret", "locks"):
                (cache / relative).mkdir(parents=True)
            before_cache = _snapshot(cache)
            outcome_records: dict[str, dict[str, object]] = {}
            small_code_record: dict[str, object] = {}
            self.assertNotIn(
                "numpy",
                sys.modules,
                "real OCR gate must run before NumPy is imported",
            )
            self.assertNotIn(
                "PIL",
                sys.modules,
                "real OCR gate must run before Pillow is imported",
            )
            with OfflineGuard(cache_home=cache, font_file=font_file):
                for case in rendering["cases"]:
                    case_id = str(case["id"])
                    try:
                        first = _render_corpus(
                            expected=expected,
                            rendering=rendering,
                            case=case,
                            font_file=font_file,
                            output_path=temporary_root / f"{case_id}.png",
                        )
                        second = _render_corpus(
                            expected=expected,
                            rendering=rendering,
                            case=case,
                            font_file=font_file,
                            output_path=temporary_root / f"{case_id}.repeat.png",
                        )
                    except (ImportError, OSError) as exc:
                        self.skipTest(
                            "locked local rendering runtime is unavailable: "
                            + type(exc).__name__
                        )
                    self.assertEqual(first.pixel_sha256, second.pixel_sha256)
                    self.assertEqual(first.png_sha256, second.png_sha256)
                    self.assertEqual(
                        first.image_bgr.shape,
                        (int(case["height"]), int(case["width"]), 3),
                    )
                    rendered_cases.append(first)
                    render_records[case_id] = {
                        "width": int(case["width"]),
                        "height": int(case["height"]),
                        "pixel_sha256": first.pixel_sha256,
                        "png_sha256": first.png_sha256,
                    }

                    if case_id == "4k-3840x2160":
                        long_bounds = first.line_bounds["END-LONG-LINE"]
                        internal_edges = {
                            edge
                            for tile in generate_tiles(
                                int(case["width"]),
                                int(case["height"]),
                            )
                            for edge in (
                                tile.x if tile.has_internal_left else None,
                                tile.right if tile.has_internal_right else None,
                            )
                            if edge is not None
                        }
                        crossed_edges = [
                            edge
                            for edge in internal_edges
                            if long_bounds[0] < edge < long_bounds[2]
                        ]
                        self.assertGreaterEqual(
                            len(crossed_edges),
                            2,
                            "4K long line must cross at least two internal tile edges",
                        )

                engine = OcrEngine(
                    detection_spec,
                    recognition_spec,
                    engine_config=engine_config,
                )
                try:
                    initialization_failure = engine.initialize()
                    self.assertIsNone(initialization_failure)
                    small_code_original = _render_small_code_probe(
                        font_file=font_file
                    )
                    assert engine._backend is not None
                    small_code_enhanced = (
                        engine._backend.enhance_recognition_crop(
                            small_code_original
                        )
                    )
                    self.assertGreater(
                        small_code_enhanced.shape[0],
                        small_code_original.shape[0],
                    )
                    original_attempt, enhanced_attempt = (
                        engine._predict_recognition(
                            [small_code_original, small_code_enhanced]
                        )
                    )
                    original_distance = _levenshtein_distance(
                        SMALL_CODE_PROBE,
                        original_attempt[0],
                    )
                    enhanced_distance = _levenshtein_distance(
                        SMALL_CODE_PROBE,
                        enhanced_attempt[0],
                    )
                    if sys.platform == "win32" and platform.machine() == "AMD64":
                        self.assertLess(
                            enhanced_distance,
                            original_distance,
                            "small-code enhancement must improve the locked "
                            "Windows x64 OCR result",
                        )
                    self.assertLessEqual(
                        enhanced_distance / len(SMALL_CODE_PROBE),
                        CHARACTER_ERROR_RATE_LIMIT,
                    )
                    small_code_record = {
                        "characters": len(SMALL_CODE_PROBE),
                        "original_distance": original_distance,
                        "enhanced_distance": enhanced_distance,
                        "original_height": int(small_code_original.shape[0]),
                        "enhanced_height": int(small_code_enhanced.shape[0]),
                    }
                    for rendered in rendered_cases:
                        outcome = engine.recognize(rendered.image_bgr)
                        self.assertIsInstance(outcome, Success)
                        assert isinstance(outcome, Success)
                        actual = outcome.result.text
                        if os.environ.get("TEXTSNAP_DIAGNOSTIC_CONTROLLED_OUTPUT") == "1":
                            print(
                                "TEXTSNAP_CONTROLLED_OCR_OUTPUT="
                                + json.dumps(
                                    {
                                        "case_id": rendered.case_id,
                                        "text": actual,
                                    },
                                    ensure_ascii=True,
                                    sort_keys=True,
                                ),
                                flush=True,
                            )
                        cer = self._assert_fixture_outcome(
                            case_id=rendered.case_id,
                            expected=expected,
                            actual=actual,
                            rendering=rendering,
                        )
                        if rendered.case_id == "4k-3840x2160":
                            for anchor in rendering["seam_anchors"]:
                                self.assertEqual(actual.count(anchor), 1)
                            self.assertTrue(
                                _same_output_line(
                                    actual,
                                    "LONG-LINE |",
                                    "END-LONG-LINE",
                                )
                            )
                            self.assertTrue(
                                _same_output_line(
                                    actual,
                                    "4K-SMALL-TEXT",
                                    "END-4K",
                                )
                            )
                        outcome_records[rendered.case_id] = {
                            "cer": cer,
                            "line_count": len(actual.splitlines()),
                            "layout_line_count": outcome.result.stats.line_count,
                        }
                finally:
                    self.assertIsNone(engine.close())
            self.assertEqual(_snapshot(cache), before_cache)

            record = {
                "python": platform.python_version(),
                "platform": sys.platform,
                "machine": platform.machine(),
                "dependencies": actual_versions,
                "font_sha256": _sha256_file(font_file),
                "engine_config": dict(engine.engine_config),
                "renders": render_records,
                "outcomes": outcome_records,
                "small_code_probe": small_code_record,
            }
            print(
                "TEXTSNAP_REAL_OCR_RECORD="
                + json.dumps(record, ensure_ascii=True, sort_keys=True),
                flush=True,
            )

        self.assertEqual(_snapshot(model_root), before_models)
        self.assertEqual(
            (
                font_file.stat().st_size,
                font_file.stat().st_mtime_ns,
                _sha256_file(font_file),
            ),
            before_font,
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--guarded-render-probe"]:
        _run_guarded_render_probe()
    else:
        unittest.main()
