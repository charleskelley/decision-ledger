"""Gate layer — universal contracts for any gate kind in DecisionLedger.

Top-level module defines the universal contracts every gate kind
satisfies: ``GateInput``, ``GateOutput``, ``GateVerdict``. Plus framework
corpus metadata (``DocumentType``, ``PolicyDocument``) used by retrieving
gates of any kind.

Per DR-20, gate-implementation-specific types live under
``core.gate.<kind>/`` subpackages — concrete subclasses, kind-specific
sub-records, and per-kind protocols. The reference LLM-backed policy gate
lives at ``core.gate.policy``. Concrete-type imports go through the
relevant subpackage explicitly: ``from core.gate.policy import
PolicyGateInput`` (not via this module's surface).

``GateRoute`` is at the framework root in ``core.routes`` — the routing
concept is produced upstream of the gate by the domain reasoner and
consumed downstream by the gate, so it lives where both can import it
without crossing subpackage boundaries.

Public API at this level — universal contracts only:
    GateInput:       Universal input contract — base for per-kind subclasses.
    GateOutput:      Universal output contract — base for per-kind subclasses.
    GateVerdict:     Universal verdict — what enforcement consumes.
    DocumentType:    Document type enum (used by retrieval corpus metadata).
    PolicyDocument:  Metadata schema for a versioned corpus document.
"""

from core.gate.input import GateInput
from core.gate.output import GateOutput
from core.gate.policy.corpus import DocumentType, PolicyDocument
from core.gate.verdict import GateVerdict

__all__ = [
    "DocumentType",
    "GateInput",
    "GateOutput",
    "GateVerdict",
    "PolicyDocument",
]
