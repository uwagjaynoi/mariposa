"""An alternate experiment runner that stops queries once their result is forced."""

import csv
import math
import multiprocessing as mp
import os
import random
import time

from analysis.partial_criteria import PartialCriteria
from base.exper import ExpTask
from base.exper_runner import print_eta
from base.query_analyzer import Stability
from base.solver import RCode
from utils.query_utils import Mutation, emit_mutant_query, emit_quake_query
from utils.system_utils import log_info


def _run_one(exp, task):
    """Run and persist one task, returning every individual solver result."""
    actual_path = task.mutant_path
    reseeded = task.perturb == Mutation.RESEED
    composed = task.perturb == Mutation.COMPOSE
    if reseeded or task.perturb is None:
        actual_path = task.origin_path
    elif task.perturb == Mutation.QUAKE:
        emit_quake_query(task.origin_path, task.mutant_path, exp.num_mutant)
    else:
        emit_mutant_query(task.origin_path, task.mutant_path, task.perturb, task.mut_seed)

    results = []
    if task.quake:
        exp.solver.start_process(task.mutant_path, exp.timeout)
        for index in range(exp.num_mutant):
            rcode, elapsed = exp.solver.run_quake_iteration(exp.timeout)
            mutant_path = f"{task.mutant_path}.{index}"
            exp.insert_exp_row(task, mutant_path, rcode.value, elapsed)
            results.append((rcode.value, elapsed))
        exp.solver.end_process()
    else:
        seed = task.mut_seed if reseeded or composed else None
        rcode, elapsed = exp.solver.run(actual_path, exp.timeout, seed)
        exp.insert_exp_row(task, task.mutant_path, rcode.value, elapsed)
        results.append((rcode.value, elapsed))

    if not exp.keep_mutants and actual_path != task.origin_path:
        os.remove(actual_path)
    return results


def _worker(exp, task_queue, result_queue):
    while True:
        task = task_queue.get()
        if task is None:
            return
        try:
            result_queue.put((task, _run_one(exp, task), None))
        except Exception as error:  # avoid leaving the parent waiting forever
            result_queue.put((task, None, repr(error)))
            return


