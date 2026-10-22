"""AMTOR mode B / SITOR-B (ITU-R M.476-5 §3.2): forward error correction by time diversity.

Every character is sent twice: first in a DX slot, then in an RX slot five character slots (350 ms start to start,
280 ms of other characters in between) later; DX and RX slots alternate. Messages start with at least four pairs of
phasing signals (phasing 2 = RQ code in the DX slot, phasing 1 = idle-alpha code in the RX slot), idle between
messages is idle-beta in the DX slot, and the end of emission is three idle-alpha in consecutive DX slots.

``CharReceiver`` finds the bit phase and character alignment from the constant-weight code alone and yields one
code per 70 ms slot (None where the 4-of-7 check failed); ``FecReceiver`` pairs DX and RX copies; ``FecTransmitter``
schedules the slots.
"""

from __future__ import annotations

import numpy as np

from . import code, fsk

SLOT = 7 * fsk.SPB                  # samples per character (560 = 70 ms)
SEARCH_BITS = 160                   # bits examined when looking for a signal
LOCK_RUN = 10                       # consecutive valid characters that establish alignment
PAIRS = 6                           # phasing pairs sent (the Recommendation asks for at least four)


class CharReceiver:
    def __init__(self, center: float = fsk.CENTER, shift: float = fsk.SHIFT) -> None:
        self.disc = fsk.Discriminator(center, shift)
        self._d = np.zeros(0)
        self._base = 0                  # absolute index of _d[0]
        self.locked = False
        self._t = 0                     # absolute sample where the next character starts (locked)
        self._since_search = 0
        self._recent: list[bool] = []

    def feed(self, x: np.ndarray) -> list[int | None]:
        self._d = np.concatenate((self._d, self.disc.process(x)))
        self._since_search += len(x)
        out: list[int | None] = []
        if not self.locked and self._since_search >= SLOT // 2:
            self._since_search = 0
            self._acquire()
        if self.locked:
            out = self._decode()
        keep = (self._t - self._base - 4 * SLOT) if self.locked else len(self._d) - (SEARCH_BITS + 8) * fsk.SPB
        if keep > 0:
            self._d = self._d[keep:]
            self._base += keep
        return out

    # ------------------------------------------------------------------------------------ acquisition

    def _acquire(self) -> None:
        nb = SEARCH_BITS
        need = (nb + 2) * fsk.SPB
        if len(self._d) < need:
            return
        seg = self._d[len(self._d) - need - fsk.SPB if len(self._d) > need + fsk.SPB else 0:]
        off = len(self._d) - len(seg)
        cs = np.concatenate(([0.0], np.cumsum(seg)))
        nb = (len(seg) - fsk.SPB) // fsk.SPB - 1
        p, _ = fsk.best_phase(cs, 0, nb)
        bits = (fsk.bit_sums(cs, p, nb) > 0).astype(int)
        best = None
        for a in range(7):
            run = 0
            nchar = (nb - a) // 7
            for k in range(nchar):
                c = code.from_bits(bits[a + 7 * k:a + 7 * k + 7])
                if code.is_valid(c):
                    run += 1
                    if run >= LOCK_RUN and (best is None or run > best[2]):
                        best = (a, k, run)
                else:
                    run = 0
        if best is None:
            return
        a, k, run = best
        # decoding starts at the first character of the valid run (the phasing signals at the head of a message)
        self._t = self._base + off + p + (a + 7 * (k - run + 1)) * fsk.SPB
        self.locked = True
        self._recent = [True] * 8

    # ---------------------------------------------------------------------------------------- decoding

    def _decode(self) -> list[int | None]:
        out: list[int | None] = []
        while True:
            lo = self._t - self._base
            if lo < 2 * fsk.SPB or lo + SLOT + 2 * fsk.SPB > len(self._d):
                break
            cs = np.concatenate(([0.0], np.cumsum(self._d[lo - 2 * fsk.SPB:lo + SLOT + 2 * fsk.SPB])))
            base = 2 * fsk.SPB
            eyes = {dl: float(np.abs(fsk.bit_sums(cs, base + dl, 7)).sum()) for dl in (-2, -1, 0, 1, 2)}
            dl = max(eyes, key=eyes.get)
            if eyes[dl] < eyes[0] * 1.05:
                dl = 0
            s = fsk.bit_sums(cs, base + dl, 7)
            c = code.from_bits((s > 0).astype(int))
            valid = code.is_valid(c)
            out.append(c if valid else None)
            self._recent = (self._recent + [valid])[-16:]
            self._t += SLOT + dl
            if len(self._recent) >= 16 and sum(self._recent) < 8:
                self.locked = False                    # signal gone or timing lost: search again
                self._since_search = SLOT
                break
        return out


