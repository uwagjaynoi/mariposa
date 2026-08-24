#!/usr/bin/env python3
"""Collect all Verita SMT2 files into one flat, project-prefixed directory."""

import argparse
import filecmp
import shutil
from pathlib import Path
from typing import Tuple


def destination_name(source_root: Path, source_file: Path) -> str:
    """Prefix the file with its project and retain nested-path uniqueness."""
    relative = source_file.relative_to(source_root)
    if len(relative.parts) < 2:
        raise ValueError(f"SMT2 file is not inside a Verita project: {source_file}")
    return "__".join(relative.parts)


def collect(source_root: Path, output_dir: Path, dry_run: bool) -> Tuple[int, int]:
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source directory does not exist: {source_root}")
    try:
        output_dir.relative_to(source_root)
        inside_source = True
    except ValueError:
        inside_source = False
    if inside_source:
        raise ValueError("output directory must not be inside the source directory")

    sources = sorted(source_root.rglob("*.smt2"))
    destinations = {}
    for source in sources:
        destination = output_dir / destination_name(source_root, source)
        if destination in destinations:
            raise ValueError(f"filename collision: {source} and {destinations[destination]}")
        destinations[destination] = source

    copied = skipped = 0
    for destination, source in destinations.items():
        if destination.exists():
            if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
                raise FileExistsError(
                    f"refusing to overwrite a different file: {destination}"
                )
            skipped += 1
            continue
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        copied += 1
    return copied, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/verita"))
    parser.add_argument("--output", type=Path, default=Path("data/verita_all"))
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied")
    args = parser.parse_args()

    copied, skipped = collect(args.source, args.output, args.dry_run)
    action = "would copy" if args.dry_run else "copied"
    print(f"{action} {copied} SMT2 files to {args.output}")
    if skipped:
        print(f"skipped {skipped} identical existing files")


if __name__ == "__main__":
    main()
