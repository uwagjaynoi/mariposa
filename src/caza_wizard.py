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
RANK_STATS_CSV = "log/caza_all_ranks.csv"
RANK_STATS_SLOTS = 10
RANK_PRIORITIES = (8, 6, 5, 4, 4, 4, 4, 3, 3, 3)
TIMING_FIELDS = [
    "index",
    "query",
    "start_time",
    "end_time",
    "initial_stability_seconds",
    "debugger_seconds",
    "finding_fixes_seconds",
    "verify_seconds",
    "filter_analysis_seconds",
    "filter_experiment_seconds",
    "carve_seconds",
    "early_stability_seconds",
    "total_seconds",
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
    return (elapsed / rank_priority(rank), rank, edit_id)


def verify_and_filter_ranked_candidates(
    project_dir, ranked_ids, set_seed, sort_mode, timings
):
    """Run verify and filter while retaining each candidate's vanilla time.

    This is the in-process equivalent of Caza's ``exper_wizard multiple -e
    verify`` and ``analysis_wizard filter`` calls.  It follows the filter's
    360-second budget.  ``sort_mode`` selects raw verification time or that
    time divided by the supplied rank prior.
    """
    project = FACT.get_project_by_path(project_dir)
    experiment = FACT.get_exper(
        project,
        FACT.get_config("verify"),
        FACT.get_solver("z3_4_16_0"),
        build=True,
    )
    if set_seed is not None:
        experiment.set_seed(set_seed)

    timed_call("verify candidates", lambda: Runner(experiment).run_experiment(True), timings)
    verified = SingletonAnalyzer(experiment, FACT.get_analyzer("60sec"))
    verification_times = {
        edit_id: elapsed
        for edit_id, (_, elapsed) in verified.edit_results.items()
    }
    rank_by_id = {edit_id: rank for rank, edit_id in enumerate(ranked_ids, start=1)}

    def heuristic(edit_id):
        rank = rank_by_id.get(edit_id, len(RANK_PRIORITIES) + 1)
        elapsed = verification_times[edit_id]
        return verification_sort_key(sort_mode, elapsed, rank, edit_id)

    filtered_dir = project_dir.replace("/base.z3", ".filtered/base.z3")

    def create_filtered_project():
        os.makedirs(filtered_dir, exist_ok=True)
        budget = 0
        selected = []
        for edit_id in sorted(verified.passed_edits, key=heuristic):
            if budget >= 360:
                break
            _, elapsed = verified.get_query_result(edit_id)
            budget += elapsed / 1000
            source = project.get_path(edit_id)
            destination = os.path.join(filtered_dir, f"{edit_id}.smt2")
            shutil.copy(source, destination)
            selected.append(edit_id)
            rank = rank_by_id.get(edit_id, None)
            prior = rank_priority(rank) if rank is not None else 1
            score = heuristic(edit_id)[0] / 1000
            score_label = "t" if sort_mode == "time" else "t/p"
            print(
                f"verify candidate {edit_id}: {elapsed / 1000:.3f}s; "
                f"rank={rank}; p={prior}; {score_label}={score:.3f}"
            )
        print(
            f"selected {len(selected)} verified candidates to {filtered_dir} "
            f"with {budget:.3f}s vanilla verification budget"
        )
        return verification_times, rank_by_id

    return timed_call("filter broken candidates", create_filtered_project, timings)


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


def build_timing_row(csv_index, query_path, started_at, timings, candidate_results):
    """Build the former slow2 timing columns directly from Caza timings."""
    def elapsed_for(label):
        return sum(elapsed for item_label, elapsed, _ in timings if item_label == label)

    return {
        "index": csv_index,
        "query": os.path.basename(query_path),
        "start_time": started_at.isoformat(timespec="seconds"),
        "end_time": datetime.now().isoformat(timespec="seconds"),
        "initial_stability_seconds": f"{elapsed_for('initial stability check'):.3f}",
        "debugger_seconds": f"{elapsed_for('build debugger trace and proof'):.3f}",
        "finding_fixes_seconds": f"{elapsed_for('finding fixes'):.3f}",
        "verify_seconds": f"{elapsed_for('verify candidates'):.3f}",
        "filter_analysis_seconds": f"{elapsed_for('filter broken candidates'):.3f}",
        "filter_experiment_seconds": f"{elapsed_for('filter candidates experiment'):.3f}",
        "carve_seconds": f"{elapsed_for('carve unstable candidates'):.3f}",
        "early_stability_seconds": f"{sum(elapsed for _, elapsed, _ in candidate_results):.3f}",
        "total_seconds": f"{sum(elapsed for _, elapsed, include_in_total in timings if include_in_total):.3f}",
    }


def append_csv_row(path, fieldnames, row):
    """Append a row, extending older timing CSVs without losing old rows."""
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


def append_timing_stats(csv_index, query_path, started_at, timings, candidate_results):
    """Append timing data for every Caza invocation to log/slow2.csv."""
    append_csv_row(
        TIMING_STATS_CSV,
        TIMING_FIELDS,
        build_timing_row(csv_index, query_path, started_at, timings, candidate_results),
    )


def append_rank_stability_stats(
    csv_index, query_path, started_at, timings, candidate_count, candidate_results
):
    """Append one all-ranks Caza call's post-filter outcomes to its own CSV."""
    fieldnames = TIMING_FIELDS + ["ranked_candidate_count"]
    for rank in range(1, RANK_STATS_SLOTS + 1):
        fieldnames.extend((f"fix_{rank}_time_seconds", f"fix_{rank}_result"))

    row = build_timing_row(csv_index, query_path, started_at, timings, candidate_results)
    row["ranked_candidate_count"] = candidate_count
    for rank, (_, elapsed, stable) in enumerate(candidate_results, start=1):
        row[f"fix_{rank}_time_seconds"] = f"{elapsed:.3f}"
        row[f"fix_{rank}_result"] = "stable" if stable else "unstable"

    append_csv_row(RANK_STATS_CSV, fieldnames, row)

def main():
    external_timings = []
    call_started_at = datetime.now()
    print("Parsing...")

    #parse query_path
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
        "--skip-filter",
        action="store_true",
        help="skip the quick filter experiment and carve step",
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
    p.add_argument(
        "--all-ranks",
        action="store_true",
        help=(
            "with --early --sort, check every surviving ranked candidate and "
            "append rank stability statistics to log/caza_all_ranks.csv"
        ),
    )
    p.add_argument("--csv-index", default="", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.sort and not args.early:
        p.error("--sort requires --early")
    if args.all_ranks and (not args.early or not args.sort):
        p.error("--all-ranks requires --early --sort")
    if args.set_seed is not None:
        try:
            args.set_seed = int(str(args.set_seed), 16)
        except ValueError:
            p.error("--set-seed must be a hexadecimal integer")

    # Caza is only useful for an unstable query.  Check the original input
    # first, before paying the cost of trace/core/proof construction.
    print("Checking whether the input query is already stable")
    if timed_call(
        "initial stability check",
        lambda: is_early_stable(args.query_path, args.set_seed),
        external_timings,
    ):
        print("Input query is already stable; skipping Caza debugging")
        append_timing_stats(
            args.csv_index, args.query_path, call_started_at, external_timings, []
        )
        if args.all_ranks:
            append_rank_stability_stats(
                args.csv_index, args.query_path, call_started_at, external_timings, 0, []
            )
        print_external_call_stats(external_timings)
        return

    # print("Although not stable, this is a test anyway")
    # print_external_call_stats(external_timings)
    # return


    print("Starting Cazamariposas")
    #set Caza options
    options = DebugOptions()
    options.verbose = True
    options.is_verus = True
    options.retry_failed = True
    options.set_seed = args.set_seed
    options.cached_proofs_only = args.fast_proof
    # Keep build_all's phase timings in the same report.  They are marked as
    # nested so the total does not count get_debugger twice.
    options.timing_records = external_timings

    experiment_seed_arg = (
        "" if args.set_seed is None else f" --set-seed {args.set_seed:x}"
    )

    #for some tasks the default 30 sec per proof and 120 total sec is not enough
    options.per_proof_time_sec = 90
    options.total_proof_time_sec = 1800

    #Mutate query until we reach a failure trace and a proof object
    dbg = timed_call(
        "build debugger trace and proof",
        lambda: get_debugger(args.query_path, options),
        external_timings,
    )

    # Caza needs both a proof object and a candidate failure trace.
    if dbg.status in {DebugStatus.NO_PROOF, DebugStatus.NO_TRACE}:
        missing = "proof object" if dbg.status == DebugStatus.NO_PROOF else "failure trace"
        logging.error(f":( could not get any mutant to produce a {missing}")
        append_timing_stats(
            args.csv_index, args.query_path, call_started_at, external_timings, []
        )
        if args.all_ranks:
            append_rank_stability_stats(
                args.csv_index, args.query_path, call_started_at, external_timings, 0, []
            )
        print_external_call_stats(external_timings)
        return

    print("Found failure trace and proof object")

    #If we could not produce a proof object, Caza cannot find fixes
    if dbg.status == DebugStatus.NO_PROOF:
        logging.error(":( could not get any mutant to produce a proof object")
        return

    proj_name = dbg.proj_name
    clean_up(proj_name)

    #produces candidate smt2 files at data/projs/<name>/base.z3/{edit_id}.smt2
    #dbg.tracker.edit_infos gets populated with ___
    ranked_ids = timed_call("finding fixes", dbg.create_project, external_timings)
    # 10 fixes

    print("Produced candidate smt2 files")

    project_dir = f"data/projs/{proj_name}/base.z3"

    verification_times = {}
    rank_by_id = {
        edit_id: rank for rank, edit_id in enumerate(ranked_ids, start=1)
    }
    if args.sort in {"time", "div"}:
        verification_times, rank_by_id = verify_and_filter_ranked_candidates(
            project_dir, ranked_ids, args.set_seed, args.sort, external_timings
        )
    else:
        # Check if proof is broken to fail fast.
        timed_system(
            "verify candidates",
            f"./src/exper_wizard.py multiple -e verify -i {project_dir} --clear"
            f"{experiment_seed_arg}",
            external_timings,
        )
        print("Checked for broken queries")

        # Pick out the candidates that are not broken.
        timed_system(
            "filter broken candidates",
            f"./src/analysis_wizard.py filter -i {project_dir}",
            external_timings,
        )
        print("Filtered out broken queries")

    # The verified candidates are copied to this directory by the preceding
    # analysis filter command.  The optional quick experiment below further
    # removes candidates that look unstable before final checking.
    filter_dir = project_dir.replace("/base.z3", ".filtered/base.z3")
    if args.skip_filter:
        print("Skipped quick filter experiment and carving")
    else:
        timed_system(
            "filter candidates experiment",
            f"./src/exper_wizard.py multiple -e filter -i {filter_dir} --clear"
            f"{experiment_seed_arg}",
            external_timings,
        )
        # verified results among 10 + filter results
        print("Ran mariposa on filtered queries")

        timed_system(
            "carve unstable candidates",
            f"./src/analysis_wizard.py carve -e filter -i {filter_dir}",
            external_timings,
        )
        # verified but unstable results among 10 fixes
        print("Carved out unstable queries")

    # Run Mariposa on each candidate to determine if the fix repaired
    # stability.  In early mode, the finite 60-second analyzer matches the
    # default experiment timeout and lets the runner stop once stable versus
    # non-stable is forced.
    rank_results = []
    if args.early:
        if args.all_ranks:
            print("Running early-stop stability checks for every ranked survivor")
        else:
            print("Ran early-stop stability checks until a stable fix was found")
        stable_paths = []
        candidate_paths = list_smt2_files(filter_dir)
        if args.sort in {"time", "div"}:
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
            if args.sort == "time":
                print("Checking candidates by increasing vanilla verification time")
            else:
                print("Checking candidates by increasing verify-time/rank-prior score")
        elif args.sort == "rank":
            candidate_paths.sort(
                key=lambda path: (
                    rank_by_id.get(os.path.splitext(os.path.basename(path))[0], float("inf")),
                    path,
                )
            )
            print("Checking candidates in Caza rank order")
        else:
            candidate_paths.sort()
        for index, candidate_path in enumerate(candidate_paths, start=1):
            print(
                f"Checking candidate {index}/{len(candidate_paths)} for stability: "
                f"{candidate_path}"
            )
            stable = timed_call(
                f"early stability check {os.path.basename(candidate_path)}",
                lambda path=candidate_path: is_early_stable(path, args.set_seed),
                external_timings,
            )
            rank_results.append((candidate_path, external_timings[-1][1], stable))
            if stable:
                stable_paths.append(candidate_path)
                if args.all_ranks:
                    print("Found a stable fix; continuing rank experiment")
                else:
                    print("Found a stable fix; stopping further stability checks")
                    break
        if args.all_ranks:
            append_rank_stability_stats(
                args.csv_index,
                args.query_path,
                call_started_at,
                external_timings,
                len(candidate_paths),
                rank_results,
            )
    else:
        print("Running full stability analysis on each candidate")
        timed_system(
            "default candidates experiment",
            f"./src/exper_wizard.py multiple -e default -i {filter_dir} --clear"
            f"{experiment_seed_arg}",
            external_timings,
        )

        print("Ran mariposa on each candidate")

        # Get a list of the fixed queries that are now stable.
        out = timed_run(
            "list stable fixes",
            ["./src/analysis_wizard.py", "basic", "-i", filter_dir, "-e", "default", "--category", "stable", "-qv", "1"],
            external_timings,
            capture_output=True,
            text=True,
        ).stdout
        stable_paths = []

    print("Got list of fixes")

    #collect stable ids in a list
    stable_ids = []
    if args.early:
        stable_ids = [
            os.path.splitext(os.path.basename(path))[0]
            for path in stable_paths
        ]
    else:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("query path:"):
                path = line.split("query path:", 1)[1].strip()
                edit_id = os.path.splitext(os.path.basename(path))[0]
                stable_ids.append(edit_id)

    append_timing_stats(
        args.csv_index, args.query_path, call_started_at, external_timings, rank_results
    )

    #if stable_ids is empty, then no fixes were found
    if not stable_ids:
        print("No fixes were found :(")
        print_external_call_stats(external_timings)
        return

    print(f"Found {len(stable_ids)} fix(es):")
    for edit_id in stable_ids:
        edit = dbg.tracker.look_up_edit_with_id(edit_id)
        qname, action = edit.get_singleton_edit()
        print(f"    {edit_id}: {action.value} {qname} -> {edit.query_path}")
    print_external_call_stats(external_timings)


if __name__ == "__main__":
    main()
