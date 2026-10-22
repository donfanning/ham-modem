"""AX.25 v2.0 connected mode (LAPB-style, modulo 8): the data-link state machine behind CONNECT.

One ``Link`` per remote station, owned by a ``LinkManager`` for one local callsign. I/O-free: frames go out
through ``send(raw_frame_bytes)``, frames heard come in through ``LinkManager.receive(frame)``, and timers run
from ``tick()`` against the manager's ``clock``; events are reported to the owner's callbacks.

States follow the AX.25 2.0/2.2 SDL in simplified form:

    DISCONNECTED -> (SABM sent) AWAITING_CONNECTION -> (UA) CONNECTED <-> TIMER_RECOVERY
    CONNECTED -> (DISC sent) AWAITING_RELEASE -> (UA/DM) DISCONNECTED

Error recovery: go-back-N on REJ; on T1 expiry the link polls (RR command with P=1) and retransmits from the
peer's N(R) when the final response arrives; N2 unanswered polls fail the link. T3 polls an idle link.
Received I frames are acknowledged after a short delay (T2) so several can share one RR, or piggybacked on our
own I frames. Not implemented: modulo-128 (SABME), SREJ, segmentation, XID negotiation.
"""

from __future__ import annotations

import logging
import time
from collections import deque

from . import ax25
from .ax25 import Address, Ax25Error, Frame

log = logging.getLogger("hammodem.ax25")

DISCONNECTED, AWAITING_CONNECTION, CONNECTED, TIMER_RECOVERY, AWAITING_RELEASE = (
    "disconnected", "awaiting connection", "connected", "timer recovery", "awaiting release")


class LinkParams:
    def __init__(self, frack: float = 4.0, maxframe: int = 4, paclen: int = 128, retry: int = 10,
                 t2: float = 0.5, t3: float = 180.0) -> None:
        self.frack, self.maxframe, self.paclen, self.retry, self.t2, self.t3 = frack, maxframe, paclen, retry, t2, t3


def _between(a: int, b: int, c: int) -> bool:
    """a <= b <= c modulo 8."""
    return (b - a) % 8 <= (c - a) % 8


