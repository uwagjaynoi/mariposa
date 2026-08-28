#USAGE (from root): python3 ./src/caza_wizard.py <path to unstable query (.smt2)>

import argparse
import csv
import logging
import os
import subprocess
import shutil
from datetime import datetime
import time

from debugger.factory import get_debugger
from debugger.options import DebugOptions
from debugger.strainer import DebugStatus
from analysis.early_stop_runner import EarlyStopRunner
from analysis.singleton_analyzer import SingletonAnalyzer
from base.factory import FACT
from base.exper_runner import Runner
from base.query_analyzer import Stability
from utils.option_utils import add_set_seed_option
from utils.system_utils import list_smt2_files


TIMING_STATS_CSV = "log/caza.csv"
RANK_STATS_SLOTS = 10
RANK_PRIORITIES = (8, 6, 5, 4, 4, 4, 4, 3, 3, 3)
TRACE_PROOF = "build trace and proof"
FIND_FIXES = "finding fixes"
VERIFY_FIXES = "verify and filter candidates"
CHECK_STABILITY = "check stability of candidates"
TIMING_FIELDS = [
    "index",
    "query",
    "start_time",
    "end_time",
    "start_timestamp",
    "end_timestamp",
    TRACE_PROOF,
    FIND_FIXES,
    VERIFY_FIXES,
    CHECK_STABILITY,
    "total_seconds",
    "result"
]

def clean_up(proj_name):
    for path in [
        f"data/projs/{proj_name}",
        f"data/projs/{proj_name}.filtered",
        f"data/dbs/{proj_name}",
        f"data/dbs/{proj_name}.filtered",
        f"gen/{proj_name}",
        f"gen/{proj_name}.filtered",
    ]:
        if os.path.exists(path):
            shutil.rmtree(path)


def rank_priority(rank):
    """Return the supplied Caza rank prior for a one-based candidate rank."""
    if 1 <= rank <= len(RANK_PRIORITIES):
        return RANK_PRIORITIES[rank - 1]
    return 1


def verification_sort_key(sort_mode, elapsed, rank, edit_id):
    """Order by raw verification time or time divided by the rank prior."""
    if sort_mode == "time":
        return (elapsed, rank, edit_id)
    elif sort_mode == "div":
        return (elapsed / rank_priority(rank), rank, edit_id)
    elif sort_mode == "rank":
        return (-rank, elapsed, edit_id)
    return edit_id

def verify_and_filter_ranked_candidates(
    project_dir, ranked_ids, set_seed, early, experiment_seed_arg
):
    """Run verify and filter while retaining each candidate's vanilla time.

    This is the in-process equivalent of Caza's ``exper_wizard multiple -e
    verify`` and ``analysis_wizard filter`` calls.  It follows the filter's
    360-second budget.
    """
    filtered_dir = project_dir.replace("/base.z3", ".filtered/base.z3")
    if not early:
        # Check if proof is broken to fail fast.
        os.system(f"./src/exper_wizard.py multiple -e verify -i {project_dir} --clear"
            f"{experiment_seed_arg}")
        print("Checked for broken queries")

        # Pick out the candidates that are not broken.
        os.system(f"./src/analysis_wizard.py filter -i {project_dir}")
        print("Filtered out broken queries")
        os.system(f"./src/exper_wizard.py multiple -e filter -i {filtered_dir} --clear"
            f"{experiment_seed_arg}")
        # verified results among 10 + filter results
        print("Ran mariposa on filtered queries")


        os.system(
            f"./src/analysis_wizard.py carve -e filter -i {filtered_dir}"
        )
        # verified but unstable results among 10 fixes
        print("Carved out unstable queries")
        return [], []

    project = FACT.get_project_by_path(project_dir)
    experiment = FACT.get_exper(
        project,
        FACT.get_config("verify"),
        FACT.get_solver("z3_4_16_0"),
        build=True,
    )
    if set_seed is not None:
        experiment.set_seed(set_seed)

    Runner(experiment).run_experiment(True)
    verified = SingletonAnalyzer(experiment, FACT.get_analyzer("60sec"))
    verification_times = {
        edit_id: elapsed
        for edit_id, (_, elapsed) in verified.edit_results.items()
    }
    rank_by_id = {edit_id: rank for rank, edit_id in enumerate(ranked_ids, start=1)}

    os.makedirs(filtered_dir, exist_ok=True)
    selected = []
    for edit_id in verified.passed_edits:
        _, elapsed = verified.get_query_result(edit_id)
        source = project.get_path(edit_id)
        destination = os.path.join(filtered_dir, f"{edit_id}.smt2")
        shutil.copy(source, destination)
        selected.append(edit_id)
        print(
            f"verify candidate {edit_id}: {elapsed / 1000:.3f}s;"
        )
    print(
        f"selected {len(selected)} verified candidates to {filtered_dir}"
    )
    return verification_times, rank_by_id

