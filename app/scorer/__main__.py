r"""Training CLI for the ATO XGBoost scorer.

Usage::

    uv run python -m app.scorer train \
        --output app/scorer/models/ato-v1.ubj \
        --samples 2000
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    """Entry point for the ATO scorer training CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m app.scorer",
        description="ATO XGBoost scorer training CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    train_parser = subparsers.add_parser("train", help="Train the ATO scorer model.")
    train_parser.add_argument(
        "--output",
        type=Path,
        default=Path("app/scorer/models/ato-v1.ubj"),
        help="Destination path for the saved model artifact.",
    )
    train_parser.add_argument(
        "--samples",
        type=int,
        default=2000,
        help="Number of synthetic training samples to generate.",
    )
    train_parser.add_argument(
        "--estimators",
        type=int,
        default=100,
        help="Number of XGBoost boosting rounds.",
    )

    args = parser.parse_args()

    if args.command == "train":
        from app.scorer.trainer import train

        print(f"Training ATO scorer: {args.samples} samples → {args.output}")
        train(
            n_samples=args.samples,
            model_path=args.output,
            n_estimators=args.estimators,
        )
        print(f"Done. Model saved to {args.output}")


if __name__ == "__main__":
    main()
