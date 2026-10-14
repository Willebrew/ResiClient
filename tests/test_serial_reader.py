import unittest

import serial

from serial_reader import SerialReader


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakePort:
    """Stands in for serial.Serial. `script` is a list of readline() results."""

    def __init__(self, script=None, error=None) -> None:
        self.script = list(script or [])
        self.error = error
        self.closed = False
        self.reset_called = False

    def readline(self) -> bytes:
        if self.error is not None:
            raise self.error
        return self.script.pop(0) if self.script else b""

    def reset_input_buffer(self) -> None:
        self.reset_called = True

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Hands out ports in order; an Exception in the list simulates a failed open."""

    def __init__(self, ports) -> None:
        self.ports = list(ports)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        port = self.ports.pop(0)
        if isinstance(port, Exception):
            raise port
        return port


class SerialReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.logs = []

    def make_reader(self, opener, stall_timeout=300.0, retry_interval=5.0):
        return SerialReader(
            "/dev/ttyUSB0",
            9600,
            0.1,
            stall_timeout=stall_timeout,
            retry_interval=retry_interval,
            opener=opener,
            clock=self.clock,
            log=self.logs.append,
        )

    def test_returns_data_without_reopening(self):
        port = FakePort([b"#DFW.0683855844...^?$\r\n"])
        opener = FakeOpener([port])
        reader = self.make_reader(opener)

        self.assertEqual(reader.readline(), b"#DFW.0683855844...^?$\r\n")
        self.assertEqual(opener.calls, 1)
        self.assertTrue(port.reset_called)
        self.assertTrue(reader.connected)

    def test_quiet_port_under_threshold_is_not_reopened(self):
        port = FakePort()
        opener = FakeOpener([port])
        reader = self.make_reader(opener, stall_timeout=300)

        self.clock.now += 299
        self.assertEqual(reader.readline(), b"")

        self.assertEqual(opener.calls, 1)
        self.assertFalse(port.closed)
        self.assertEqual(self.logs, [])

    def test_silent_port_is_reopened_after_stall_timeout(self):
        stalled = FakePort()
        fresh = FakePort([b"#DNT.1268287922...^?$\r\n"])
        opener = FakeOpener([stalled, fresh])
        reader = self.make_reader(opener, stall_timeout=300)

        self.clock.now += 300
        self.assertEqual(reader.readline(), b"")
        self.assertTrue(stalled.closed)
        self.assertEqual(opener.calls, 2)
        self.assertTrue(any("reopening" in line for line in self.logs))

        # The new port delivers the next tag.
        self.assertEqual(reader.readline(), b"#DNT.1268287922...^?$\r\n")
        self.assertEqual(opener.calls, 2)

    def test_stall_timer_restarts_after_reopen(self):
        opener = FakeOpener([FakePort(), FakePort(), FakePort()])
        reader = self.make_reader(opener, stall_timeout=300)

        self.clock.now += 300
        reader.readline()  # reopen #1
        self.clock.now += 100
        reader.readline()  # only 100s since reopen: no second reopen

        self.assertEqual(opener.calls, 2)

    def test_read_error_triggers_reopen(self):
        broken = FakePort(error=serial.SerialException("device reports readiness but gone"))
        fresh = FakePort([b"#HCTR1223813669...!H$\r\n"])
        opener = FakeOpener([broken, fresh])
        reader = self.make_reader(opener)

        self.assertEqual(reader.readline(), b"")
        self.assertTrue(broken.closed)
        self.assertEqual(reader.readline(), b"#HCTR1223813669...!H$\r\n")
        self.assertEqual(opener.calls, 2)

    def test_missing_port_at_startup_does_not_raise(self):
        opener = FakeOpener([serial.SerialException("no such device")])
        reader = self.make_reader(opener, retry_interval=5)

        self.assertFalse(reader.connected)
        self.assertEqual(reader.readline(), b"")

    def test_failed_open_retries_after_interval_and_logs_once(self):
        opener = FakeOpener(
            [
                serial.SerialException("busy"),
                serial.SerialException("busy"),
                FakePort([b"#DFW.0683855844...^?$\r\n"]),
            ]
        )
        reader = self.make_reader(opener, retry_interval=5)

        # Still inside the retry interval: no new open attempt.
        self.clock.now += 4
        reader.readline()
        self.assertEqual(opener.calls, 1)

        self.clock.now += 1
        reader.readline()  # attempt #2 fails
        self.assertEqual(opener.calls, 2)
        self.assertFalse(reader.connected)

        self.clock.now += 5
        self.assertEqual(reader.readline(), b"")  # attempt #3 succeeds
        self.assertTrue(reader.connected)
        self.assertEqual(reader.readline(), b"#DFW.0683855844...^?$\r\n")

        open_errors = [line for line in self.logs if "cannot open" in line]
        self.assertEqual(len(open_errors), 1)
        self.assertTrue(any("reopened" in line for line in self.logs))

    def test_close_releases_port(self):
        port = FakePort()
        reader = self.make_reader(FakeOpener([port]))

        reader.close()

        self.assertTrue(port.closed)
        self.assertFalse(reader.connected)


if __name__ == "__main__":
    unittest.main()
