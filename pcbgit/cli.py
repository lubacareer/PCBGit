"""Command-line entry point."""
import argparse
from pathlib import Path
import sys

from .review import ReviewError, review


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pcbgit", description="Review hardware changes in Git")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("review", help="Compare a merge base to a commit and run checks")
    cmd.add_argument("--repo", type=Path, default=Path.cwd())
    cmd.add_argument("--base", required=True, help="Base Git revision, or :empty: for an initial commit")
    cmd.add_argument("--head", default="HEAD", help="Head Git revision")
    cmd.add_argument("--output", type=Path, default=Path("pcbgit-report"))
    cmd.add_argument("--profile", help="Repository-relative JSON DFM profile at the head revision")
    cmd.add_argument("--bom", help="Repository-relative KiCad-exported BOM CSV at the head revision")
    cmd.add_argument("--build-quantity", type=int, default=1, help="Number of boards to build")
    cmd.add_argument("--skip-inventory", action="store_true", help="Skip stock gate for an untrusted PR")
    args = parser.parse_args(argv)
    try:
        result = review(args.repo, args.base, args.head, args.output, profile=args.profile,
                        bom=args.bom, build_quantity=args.build_quantity, skip_inventory=args.skip_inventory)
    except (ReviewError, OSError, ValueError) as exc:
        print(f"PCBGit error: {exc}", file=sys.stderr)
        return 2
    print(f"PCBGit: {result['status']} — {args.output / 'index.html'}")
    return {"pass": 0, "fail": 1, "unknown": 2}[result["status"]]