class Link:
    def __init__(self, mgr: "LinkManager", remote: Address, path: list[Address]) -> None:
        self.mgr, self.remote, self.path = mgr, remote, path
        self.state = DISCONNECTED
        self._reset()
        self.t1_at: float | None = None
        self.t2_at: float | None = None
        self.t3_at: float | None = None
        self.retries = 0
        self.tx_queue: deque[bytes] = deque()          # data waiting for a send window
        self.stats = {"i_sent": 0, "i_resent": 0, "i_received": 0, "rej_sent": 0}

    def _reset(self) -> None:
        self.vs = self.vr = self.va = 0
        self.unacked: dict[int, bytes] = {}             # N(S) -> info field
        self.rej_sent = False
        self.peer_busy = False

    @property
    def name(self) -> str:
        return str(self.remote)

    @property
    def outstanding(self) -> int:
        return len(self.unacked) + len(self.tx_queue)

    # ------------------------------------------------------------------ user requests

    def connect(self) -> None:
        self._reset()
        self.retries = 0
        self.state = AWAITING_CONNECTION
        self._send_u(ax25.SABM, poll=True)
        self._start_t1()

    def disconnect(self) -> None:
        if self.state == DISCONNECTED:
            return
        if self.state == AWAITING_CONNECTION:
            self._finish("connection cancelled")
            return
        self.tx_queue.clear()
        self.retries = 0
        self.state = AWAITING_RELEASE
        self._send_u(ax25.DISC, poll=True)
        self._start_t1()

    def write(self, data: bytes) -> None:
        """Queue user data; it is cut into PACLEN-sized I frames."""
        p = self.mgr.params.paclen
        for i in range(0, len(data), p):
            self.tx_queue.append(data[i:i + p])
        self._push()

    # ------------------------------------------------------------------ frames from the peer

    def receive(self, f: Frame) -> None:
        kind = f.kind
        if kind == "SABM":
            self._on_sabm(f)
        elif kind == "DISC":
            self._send_u(ax25.UA if self.state != DISCONNECTED else ax25.DM, poll=f.poll, command=False)
            if self.state != DISCONNECTED:
                self._finish("disconnected by peer")
        elif kind == "UA":
            if self.state == AWAITING_CONNECTION:
                self.state = CONNECTED
                self._stop_t1()
                self._start_t3()
                self.mgr.on_connect(self, False)
            elif self.state == AWAITING_RELEASE:
                self._finish("disconnected")
        elif kind == "DM":
            if self.state == AWAITING_CONNECTION:
                self._finish("busy")
            elif self.state in (CONNECTED, TIMER_RECOVERY, AWAITING_RELEASE):
                self._finish("disconnected" if self.state == AWAITING_RELEASE else "disconnected by peer (DM)")
        elif kind == "FRMR":
            if self.state in (CONNECTED, TIMER_RECOVERY):
                log.warning("%s: FRMR from peer, re-establishing", self.name)
                self.connect()
        elif self.state in (CONNECTED, TIMER_RECOVERY):
            if kind == "I":
                self._on_i(f)
            elif kind in ("RR", "RNR", "REJ"):
                self._on_s(f)
        elif f.command and f.poll and self.state == DISCONNECTED:
            self._send_u(ax25.DM, poll=True, command=False)       # we have no link: tell the peer
        self._push()

    def _on_sabm(self, f: Frame) -> None:
        if self.state in (CONNECTED, TIMER_RECOVERY):
            self._reset()                                         # link reset by the peer
            self._send_u(ax25.UA, poll=f.poll, command=False)
            self.state = CONNECTED
            self._stop_t1()
            self._start_t3()
            return
        if self.state == AWAITING_RELEASE:
            self._send_u(ax25.DM, poll=f.poll, command=False)
            return
        if not self.mgr.accept_incoming(self):
            self._send_u(ax25.DM, poll=f.poll, command=False)
            return
        was_connecting = self.state == AWAITING_CONNECTION       # both sides sent SABM at once
        self._reset()
        self._send_u(ax25.UA, poll=f.poll, command=False)
        self.state = CONNECTED
        self._stop_t1()
        self._start_t3()
        self.mgr.on_connect(self, not was_connecting)

    def _on_i(self, f: Frame) -> None:
        if not self._ack_upto(f.nr):
            return
        if f.ns == self.vr:
            self.vr = (self.vr + 1) % 8
            self.rej_sent = False
            self.stats["i_received"] += 1
            self.mgr.on_data(self, f.info)
            if f.poll:
                self._send_s(ax25.RR, final=True)
            elif self.t2_at is None:
                self.t2_at = self.mgr.now + self.mgr.params.t2      # delayed ack, may piggyback
        else:
            if not self.rej_sent:
                self.rej_sent = True
                self.stats["rej_sent"] += 1
                self._send_s(ax25.REJ, final=f.poll)
            elif f.poll:
                self._send_s(ax25.RR, final=True)

    def _on_s(self, f: Frame) -> None:
        kind = f.kind
        self.peer_busy = kind == "RNR"
        if f.command and f.poll:
            self._send_s(ax25.RR, final=True)                    # answer an enquiry
        if not self._ack_upto(f.nr):
            return
        if self.state == TIMER_RECOVERY and not f.command and f.poll:
            # Final response to our poll: back to normal, resend whatever is still outstanding.
            self.state = CONNECTED
            self._stop_t1()
            self._start_t3()
            self._go_back(f.nr)
        elif kind == "REJ":
            self._go_back(f.nr)

    def _ack_upto(self, nr: int) -> bool:
        if not _between(self.va, nr, self.vs):
            log.warning("%s: N(R)=%d outside V(A)=%d..V(S)=%d, re-establishing", self.name, nr, self.va, self.vs)
            self.connect()
            return False
        while self.va != nr:
            self.unacked.pop(self.va, None)
            self.va = (self.va + 1) % 8
        if self.state == CONNECTED:
            if self.unacked:
                self._start_t1()
            else:
                self._stop_t1()
                self._start_t3()
        return True

    def _go_back(self, nr: int) -> None:
        """Retransmit every unacknowledged I frame from N(R) on."""
        resend = [(n, self.unacked[n]) for n in ((nr + i) % 8 for i in range((self.vs - nr) % 8))
                  if n in self.unacked]
        for ns, info in resend:
            self._send_i(ns, info)
            self.stats["i_resent"] += 1
        if resend:
            self._start_t1()

    # ------------------------------------------------------------------ timers

    def tick(self, now: float) -> None:
        if self.t2_at is not None and now >= self.t2_at:
            self.t2_at = None
            if self.state in (CONNECTED, TIMER_RECOVERY):
                self._send_s(ax25.RR)
        if self.t1_at is not None and now >= self.t1_at:
            self.t1_at = None
            self._t1_expired()
        if self.t3_at is not None and now >= self.t3_at and self.state == CONNECTED:
            self.t3_at = None
            self.retries = 0
            self._enquire()

    def _t1_expired(self) -> None:
        n2 = self.mgr.params.retry
        if self.state == AWAITING_CONNECTION:
            if self.retries >= n2:
                self._finish("retry count exceeded")
                return
            self.retries += 1
            self._send_u(ax25.SABM, poll=True)
            self._start_t1()
        elif self.state == AWAITING_RELEASE:
            if self.retries >= n2:
                self._finish("disconnected (no reply to DISC)")
                return
            self.retries += 1
            self._send_u(ax25.DISC, poll=True)
            self._start_t1()
        elif self.state == CONNECTED:
            self.retries = 0
            self._enquire()
        elif self.state == TIMER_RECOVERY:
            if self.retries >= n2:
                self._send_u(ax25.DM, poll=False, command=False)
                self._finish("retry count exceeded")
                return
            self._enquire()

    def _enquire(self) -> None:
        self.state = TIMER_RECOVERY
        self.retries += 1
        self._send_s(ax25.RR, poll=True)
        self._start_t1()

    def _start_t1(self) -> None:
        # FRACK grows with the number of digipeaters the frame and its ack must cross (TNC-2 convention).
        self.t1_at = self.mgr.now + self.mgr.params.frack * (2 * len(self.path) + 1)
        self.t3_at = None

    def _stop_t1(self) -> None:
        self.t1_at = None

    def _start_t3(self) -> None:
        self.t3_at = self.mgr.now + self.mgr.params.t3

    # ------------------------------------------------------------------ transmit

    def _push(self) -> None:
        """Send queued data while the window allows."""
        if self.state != CONNECTED or self.peer_busy:
            return
        while self.tx_queue and len(self.unacked) < self.mgr.params.maxframe:
            info = self.tx_queue.popleft()
            ns = self.vs
            self.unacked[ns] = info
            self.vs = (self.vs + 1) % 8
            self._send_i(ns, info)
            self.stats["i_sent"] += 1
            if self.t1_at is None:
                self._start_t1()

    def _send_i(self, ns: int, info: bytes) -> None:
        self.t2_at = None                                           # N(R) rides on this frame
        self._emit(Frame(self.remote, self.mgr.mycall, list(self.path), (self.vr << 5) | (ns << 1),
                         ax25.PID_NO_L3, info, True))

    def _send_s(self, kind: int, poll: bool = False, final: bool = False) -> None:
        if kind == ax25.RR:
            kind = ax25.RNR if self.mgr.busy else ax25.RR
        self.t2_at = None
        command = poll
        self._emit(Frame(self.remote, self.mgr.mycall, list(self.path),
                         kind | (self.vr << 5) | (ax25.PF if poll or final else 0), None, b"", command))

    def _send_u(self, kind: int, poll: bool, command: bool = True) -> None:
        self._emit(Frame(self.remote, self.mgr.mycall, list(self.path), kind | (ax25.PF if poll else 0), None, b"",
                         command))

    def _emit(self, f: Frame) -> None:
        for d in f.digis:
            d.flag = False                                           # not yet repeated
        self.mgr.send(f.encode())

    def _finish(self, reason: str) -> None:
        self.state = DISCONNECTED
        self.t1_at = self.t2_at = self.t3_at = None
        self.tx_queue.clear()
        self._reset()
        self.mgr.on_disconnect(self, reason)