class FecReceiver:
    """Characters -> text. Collective B-mode only (selective calling inverts the code and is not implemented)."""

    def __init__(self, center: float = fsk.CENTER, shift: float = fsk.SHIFT) -> None:
        self.chars = CharReceiver(center, shift)
        self.text = code.Decoder()
        self._slots: list[int | None] = []
        self._dx = -1                    # parity (0/1) of the slot index that carries DX characters; -1 unknown
        self._n = 0
        self.events: list[str] = []
        self.corrected = self.lost = 0   # characters saved by the RX copy / lost in both copies

    def feed(self, x: np.ndarray) -> str:
        was = self.chars.locked
        out = []
        for c in self.chars.feed(x):
            out.append(self._slot(c))
        if was and not self.chars.locked:
            self._reset()
        return "".join(out)

    def _reset(self) -> None:
        self._slots.clear()
        self._dx = -1
        self._n = 0
        self.text = code.Decoder()

    def _slot(self, c: int | None) -> str:
        self._slots = (self._slots + [c])[-12:]
        n = self._n
        self._n += 1
        if self._dx < 0:
            # phasing: RQ code (phasing 2) in DX slots, idle-alpha (phasing 1) in RX slots
            if len(self._slots) >= 6:
                tail = self._slots[-6:]
                for par in (0, 1):
                    dxs = [tail[i] for i in range(6) if (n - 5 + i) % 2 == par]
                    rxs = [tail[i] for i in range(6) if (n - 5 + i) % 2 != par]
                    if all(v == code.PHASE_2 for v in dxs) and all(v == code.PHASE_1 for v in rxs):
                        self._dx = par
                        self.events.append("phased")
                        break
            return ""
        if n % 2 == self._dx or len(self._slots) < 6:
            return ""                                    # act when the RX copy arrives
        return self._char(self._slots[-6], self._slots[-1])

    def _char(self, dx: int | None, rx: int | None) -> str:
        c = dx if dx is not None else rx
        if c is None:
            if sum(v is not None for v in self._slots[-5:-1]) < 2:       # nothing around it: no signal, not an error
                return ""
            self.lost += 1
            return self.text.char(None)
        if dx is None:
            self.corrected += 1
        if c in (code.PHASE_1, code.PHASE_2, code.IDLE_B):    # phasing, idle, end-of-emission (idle-alpha)
            return ""
        return self.text.char(c)


class FecTransmitter:
    """Schedules character slots: phasing, the queued text with each character repeated five slots later, then the
    end of emission signal. ``next_slot()`` returns the code for the next 70 ms slot, None when the carrier is off."""

    HOLD_DX_SLOTS = 20                  # idle-beta slots (2.8 s) kept after the queue empties, before ending

    def __init__(self) -> None:
        self.queue: list[int] = []
        self.figures = False
        self._hist: list[int] = []
        self._n = 0                     # slot counter; even slots are DX
        self._state = "off"
        self._pairs = self._end = self._hold = 0

    def send(self, text: str) -> None:
        codes, self.figures = code.encode_text(text, self.figures)
        self.queue += codes

    @property
    def active(self) -> bool:
        return self._state != "off"

    def next_slot(self) -> int | None:
        if self._state == "off":
            if not self.queue:
                return None
            self._state, self._pairs, self._n, self._hist = "phasing", 0, 0, []
        if self._state == "stopping":
            self._state = "off"
            return None
        if self._n % 2 == 0:                                       # DX slot
            if self._state == "phasing":
                c = code.PHASE_2
                self._pairs += 1
                if self._pairs >= PAIRS:
                    self._state, self._hold = "text", 0
            elif self._state == "text":
                if self.queue:
                    c, self._hold = self.queue.pop(0), 0
                elif self._hold < self.HOLD_DX_SLOTS:
                    c, self._hold = code.IDLE_B, self._hold + 1
                else:
                    c, self._state, self._end = code.IDLE_A, "end", 1
            else:                                                  # end of emission: three idle-alpha, DX only
                c, self._end = code.IDLE_A, self._end + 1
                if self._end >= 3:
                    self._state = "stopping"
        else:                                                      # RX slot
            c = code.PHASE_1 if self._state == "phasing" or len(self._hist) < 5 else self._hist[-5]
        self._hist = (self._hist + [c])[-8:]
        self._n += 1
        return c
