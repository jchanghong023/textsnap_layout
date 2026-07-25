# ruff: noqa: E402
import sys

sys.dont_write_bytecode = True

from textsnap.main import main as run_main
from textsnap.paths import BundlePaths


def main() -> int:
    paths = BundlePaths.from_entry_script(__file__)
    return run_main(paths, tuple(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
