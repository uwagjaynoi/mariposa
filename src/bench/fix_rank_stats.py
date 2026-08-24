#!/usr/bin/env python3
"""Print singleton stabilizing-fix counts for the four Fig. 4(b) groups."""

import os
import subprocess
import sys


ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    text=True,
).strip()
sys.path.insert(0, os.path.join(ROOT, "src"))

from bench.consts import METAS, mariposa_rd1_ranks, verus_rd1_ranks


def ranks_for_query(query):
    """Return all recorded singleton ranks, matching rank.ipynb's lookup."""
    if query in verus_rd1_ranks:
        return set(verus_rd1_ranks[query].values())
    if query in mariposa_rd1_ranks:
        return set(mariposa_rd1_ranks[query].values())
    return set()


def main():
    queries = [query for meta in METAS for query in meta.members]
    query_ranks = [ranks_for_query(query) for query in queries]

    cumulative_counts = []
    exact_counts = []
    for rank in range(1, 11):
        cumulative_counts.append(
            sum(any(1 <= value <= rank for value in ranks) for ranks in query_ranks)
        )
        exact_counts.append(sum(rank in ranks for ranks in query_ranks))

    print("rank i:                    " + " ".join(str(rank) for rank in range(1, 11)))
    print("any good fix through i:    " + " ".join(map(str, cumulative_counts)))
    print("good fix exactly at i:     " + " ".join(map(str, exact_counts)))


if __name__ == "__main__":
    main()
