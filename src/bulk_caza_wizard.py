#!/usr/bin/env python3
"""Run CazaMariposas over every SMT2 query in a directory.

Each Caza invocation runs sequentially.  Its stdout and stderr, including the
output of the experiment and analysis commands it starts, are streamed to one
log file with query-specific start and end markers.
"""

import argparse
from datetime import datetime
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from utils.option_utils import add_set_seed_option


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAZA = REPO_ROOT / "src" / "caza_wizard.py"
DEFAULT_TIMING_STATS_CSV = Path("log/caza.csv")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i", "--input-dir", type=Path, default=Path("data/verita_bad"),
        help="directory recursively containing .smt2 queries (default: data/verita_bad)",
    )
    parser.add_argument(
        "-o", "--output-log", type=Path, default=Path("log/caza.log"),
        help="combined Caza stdout/stderr log (default: log/caza.log)",
    )
    parser.add_argument(
        "--timing-stats-csv",
        type=Path,
        default=DEFAULT_TIMING_STATS_CSV,
        help="per-query Caza timing CSV (default: log/caza.csv)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--append", action="store_true", help="append to an existing log")
    mode.add_argument("--overwrite", action="store_true", help="replace an existing log")
    parser.add_argument(
        "--caza", type=Path, default=DEFAULT_CAZA,
        help="Caza wizard script to invoke (default: src/caza_wizard.py)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="run only the first N sorted queries; useful for a smoke test",
    )
    add_set_seed_option(parser)
    parser.add_argument(
        "--early", action="store_true",
        help="forward --early to Caza, enabling its final early-stop stability pass",
    )
    parser.add_argument(
        "--fast-proof",
        action="store_true",
        help="forward --fast-proof to Caza and retain the debugger artifact cache",
    )
    parser.add_argument(
        "--sort",
        nargs="?",
        choices=("rank", "time", "div"),
        const="rank",
        default=None,
        help="with --early, forward rank, verify-time, or verify-time/rank-prior ordering to Caza",
    )
    return parser.parse_args()


def log_marker(output, text):
    output.write(f"\n{'=' * 80}\n{text}\n{'=' * 80}\n")
    output.flush()


def select_log_mode(output_log, args):
    if args.append:
        return "a"
    if args.overwrite:
        return "w"
    if output_log.exists():
        raise FileExistsError(
            f"log already exists: {output_log}; use --append or --overwrite"
        )
    return "x"


def clear_debug_root():
    """Discard cached debugger artifacts before starting a Caza batch."""
    debug_root = REPO_ROOT / "dbg"
    if debug_root.exists():
        shutil.rmtree(debug_root)
    debug_root.mkdir()
    return debug_root


def main():
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_log = args.output_log.resolve()
    timing_stats_csv = args.timing_stats_csv.resolve()
    caza = args.caza.resolve()

    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")
    if not caza.is_file():
        raise ValueError(f"Caza wizard does not exist: {caza}")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be non-negative")
    if args.sort and not args.early:
        raise ValueError("--sort requires --early")

    queries = sorted(input_dir.rglob("*.smt2"))
    if args.limit is not None:
        queries = queries[:args.limit]
    if not queries:
        print(f"no SMT2 files found under {input_dir}")
        return

    output_log.parent.mkdir(parents=True, exist_ok=True)
    mode = select_log_mode(output_log, args)
    if args.fast_proof:
        debug_root = REPO_ROOT / "dbg"
        debug_root.mkdir(exist_ok=True)
        debug_cache_action = "retained"
    else:
        debug_root = clear_debug_root()
        debug_cache_action = "cleared"
    failures = []
    with output_log.open(mode, encoding="utf-8") as output:
        log_marker(output, f"Caza batch started {datetime.now().isoformat(timespec='seconds')}")
        output.write(f"input directory: {input_dir}\nqueries: {len(queries)}\n")
        output.write(f"{debug_cache_action} debugger cache: {debug_root}\n")
        output.write(f"early stopping: {args.early}\n")
        output.write(f"fast proof: {args.fast_proof}\n")
        output.write(f"ranked ordering: {args.sort or 'none'}\n")
        output.write(f"set seed: {args.set_seed}\n")
        output.write(f"timing CSV: {timing_stats_csv}\n")
        output.flush()

        for index, query in enumerate(queries, start=1):
            # Caza's stdout and stderr are combined into one file below.
            # ``-u`` prevents stdout from being written after a later stderr
            # message merely because it was buffered.
            command = [sys.executable, "-u", str(caza)]
            if args.set_seed is not None:
                command.extend(["--set-seed", args.set_seed])
            if args.early:
                command.append("--early")
            if args.fast_proof:
                command.append("--fast-proof")
            if args.sort:
                command.extend(["--sort", args.sort])
            command.extend(["--csv-index", str(index)])
            command.extend(["--timing-stats-csv", str(timing_stats_csv)])
            command.append(str(query))
            command_text = shlex.join(command)
            log_marker(
                output,
                f"[{index}/{len(queries)}] Caza query started "
                f"{datetime.now().isoformat(timespec='seconds')}\n"
                f"query: {query}\ncommand: {command_text}",
            )
            print(f"[{index}/{len(queries)}] running {query}", flush=True)
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log_marker(
                output,
                f"[{index}/{len(queries)}] Caza query finished "
                f"{datetime.now().isoformat(timespec='seconds')}\n"
                f"exit status: {result.returncode}",
            )
            if result.returncode:
                failures.append((index, query, result.returncode, command_text))
                print(f"[{index}/{len(queries)}] failed ({result.returncode})", flush=True)

        failure_details = ""
        if failures:
            failure_details = "\nfailures:\n" + "\n".join(
                f"  [{index}/{len(queries)}] exit status {returncode}\n"
                f"    query: {query}\n"
                f"    command: {command}"
                for index, query, returncode, command in failures
            )
        log_marker(
            output,
            f"Caza batch finished {datetime.now().isoformat(timespec='seconds')}\n"
            f"completed: {len(queries)}\nfailed: {len(failures)}{failure_details}",
        )

    print(f"combined Caza output written to {output_log}")
    if failures:
        print(f"{len(failures)} query runs failed; details are in the log", file=sys.stderr)


if __name__ == "__main__":
    main()