class LinkManager:
    """All connected-mode links for one local callsign.

    Callbacks (set by the owner): ``on_connect(link, incoming)``, ``on_data(link, bytes)``,
    ``on_disconnect(link, reason)``; ``accept_incoming(link) -> bool`` decides whether to answer a SABM.
    """

    def __init__(self, mycall: str, send, params: LinkParams | None = None, clock=time.monotonic) -> None:
        self.mycall = Address.parse(mycall)
        self.send = send
        self.params = params or LinkParams()
        self.clock = clock
        self.links: dict[str, Link] = {}
        self.busy = False                                            # our receive buffer is full: send RNR
        self.on_connect = lambda link, incoming: None
        self.on_data = lambda link, data: None
        self.on_disconnect = lambda link, reason: None
        self.accept_incoming = lambda link: True

    def link(self, remote: str | Address, via: list[str] | None = None) -> Link:
        addr = remote if isinstance(remote, Address) else Address.parse(remote)
        key = str(addr)
        lk = self.links.get(key)
        if lk is None:
            lk = self.links[key] = Link(self, Address(addr.call, addr.ssid), [Address.parse(v) for v in via or []])
        elif via is not None:
            lk.path = [Address.parse(v) for v in via]
        return lk

    def connect(self, remote: str, via: list[str] | None = None) -> Link:
        lk = self.link(remote, via)
        lk.connect()
        return lk

    def receive(self, frame: Frame | bytes) -> bool:
        """Handle a frame heard on the channel; True if it was for one of our links."""
        if isinstance(frame, (bytes, bytearray)):
            try:
                frame = Frame.decode(bytes(frame))
            except Ax25Error:
                return False
        if (frame.dest.call, frame.dest.ssid) != (self.mycall.call, self.mycall.ssid) or frame.kind == "UI":
            return False
        if frame.digis and not frame.digis[-1].flag:
            return False                                             # still on its way through the digipeaters
        key = str(frame.src)
        lk = self.links.get(key)
        if lk is None:
            if frame.kind != "SABM" and not (frame.command and frame.poll):
                return True
            # Reply along the reverse of the path the frame took.
            lk = self.links[key] = Link(self, Address(frame.src.call, frame.src.ssid),
                                        [Address(d.call, d.ssid) for d in reversed(frame.digis)])
        lk.receive(frame)
        return True

    @property
    def now(self) -> float:
        return self.clock()

    def tick(self) -> None:
        """Run due timers; call often (every audio block)."""
        now = self.clock()
        for lk in list(self.links.values()):
            lk.tick(now)
