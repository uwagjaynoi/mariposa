#!/usr/bin/env python3
"""Alternate combined experiment-and-analysis command with sound early stopping.

This intentionally leaves ``exper_wizard.py`` and ``analysis_wizard.py``
unchanged.  Use it instead of the two-command workflow when the analyzer is
known before the experiment starts.
"""

import argparse

from analysis.early_stop_runner import EarlyStopRunner
from utils.option_utils import add_analysis_options, add_clear_option, add_input_dir_option, add_set_seed_option, deep_parse_args
from utils.system_utils import log_warn


def main():
    parser = argparse.ArgumentParser(
        description="Run an experiment and stop each query once its analyzer result is forced."
    )
    subparsers = parser.add_subparsers(dest="sub_command", required=True)
    multiple = subparsers.add_parser("multiple", help="run an early-stopping project experiment")
    add_input_dir_option(multiple)
    add_analysis_options(multiple)
    add_clear_option(multiple)
    add_set_seed_option(multiple)
    multiple.add_argument(
        "--stable-only",
        default=False,
        action="store_true",
        help="stop once stable vs not_stable is forced, without waiting for the exact non-stable category",
    )
    multiple.add_argument(
        "--stable-output",
        default=None,
        help="file to receive one final stable query path per line",
    )

    args = deep_parse_args(parser.parse_args())
    exp = args.experiment
    if exp.is_done() and not args.clear_existing:
        log_warn(f"complete experiment results already exist for {exp.proj.sub_root}; use --clear-existing")
        return
    EarlyStopRunner(exp, args.analyzer, stable_only=args.stable_only).run_experiment(
        args.clear_existing, category=args.category, stable_output_path=args.stable_output
    )


if __name__ == "__main__":
    main()
