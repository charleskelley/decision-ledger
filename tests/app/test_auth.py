"""Behavioral tests for ``app.auth`` — API keys to principals (DR-31).

- A reasoner key maps to its reasoner; the operator key to the operator; an
  unknown or missing key to nobody.
- A reasoner may write and read its own decisions only; the operator reads
  everything, resolves, and writes nothing; the in-process principal may do
  everything.
- Keys come from ``DECISIONLEDGER_API_KEYS`` (``reasoner_id:key`` pairs, a
  key may contain colons), a mounted YAML file (a key or a list per
  reasoner), and ``DECISIONLEDGER_OPERATOR_KEY``. A blank key, a malformed
  pair, and one key shared by two principals are refused.
- Keys are indexed by digest: the ring never holds the key text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from app.auth import (
    OPERATOR,
    TRUSTED,
    KeyRing,
    Principal,
    load_api_keys_file,
    parse_api_keys,
    require_operator,
    require_submitter,
)
from app.settings import FrameworkSettings
from core.exceptions import LedgerError

if TYPE_CHECKING:
    from pathlib import Path

STUB = Principal.for_reasoner("stub-reasoner")


def test_keys_map_to_their_principals():
    ring = KeyRing({"stub-reasoner": ["k1", "k2"], "alpha": ["k3"]}, "op")

    assert ring.principal("k1") == STUB
    assert ring.principal("k2") == STUB
    assert ring.principal("k3") == Principal.for_reasoner("alpha")
    assert ring.principal("op") == OPERATOR
    assert ring.principal("nope") is None
    assert ring.principal("") is None
    assert ring.principal(None) is None
    assert ring.reasoner_ids == frozenset({"stub-reasoner", "alpha"})
    assert not ring.empty


def test_the_ring_never_holds_key_text():
    ring = KeyRing({"stub-reasoner": ["super-secret-key"]}, "operator-secret")
    held = repr(vars(ring))
    assert "super-secret-key" not in held
    assert "operator-secret" not in held


def test_an_empty_ring_authenticates_nobody():
    ring = KeyRing()
    assert ring.empty
    assert ring.principal("anything") is None


@pytest.mark.parametrize(
    ("reasoner_keys", "operator_key", "message"),
    [
        ({"stub-reasoner": [" "]}, None, "blank API key"),
        ({"a": ["shared"], "b": ["shared"]}, None, "both reasoner:a and reasoner:b"),
        ({"a": ["shared"]}, "shared", "both reasoner:a and operator"),
    ],
)
def test_a_blank_or_shared_key_is_refused(reasoner_keys, operator_key, message):
    with pytest.raises(ValueError, match=message):
        KeyRing(reasoner_keys, operator_key)


def test_permissions_by_principal():
    assert STUB.may_submit_for("stub-reasoner")
    assert not STUB.may_submit_for("alpha")
    assert STUB.may_read("stub-reasoner")
    assert not STUB.may_read("alpha")
    assert not STUB.may_resolve

    assert not OPERATOR.may_submit_for("stub-reasoner")
    assert OPERATOR.may_read("alpha")
    assert OPERATOR.may_resolve

    assert TRUSTED.may_submit_for("anyone")
    assert TRUSTED.may_read("anyone")
    assert TRUSTED.may_resolve


def test_refusals_carry_their_codes():
    with pytest.raises(LedgerError) as err:
        require_submitter(STUB, "alpha")
    assert (err.value.status, err.value.code) == (403, "REASONER_MISMATCH")
    assert err.value.extra == {
        "reasoner_id": "alpha",
        "key_reasoner_id": "stub-reasoner",
    }

    with pytest.raises(LedgerError) as err:
        require_submitter(OPERATOR, "alpha")
    assert err.value.code == "REASONER_KEY_REQUIRED"

    with pytest.raises(LedgerError) as err:
        require_operator(STUB)
    assert (err.value.status, err.value.code) == (403, "OPERATOR_REQUIRED")

    require_submitter(STUB, "stub-reasoner")
    require_submitter(TRUSTED, "alpha")
    require_operator(OPERATOR)


def test_api_keys_setting_parses_pairs():
    assert parse_api_keys("") == {}
    assert parse_api_keys(" a:k1 , b:k:with:colons,a:k2,") == {
        "a": ["k1", "k2"],
        "b": ["k:with:colons"],
    }


@pytest.mark.parametrize("value", ["no-colon", ":key", "id:", " : "])
def test_a_malformed_pair_is_refused(value):
    with pytest.raises(ValueError, match="reasoner_id:key"):
        parse_api_keys(value)


def test_the_key_file_maps_reasoners_to_a_key_or_keys(tmp_path: Path):
    path = tmp_path / "keys.yaml"
    path.write_text("stub-reasoner: one\nalpha: [two, three]\n", encoding="utf-8")
    assert load_api_keys_file(path) == {
        "stub-reasoner": ["one"],
        "alpha": ["two", "three"],
    }


@pytest.mark.parametrize("content", ["- a\n- b\n", "stub-reasoner: [1]\n", "a: ''\n"])
def test_a_malformed_key_file_is_refused(tmp_path: Path, content: str):
    path = tmp_path / "keys.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="API key file"):
        load_api_keys_file(path)


def test_a_missing_key_file_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot read"):
        load_api_keys_file(tmp_path / "absent.yaml")


def test_the_ring_combines_the_environment_the_file_and_the_operator_key(
    tmp_path: Path,
):
    path = tmp_path / "keys.yaml"
    path.write_text("stub-reasoner: from-file\n", encoding="utf-8")
    settings = FrameworkSettings(
        decisionledger_api_keys=SecretStr("stub-reasoner:from-env,alpha:alpha-key"),
        decisionledger_api_keys_file=str(path),
        decisionledger_operator_key=SecretStr(" op-key "),
    )

    ring = KeyRing.from_settings(settings)

    assert ring.principal("from-env") == STUB
    assert ring.principal("from-file") == STUB
    assert ring.principal("alpha-key") == Principal.for_reasoner("alpha")
    assert ring.principal("op-key") == OPERATOR


def test_no_operator_key_means_no_operator():
    ring = KeyRing.from_settings(
        FrameworkSettings(
            decisionledger_api_keys=SecretStr("a:k"),
            decisionledger_operator_key=SecretStr(""),
        )
    )
    assert ring.principal("") is None
    assert OPERATOR not in {ring.principal("k")}


def test_principal_labels_are_log_safe():
    assert STUB.label == "reasoner:stub-reasoner"
    assert OPERATOR.label == "operator"
    assert TRUSTED.label == "trusted"


@pytest.mark.parametrize(
    "content",
    [
        "stub-reasoner: s3cr3t-one\n\talpha: s3cr3t-two\n",
        "ato-reasoner: [s3cr3t-one, s3cr3t-two\nnext: s3cr3t-three\n",
    ],
)
def test_a_malformed_key_file_never_quotes_its_keys(tmp_path: Path, content: str):
    path = tmp_path / "keys.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML") as err:
        load_api_keys_file(path)
    assert "s3cr3t" not in str(err.value)
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__
