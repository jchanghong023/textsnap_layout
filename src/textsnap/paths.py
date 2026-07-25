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
    "inference.json": "0f1a7ec35da36173529c7a60238b7f7919e3831929c3f700ad90ad4896adecd5",
    "inference.pdiparams": "85218d2e3d98f5a21c58b4220627be923a97aee5db3cc71f39536ab31ac53960",
    "inference.yml": "7298d5ead546584af2504d03355f881ac7a7bc0eb1e282d3e159277c1d0af871",
}
RECOGNITION_MODEL_HASHES = {
    "inference.json": "0b2e25e990bd072f1bf77d59d67d508bce6c4bd44af6624e0fb27d6da2cd00e8",
    "inference.pdiparams": "1b01c79a914587933f615569e75de54f2e638ebb5d3f3b3c1b38c24ede8c7319",
    "inference.yml": "991b700facf5b50a7de193468207d5f4255b538dde0d312ae3b7c7a9b6873129",
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
