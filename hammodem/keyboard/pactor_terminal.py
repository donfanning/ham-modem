"""Text terminal shared by the working PACTOR-II/III links."""

from __future__ import annotations

import numpy as np

from .pactor_link import LinkBase

HELP = (
    "/connect CALL   call a station\r\n"
    "/disconnect     end the link\r\n"
    "/turn           change direction\r\n"
    "/status         link state and counters\r\n"
    "/help           this text\r\n"
    "Anything else is traffic; a line ending in +? changes direction.\r\n"
)


class PactorTerminalBase:
    """Host/audio adapter for :class:`LinkBase`.

    The object has the same surface as ``AmtorTnc``/``KeyboardTnc``, so the
    existing ``keyboard.tnc.run`` audio loop can drive it unchanged.
    """

    LINK: type[LinkBase]

    def __init__(self, mycall: str, fs: int = 8000, level: int = 2, write=None,
                 max_retries: int = 8) -> None:
        self.mycall = mycall.upper()
        self.fs = fs
        self.level = level
        self.write = write or (lambda data: None)
        self._line = ""
        self._skip_lf = False
        self._rxbuf = np.zeros(0, dtype=np.float64)
        self.link = self.make_link()

    def make_link(self) -> LinkBase:
        raise NotImplementedError

    # -- host ---------------------------------------------------------------

    def _out(self, text: str) -> None:
        self.write(text.replace("\n", "\r\n").encode("latin-1", "replace"))

    def _event(self, text: str) -> None:
        self._out(f"\r\n*** {text}\r\n")

    def _rx_text(self, data: bytes) -> None:
        self.write(data)

    def feed_host(self, data: bytes) -> None:
        for ch in data.decode("latin-1"):
            if ch == "\n" and self._skip_lf:
                self._skip_lf = False
                continue
            self._skip_lf = ch == "\r"
            if ch in "\r\n":
                self._submit(self._line)
                self._line = ""
            elif ch in "\x7f\b":
                self._line = self._line[:-1]
            else:
                self._line += ch

    def _submit(self, line: str) -> None:
        if line.startswith("/"):
            self._command(line[1:].split())
            return
        turn = line.rstrip().endswith("+?")
        text = (line.rstrip()[:-2] if turn else line) + "\r\n"
        self.link.send(text.encode("latin-1", "replace"))
        if turn:
            self.link.turn()

    def _command(self, words: list[str]) -> None:
        if not words:
            return
        cmd, args = words[0].lower(), words[1:]
        if cmd == "connect" and args:
            self.link.connect(args[0])
        elif cmd == "disconnect":
            self.link.disconnect()
        elif cmd == "turn":
            self.link.turn()
        elif cmd == "status":
            link = self.link
            self._out(
                f"state={link.state} role={link.role or '-'} remote={link.remote or '-'} "
                f"sent={link.stats.sent} retries={link.stats.retries} "
                f"received={link.stats.received} bad={link.stats.bad}\r\n"
            )
        else:
            self._out(HELP)

    # -- audio --------------------------------------------------------------

    def audio_in(self, block: np.ndarray) -> None:
        self._rxbuf = np.concatenate((self._rxbuf, np.asarray(block, dtype=np.float64)))

    def audio_out(self, n: int) -> np.ndarray:
        if len(self._rxbuf) >= n:
            rx, self._rxbuf = self._rxbuf[:n], self._rxbuf[n:]
        else:
            rx = self._rxbuf
            self._rxbuf = np.zeros(0, dtype=np.float64)
        return self.link.tick(rx, n)
