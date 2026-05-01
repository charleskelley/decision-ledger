r"""Training and evaluation CLI for the ATO XGBoost scorer.

Subcommands:

  ``train``  Generate synthetic data, fit XGBoost, write the artifact +
             ``ModelCard`` sidecar + train/test CSVs.

  ``eval``   Load an existing artifact + card, generate a fresh test set
             with a different seed, print metrics + routing distribution,
             exit non-zero if ``test_auc`` falls below a sanity floor.

Usage::

    uv run python -m app.scorer train \
        --output app/scorer/models/ato-v1.ubj \
        --samples 5000

    uv run python -m app.scorer eval \
        --model app/scorer/models/ato-v1.ubj
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import xgboost as xgb

from app.scorer.eval import (
    confusion_matrix_at_threshold,
    log_loss,
    precision_recall_at_threshold,
    roc_auc,
    routing_distribution,
)
from app.scorer.scorer import (
    FAST_PATH_ALLOW_THRESHOLD,
    FAST_PATH_BLOCK_THRESHOLD,
    FEATURE_NAMES,
)

_EVAL_SANITY_FLOOR_AUC: float = 0.85


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse hierarchy for ``train`` and ``eval`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m app.scorer",
        description="ATO XGBoost scorer training and evaluation CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # train
    train_parser = subparsers.add_parser(
        "train", help="Train the ATO scorer model and write artifact + card."
    )
    train_parser.add_argument(
        "--output",
        type=Path,
        default=Path("app/scorer/models/ato-v1.ubj"),
        help="Destination .ubj path (sidecar .json and CSVs go alongside).",
    )
    train_parser.add_argument(
        "--samples",
        type=int,
        default=5000,
        help="Number of synthetic samples to generate.",
    )
    train_parser.add_argument(
        "--estimators",
        type=int,
        default=100,
        help="Number of XGBoost boosting rounds.",
    )
    train_parser.add_argument(
        "--no-persist-data",
        action="store_true",
        help="Skip writing train/test CSVs (still writes the binary + card).",
    )

    # eval
    eval_parser = subparsers.add_parser(
        "eval",
        help="Evaluate an existing artifact on a fresh-seed test set.",
    )
    eval_parser.add_argument(
        "--model",
        type=Path,
        default=Path("app/scorer/models/ato-v1.ubj"),
        help="Path to the trained .ubj artifact.",
    )
    eval_parser.add_argument(
        "--samples",
        type=int,
        default=2000,
        help="Number of synthetic test samples to generate.",
    )

    return parser


def _run_train(args: argparse.Namespace) -> int:
    from app.scorer.trainer import train

    print(
        f"Training ATO scorer: {args.samples} samples → {args.output}",
        file=sys.stderr,
    )
    card = train(
        n_samples=args.samples,
        model_path=args.output,
        n_estimators=args.estimators,
        persist_data=not args.no_persist_data,
    )
    print(
        f"Done. Artifact: {args.output}\n"
        f"      Card:     {args.output.with_suffix('.json')}\n"
        f"      test_auc: {card.training.test_auc:.4f}\n"
        f"      routing:  {card.training.test_routing_distribution}",
        file=sys.stderr,
    )
    return 0


def _run_eval(args: argparse.Namespace) -> int:
    from app.scorer.trainer import _generate_dataset

    if not args.model.exists():
        print(f"Model not found: {args.model}", file=sys.stderr)
        return 1

    # Re-derive a fresh test set with a perturbed seed to avoid the
    # exact-same-distribution-as-training case.
    saved_seed_attr = "_SEED"
    from app.scorer import trainer as trainer_mod

    original_seed = getattr(trainer_mod, saved_seed_attr)
    fresh_seed = original_seed + 1
    try:
        trainer_mod._SEED = fresh_seed
        x_arr, y = _generate_dataset(args.samples)
    finally:
        trainer_mod._SEED = original_seed

    booster = xgb.Booster()
    booster.load_model(str(args.model))
    dmat = xgb.DMatrix(x_arr, label=y, feature_names=FEATURE_NAMES)
    pred = np.clip(booster.predict(dmat), 0.0, 1.0)

    auc = roc_auc(y, pred)
    ll = log_loss(y, pred)
    precision, recall = precision_recall_at_threshold(y, pred, 0.5)
    cm = confusion_matrix_at_threshold(y, pred, 0.5)
    routing = routing_distribution(
        pred,
        allow_threshold=FAST_PATH_ALLOW_THRESHOLD,
        block_threshold=FAST_PATH_BLOCK_THRESHOLD,
    )

    print(f"Evaluation on {args.samples} fresh-seed samples (seed={fresh_seed}):")
    print(f"  test_auc                = {auc:.4f}")
    print(f"  test_log_loss           = {ll:.4f}")
    print(f"  test_precision_at_0_5   = {precision:.4f}")
    print(f"  test_recall_at_0_5      = {recall:.4f}")
    print(f"  test_confusion_matrix   = {cm}")
    print(f"  test_routing_distribution = {routing}")

    if auc < _EVAL_SANITY_FLOOR_AUC:
        print(
            f"\nFAIL: test_auc {auc:.4f} below sanity floor "
            f"{_EVAL_SANITY_FLOOR_AUC:.2f}.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_parser().parse_args()
    if args.command == "train":
        return _run_train(args)
    if args.command == "eval":
        return _run_eval(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
