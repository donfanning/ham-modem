"""DTE-side byte links: the serial ports a legacy packet program opens to reach the TNC.

Slimmed from El-Modem (``serial_port.py``, ``modem_serial_device.py``); one interface for all backends:
``fds()`` / ``write_fds()`` -> selectable objects; ``service(readable, writable)`` -> (data, dropped);
``write(data)`` queues output; ``name``; ``close()``. ``update_lines(dcd)`` drives modem-control outputs where
the backend has them (a real tty's DCD shows the radio channel busy, as on a hardware TNC).

``PtySerial``     POSIX pseudo-terminal (kissattach, Xastir, minicom open the slave or the ``link`` symlink).
                  The slave is not held open here, so EIO on the master means the program closed the port.
``TcpSerial``     one TCP client at a time is "the serial cable" (Windows via com0com + a TCP redirector).
``DeviceSerial``  a real tty such as /dev/ttyUSB0, raw mode, no flow control.
"""
import os
import socket
import time

if os.name == "posix":
    import array
    import errno
    import fcntl
    import termios
    import tty


class PtySerial:
    def __init__(self, link: str | None = None):
        import pty  # noqa: PLC0415 -- POSIX only
        import tty  # noqa: PLC0415

        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.name = os.ttyname(slave)
        os.close(slave)
        os.set_blocking(self.master, False)
        self._open = False
        self._pending = bytearray()
        self._next_probe = 0.0
        self._link = link
        if link:
            os.symlink(self.name, link)

    def fds(self):
        if self._open or time.monotonic() >= self._next_probe:
            return [self.master]
        return []

    def service(self, readable, writable=()):
        if self.master in writable:
            self._flush()
        probing = not self._open and time.monotonic() >= self._next_probe
        if probing:
            self._next_probe = time.monotonic() + 0.1
            if self.master not in readable:
                self._open = True               # not EIO any more: a terminal has opened the port
        dropped = False
        data = b""
        if self.master in readable:
            peer_closed = False
            try:
                data = os.read(self.master, 4096)
                peer_closed = not data
            except BlockingIOError:
                data = b""
            except OSError:
                data = b""
                peer_closed = True               # Linux reports a closed PTY as EIO
            if peer_closed:
                dropped = self._open
                self._open = False
                self._pending.clear()
                self._next_probe = time.monotonic() + 0.1
                # Darwin reports a closed PTY slave as a readable master whose
                # read returns b""; Linux reports EIO. Both mean the terminal
                # dropped DTR.
            if data:
                self._open = True
        return data, dropped

    def write_fds(self):
        return [self.master] if self._pending else []

    def _flush(self):
        try:
            n = os.write(self.master, self._pending)
            del self._pending[:n]
        except BlockingIOError:
            pass
        except OSError:
            self._pending.clear()

    def write(self, data: bytes) -> None:
        self._pending.extend(data)
        self._flush()

    def update_lines(self, dcd: bool) -> None:
        pass

    def close(self) -> None:
        os.close(self.master)
        if self._link:
            os.unlink(self._link)


class TcpSerial:
    def __init__(self, host: str = "127.0.0.1", port: int = 9100, exclusive: bool = False):
        self.exclusive = exclusive
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self._srv.setblocking(False)
        self._conn: socket.socket | None = None
        self._pending = bytearray()
        self.name = f"tcp://{host}:{port}"

    def fds(self):
        return [self._srv] + ([self._conn] if self._conn else [])

    def service(self, readable, writable=()):
        if self._conn is not None and self._conn in writable:
            self._flush()
        dropped = False
        data = b""
        if self._srv in readable:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                conn = None
            if conn is not None:
                if self._conn is not None and self.exclusive:
                    conn.close()
                    conn = None
                if conn is None:
                    return b"", False
                if self._conn is not None:      # a newer client replaces the old one
                    self._close_conn()
                    dropped = True
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.setblocking(False)
                self._conn = conn
        if self._conn is not None and self._conn in readable:
            try:
                data = self._conn.recv(4096)
                if not data:
                    raise ConnectionResetError
            except BlockingIOError:
                data = b""
            except OSError:
                data = b""
                self._close_conn()
                dropped = True
        return data, dropped

    def _close_conn(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass
        self._conn = None
        self._pending.clear()

    def write_fds(self):
        return [self._conn] if self._conn is not None and self._pending else []

    def _flush(self):
        try:
            n = self._conn.send(self._pending)
            del self._pending[:n]
        except (BlockingIOError, OSError):
            pass

    def write(self, data: bytes) -> None:
        if self._conn is None:
            return
        self._pending.extend(data)
        self._flush()

    def update_lines(self, dcd: bool) -> None:
        pass

    def close(self) -> None:
        if self._conn is not None:
            self._close_conn()
        self._srv.close()


_BAUDS = {rate: getattr(termios, f"B{rate}") for rate in
          (300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)
          if hasattr(termios, f"B{rate}")} if os.name == "posix" else {}


class DeviceSerial:
    """Own a POSIX tty; DTR going low from the attached computer counts as a drop."""

    def __init__(self, device: str, baud: int = 9600):
        if baud not in _BAUDS:
            raise ValueError(f"unsupported serial baud rate {baud!r}")
        self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        try:
            tty.setraw(self.fd, termios.TCSANOW)
            attrs = termios.tcgetattr(self.fd)
            attrs[4] = attrs[5] = _BAUDS[baud]
            attrs[2] |= termios.CLOCAL | termios.CREAD
            if hasattr(termios, "CRTSCTS"):
                attrs[2] &= ~termios.CRTSCTS
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
        except BaseException:
            os.close(self.fd)
            raise
        self.name = f"{device}@{baud}"
        self._pending = bytearray()
        self._previous_dtr = self._get_line(getattr(termios, "TIOCM_DTR", 0))

    def fds(self):
        return [self.fd]

    def write_fds(self):
        return [self.fd] if self._pending else []

    def service(self, readable, writable=()):
        if self.fd in writable:
            self._flush()
        data = b""
        if self.fd in readable:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                pass
        dtr = self._get_line(getattr(termios, "TIOCM_DTR", 0))
        dropped = self._previous_dtr is True and dtr is False
        if dtr is not None:
            self._previous_dtr = dtr
        return data, dropped

    def _get_line(self, bit):
        if not bit or not hasattr(termios, "TIOCMGET"):
            return None
        bits = array.array("i", [0])
        try:
            fcntl.ioctl(self.fd, termios.TIOCMGET, bits, True)
        except OSError as exc:
            if exc.errno in (errno.ENOTTY, errno.EINVAL, errno.ENOSYS):
                return None
            raise
        return bool(bits[0] & bit)

    def update_lines(self, dcd: bool) -> None:
        bit = getattr(termios, "TIOCM_CAR", 0)
        if not bit or not hasattr(termios, "TIOCMBIS"):
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCMBIS if dcd else termios.TIOCMBIC, array.array("i", [bit]), True)
        except OSError as exc:
            if exc.errno not in (errno.ENOTTY, errno.EINVAL, errno.ENOSYS):
                raise

    def _flush(self):
        try:
            n = os.write(self.fd, self._pending)
            del self._pending[:n]
        except BlockingIOError:
            pass

    def write(self, data: bytes) -> None:
        if len(self._pending) + len(data) > 65536:
            return                              # the attached program is not reading; drop like a full TNC buffer
        self._pending.extend(data)
        self._flush()

    def close(self) -> None:
        os.close(self.fd)
