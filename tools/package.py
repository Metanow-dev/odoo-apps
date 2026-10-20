#!/usr/bin/env python3
"""Build the module's zip: byte-identical for every customer and every rebuild.

DEV-1766 / ARCHITECTURE 41: one artifact for everyone, with no key, no credential and no
customer data in it. The zip holds metanow_fiscalization/ at its top level, which is what both
an addons path and Odoo's module upload expect. Entries are sorted and stamped with one fixed
time, so the same source always gives the same bytes, and the SHA-256 written beside it is the
checksum a customer can compare with the one the efis web app shows.

Usage: package.py [--out DIR] [--manifest]   (prints the zip path and its SHA-256)

With --manifest it also writes manifest.json beside the zip: the file name, the module's
version, the size and the SHA-256, for the efis web app's Plugins page to read. The image build
runs it this way (Dockerfile, stage odoo-package); the web app never states a checksum it did
not read from this file.
"""

import argparse
import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ODOO_DIR = Path(__file__).resolve().parents[1]
MODULE = "metanow_fiscalization"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

# Nothing a customer must not receive, and nothing that is not the module.
EXCLUDED_DIRS = {"__pycache__", "tests"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".orig", ".rej"}
EXCLUDED_NAMES = {".DS_Store", ".env"}
MANIFEST_NAME = "manifest.json"


def manifest_version(module_dir):
    manifest = ast.literal_eval((module_dir / "__manifest__.py").read_text(encoding="utf-8"))
    return manifest["version"]


def files(module_dir):
    for path in sorted(module_dir.rglob("*")):
        relative = path.relative_to(module_dir)
        if path.is_dir() or EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name in EXCLUDED_NAMES:
            continue
        yield path, relative


def build(out_dir, module_dir=None, manifest=False):
    module_dir = Path(module_dir or ODOO_DIR / MODULE)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    version = manifest_version(module_dir)
    target = out_dir / f"{MODULE}-{version}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files(module_dir):
            info = zipfile.ZipInfo(f"{MODULE}/{relative.as_posix()}", date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out_dir / f"{target.name}.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    if manifest:
        write_manifest(out_dir, target, version, digest)
    return target, digest


def write_manifest(out_dir, target, version, digest):
    """The facts about the zip, as the web app reads them. Same zip, same bytes."""
    facts = {
        "module": MODULE,
        "fileName": target.name,
        "version": version,
        "size": target.stat().st_size,
        "sha256": digest,
    }
    path = Path(out_dir) / MANIFEST_NAME
    path.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ODOO_DIR.parent / "dist" / "odoo"))
    parser.add_argument("--manifest", action="store_true",
                        help=f"also write {MANIFEST_NAME} (file name, version, size, SHA-256) beside the zip")
    args = parser.parse_args(argv)
    target, digest = build(args.out, manifest=args.manifest)
    print(f"{target}\n{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
