"""The wire contract's JSON schemas, exported from the Pydantic models (DR-31).

Two files under ``schemas/`` at the repository root are the published
contract a reasoner team (or an agent) builds against without reading the
code:

- ``submission-v2.json`` — what ``POST /api/v1/decisions`` accepts (and
  ``POST /api/v1/decisions:batch``, one per line): ``core.submission.Submission``
  in validation mode.
- ``bundle-v2.json`` — what ``GET /api/v1/decisions/{decision_id}`` returns
  for a schema-version-2 decision: ``core.bundle.DecisionBundle`` in
  serialization mode.

They are generated, never edited by hand. ``tests/test_schemas.py`` fails
when a committed file differs from what the models export, and CI runs it
with the unit tests, so a contract change is always a reviewed diff of these
files too.

Usage::

    uv run python -m app.schemas --write   # regenerate schemas/ (make schemas)
    uv run python -m app.schemas --check   # exit 1 when a file drifted
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.bundle import BUNDLE_SCHEMA_VERSION, DecisionBundle
from core.submission import SUBMISSION_SCHEMA_VERSION, Submission

#: JSON Schema dialect Pydantic generates.
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def export_schemas() -> dict[str, dict[str, Any]]:
    """Return every contract schema, keyed by its file name.

    Returns:
        ``{"submission-v2.json": ..., "bundle-v2.json": ...}``, each with
        ``$schema`` and a ``$id`` naming the contract and its version.
    """
    submission = Submission.model_json_schema(mode="validation")
    bundle = DecisionBundle.model_json_schema(mode="serialization")
    return {
        f"submission-v{SUBMISSION_SCHEMA_VERSION}.json": {
            "$schema": _DIALECT,
            "$id": f"urn:decisionledger:schema:submission:{SUBMISSION_SCHEMA_VERSION}",
            **submission,
        },
        f"bundle-v{BUNDLE_SCHEMA_VERSION}.json": {
            "$schema": _DIALECT,
            "$id": f"urn:decisionledger:schema:bundle:{BUNDLE_SCHEMA_VERSION}",
            **bundle,
        },
    }


def render(schema: dict[str, Any]) -> str:
    """Serialize a schema the way the committed files are written."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def drifted(directory: Path) -> list[str]:
    """Names of the schema files under ``directory`` that differ from the models.

    A missing file counts as drifted.
    """
    return [
        name
        for name, schema in export_schemas().items()
        if not (directory / name).is_file()
        or (directory / name).read_text(encoding="utf-8") != render(schema)
    ]


def main(argv: list[str] | None = None) -> int:
    """Write or check the committed schema files.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``1`` when ``--check`` finds a drifted file.
    """
    parser = argparse.ArgumentParser(
        prog="app.schemas", description="Export the wire contract's JSON schemas."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="rewrite the files")
    action.add_argument("--check", action="store_true", help="fail on drift")
    parser.add_argument(
        "--dir", type=Path, default=Path("schemas"), help="directory (./schemas)"
    )
    args = parser.parse_args(argv)
    directory: Path = args.dir
    if args.check:
        stale = drifted(directory)
        if stale:
            print(
                f"schemas drifted from the models: {stale}; run "
                "`uv run python -m app.schemas --write` (make schemas)",
                file=sys.stderr,
            )
            return 1
        print(f"schemas up to date in {directory}/")
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    for name, schema in export_schemas().items():
        (directory / name).write_text(render(schema), encoding="utf-8")
        print(f"wrote {directory / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
