from __future__ import annotations

from dataclasses import dataclass

from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import functional_state_bytes
from gh_slate.codec.model import MAX_REVISION, StateDraftV1, StateV1


@dataclass(frozen=True, slots=True)
class RevisionResult:
    state: StateV1
    changed: bool


def resolve_revision(
    draft: StateDraftV1,
    *,
    previous: StateV1 | None = None,
) -> RevisionResult:
    """Materialize a draft with a monotonic functional revision.

    An identical draft returns the exact previous state object. This keeps the
    caller's no-op path honest: it neither changes the revision nor rewrites
    canonical state that was already stored.
    """

    if not isinstance(draft, StateDraftV1):
        raise CodecError(
            "revision resolution requires a StateDraftV1",
            code="invalid_state",
            details={"path": "draft"},
        )
    if previous is None:
        return RevisionResult(state=draft.with_revision(1), changed=True)
    if not isinstance(previous, StateV1):
        raise CodecError(
            "previous state must be a StateV1",
            code="invalid_state",
            details={"path": "previous"},
        )
    if functional_state_bytes(previous) == functional_state_bytes(draft):
        return RevisionResult(state=previous, changed=False)
    if previous.revision == MAX_REVISION:
        raise CodecError(
            "state revision cannot be incremented beyond 2^63-1",
            code="revision_overflow",
            details={"revision": previous.revision},
        )
    return RevisionResult(
        state=draft.with_revision(previous.revision + 1),
        changed=True,
    )
