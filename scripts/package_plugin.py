"""Build the ZIP accepted by AstrBot's local plugin upload."""

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    """Package only distributable plugin files into a versioned ZIP.

    Raises:
        ValueError: If metadata or the requested release version is invalid.
        OSError: If required files cannot be read or the ZIP cannot be written.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Require metadata to match this release tag")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(
        r"^version:\s*(v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*$",
        metadata,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("metadata.yaml must contain an unquoted version like v0.1.0")
    version = match[1]
    if args.version is not None and args.version != version:
        raise ValueError("Release tag must match the version in metadata.yaml")
    plugin_name = "astrbot_plugin_acm_assistant"
    files = (
        "main.py",
        "training.py",
        "metadata.yaml",
        "_conf_schema.json",
        "requirements.txt",
        "README.md",
    )
    # Use an explicit list so local credentials, caches, and repository files
    # cannot accidentally enter a plugin release.
    for filename in files:
        if not (root / filename).is_file():
            raise FileNotFoundError(f"Missing required plugin file: {filename}")
    output = root / "dist" / f"{plugin_name}-{version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename in files:
            archive.write(root / filename, arcname=f"{plugin_name}/{filename}")
    print(output)


if __name__ == "__main__":
    main()
