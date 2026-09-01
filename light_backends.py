"""How one composed effect becomes light on a particular keyboard.

The state machine decides *what* to show ("codex is working"); a backend
decides *how* to say it on the hardware in front of it. Keeping those apart
is what lets a second keyboard be a new backend rather than a new fork.

Two vocabularies exist on purpose:

- `EFFECT_SEMANTICS` describes an effect as (animation, colour). It is what a
  generic backend needs, because it can synthesise any colour.
- A device profile's captured `packets` are exact bytes recorded from the
  vendor driver. They cannot be synthesised -- the trailing two bytes are a
  checksum whose algorithm is not any standard CRC16 (tested) -- so a captured
  profile is the most faithful backend available and is always preferred.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

# The семantic reading of the seven effects, recovered from the K500 captures:
# byte[6] selects the animation and bytes[9:12] are RGB. Every captured packet
# agrees with the table below -- baseline's 00 FF AA is exactly the documented
# idle colour RGB(0, 255, 170) -- so this is a description of the real data,
# not an invented parallel vocabulary.
ANIMATIONS = ("single", "marquee", "solid", "breathing", "off")

EFFECT_SEMANTICS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "baseline":       ("single",    (0, 255, 170)),
    "codex_working":  ("marquee",   (0, 0, 255)),
    "claude_working": ("marquee",   (255, 85, 0)),
    "success":        ("solid",     (0, 255, 0)),
    "permission":     ("breathing", (170, 0, 255)),
    "error":          ("solid",     (255, 0, 0)),
    "off":            ("off",       (0, 0, 0)),
}


class Backend(Protocol):
    """Anything that can put one named effect onto a keyboard."""

    name: str

    def available(self) -> bool: ...
    def send(self, effect: str) -> None: ...
    def describe(self) -> str: ...


class CapturedBackend:
    """Replay the exact bytes captured from the vendor's own driver.

    Most faithful and therefore first choice: the vendor's marquee is the
    vendor's marquee, not an approximation of it. Only works for a keyboard
    that has a profile in devices/.
    """

    name = "captured"

    def __init__(self, kl: Any) -> None:
        self._kl = kl

    @property
    def device(self) -> Any:
        return self._kl.DEVICE

    def available(self) -> bool:
        return self._kl.device_present()

    def send(self, effect: str) -> None:
        # send_packet re-checks the lighting prefix, so the safety gate still
        # applies no matter which backend asked for the write.
        self._kl.send_packet(self.device.packets[effect])

    def describe(self) -> str:
        return f"captured packets for {self.device.name} ({self.device.hid_id})"


def semantics(effect: str) -> tuple[str, tuple[int, int, int]]:
    """(animation, rgb) for one effect name."""
    return EFFECT_SEMANTICS[effect]


def resolve_backend(
    kl: Any, extra: list[Backend] | None = None
) -> tuple[Backend | None, list[str]]:
    """Pick the first available backend; also report what was considered.

    Returns (backend, notes). A None backend means nothing on this machine can
    be driven -- the caller must say so loudly rather than run a service that
    silently fails every write.
    """
    candidates: list[Backend] = [CapturedBackend(kl)]
    candidates.extend(extra or [])
    notes: list[str] = []
    chosen: Backend | None = None
    for backend in candidates:
        try:
            ok = backend.available()
        except Exception as exc:
            notes.append(f"{backend.name}: unavailable ({exc!r})")
            continue
        notes.append(f"{backend.name}: {'available' if ok else 'not available'}")
        if ok and chosen is None:
            chosen = backend
    return chosen, notes
