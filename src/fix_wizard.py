#!/usr/bin/env python3
"""Extract one incremental check-sat query and write one stable Caza repair.

The check index is one-based and uses the same retained-check numbering as
``mariposa -a split``.  For a multi-query input, index 1 corresponds to the
``<name>.1.smt2`` output of that action.
"""


import argparse
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from base.defs import MARIPOSA
from caza_wizard import main as caza_main
from debugger.strainer import DebugStatus
from caza_wizard import is_early_stable

REPO_ROOT = Path(__file__).resolve().parent.parent
STABLE_CANDIDATE = re.compile(r"^\s+[0-9a-f]+: .+ -> (.+\.smt2)$")


class Tee:
    """Write Caza's Python output to the terminal and an in-memory buffer."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


@contextmanager
def silence_child_output():
    """Suppress output inherited by commands started inside Caza."""
    with open(os.devnull, "w") as null:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        try:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
            yield
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(title="subcommands", dest="subcommand", required=True)

    test_parser = subparsers.add_parser("test", help="run Mariposa to check for stability")
    test_parser.add_argument("input_path", type=Path, help="SMT2 input with check-sat commands")
    test_parser.add_argument(
        "check_index",
        type=int,
        nargs="?",
        help="one-based ordinal of the retained check-sat command",
        default=1,
    )
    test_parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show Caza output; omit for quiet mode",
    )

    fix_parser = subparsers.add_parser("fix", help="run Caza to produce a stable repair")
    fix_parser.add_argument("input_path", type=Path, help="SMT2 input with check-sat commands")
    fix_parser.add_argument("output_path", type=Path, help="path for the stable repaired query")
    fix_parser.add_argument(
        "check_index",
        type=int,
        nargs="?",
        help="one-based ordinal of the retained check-sat command",
        default=1,
    )
    fix_parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show Caza output; omit for quiet mode",
    )

    args = parser.parse_args()
    if args.check_index < 1:
        parser.error("check_index must be at least 1")
    return args


def split_query(input_path, check_index, workspace):
    """Return the split file named by ``check_index`` or raise ValueError."""

    split_target = workspace / input_path.name
    split_result = subprocess.run(
        [
            MARIPOSA,
            "-i",
            str(input_path),
            "-o",
            str(split_target),
            "-a",
            "split",
            "--convert-comments",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    # A single retained check keeps the requested output name; otherwise the
    # splitter writes <stem>.1.smt2, <stem>.2.smt2, and so on.
    direct_output = split_target if check_index == 1 else None
    indexed_output = split_target.with_name(
        f"{split_target.stem}.{check_index}{split_target.suffix}"
    )
    if direct_output is not None and direct_output.is_file():
        return direct_output
    if not indexed_output.is_file():
        raise ValueError(
            f"retained check-sat #{check_index} was not produced; splitter output: "
            f"{split_result.stdout.strip() or '<none>'}"
        )
    return indexed_output



def find_stable_candidate(caza_stdout):
    """Extract the first stable candidate path printed by caza_wizard."""
    for line in caza_stdout.splitlines():
        match = STABLE_CANDIDATE.match(line)
        if match is not None:
            candidate = Path(match.group(1))
            return candidate if candidate.is_absolute() else REPO_ROOT / candidate
    return None


def fix(args):
    input_path = args.input_path.resolve()
    output_path = args.output_path.resolve()
    if not input_path.is_file():
        raise ValueError(f"input SMT2 file does not exist: {input_path}")

    with tempfile.TemporaryDirectory(prefix="fix_wizard_") as temp_dir:
        workspace = Path(temp_dir)
        query_path = split_query(input_path, args.check_index, workspace)
        if args.verbose:
            print(f"Running Caza for retained check-sat #{args.check_index}: {query_path}")
        caza_stdout = StringIO()
        previous_argv = sys.argv
        sys.argv = [
            "caza_wizard.py",
            "--early",
            "--sort",
            "time",
            str(query_path),
        ]
        try:
            output_stream = Tee(sys.stdout, caza_stdout) if args.verbose else caza_stdout
            error_stream = sys.stderr if args.verbose else StringIO()
            child_output = nullcontext() if args.verbose else silence_child_output()
            with child_output, redirect_stdout(output_stream), redirect_stderr(error_stream):
                status = caza_main()
        finally:
            sys.argv = previous_argv

        if status == DebugStatus.NO_TRACE:
            print("No failure trace found. Probably the query is already stable.")
            return
        if status == DebugStatus.NO_PROOF:
            print("No proof object found.")
            return
        if status == DebugStatus.FIX_NOT_FOUND:
            print("No fixes were found :(")
            return
        if status != DebugStatus.FIX_FOUND:
            print("Unexpected Caza status:", status)
            return

        candidate = find_stable_candidate(caza_stdout.getvalue())
        if candidate is None or not candidate.is_file():
            print("Unexpected Caza output retreival failure")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, output_path)
        print(f"Wrote stable repair to {output_path}")

def test(args):
    input_path = args.input_path.resolve()
    if not input_path.is_file():
        raise ValueError(f"input SMT2 file does not exist: {input_path}")

    with tempfile.TemporaryDirectory(prefix="test_wizard_") as temp_dir:
        workspace = Path(temp_dir)
        query_path = split_query(input_path, args.check_index, workspace)
        caza_stdout = StringIO()
        previous_argv = sys.argv
        sys.argv = [
            "caza_wizard.py",
            "--early",
            "--sort",
            "time",
            str(query_path),
        ]
        try:
            output_stream = Tee(sys.stdout, caza_stdout) if args.verbose else caza_stdout
            error_stream = sys.stderr if args.verbose else StringIO()
            child_output = nullcontext() if args.verbose else silence_child_output()
            with child_output, redirect_stdout(output_stream), redirect_stderr(error_stream):
                result = is_early_stable(str(query_path), 128736132)
        finally:
            sys.argv = previous_argv
        print("Stable." if result else "Unstable.")


def main():
    args = parse_args()
    if args.subcommand == "fix":
        fix(args)
    elif args.subcommand == "test":
        test(args)
    else:
        raise ValueError(f"unexpected subcommand: {args.subcommand}")

if __name__ == "__main__":
    main()
