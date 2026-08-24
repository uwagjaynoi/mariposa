"""Sound early-decision criteria for :class:`base.query_analyzer.QueryAnalyzer`.

The criteria use the same finite information as ``QueryAnalyzer``: whether a
run is a successful UNSAT result and, for successful runs, whether its elapsed
time is below the analyzer's discounted timeout.  Unknown runs are represented
by the range of every result they may still produce.  A query is decidable only
when *every* completion of its missing runs has the same final stability.
"""

from itertools import product

import numpy as np
from statsmodels.stats.proportion import proportions_ztest

from base.query_analyzer import Stability
from base.solver import RCode


class PartialCriteria:
    """Incrementally decide a query's stability without changing its analyzer."""

    def __init__(self, analyzer, mutations, num_mutants):
        if not np.isfinite(analyzer._timeout):
            raise ValueError("early stopping requires an analyzer with a finite timeout")
        self.analyzer = analyzer
        self.mutations = list(mutations)
        self.group_size = num_mutants + 1  # one vanilla run plus the mutants
        self._vanilla = None
        self._groups = {mutation: [] for mutation in self.mutations}

        method = analyzer.categorize_group.__name__
        if method not in {"_categorize_cutoff", "_categorize_z_test"}:
            raise ValueError(f"early stopping does not support analyzer method {method}")
        self._method = method

    def add_result(self, mutation, rcode, elapsed):
        """Record one completed run and return the forced final result, if any."""
        result = self._normalise(rcode, elapsed)
        if mutation is None:
            if self._vanilla is not None:
                raise ValueError("received more than one vanilla result")
            self._vanilla = result
        else:
            if mutation not in self._groups:
                raise ValueError(f"unexpected mutation {mutation}")
            self._groups[mutation].append(result)
            if len(self._groups[mutation]) > self.group_size - 1:
                raise ValueError(f"too many {mutation} results")
        return self.decision()

    def decision(self):
        """Return a Stability only when all possible completions agree."""
        possible_queries = self.possible_decisions()
        return next(iter(possible_queries)) if len(possible_queries) == 1 else None

    def stable_decision(self):
        """Return STABLE or ``"not_stable"`` when that binary answer is forced."""
        possible_queries = self.possible_decisions()
        if possible_queries == {Stability.STABLE}:
            return Stability.STABLE
        if Stability.STABLE not in possible_queries:
            return "not_stable"
        return None

    def possible_decisions(self):
        """Return all final query categories still reachable by missing runs."""
        possible_groups = [self._possible_group_results(mutation)
                           for mutation in self.mutations]
        return {
            self._combine(group_results)
            for group_results in product(*possible_groups)
        }

    def format_available_information(self):
        """Return a concise, deterministic snapshot of completed evidence."""
        def format_result(result):
            if result is None:
                return "pending"
            rcode, elapsed = result
            return f"{rcode}@{elapsed / 1000:.3f}s"

        groups = []
        for mutation in self.mutations:
            results = self._groups[mutation]
            completed = ", ".join(format_result(result) for result in results)
            remaining = self.group_size - 1 - len(results)
            groups.append(
                f"{mutation}=[{completed}] ({remaining} mutant result(s) pending)"
            )
        possible = ", ".join(
            sorted(decision.value for decision in self.possible_decisions())
        )
        return (
            f"vanilla={format_result(self._vanilla)}; "
            f"groups: {'; '.join(groups)}; "
            f"possible final categories=[{possible}]"
        )

    def _normalise(self, rcode, elapsed):
        timeout = self.analyzer._timeout
        if elapsed > timeout:
            return RCode.TIMEOUT, timeout
        return RCode(rcode), elapsed

    def _possible_group_results(self, mutation):
        results = list(self._groups[mutation])
        if self._vanilla is not None:
            results.insert(0, self._vanilla)
        missing = self.group_size - len(results)
        assert missing >= 0

        # ``cutoff`` counts UNSAT runs at exactly the timeout, while ``z_test``
        # uses a strict less-than comparison.  Keep that subtle difference so
        # an early decision is identical to QueryAnalyzer's final decision.
        successes = [
            (rcode, elapsed)
            for rcode, elapsed in results
            if rcode == RCode.UNSAT
            and (self._method == "_categorize_cutoff" or elapsed < self.analyzer._timeout)
        ]
        success_count = len(successes)
        success_time = sum(elapsed for _, elapsed in successes)
        if self._method == "_categorize_cutoff":
            return self._cutoff_results(success_count, success_time, missing)
        return self._z_test_results(success_count, success_time, missing)

    def _cutoff_results(self, known_successes, known_time, missing):
        possible = set()
        timeout = self.analyzer._timeout
        threshold = timeout * self.analyzer.discount
        size = self.group_size

        for successes in range(known_successes, known_successes + missing + 1):
            if successes / size < self.analyzer.r_solvable:
                possible.add(Stability.UNSOLVABLE)
            elif successes / size >= self.analyzer.r_stable:
                possible.add(Stability.STABLE)
            else:
                added = successes - known_successes
                min_mean = known_time / successes
                max_mean = (known_time + added * timeout) / successes
                if min_mean < threshold:
                    possible.add(Stability.UNSTABLE)
                if max_mean >= threshold:
                    possible.add(Stability.UNSOLVABLE)
        return possible

    def _z_test_results(self, known_successes, known_time, missing):
        possible = set()
        timeout = self.analyzer._timeout
        threshold = timeout * self.analyzer.discount
        size = self.group_size

        for successes in range(known_successes, known_successes + missing + 1):
            _, p_value = proportions_ztest(
                count=successes, nobs=size,
                value=self.analyzer.r_solvable / 100,
                alternative="smaller",
            )
            if p_value <= self.analyzer.confidence:
                possible.add(Stability.UNSOLVABLE)
                continue

            _, p_value = proportions_ztest(
                count=successes, nobs=size,
                value=self.analyzer.r_stable / 100,
                alternative="smaller",
            )
            if p_value <= self.analyzer.confidence:
                added = successes - known_successes
                min_mean = known_time / successes
                max_mean = (known_time + added * timeout) / successes
                if min_mean < threshold:
                    possible.add(Stability.UNSTABLE)
                # Extra z-test successes must be strictly below timeout.  The
                # expression below is their supremum, not necessarily an
                # attainable value, so equality is only possible with no
                # additional successes.
                can_be_slow = (
                    max_mean > threshold
                    or (added == 0 and max_mean >= threshold)
                )
                if can_be_slow:
                    possible.add(self._z_test_tail(successes, size))
            else:
                possible.add(self._z_test_tail(successes, size))
        return possible

    def _z_test_tail(self, successes, size):
        _, p_value = proportions_ztest(
            count=successes, nobs=size,
            value=self.analyzer.r_stable / 100,
            alternative="larger",
        )
        if p_value <= self.analyzer.confidence:
            return Stability.STABLE
        return Stability.INCONCLUSIVE

    @staticmethod
    def _combine(group_results):
        """The final part of QueryAnalyzer.categorize_query, factored verbatim."""
        results = set(group_results)
        if results == {Stability.INCONCLUSIVE}:
            return Stability.INCONCLUSIVE
        results -= {Stability.INCONCLUSIVE}
        if len(results) == 1:
            return results.pop()
        return Stability.UNSTABLE
