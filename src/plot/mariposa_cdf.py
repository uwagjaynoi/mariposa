#!/usr/bin/env python3
"""Plot slow and fast running-time CDFs for every result in mariposa.csv."""

import argparse
import csv
import math
from pathlib import Path
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


TIME_COLUMNS = ("slow time", "fast time")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_csv",
        nargs="?",
        type=Path,
        default=Path("mariposa.csv"),
        help="timing CSV (default: mariposa.csv)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("fig/mariposa_cdf"),
        help="directory for one PNG per result category (default: fig/mariposa_cdf)",
    )
    return parser.parse_args()


def load_rows(input_csv):
    with input_csv.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"result", *TIME_COLUMNS}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing CSV column(s): {', '.join(sorted(missing))}")
        return list(reader)


def cdf(values):
    values = sorted(values)
    return values, [(index + 1) / len(values) for index in range(len(values))]


def valid_times(rows, column):
    values = []
    for row in rows:
        try:
            value = float(row[column])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            values.append(value)
    return values


def output_name(category):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", category) or "empty"


def plot_category(category, rows, output_path):
    fig, axis = plt.subplots(figsize=(6, 4))
    colors = {"slow time": "tab:orange", "fast time": "tab:blue"}

    for column in TIME_COLUMNS:
        values = valid_times(rows, column)
        if not values:
            continue
        xs, ys = cdf(values)
        label = f"{column[:-len(' time')]} (n={len(xs)})"
        axis.step(xs, ys, where="post", label=label, color=colors[column])

    axis.set_xscale("log")
    axis.set_xlabel("Time (seconds, log scale)")
    axis.set_ylabel("CDF")
    axis.set_ylim(0, 1.02)
    axis.set_title(f"Mariposa runtime CDF — result: {category}")
    axis.grid(True, which="both", linestyle=":", alpha=0.5)
    axis.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def main():
    args = parse_args()
    input_csv = args.input_csv.resolve()
    if not input_csv.is_file():
        raise ValueError(f"input CSV does not exist: {input_csv}")

    categories = {}
    for row in load_rows(input_csv):
        categories.setdefault(row["result"], []).append(row)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for category in sorted(categories):
        output_path = args.output_dir / f"{output_name(category)}.png"
        plot_category(category, categories[category], output_path)
        print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
