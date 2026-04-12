# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Entry point: python -m callen"""

import sys
import argparse

from callen.app import main


def cli():
    parser = argparse.ArgumentParser(description="Callen IVR System")
    parser.add_argument(
        "-c", "--config",
        default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )
    args = parser.parse_args()
    main(args.config)


if __name__ == "__main__":
    cli()
