"""Portable bundle path resolution without consulting cwd or environment."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .ocr import (
    DETECTION_MODEL_NAME,
    RECOGNITION_MODEL_NAME,
    LocalModelSpec,
)

DETECTION_MODEL_HASHES = {
    "inference.onnx": "3914f972d833af87d23bb2338bd09238f978a48f3c4dbb8e1a4ee26a93869940",
    "inference.yml": "193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516",
}
RECOGNITION_MODEL_HASHES = {
    "inference.onnx": "3e3def686ac9a1676b59bc9749ad896263d8f68b53f352060774de359a2e23ed",
    "inference.yml": "ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1",
}


@dataclass(frozen=True, slots=True)
class BundlePaths:
    root: Path = field(repr=False)

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not root.is_absolute():
            raise ValueError("bundle root must be absolute")
        object.__setattr__(self, "root", root)

    @classmethod
    def from_entry_script(cls, entry_script: Path | str) -> BundlePaths:
        """Resolve root from ``<root>/app/main.py`` without using cwd."""

        entry = Path(entry_script).resolve(strict=False)
        if entry.name != "main.py" or entry.parent.name != "app":
            raise ValueError("entry script must be <bundle>/app/main.py")
        return cls(entry.parent.parent)

    @property
    def executable(self) -> Path:
        return self.root / "TextSnapLayout.exe"

    @property
    def data_directory(self) -> Path:
        return self.root / "data"

    @property
    def settings_file(self) -> Path:
        return self.data_directory / "settings.json"

    @property
    def font_file(self) -> Path:
        return self.root / "assets" / "fonts" / "NotoSansMonoCJKsc-Regular.otf"

    @property
    def paddlex_cache(self) -> Path:
        return self.root / "runtime" / "pdx-cache"

    @property
    def detection_model_directory(self) -> Path:
        return self.root / "models" / DETECTION_MODEL_NAME

    @property
    def recognition_model_directory(self) -> Path:
        return self.root / "models" / RECOGNITION_MODEL_NAME

    def model_specs(self) -> tuple[LocalModelSpec, LocalModelSpec]:
        return (
            LocalModelSpec(
                DETECTION_MODEL_NAME,
                self.detection_model_directory,
                DETECTION_MODEL_HASHES,
            ),
            LocalModelSpec(
                RECOGNITION_MODEL_NAME,
                self.recognition_model_directory,
                RECOGNITION_MODEL_HASHES,
            ),
        )
