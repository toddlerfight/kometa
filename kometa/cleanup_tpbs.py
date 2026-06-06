#!/usr/bin/env python3
"""
Move TPB files (matching '- Vol. N' pattern) out of series folders into
a '[Series] TPB' sibling folder. Run --dry-run first, always.
"""
import argparse
import os
import re
import shutil
import sys

VOL_PATTERN = re.compile(r" - Vol\. \d+")


def find_tpbs(root: str) -> list[tuple[str, str]]:
    """Return list of (src_path, dest_path) for every TPB file found."""
    moves = []
    for dirpath, _dirs, files in os.walk(root):
        for fname in sorted(files):
            if not VOL_PATTERN.search(fname):
                continue
            src = os.path.join(dirpath, fname)
            series_name = os.path.basename(dirpath)
            tpb_dir = os.path.join(os.path.dirname(dirpath), f"{series_name} TPB")
            dest = os.path.join(tpb_dir, fname)
            moves.append((src, dest))
    return moves


def main():
    parser = argparse.ArgumentParser(description="Move TPB files out of series folders")
    parser.add_argument("--root", default="test-fixtures/working",
                        help="Root path to walk (default: test-fixtures/working)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print moves without executing them")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print(f"ERROR: root not found: {args.root}", file=sys.stderr)
        sys.exit(1)

    moves = find_tpbs(args.root)

    if not moves:
        print("No TPB files found.")
        return

    if args.dry_run:
        print(f"DRY RUN — {len(moves)} file(s) would move:\n")
        for src, dest in moves:
            rel_src = os.path.relpath(src, args.root)
            rel_dest = os.path.relpath(dest, args.root)
            print(f"  {rel_src}")
            print(f"    → {rel_dest}")
        print(f"\nTotal: {len(moves)} file(s)")
        return

    moved = 0
    errors = 0
    for src, dest in moves:
        tpb_dir = os.path.dirname(dest)
        try:
            os.makedirs(tpb_dir, exist_ok=True)
            shutil.move(src, dest)
            print(f"MOVED  {os.path.relpath(src, args.root)}")
            print(f"    → {os.path.relpath(dest, args.root)}")
            moved += 1
        except Exception as e:
            print(f"ERROR  {src}: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone: {moved} moved, {errors} errors")


if __name__ == "__main__":
    main()