class EarlyStopRunner:
    """Run an experiment and return decisions without producing a fake sum table.

    Raw completed rows are retained in the normal experiment database.  A sum
    table is intentionally not produced: its missing rows would otherwise be
    replaced by ERROR values and misrepresent a stopped experiment as complete.
    """

    def __init__(self, experiment, analyzer, stable_only=False):
        self.exp = experiment
        self.analyzer = analyzer
        self.stable_only = stable_only
        analyzer_timeout_ms = analyzer._timeout
        if not math.isfinite(analyzer_timeout_ms) or analyzer_timeout_ms <= 0:
            raise ValueError("early stopping requires a finite, positive analyzer timeout")
        if analyzer_timeout_ms % 1000 != 0:
            raise ValueError("early stopping requires a whole-second analyzer timeout")
        analyzer_timeout = int(analyzer_timeout_ms / 1000)
        self.exp.timeout = min(self.exp.timeout, analyzer_timeout)
        log_info(f"using {self.exp.timeout:g}s solver timeout (capped by analyzer {analyzer.name})")

    def run_experiment(self, clear, category=None, stable_output_path=None):
        tasks = self.exp.create_tasks(clear)
        grouped = self._group_tasks(tasks)
        timing_columns = self._add_timing_columns(grouped)
        criteria = {
            qid: PartialCriteria(self.analyzer, self.exp.enabled_muts,
                                 self.exp.num_mutant)
            for qid in grouped
        }
        decisions, timings = self._run(grouped, criteria)
        csv_path = self._write_timing_csv(decisions, timings, timing_columns)
        self._write_stable_results(decisions, stable_output_path)
        self._print_report(decisions, csv_path, category)
        return decisions

    @staticmethod
    def _group_tasks(tasks):
        grouped = {}
        for task in tasks:
            grouped.setdefault(task.origin_path, []).append(task)
        # Run each vanilla task first. This gives every mutation group its
        # shared baseline before scheduling further work for that query.
        for query_tasks in grouped.values():
            query_tasks.sort(key=lambda task: task.perturb is not None)
        return grouped

    def _add_timing_columns(self, grouped):
        """Assign a stable timing-table column to every task result."""
        columns = ["vanilla"]
        for mutation in self.exp.enabled_muts:
            columns.extend(
                f"{mutation}_{index}"
                for index in range(1, self.exp.num_mutant + 1)
            )

        for query_tasks in grouped.values():
            mutation_counts = {}
            for task in query_tasks:
                if task.perturb is None:
                    task.timing_columns = ["vanilla"]
                    continue
                mutation = str(task.perturb)
                if task.quake:
                    task.timing_columns = [
                        f"{mutation}_{index}"
                        for index in range(1, self.exp.num_mutant + 1)
                    ]
                    continue
                mutation_counts[mutation] = mutation_counts.get(mutation, 0) + 1
                task.timing_columns = [f"{mutation}_{mutation_counts[mutation]}"]
        return columns

    def _run(self, grouped, criteria):
        task_queue, result_queue = mp.Queue(), mp.Queue()
        pending = {path: list(tasks) for path, tasks in grouped.items()}
        paths = list(pending)
        order_seed = self.exp.get_task_order_seed("early_stop")
        if order_seed is None:
            random.shuffle(paths)
        else:
            random.Random(order_seed).shuffle(paths)
        ready = list(paths)
        stopped = set()
        decisions = {}
        timings = {path: {} for path in pending}
        in_flight = 0
        start_time = time.time()
        prev_time = 0
        total_size = sum(len(tasks) for tasks in pending.values())
        completed_size = 0
        worker_count = self.exp.num_procs
        workers = [mp.Process(target=_worker, args=(self.exp, task_queue, result_queue))
                   for _ in range(worker_count)]
        for worker in workers:
            worker.start()

        def submit_available():
            nonlocal in_flight
            while in_flight < worker_count and ready:
                path = ready.pop(0)
                if path in stopped or not pending[path]:
                    continue
                task_queue.put(pending[path].pop(0))
                in_flight += 1
                if pending[path]:
                    ready.append(path)

        submit_available()
        while in_flight:
            task, results, error = result_queue.get()
            in_flight -= 1
            completed_size += 1
            if error is not None:
                for worker in workers:
                    worker.terminate()
                raise RuntimeError(f"task failed for {task.origin_path}: {error}")

            for column, (rcode, elapsed) in zip(task.timing_columns, results):
                timings[task.origin_path][column] = (rcode, elapsed)

            if task.origin_path not in stopped:
                mutation = task.perturb
                for rcode, elapsed in results:
                    criteria[task.origin_path].add_result(mutation, rcode, elapsed)
                if self.stable_only:
                    decision = criteria[task.origin_path].stable_decision()
                else:
                    decision = criteria[task.origin_path].decision()
                if decision is not None:
                    stopped.add(task.origin_path)
                    decisions[task.origin_path] = decision
                    label = decision.value if hasattr(decision, "value") else decision
                    skipped = len(pending[task.origin_path])
                    if skipped > 0:
                        total_size -= skipped
                        log_info(
                            "criteria: "
                            f"{criteria[task.origin_path].format_available_information()}"
                        )
                        log_info(f"early decision: {task.origin_path} -> {label}; "
                                 f"skipping {skipped} queued tasks")
            submit_available()

            elapsed = time.time() - start_time
            if elapsed - prev_time > 600:
                prev_time = elapsed
                remaining = total_size - completed_size
                print_eta(elapsed, remaining, total_size)

        for worker in workers:
            task_queue.put(None)
        for worker in workers:
            worker.join()
        return decisions, timings

    def _write_timing_csv(self, decisions, timings, timing_columns):
        db_stem, _ = os.path.splitext(self.exp.db_path)
        csv_path = f"{db_stem}.{self.analyzer.name}.early_stop.csv"
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)

        with open(csv_path, "w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["query", "stability"] + timing_columns)
            for path in sorted(timings):
                decision = decisions[path]
                label = decision.value if hasattr(decision, "value") else decision
                row = [path, label]
                for column in timing_columns:
                    result = timings[path].get(column)
                    if result is None:
                        row.append("")
                        continue
                    rcode, elapsed = result
                    row.append(f"{RCode(rcode)}@{elapsed / 1000:.3f}s")
                writer.writerow(row)
        return csv_path

    def _write_stable_results(self, decisions, output_path=None):
        """Write the final stable query paths for machine consumers.

        This file is intentionally separate from the timing CSV: it contains
        only one path per line, making it safe for callers such as
        ``caza_wizard.py`` to consume without parsing human-facing output.
        """
        if output_path is None:
            db_stem, _ = os.path.splitext(self.exp.db_path)
            output_path = f"{db_stem}.{self.analyzer.name}.early_stop.stable.txt"
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        stable_paths = sorted(
            path for path, decision in decisions.items()
            if decision == Stability.STABLE
        )
        with open(output_path, "w") as output:
            for path in stable_paths:
                output.write(path + "\n")
        return output_path

    @staticmethod
    def _print_report(decisions, csv_path, category=None):
        def category_label(value):
            return value.value if hasattr(value, "value") else value

        if category is not None:
            paths = sorted(
                path for path, decision in decisions.items()
                if decision == category
            )
            print(f"=== {category_label(category)} ({len(paths)}) ===")
            for path in paths:
                print(f"query path: {path}")
            return

        categories = {}
        for path, decision in decisions.items():
            categories.setdefault(decision, []).append(path)
        print("=== Early-stop analysis report ===")

        for category in sorted(categories, key=category_label):
            paths = sorted(categories[category])
            print(f"{category_label(category)}: {len(paths)}")
            for path in paths:
                print(f"  {path}")
        print(f"\nPer-query running times (seconds) written to: {csv_path}")
