#!/usr/bin/env python3
"""Build the module's zip: byte-identical for every customer and every rebuild.

DEV-1766 / ARCHITECTURE 41: one artifact for everyone, with no key, no credential and no
customer data in it. The zip holds metanow_fiscalization/ at its top level, which is what both
an addons path and Odoo's module upload expect. Entries are sorted and stamped with one fixed
time, so the same source always gives the same bytes, and the SHA-256 written beside it is the
checksum a customer can compare with the one the efis web app shows.

Usage: package.py [--out DIR]   (prints the zip path and its SHA-256)
"""

import argparse
import ast
import hashlib
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


def build(out_dir, module_dir=None):
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
    return target, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ODOO_DIR.parent / "dist" / "odoo"))
    args = parser.parse_args(argv)
    target, digest = build(args.out)
    print(f"{target}\n{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
