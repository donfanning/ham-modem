"""AMTOR mode A / SITOR-A (ITU-R M.476-5 §3.1): the ARQ link.

A 450 ms cycle. The master (the station that called) owns the clock. The information sending station (ISS) sends a
block of three characters (210 ms); the information receiving station (IRS) answers with one control character
(70 ms) -- CS1 or CS2 name the block number it wants next (so a repeat request repeats the number), CS3 asks to change
over. The slave answers a fixed time after what it hears; the master transmits at the start of every cycle.

This module follows the Recommendation's rules: block numbering (1/2), repeat on a mutilated reply with a block of
three RQ ("signal repetition"), the two-block selective call (c1 RQ c2 / c3 c4 RQ), change-over with the
FIGS + ? sequence and CS3, the idle-beta fill, end of communication with idle-alpha, and the time-outs.

Over VoIP the master's 240 ms listening window has to hold the slave's reaction time plus the audio round trip.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from . import code, fsk
from .burst import Burst, BurstReceiver

CH = 7 * fsk.SPB                    # one character (70 ms)
BLOCK = 3 * CH
CYCLE = 450 * fsk.FS // 1000
TO_STANDBY_CYCLES = 64              # continuous repetition before giving up (§3.1.9.1)
CALL_CYCLES = 32
END_TRIES = 4
TURN_SEQ = [code.FIGS, 0x63, 0x72]  # FIGS, '+', '?'


def selcal(callsign: str) -> str:
    """Four-letter selective call from a callsign (amateur convention: first letter plus the suffix letters,
    padded with the first letter): W1AW -> WWAW, G3PLX -> GPLX."""
    letters = [c for c in callsign.upper() if c.isalpha()]
    suffix = []
    for c in reversed(callsign.upper()):
        if c.isalpha():
            suffix.insert(0, c)
        elif suffix:
            break
    first = letters[0] if letters else "X"
    sel = [first] + suffix
    while len(sel) < 4:
        sel.insert(1, first)
    return "".join(sel[:4])


class ArqStation:
    def __init__(self, mycall: str, fs: int = fsk.FS, center: float = fsk.CENTER, shift: float = fsk.SHIFT,
                 level: int = fsk.LEVEL, reply_ms: int = 50, on_text: Callable[[str], None] | None = None,
                 on_event: Callable[[str], None] | None = None) -> None:
        if fs != fsk.FS:
            raise ValueError("AMTOR runs at 8 kHz")
        self.mycall, self.selcal = mycall.upper(), selcal(mycall)
        self.center, self.shift, self.level = center, shift, level
        self.reply = reply_ms * fsk.FS // 1000
        self.on_text, self.on_event = on_text, on_event
        self.rx = BurstReceiver(center, shift)
        self.now = 0
        self._tx: list[tuple[int, np.ndarray]] = []
        self.state = "standby"          # standby calling iss irs ending
        self.master = False
        self.remote = ""
        self.queue: list[int] = []
        self.figures = False
        self.decoder = code.Decoder()
        self.events: list[str] = []
        self.stats = {"blocks_sent": 0, "repeats": 0, "rq": 0, "blocks_ok": 0, "blocks_bad": 0}
        self.listen = True
        self._reset_link()

    # ------------------------------------------------------------------------------------ host API

    def connect(self, call: str) -> None:
        """Start a selective call to ``call`` (a callsign, or a four-letter selcal given in capitals)."""
        sel = call.upper() if len(call) == 4 and call.isalpha() else selcal(call)
        self._call_chars = [[sel[0], code.RQ, sel[1]], [sel[2], sel[3], code.RQ]]
        self._call_chars = [[ord_to_code(x) for x in blk] for blk in self._call_chars]
        self._reset_link()
        self.master, self.state, self.remote = True, "calling", call.upper()
        self._next_cycle = self.now + fsk.FS // 10
        self._event(f"CALLING {self.remote}")

    def send(self, text: str) -> None:
        codes, self.figures = code.encode_text(text, self.figures)
        self.queue += codes

    def turn(self) -> None:
        """Ask to change direction (ISS sends FIGS + ?, IRS answers its next block with CS3)."""
        if self.state == "iss":
            self.queue += TURN_SEQ
            self.figures = True
        elif self.state == "irs":
            self._want_turn = True

    def disconnect(self) -> None:
        if self.state == "iss":
            self._end_tries = 0
            self._end_when_idle = True
        elif self.state == "irs":
            self.turn()
            self._then_end = True

    @property
    def keyed(self) -> bool:
        return any(s + len(w) > self.now for s, w in self._tx)

    # ------------------------------------------------------------------------------------- audio

    def process(self, block: np.ndarray) -> np.ndarray:
        n = len(block)
        for b in self.rx.feed(np.asarray(block, dtype=float)):
            self._on_burst(b)
        end = self.now + n
        while self.master and self.state != "standby" and self._next_cycle < end:
            self._master_cycle(self._next_cycle)
            self._next_cycle += CYCLE
        out = np.zeros(n, np.int16)
        keep = []
        for s, w in self._tx:
            lo, hi = max(s, self.now), min(s + len(w), end)
            if hi > lo:
                out[lo - self.now:hi - self.now] = w[lo - s:hi - s]
            if s + len(w) > end:
                keep.append((s, w))
        self._tx = keep
        self.now = end
        self._watchdog()
        return out

    # ----------------------------------------------------------------------------------- internals

    def _reset_link(self) -> None:
        self._next_cycle = 0
        self._b = None                  # ISS: number of the block last sent (1/2)
        self._last_block: list[int] = []
        self._exp = 1                   # IRS: number of the block it expects
        self._heard: object = "none"    # master ISS: what came back this cycle: "none", "bad", 1, 2, 3
        self._cs_run: list[int] = []    # master calling: consecutive control signals
        self._pending_cs = code.CS1     # master IRS: control signal to send at the next cycle
        self._cycles_quiet = 0
        self._call_n = 0
        self._call_seen: list[int] = []
        self._want_turn = False
        self._then_end = False
        self._end_when_idle = False
        self._handover = ""
        self._end_tries = 0
        self._last_valid = self.now
        self._recent_text: list[int] = []

    def _event(self, text: str) -> None:
        self.events.append(text)
        if self.on_event:
            self.on_event(text)

    def _text(self, s: str) -> None:
        if s and self.on_text:
            self.on_text(s)

    def _burst_audio(self, chars: list[int]) -> np.ndarray:
        m = fsk.Modulator(self.center, self.shift, self.level)
        return m.bits([b for c in chars for b in code.bits(c)])

    def _transmit(self, start: int, chars: list[int]) -> None:
        self._tx.append((max(start, self.now), self._burst_audio(chars)))

    def _take3(self) -> list[int]:
        blk = self.queue[:3]
        del self.queue[:3]
        return blk + [code.IDLE_B] * (3 - len(blk))

    # ISS side ----------------------------------------------------------------------------------

    def _iss_block(self, heard) -> list[int]:
        """What the ISS sends next, from what it heard back (1, 2, 3, "bad" or "none")."""
        if self._end_when_idle and not self.queue and heard in (1, 2):    # everything queued has been sent
            self._end_when_idle, self.state = False, "ending"
        if self.state == "ending":
            if self._end_tries >= 1 and heard in (1, 2, 3):          # the IRS confirmed the end
                self._to_standby("ended")
                return []
            self._end_tries += 1
            if self._end_tries > END_TRIES:
                self._to_standby("ended, no confirmation")
                return []
            return [code.IDLE_A] * 3
        if heard == 3:
            self._handover = "wait"
            return [code.IDLE_B, code.IDLE_A, code.IDLE_B]
        if heard in (1, 2):
            if self._b is not None and heard == self._b and self._last_block:
                self.stats["repeats"] += 1
                return self._last_block
            self._b, self._last_block = heard, self._take3()
            self.stats["blocks_sent"] += 1
            return self._last_block
        if self._handover == "wait":
            return [code.IDLE_B, code.IDLE_A, code.IDLE_B]
        if self._b is None:
            return [code.RQ] * 3 if heard == "bad" else []
        self.stats["rq"] += 1
        return [code.RQ] * 3

    # IRS side ----------------------------------------------------------------------------------

    def _irs_reply(self, chars: list) -> int:
        """Control signal for a received block, updating the expected block number and delivering text."""
        good = all(c is not None for c in chars) and code.RQ not in chars
        if not good:
            self.stats["blocks_bad"] += 1
            return code.CS1 if self._exp == 1 else code.CS2
        self.stats["blocks_ok"] += 1
        for c in chars:
            if c in (code.IDLE_B, code.IDLE_A):
                continue
            self._text(self.decoder.char(c))
            if c not in (code.LTRS, code.FIGS):
                self._recent_text = (self._recent_text + [c])[-3:]
            else:
                self._recent_text = (self._recent_text + [c])[-3:]
        if chars == [code.IDLE_A] * 3:
            cs = code.CS1 if self._exp == 2 else code.CS2          # acknowledge the end of communication
            self._to_standby("ended by remote")
            return cs
        turn = self._recent_text[-3:] == TURN_SEQ or self._want_turn
        cs = code.CS1 if self._exp == 2 else code.CS2
        self._exp = 2 if self._exp == 1 else 1
        if turn:
            self._want_turn = False
            self._recent_text = []
            self._handover = "to_iss_wait"
            return code.CS3
        return cs

    # receiving ------------------------------------------------------------------------------------

    def _on_burst(self, b: Burst) -> None:
        n = len(b.chars)
        reply_at = b.end + self.reply
        if self.state == "standby":
            if self.listen and n == 3:
                self._standby_block(b, reply_at)
            return
        self._last_valid = self.now
        if self.state == "calling":
            if n == 1 and b.chars[0] in (code.CS1, code.CS2):
                self._cs_run = (self._cs_run + [b.chars[0]])[-2:]
                if len(self._cs_run) == 2 and self._cs_run[0] == self._cs_run[1]:
                    self.state = "iss"
                    self._heard = 1 if self._cs_run[1] == code.CS1 else 2
                    self._event(f"LINKED {self.remote}")
            return
        if self.state in ("iss", "ending"):
            if self.master:
                if n == 1:
                    cs = b.chars[0]
                    self._heard = {code.CS1: 1, code.CS2: 2, code.CS3: 3}.get(cs, "bad")
                elif n == 3 and self._handover == "wait" and code.RQ in b.chars:
                    self._become_irs()
                return
            # slave ISS: reacts to the master's control character
            if n == 1:
                cs = b.chars[0]
                if cs == code.RQ and self._handover == "wait":      # the new ISS's signal repetition: we are the IRS now
                    self._become_irs()
                    self._transmit(reply_at, [code.CS1])
                    return
                heard = {code.CS1: 1, code.CS2: 2, code.CS3: 3}.get(cs, "bad")
                blk = self._iss_block(heard)
                if blk:
                    self._transmit(reply_at, blk)
            return
        if self.state == "irs":
            if n == 3:
                self._irs_block(b, reply_at)

    def _irs_block(self, b: Burst, reply_at: int) -> None:
        if self._handover == "to_iss_wait" and b.chars == [code.IDLE_B, code.IDLE_A, code.IDLE_B]:
            self._become_iss(reply_at)
            return
        was_master = self.master
        cs = self._irs_reply(b.chars)
        if self.state == "standby":                                  # the remote ended the link: confirm once
            self._transmit(self._next_cycle if was_master else reply_at, [cs])
        elif was_master:
            self._pending_cs = cs
        else:
            self._transmit(reply_at, [cs])

    def _become_iss(self, reply_at: int) -> None:
        self.state, self._b, self._last_block, self._handover = "iss", None, [], ""
        self.queue.insert(0, code.LTRS)                              # establish the case for the new receiver
        self.figures = False
        self._event("CHANGEOVER to ISS")
        if self.master:
            self._pending_rq = True        # one signal repetition, sent at the next cycle
        else:
            self._transmit(reply_at, [code.RQ] * 3)

    def _become_irs(self) -> None:
        self.state, self._exp, self._handover = "irs", 1, ""
        self.decoder = code.Decoder()
        self._pending_cs = code.CS1
        self._event("CHANGEOVER to IRS")

    def _standby_block(self, b: Burst, reply_at: int) -> None:
        c = b.chars
        me = [ord_to_code(x) for x in self.selcal]
        if c[1] == code.RQ and c[0] is not None and c[2] is not None and code.RQ not in (c[0], c[2]):
            self._call_seen = [c[0], c[2]]
        elif c[2] == code.RQ and None not in c and code.RQ not in c[:2] and len(self._call_seen) == 2:
            if self._call_seen + c[:2] == me:
                self._reset_link()
                self.master, self.state, self._exp = False, "irs", 1
                self._last_valid = self.now
                self._transmit(reply_at, [code.CS1])
                self.remote = "?"
                self._event("CALLED")
            self._call_seen = []
        else:
            self._call_seen = []

    # master clock --------------------------------------------------------------------------------

    def _master_cycle(self, t: int) -> None:
        if self.state == "calling":
            self._cycles_quiet += 1
            if self._cycles_quiet > CALL_CYCLES:
                self._to_standby("no answer")
                return
            self._transmit(t, self._call_chars[self._call_n % 2])
            self._call_n += 1
        elif self.state in ("iss", "ending"):
            heard, self._heard = self._heard, "none"
            self._cycles_quiet = self._cycles_quiet + 1 if heard in ("none", "bad") else 0
            if self._cycles_quiet > TO_STANDBY_CYCLES:
                self._to_standby("link lost")
                return
            blk = self._iss_block(heard)
            if getattr(self, "_pending_rq", False):
                self._pending_rq = False
                self._transmit(t, [code.RQ])
            elif blk:
                self._transmit(t, blk)
        elif self.state == "irs":
            self._transmit(t, [self._pending_cs])

    def _watchdog(self) -> None:
        if self.state in ("irs", "iss") and not self.master and self.now - self._last_valid > TO_STANDBY_CYCLES * CYCLE:
            self._to_standby("link lost")

    def _to_standby(self, why: str) -> None:
        self.state = "standby"
        self.master = False
        self._event(f"STANDBY ({why})")


def ord_to_code(x) -> int:
    return x if isinstance(x, int) else code._LETTERS[x]