def timed_system(label, command, timings):
    """Run one external workflow command and retain its wall-clock time."""
    start = time.perf_counter()
    try:
        return os.system(command)
    finally:
        timings.append((label, time.perf_counter() - start, True))


def timed_run(label, command, timings, **kwargs):
    """Run one external workflow command and retain its wall-clock time."""
    start = time.perf_counter()
    try:
        return subprocess.run(command, **kwargs)
    finally:
        timings.append((label, time.perf_counter() - start, True))


def timed_call(label, callback, timings):
    """Run an in-process workflow callback and retain its wall-clock time."""
    start = time.perf_counter()
    try:
        return callback()
    finally:
        timings.append((label, time.perf_counter() - start, True))


def is_early_stable(candidate_path, set_seed):
    """Check one candidate with early stopping and return whether it is stable."""
    experiment = FACT.get_single_exper(
        candidate_path,
        FACT.get_config("default"),
        FACT.get_solver("z3_4_16_0"),
        skip_split=True,
        clear=True,
    )
    if set_seed is not None:
        experiment.set_seed(set_seed)

    decisions = EarlyStopRunner(
        experiment,
        FACT.get_analyzer("60sec"),
        stable_only=True,
    ).run_experiment(True, category=Stability.STABLE)
    return any(decision == Stability.STABLE for decision in decisions.values())


def print_external_call_stats(timings):
    print("\n=== Caza external-call time (wall clock) ===")
    for label, elapsed, _ in timings:
        print(f"{elapsed:8.2f}s  {label}")
    # build_* entries are nested within get_debugger, whose time is already
    # represented by "build debugger trace and proof".
    total = sum(elapsed for _, elapsed, include_in_total in timings if include_in_total)
    print(f"{total:8.2f}s  total")

