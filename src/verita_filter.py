#!/usr/bin/env python3
"""Classify all queries in a Verita project with one early-stop experiment.

Every SMT2 file under ``data/verita_all`` is checked with the fixed seed
``0x12345678``.  Queries that are not stable are copied to
``data/verita_bad``.  A batch summary, including total wall-clock time, is
appended to ``log/verita_filter.txt``.
"""

import argparse
import csv
from datetime import datetime
import filecmp
import os
from pathlib import Path
import shutil
import time

from analysis.early_stop_runner import EarlyStopRunner
from base.exper import ExpConfig
from base.factory import FACT
from base.query_analyzer import Stability


DEFAULT_SEED = 0x12345678
FILTER_EXPERIMENT = "verita_early"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i", "--input-dir", type=Path, default=Path("data/projs/verita_all/base.z3"),
        help="Verita base.z3 project directory",
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path("data/verita_bad"),
        help="directory that receives non-stable SMT2 files",
    )
    parser.add_argument(
        "--log", type=Path, default=Path("log/verita_filter.txt"),
        help="append the batch summary to this file",
    )
    return parser.parse_args()


def copy_nonstable(source: Path, output_dir: Path):
    """Copy a flat Verita input safely, refusing conflicting destination data."""
    destination = output_dir / source.name
    if destination.exists():
        if not filecmp.cmp(source, destination, shallow=False):
            raise FileExistsError(
                f"destination already exists with different contents: {destination}"
            )
        return False
    shutil.copy2(source, destination)
    return True


def read_early_stop_decisions(csv_path: Path):
    """Load the per-query classifications emitted by ``EarlyStopRunner``."""
    with csv_path.open(newline="", encoding="utf-8") as source:
        return {
            row["query"]: Stability(row["stability"])
            for row in csv.DictReader(source)
        }


def main():
    args = parse_args()
    input_dir = args.input_dir
    output_dir = args.output_dir.resolve()
    log_path = args.log.resolve()

    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")

    project = FACT.get_project_by_path(os.path.relpath(input_dir))
    # Keep this batch separate from a normal ``default`` experiment on the
    # project.  Its database and early-stop CSV are therefore reproducible and
    # may be safely replaced on the next filter run.
    config = ExpConfig(FILTER_EXPERIMENT, FACT.get_config("default").as_dict())
    experiment = FACT.get_exper(
        project, config, FACT.get_solver("z3_4_16_0"), build=True
    )
    experiment.set_seed(DEFAULT_SEED)
    queries = sorted(Path(query) for query in experiment.list_queries())
    if not queries:
        raise ValueError(f"no SMT2 files found under {project.sub_root}")

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()
    started = time.perf_counter()
    stable_count = 0
    unstable_count = 0
    copied_count = 0

    print(f"checking {len(queries)} Verita queries with seed {DEFAULT_SEED:#x}")
    runner = EarlyStopRunner(
        experiment, FACT.get_analyzer("60sec"), stable_only=True
    )
    runner.run_experiment(True, category=Stability.STABLE)
    csv_path = Path(experiment.db_path).with_suffix(
        f".{runner.analyzer.name}.early_stop.csv"
    )
    decisions = read_early_stop_decisions(csv_path)
    missing = {str(query) for query in queries} - decisions.keys()
    if missing:
        raise RuntimeError(f"early-stop CSV omitted {len(missing)} project queries")

    for query in queries:
        if decisions.get(str(query)) == Stability.STABLE:
            stable_count += 1
            continue
        unstable_count += 1
        copied_count += copy_nonstable(query, output_dir)

    elapsed = time.perf_counter() - started
    finished_at = datetime.now()
    summary = (
        f"Verita early filter started {started_at.isoformat(timespec='seconds')}\n"
        f"Verita early filter finished {finished_at.isoformat(timespec='seconds')}\n"
        f"input directory: {input_dir}\n"
        f"seed: {DEFAULT_SEED:#x}\n"
        f"queries: {len(queries)}\n"
        f"stable: {stable_count}\n"
        f"unstable: {unstable_count}\n"
        f"copied: {copied_count}\n"
        f"early_stop_csv: {csv_path}\n"
        f"total_seconds: {elapsed:.3f}\n"
    )
    with log_path.open("a", encoding="utf-8") as output:
        output.write("\n" + "=" * 80 + "\n")
        output.write(summary)
    print(summary, end="")


if __name__ == "__main__":
    main()
