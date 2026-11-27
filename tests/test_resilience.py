import unittest
from subprocess import TimeoutExpired
from resilience import CircuitOpen, ReadCircuit, ReadServiceError


class TestReadCircuit(unittest.TestCase):
    def test_timeout_probe_and_recovery(self):
        now = [0]
        circuit = ReadCircuit(threshold=2, cooldown=10, clock=lambda: now[0])
        with circuit.call():  # This earlier success must not close the newer circuit.
            for error in (TimeoutExpired("read", 1), ReadServiceError("unavailable")):
                with self.assertRaises(type(error)), circuit.call():
                    raise error
        with self.assertRaises(CircuitOpen), circuit.call():
            self.fail("read admitted during cooldown")
        now[0] = 10
        with self.assertRaises(OSError), circuit.call():
            with self.assertRaises(CircuitOpen), circuit.call():
                self.fail("second half-open probe admitted")
            raise OSError("still offline")
        now[0] = 19
        with self.assertRaises(CircuitOpen), circuit.call():
            self.fail("failed probe did not restore cooldown")
        now[0] = 20
        with circuit.call():
            pass
        with circuit.call(), circuit.call():
            pass