def append_timing_stats(csv_index, query_path, started_at, timings, result, path):
    """Append a row, extending older timing CSVs without losing old rows."""
    fieldnames = TIMING_FIELDS
    def elapsed_for(label):
        return sum(elapsed for item_label, elapsed, _ in timings if item_label == label)
    row = {
        "index": csv_index,
        "query": os.path.basename(query_path),
        "start_time": started_at.isoformat(timespec="seconds"),
        "end_time": datetime.now().isoformat(timespec="seconds"),
        "start_timestamp": int(started_at.timestamp()),
        "end_timestamp": int(datetime.now().timestamp()),
        TRACE_PROOF: f"{elapsed_for(TRACE_PROOF):.3f}",
        FIND_FIXES: f"{elapsed_for(FIND_FIXES):.3f}",
        VERIFY_FIXES: f"{elapsed_for(VERIFY_FIXES):.3f}",
        CHECK_STABILITY: f"{elapsed_for(CHECK_STABILITY):.3f}",
        "total_seconds": f"{sum(elapsed for _, elapsed, include_in_total in timings if include_in_total):.3f}",
        "result": result
    }

    os.makedirs(os.path.dirname(path), exist_ok=True)
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
    if not needs_header:
        with open(path, newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            old_rows = list(reader)
            old_fields = reader.fieldnames
        if old_fields != fieldnames:
            with open(path, "w", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                for old_row in old_rows:
                    writer.writerow({field: old_row.get(field, "") for field in fieldnames})
    with open(path, "a", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)

def parse_args():
    p = argparse.ArgumentParser(
        description="Given unstable query path, runs cazamariposa workflow"
    )
    p.add_argument("query_path", help="path to unstable query")
    add_set_seed_option(p)
    p.add_argument(
        "--early",
        action="store_true",
        help=(
            "use early stopping for the final candidate-stability pass; "
            "only stable candidates are fully identified"
        ),
    )
    p.add_argument(
        "--fast-proof",
        action="store_true",
        help="use only cached trace and proof artifacts; construct no debugger artifacts",
    )
    p.add_argument(
        "--sort",
        nargs="?",
        choices=("rank", "time", "div"),
        const="rank",
        default=None,
        help="with --early, order by Caza rank, vanilla verify time, or vanilla verify time/rank prior",
    )
    p.add_argument("--csv-index", default="", help=argparse.SUPPRESS)
    p.add_argument(
        "--timing-stats-csv",
        default=TIMING_STATS_CSV,
        help=f"per-query timing CSV (default: {TIMING_STATS_CSV})",
    )
    args = p.parse_args()
    if args.sort and not args.early:
        p.error("--sort requires --early")
    if args.set_seed is not None:
        try:
            args.set_seed = int(str(args.set_seed), 16)
        except ValueError:
            p.error("--set-seed must be a hexadecimal integer")
    return args

def main():
    call_started_at = datetime.now()
    external_timings = []
    print("Parsing...")
    args = parse_args()

    print("Starting Cazamariposas")
    options = DebugOptions()
    options.verbose = True
    options.is_verus = True
    options.retry_failed = True
    options.set_seed = args.set_seed
    options.cached_proofs_only = args.fast_proof
    options.timing_records = external_timings

    experiment_seed_arg = (
        "" if args.set_seed is None else f" --set-seed {args.set_seed:x}"
    )

    options.per_proof_time_sec = 90
    options.total_proof_time_sec = 7200

    dbg = timed_call(
        TRACE_PROOF,
        lambda: get_debugger(args.query_path, options),
        external_timings,
    )
    if dbg.status in {DebugStatus.NO_PROOF, DebugStatus.NO_TRACE}:
        if dbg.status == DebugStatus.NO_PROOF:
            logging.error("No proof object found.")
        else:
            logging.error("No failure trace found. Probably the query is already stable.")
        result = "no proof" if dbg.status == DebugStatus.NO_PROOF else "no trace"

        if args.csv_index:
            append_timing_stats(
                args.csv_index,
                args.query_path,
                call_started_at,
                external_timings,
                result,
                args.timing_stats_csv,
            )
        else:
            print_external_call_stats(external_timings)
        return
    print("Found failure trace and proof object")

    proj_name = dbg.proj_name
    clean_up(proj_name)

    #produces candidate smt2 files at data/projs/<name>/base.z3/{edit_id}.smt2
    ranked_ids = timed_call(FIND_FIXES, dbg.create_project, external_timings)
    print("Produced candidate smt2 files")
    project_dir = f"data/projs/{proj_name}/base.z3"

    verification_times, rank_by_id = timed_call(
        VERIFY_FIXES,
        lambda: verify_and_filter_ranked_candidates(
            project_dir, ranked_ids, args.set_seed, args.early, experiment_seed_arg
        ),
        external_timings,
    )


    # The verified candidates are copied to this directory by the preceding
    # analysis filter command.  The optional quick experiment below further
    # removes candidates that look unstable before final checking.
    filter_dir = project_dir.replace("/base.z3", ".filtered/base.z3")

    # Run Mariposa on each candidate to determine if the fix repaired
    # stability.  In early mode, the finite 60-second analyzer matches the
    # default experiment timeout and lets the runner stop once stable versus
    # non-stable is forced.
    stable_ids = []
    if args.early:
        print("Ran early-stop stability checks until a stable fix was found")
        stable_paths = []
        candidate_paths = list_smt2_files(filter_dir)
        candidate_paths.sort(
            key=lambda path: (
                verification_sort_key(
                    args.sort,
                    verification_times.get(
                        os.path.splitext(os.path.basename(path))[0], float("inf")
                    ),
                    rank_by_id.get(
                        os.path.splitext(os.path.basename(path))[0],
                        len(RANK_PRIORITIES) + 1,
                    ),
                    path,
                )
            )
        )
        print("Checking candidates by heuristic order")
        def check_candidates_early():
            for index, candidate_path in enumerate(candidate_paths, start=1):
                print(
                    f"Checking candidate {index}/{len(candidate_paths)} for stability: "
                    f"{candidate_path}"
                )
                if is_early_stable(candidate_path, args.set_seed):
                    stable_paths.append(candidate_path)
                    print("Found a stable fix; stopping further stability checks")
                    break

        timed_call(CHECK_STABILITY, check_candidates_early, external_timings)
        stable_ids = [
            os.path.splitext(os.path.basename(path))[0]
            for path in stable_paths
        ]
    else:
        def check_candidates():
            """Run and analyze the complete non-early stability check."""
            print("Running full stability analysis on each candidate")
            os.system(
                f"./src/exper_wizard.py multiple -e default -i {filter_dir} --clear"
                f"{experiment_seed_arg}"
            )
            print("Ran mariposa on each candidate")

            out = subprocess.run(
                [
                    "./src/analysis_wizard.py",
                    "basic",
                    "-i",
                    filter_dir,
                    "-e",
                    "default",
                    "--category",
                    "stable",
                    "-qv",
                    "1",
                ],
                capture_output=True,
                text=True,
            ).stdout
            return [
                os.path.splitext(os.path.basename(line.split("query path:", 1)[1].strip()))[0]
                for line in out.splitlines()
                if line.strip().startswith("query path:")
            ]

        stable_ids = timed_call(CHECK_STABILITY, check_candidates, external_timings)

    append_timing_stats(
        args.csv_index,
        args.query_path,
        call_started_at,
        external_timings,
        f"{len(stable_ids)} fixes" if stable_ids else "no fixes",
        args.timing_stats_csv,
    )

    if not stable_ids:
        print("No fixes were found :(")
    else:
        print(f"Found {len(stable_ids)} fix(es):")

    for edit_id in stable_ids:
        edit = dbg.tracker.look_up_edit_with_id(edit_id)
        qname, action = edit.get_singleton_edit()
        print(f"    {edit_id}: {action.value} {qname} -> {edit.query_path}")
    print_external_call_stats(external_timings)


if __name__ == "__main__":
    main()
