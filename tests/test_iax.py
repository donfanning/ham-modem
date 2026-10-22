import select
import socket
import time
import unittest

try:
    import ham_modem_iax  # noqa: F401
    HAVE_IAX = True
except ImportError:
    HAVE_IAX = False


@unittest.skipUnless(HAVE_IAX, "native IAX2 module not built (native/iax)")
class IaxLegTest(unittest.TestCase):
    def test_registers_and_dials_towards_the_server(self):
        from hammodem.audio.iax import IaxLeg
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        leg = IaxLeg(f"127.0.0.1:{server.getsockname()[1]}", "station", "x", dial="7300", redial_after=60)
        leg._next_dial = 0
        seen = 0
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and seen < 2:
            leg.service([])
            r, _, _ = select.select([server], [], [], 0.05)
            if r:
                data, _ = server.recvfrom(4096)
                self.assertTrue(data[0] & 0x80, "IAX2 full frame")        # F bit set on control frames
                seen += 1
        self.assertGreaterEqual(seen, 2)                                   # REGREQ and NEW at least
        self.assertEqual(leg.state, "calling")
        leg.close()
        server.close()

    def test_needs_a_role(self):
        from hammodem.audio.iax import IaxLeg
        with self.assertRaises(ValueError):
            IaxLeg("127.0.0.1:4569", "station", "x")


if __name__ == "__main__":
    unittest.main()
