"""The committed wire-contract schemas match the models (DR-31).

``schemas/submission-v2.json`` and ``schemas/bundle-v2.json`` are the
contract a reasoner team builds against without reading the code. They are
generated from ``Submission`` and ``DecisionBundle`` (``app.schemas``); this
test fails when a model change is not reflected in the committed files, so
every contract change shows up as a reviewed diff of them. Regenerate with
``make schemas``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import drifted, export_schemas, main, render

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def test_the_committed_schemas_match_the_models():
    stale = drifted(SCHEMA_DIR)
    assert not stale, (
        f"schemas/ drifted from the models: {stale}; run `make schemas` and "
        "review the diff as a contract change"
    )


def test_the_schemas_name_their_contract_and_version():
    schemas = export_schemas()
    assert set(schemas) == {"submission-v2.json", "bundle-v2.json"}
    submission = schemas["submission-v2.json"]
    assert submission["$id"] == "urn:decisionledger:schema:submission:2"
    assert submission["title"] == "Submission"
    assert {"mode", "reasoner_context", "raw_event"} <= set(submission["properties"])
    assert submission["additionalProperties"] is False
    bundle = schemas["bundle-v2.json"]
    assert bundle["$id"] == "urn:decisionledger:schema:bundle:2"
    assert "registration_version" in bundle["properties"]


def test_check_fails_on_drift_and_write_repairs_it(tmp_path: Path, capsys):
    assert main(["--check", "--dir", str(tmp_path)]) == 1
    assert main(["--write", "--dir", str(tmp_path)]) == 0
    assert main(["--check", "--dir", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "bundle-v2.json").read_text(encoding="utf-8"))
    assert render(written) == (tmp_path / "bundle-v2.json").read_text(encoding="utf-8")
    capsys.readouterr()
