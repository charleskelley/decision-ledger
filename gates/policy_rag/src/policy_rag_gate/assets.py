"""Where the policy gate reads a reasoner's prompts and corpus (DR-33).

A reasoner the deployment composes as a plugin ships its prompt templates
and its policy corpus with the plugin (``prompts_dir``, ``corpus_dir``). A
reasoner the deployment registers without a plugin — by the registration
file or over the API — ships them under the gate's asset root
(``POLICY_RAG_GATE_ASSETS_ROOT``), one directory per reasoner::

    <root>/<reasoner_id>/prompts/*.yaml     prompt templates (not recursive)
    <root>/<reasoner_id>/corpus/**/*.md     policy documents with front matter

The gate reads a reasoner's directory once, when it first admits that
reasoner's binding (``PolicyGate.validate_binding``, at startup or when a
registration is written), and the corpus loader indexes the same
``corpus/`` directory (``python -m policy_rag_gate.corpus_loader
--reasoner-id <reasoner_id>``). A reasoner composed as a plugin is always
served from its plugin; its directory under the root, if any, is not read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from core.exceptions import GateConfigError
from policy_rag_gate.config import ReasonerProfile, corpus_jurisdictions
from policy_rag_gate.prompt_registry import load_templates

if TYPE_CHECKING:
    from pathlib import Path

    from policy_rag_gate.models import PromptTemplate

#: A reasoner id that names exactly one directory under the asset root: no
#: path separator, and not ``.`` or ``..``.
_DIRECTORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class ReasonerAssets:
    """One reasoner's asset directories under the asset root.

    Attributes:
        prompts_dir: ``<root>/<reasoner_id>/prompts``.
        corpus_dir: ``<root>/<reasoner_id>/corpus``.
    """

    prompts_dir: Path
    corpus_dir: Path


def reasoner_assets(root: Path, reasoner_id: str) -> ReasonerAssets:
    """Locate a reasoner's directories under the asset root (nothing is read).

    Args:
        root: The asset root.
        reasoner_id: The reasoner.

    Returns:
        Its prompts and corpus directories (which may not exist).

    Raises:
        ValueError: ``reasoner_id`` cannot name a directory under the root
            (a path separator, ``..``, or another character outside letters,
            digits, ``.``, ``_``, and ``-``).
    """
    if not _DIRECTORY_NAME.fullmatch(reasoner_id):
        msg = (
            f"reasoner id {reasoner_id!r} cannot name a directory under the asset "
            "root (letters, digits, '.', '_', and '-' only, starting with a "
            "letter or digit)"
        )
        raise ValueError(msg)
    base = root / reasoner_id
    return ReasonerAssets(prompts_dir=base / "prompts", corpus_dir=base / "corpus")


def load_root_assets(
    root: Path, reasoner_id: str
) -> tuple[ReasonerProfile, dict[str, tuple[PromptTemplate, Path]]]:
    """Read and check one reasoner's prompts and corpus from the asset root.

    Args:
        root: The asset root.
        reasoner_id: The reasoner being admitted.

    Returns:
        The reasoner's profile (its template ids and the jurisdictions its
        corpus covers) and its templates, ready for the prompt registry.

    Raises:
        GateConfigError: The reasoner id cannot name a directory; its
            ``prompts/`` or ``corpus/`` directory is missing or cannot be
            read; a template is malformed, or two of its templates declare
            one id; it ships no template; or no policy document under
            ``corpus/`` parses. Every problem with the files is this error,
            never an ``OSError`` or a parser's own.
    """
    try:
        assets = reasoner_assets(root, reasoner_id)
    except ValueError as err:
        raise GateConfigError(str(err)) from err
    for kind, directory in (
        ("prompts", assets.prompts_dir),
        ("corpus", assets.corpus_dir),
    ):
        try:
            present = directory.is_dir()
        except OSError as err:  # unreadable parent, a name too long, ...
            msg = (
                f"the {kind} directory of reasoner {reasoner_id!r} under the "
                f"asset root ({directory}) cannot be read: {err}"
            )
            raise GateConfigError(msg) from err
        if not present:
            msg = (
                f"reasoner {reasoner_id!r} has no {kind} directory under the "
                f"asset root ({directory}); a reasoner registered without a "
                "plugin ships <root>/<reasoner_id>/prompts/*.yaml and "
                "<root>/<reasoner_id>/corpus/**/*.md"
            )
            raise GateConfigError(msg)
    try:
        templates = load_templates(assets.prompts_dir)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
    ) as err:
        msg = (
            f"the prompt templates of reasoner {reasoner_id!r} under "
            f"{assets.prompts_dir} do not load: {type(err).__name__}: {err}"
        )
        raise GateConfigError(msg) from err
    if not templates:
        msg = (
            f"reasoner {reasoner_id!r} ships no prompt template "
            f"({assets.prompts_dir} holds no *.yaml file)"
        )
        raise GateConfigError(msg)
    try:
        jurisdictions = corpus_jurisdictions(assets.corpus_dir)
    except OSError as err:  # a subdirectory the walk cannot read
        msg = (
            f"the corpus of reasoner {reasoner_id!r} under {assets.corpus_dir} "
            f"cannot be read: {err}"
        )
        raise GateConfigError(msg) from err
    if not jurisdictions:
        msg = (
            f"no policy document of reasoner {reasoner_id!r} under "
            f"{assets.corpus_dir} has front matter that parses"
        )
        raise GateConfigError(msg)
    profile = ReasonerProfile(
        template_ids=frozenset(templates), jurisdictions=jurisdictions
    )
    return profile, templates
