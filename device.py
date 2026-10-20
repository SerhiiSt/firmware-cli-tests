import json
import time
import serial


class Device:
    def __init__(self, port, baudrate=115200, timeout=1.0):
        # serial_for_url accepts both a COM port (COM5, /dev/ttyUSB0)
        # and a Wokwi RFC2217 URL (rfc2217://localhost:4000)
        self.ser = serial.serial_for_url(port, baudrate=baudrate, timeout=timeout)
        self.transcript = []     # every TX/RX line, saved as evidence on failure
        self.timed_out = False   # True after any timeout: a late reply may still arrive
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def log(self, direction, text):
        stamp = time.strftime("%H:%M:%S")
        self.transcript.append(f"{stamp} {direction} {text}")

    def wait_ready(self, wait=1.0):
        """The firmware prints {"status":"READY"} after boot. Opening the port
        can reboot the board, so drain boot output before the first command.
        Returns True if the READY banner was seen."""
        old_timeout = self.ser.timeout
        self.ser.timeout = 0.2
        deadline = time.monotonic() + wait
        seen = False
        try:
            while time.monotonic() < deadline:
                line = self.ser.readline().decode(errors="replace").strip()
                if line:
                    self.log("BOOT", line)
                    if "READY" in line:
                        seen = True
                        break
        finally:
            self.ser.timeout = old_timeout
        return seen

    def drain(self, quiet=2.0, limit=8.0):
        """Read and log late/stale replies until the line is silent for `quiet`
        seconds. Needed after a timeout: the device may answer late and that
        reply would be read as the answer to the NEXT command (desync)."""
        old_timeout = self.ser.timeout
        self.ser.timeout = quiet
        end = time.monotonic() + limit
        try:
            while time.monotonic() < end:
                line = self.ser.readline().decode(errors="replace").strip()
                if not line:
                    break
                self.log("LATE", line)
        finally:
            self.ser.timeout = old_timeout
        self.timed_out = False

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def command_raw(self, command, timeout=None):
        """Send one command and return the first reply line.
        `timeout` overrides the read timeout for this call only."""
        old_timeout = self.ser.timeout
        if timeout is not None:
            self.ser.timeout = timeout
        try:
            self.ser.reset_input_buffer()
            self.log("TX", command)
            start = time.monotonic()
            self.ser.write((command + "\n").encode())
            self.ser.flush()
            line = self.ser.readline().decode(errors="replace").strip()
            elapsed = time.monotonic() - start
        finally:
            self.ser.timeout = old_timeout
        if not line:
            self.timed_out = True
            self.log("RX", f"<no response, timeout after {elapsed:.2f}s>")
            raise TimeoutError(f"No response for command: {command}")
        self.log("RX", f"{line}   ({elapsed:.2f}s)")
        self.last_elapsed = elapsed
        return line

    def command(self, command, timeout=None):
        raw = self.command_raw(command, timeout=timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AssertionError(f"Invalid JSON from device: {raw!r}") from e
