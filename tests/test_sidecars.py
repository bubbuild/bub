from __future__ import annotations

from bub.builtin.spill import SpillSettings, SpillStore
from bub.sidecars import (
    CommittedTapeSidecar,
    ForkOverlaySidecar,
    SiblingTapeSidecar,
    TapeOverlaySidecar,
)
from bub.standards import OpenTelemetrySidecar


def test_builtin_tape_capabilities_implement_sidecar_specs() -> None:
    spill = SpillStore(SpillSettings())
    overlay = ForkOverlaySidecar()
    otel = OpenTelemetrySidecar()

    assert isinstance(spill, SiblingTapeSidecar)
    assert isinstance(overlay, TapeOverlaySidecar)
    assert isinstance(otel, CommittedTapeSidecar)
