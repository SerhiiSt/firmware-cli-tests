"""Automated tests for the firmware serial CLI (Wokwi / ESP32): all tests in one module.

Sections (in pytest execution order):
  1. Course tests: PWM boundaries, LED / RESET, sensor, PING / STATUS, timeout
  2. Candidate bugs: PING blocking, PWM scale, LED/PWM coupling
  3. Exploratory: LED vs PWM, case and whitespace, missing arguments, long lines,
     two commands in one write, heap, sensor error, CRLF
  4. Own parameterized tests: boundary and negative values, RESET, GET TEMP,
     PING latency, timeout behaviour

Requirement 10 applies to every failure: a failed test is evidence of a
mismatch, not automatic proof of a firmware defect. For each FAIL decide the
cause: test error / environment / communication / requirement / firmware.

Run:
    export DEVICE_PORT=rfc2217://localhost:4000      # Wokwi, or COM5 / /dev/ttyUSB0
    python -m pytest -v test_firmware_cli.py
Optional: RUN_SLOW=1 (heap sampling, full PWM sweep),
          SENSOR_DISCONNECTED=1 (sensor error test, needs the sensor disconnected).
Needs conftest.py (fixture `device`: RESET before and after each test, evidence
log for every failure) and device.py (serial helper with a finite timeout).
"""
import json
import os
import statistics
import time

import pytest
from device import Device

HARNESS_TIMEOUT = 1.0   # default read timeout of the device fixture
PROBE_TIMEOUT = 3.0     # PING answers in ~1.3 s, so probes must wait longer than 1 s
SWEEP_ALL = bool(os.getenv("RUN_SLOW"))


def raw_duty(percent):
    """Value STATUS shows for a PWM percent (measured: floor(percent * 255 / 100))."""
    return percent * 255 // 100


# =====================================================================
# 1. Course tests: PWM boundaries
# =====================================================================

@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_pwm_valid_boundaries(device, value):
    r = device.command(f"SET PWM {value}")
    assert r["status"] == "OK"
    assert r["pwm"] == value

@pytest.mark.parametrize("value", [-1, 101, 999])
def test_pwm_invalid_boundaries(device, value):
    r = device.command(f"SET PWM {value}")
    assert r["status"] == "ERROR"
    assert r["code"] == "INVALID_VALUE"


# =====================================================================
# 2. Candidate bugs: PING blocking, PWM scale, LED/PWM coupling
# =====================================================================





def send_and_time(device, payload, count, wait=5.0):
    """Write `payload` in ONE write, read `count` reply lines.
    Returns [(seconds since the write, parsed JSON)]. A missing reply is simply
    not in the list, so callers can assert on it."""
    device.ser.reset_input_buffer()
    device.log("TX", repr(payload))
    old_timeout = device.ser.timeout
    t0 = time.monotonic()
    device.ser.write(payload)
    device.ser.flush()
    replies = []
    try:
        while len(replies) < count and time.monotonic() - t0 < wait:
            device.ser.timeout = max(0.05, wait - (time.monotonic() - t0))
            line = device.ser.readline().decode(errors="replace").strip()
            if not line:
                break
            elapsed = time.monotonic() - t0
            device.log("RX", f"{line}   ({elapsed:.2f}s)")
            try:
                replies.append((elapsed, json.loads(line)))
            except json.JSONDecodeError:
                replies.append((elapsed, {"raw": line}))
    finally:
        device.ser.timeout = old_timeout
    if len(replies) < count:
        device.timed_out = True  # fixture will drain late replies
    return replies


def find_reply(replies, predicate):
    for elapsed, reply in replies:
        if predicate(reply):
            return elapsed, reply
    return None, None


# =====================================================================
# 1. PING blocks the command queue
# =====================================================================
def test_status_right_after_ping_is_not_blocked(device, record_property):
    """evidence: PING and STATUS are sent in one write. If PING blocks the
    firmware, STATUS is answered only after PING (~1.3 s) and misses the
    1.0 s harness timeout. Every other command answers in 0.2-0.4 s."""
    replies = send_and_time(device, b"PING\nSTATUS\n", count=2)
    t_ping, _ = find_reply(replies, lambda r: r.get("command") == "PING")
    t_status, _ = find_reply(replies, lambda r: "heap" in r)
    record_property("ping_reply_s", None if t_ping is None else round(t_ping, 2))
    record_property("status_reply_s", None if t_status is None else round(t_status, 2))
    assert t_ping is not None, f"no PING reply: {replies!r}"
    assert t_status is not None, f"no STATUS reply: {replies!r}"
    assert t_status < HARNESS_TIMEOUT, (
        f"STATUS answered after {t_status:.2f}s (PING after {t_ping:.2f}s): PING blocks the queue"
    )


def test_two_status_commands_in_one_write_are_fast(device):
    """control: the same trick with STATUS only. Both answers must be quick,
    otherwise the delay is not specific to PING."""
    replies = send_and_time(device, b"STATUS\nSTATUS\n", count=2)
    assert len(replies) == 2, f"missing reply: {replies!r}"
    assert all("heap" in r for _, r in replies)
    assert replies[-1][0] < HARNESS_TIMEOUT, f"second STATUS after {replies[-1][0]:.2f}s"


def test_ping_is_not_an_outlier_vs_status(device, record_property):
    """evidence (heuristic, not a requirement): PING should not be several
    times slower than STATUS. Medians of 5 STATUS and 3 PING requests."""
    def median_latency(cmd, n):
        values = []
        for _ in range(n):
            device.command_raw(cmd, timeout=5.0)
            values.append(device.last_elapsed)
        return statistics.median(values)

    status_s = median_latency("STATUS", 5)
    ping_s = median_latency("PING", 3)
    record_property("status_median_s", round(status_s, 2))
    record_property("ping_median_s", round(ping_s, 2))
    assert ping_s <= 3 * status_s, (
        f"PING median {ping_s:.2f}s is {ping_s / status_s:.1f}x STATUS median {status_s:.2f}s"
    )


# =====================================================================
# 2. PWM scale: percent in SET PWM, 0..255 in STATUS
# =====================================================================
PWM_VALUES = range(0, 100) if SWEEP_ALL else (0, 1, 2, 10, 25, 50, 75, 98, 99)
# 100 is left out on purpose: SET PWM 100 is rejected (BUG-1), so it has no STATUS value.


def test_pwm_percent_to_status_mapping(device, record_property):
    """Measure SET PWM percent -> STATUS pwm and check the mapping:
    follows the formula, is strictly increasing (no two percents share a
    value) and can be reversed by ROUNDING. Plain truncation loses a percent
    (99 -> 252 -> 98) and is only recorded, for QUESTION-1."""
    mapping = {}
    for p in PWM_VALUES:
        r = device.command(f"SET PWM {p}", timeout=PROBE_TIMEOUT)
        assert r["status"] == "OK" and r["pwm"] == p, f"SET PWM {p} -> {r!r}"
        mapping[p] = device.command("STATUS", timeout=PROBE_TIMEOUT)["pwm"]

    record_property("mapping", json.dumps(mapping))
    assert mapping == {p: raw_duty(p) for p in PWM_VALUES}, "mapping differs from floor(p*255/100)"

    raws = [mapping[p] for p in PWM_VALUES]
    assert all(a < b for a, b in zip(raws, raws[1:])), "two percent values give the same STATUS pwm"
    assert all(round(raw * 100 / 255) == p for p, raw in mapping.items()), "not reversible by rounding"

    truncation_errors = {p: raw * 100 // 255 for p, raw in mapping.items() if raw * 100 // 255 != p}
    record_property("truncation_errors", json.dumps(truncation_errors))


def test_pwm_endpoints_map_to_range_ends(device):
    """0 % must be 0 and the highest accepted percent must stay inside 0..255."""
    device.command("SET PWM 0")
    assert device.command("STATUS")["pwm"] == 0
    device.command("SET PWM 99")
    assert 0 < device.command("STATUS")["pwm"] <= 255


# =====================================================================
# 3. SET LED ON changes PWM
# =====================================================================
@pytest.mark.parametrize("pwm", [0, 30, 99])
def test_led_on_and_off_keep_pwm_value(device, record_property, pwm):
    """evidence: LED and PWM are separate outputs (requirements 2 and 7 list
    them side by side). SET LED ON/OFF must not change the PWM value.
    Measured so far: SET LED ON gives STATUS pwm 255. If this fails, report
    it as a defect candidate AND as a question to the requirement owner
    (requirement 4 only says 'output state changes accordingly')."""
    device.command(f"SET PWM {pwm}")
    before = device.command("STATUS")["pwm"]
    device.command("SET LED ON")
    after_on = device.command("STATUS")["pwm"]
    device.command("SET LED OFF")
    after_off = device.command("STATUS")["pwm"]
    record_property("pwm_before", before)
    record_property("pwm_after_led_on", after_on)
    record_property("pwm_after_led_off", after_off)
    assert before == raw_duty(pwm)
    assert after_on == before, f"SET LED ON changed pwm {before} -> {after_on}"
    assert after_off == before, f"SET LED OFF changed pwm {before} -> {after_off}"


def test_set_pwm_keeps_led_on(device):
    """Reverse direction: with the LED on, changing PWM must not switch it off."""
    device.command("SET LED ON")
    device.command("SET PWM 50")
    status = device.command("STATUS")
    assert status["led"] is True
    assert status["pwm"] == raw_duty(50)


# =====================================================================
# 4. Extra checks from the evidence review
# =====================================================================
@pytest.mark.parametrize("pwm", [10, 25, 50])
def test_status_is_answered_after_set_pwm_repeatedly(device, record_property, pwm):
    """Intermittent timeout check. In one earlier run STATUS got no answer within
    1.0 s right after SET PWM 25 (latencies had grown from 0.2 s to 0.4 s) and the
    next run was clean. Repeat 8 times and record the slowest answer: a stable pass
    points to a one-off environment/communication event, a repeat to the firmware."""
    slowest = 0.0
    for i in range(8):
        device.command(f"SET PWM {pwm}", timeout=PROBE_TIMEOUT)
        device.command_raw("STATUS", timeout=PROBE_TIMEOUT)
        slowest = max(slowest, device.last_elapsed)
    record_property("slowest_status_s", round(slowest, 2))
    assert slowest < HARNESS_TIMEOUT, f"STATUS took {slowest:.2f}s after SET PWM {pwm}"


@pytest.mark.parametrize("pwm, led", [(0, False), (1, True), (50, True), (99, True)])
def test_led_flag_follows_pwm_value(device, pwm, led):
    """characterization (QUESTION-2): measured behaviour is led = (pwm > 0).
    Locks the observed coupling so a firmware change is noticed. If the requirement
    owner says LED and PWM must be independent, remove this test; the evidence
    tests above (test_led_on_and_off_keep_pwm_value) then describe the defect."""
    device.command(f"SET PWM {pwm}")
    assert device.command("STATUS")["led"] is led


def test_led_commands_overwrite_pwm_value(device):
    """characterization (QUESTION-2): measured SET LED ON -> pwm 255 and
    SET LED OFF -> pwm 0, whatever PWM was set before."""
    device.command("SET PWM 30")
    device.command("SET LED ON")
    assert device.command("STATUS")["pwm"] == 255
    device.command("SET LED OFF")
    assert device.command("STATUS")["pwm"] == 0


# =====================================================================
# 3. Exploratory tests
# =====================================================================

KNOWN_ERROR_CODES = {"UNKNOWN_COMMAND", "INVALID_VALUE"}

SLOW = pytest.mark.skipif(not os.getenv("RUN_SLOW"), reason="set RUN_SLOW=1 to run slow tests")
SENSOR_OFF = pytest.mark.skipif(
    not os.getenv("SENSOR_DISCONNECTED"),
    reason="disconnect the temperature sensor in diagram.json, restart Wokwi, set SENSOR_DISCONNECTED=1",
)


def assert_structured(reply):
    """Reply is a JSON object with status OK/ERROR; ERROR must carry a code."""
    assert isinstance(reply, dict), f"not a JSON object: {reply!r}"
    assert reply.get("status") in ("OK", "ERROR"), f"bad status: {reply!r}"
    if reply["status"] == "ERROR":
        assert "code" in reply, f"ERROR without code: {reply!r}"


def assert_still_responsive(device):
    """After an odd input the next normal command must still be answered."""
    device.drain(0.5)  # drop any extra/late reply produced by the probe
    r = device.command("STATUS", timeout=PROBE_TIMEOUT)
    assert r["status"] == "OK" and "heap" in r, f"device not responsive: {r!r}"


# ---------- 1. LED and PWM are independent outputs ----------
def test_set_pwm_does_not_turn_led_on(device):
    """SET LED OFF, SET PWM 50, STATUS -> led must stay false.
    If it is true the LED follows PWM: ask the requirement owner (req 4)."""
    device.command("SET LED OFF")
    device.command("SET PWM 50")
    status = device.command("STATUS")
    assert status["led"] is False


def test_led_on_off_cycle_reports_pwm_side_effect(device, record_property):
    """SET PWM 30, SET LED ON/OFF: LED flag must follow the command.
    The pwm value after each step is only recorded (no requirement)."""
    device.command("SET PWM 30")
    on = device.command("SET LED ON")
    s_on = device.command("STATUS")
    off = device.command("SET LED OFF")
    s_off = device.command("STATUS")
    record_property("pwm_after_led_on", s_on["pwm"])
    record_property("pwm_after_led_off", s_off["pwm"])
    assert on["led"] is True and s_on["led"] is True
    assert off["led"] is False and s_off["led"] is False


# ---------- 2. Case and whitespace ----------
@pytest.mark.parametrize("cmd", [
    "ping", "set pwm 10", "SET  PWM 10", "SET PWM 10 ", "SET PWM 010", "SET PWM +10",
])
def test_case_and_whitespace_variants_are_handled(device, record_property, cmd):
    reply = device.command(cmd, timeout=PROBE_TIMEOUT)
    record_property("reply", json.dumps(reply))
    assert_structured(reply)
    if reply["status"] == "ERROR":
        assert reply["code"] in KNOWN_ERROR_CODES
    assert_still_responsive(device)


# ---------- 3. Command without argument ----------
@pytest.mark.parametrize("cmd", ["SET PWM", "SET LED", "SET"])
def test_command_without_argument_gives_structured_error(device, record_property, cmd):
    reply = device.command(cmd, timeout=PROBE_TIMEOUT)
    record_property("reply", json.dumps(reply))
    assert reply["status"] == "ERROR"
    assert reply.get("code") in KNOWN_ERROR_CODES
    assert_still_responsive(device)


# ---------- 4. Very long line ----------
@pytest.mark.parametrize("name, cmd, allowed", [
    ("garbage", "A" * 300, {"UNKNOWN_COMMAND"}),
    ("huge_pwm", "SET PWM " + "9" * 300, {"INVALID_VALUE"}),          # req 6: out of range
    ("long_led_arg", "SET LED " + "X" * 300, KNOWN_ERROR_CODES),
])
def test_long_line_does_not_break_device(device, record_property, name, cmd, allowed):
    reply = device.command(cmd, timeout=PROBE_TIMEOUT)
    record_property("reply", json.dumps(reply))
    assert reply["status"] == "ERROR"
    assert reply.get("code") in allowed
    assert_still_responsive(device)


# ---------- 5. Two commands without waiting for the first answer ----------
def test_two_commands_in_one_write_get_two_answers(device):
    device.ser.reset_input_buffer()
    device.log("TX", "STATUS + STATUS in one write")
    device.ser.write(b"STATUS\nSTATUS\n")
    device.ser.flush()
    device.ser.timeout = PROBE_TIMEOUT
    lines = []
    for _ in range(2):
        line = device.ser.readline().decode(errors="replace").strip()
        device.log("RX", line or "<timeout>")
        lines.append(line)
    assert all(lines), f"missing answer: {lines!r}"
    for line in lines:
        reply = json.loads(line)          # glued/garbled lines fail here
        assert reply["status"] == "OK" and "heap" in reply
    device.ser.timeout = 1.0
    assert_still_responsive(device)


# ---------- 6. Heap stays stable ----------
@SLOW
def test_heap_does_not_leak_over_many_status_calls(device, record_property):
    samples = int(os.getenv("HEAP_SAMPLES", "200"))
    heaps = [device.command("STATUS")["heap"] for _ in range(samples)]
    record_property("heap_first", heaps[0])
    record_property("heap_last", heaps[-1])
    record_property("heap_min", min(heaps))
    assert min(heaps) >= heaps[0] * 0.98, f"heap fell from {heaps[0]} to {min(heaps)}"
    assert heaps[-1] >= heaps[0] - 2048, f"heap fell from {heaps[0]} to {heaps[-1]}"


# ---------- 7. Sensor error path (requirement 3) ----------
@SENSOR_OFF
def test_disconnected_sensor_gives_explicit_error(device):
    """Needs a setup change: disconnect the sensor data wire in diagram.json and
    restart the simulation. Requirement 3 allows an explicit sensor error."""
    raw = device.command_raw("GET TEMP")
    reply = json.loads(raw)               # also checks the closing brace (BUG-3)
    assert reply["status"] == "ERROR"
    assert reply["code"] == "SENSOR_UNAVAILABLE"


# ---------- 8. Line ending CRLF ----------
@pytest.mark.parametrize("cmd", ["STATUS", "SET LED ON", "SET PWM 10"])
def test_crlf_line_ending_is_handled(device, record_property, cmd):
    """Terminals on Windows send \\r\\n. Not in the requirements, so only a
    structured reply and a responsive device are demanded; acceptance is recorded."""
    device.ser.reset_input_buffer()
    device.log("TX", f"{cmd}<CR><LF>")
    device.ser.write((cmd + "\r\n").encode())
    device.ser.flush()
    device.ser.timeout = PROBE_TIMEOUT
    line = device.ser.readline().decode(errors="replace").strip()
    device.ser.timeout = 1.0
    device.log("RX", line or "<timeout>")
    assert line, f"no answer to {cmd!r} with CRLF"
    reply = json.loads(line)
    record_property("reply", line)
    assert_structured(reply)
    assert_still_responsive(device)


# =====================================================================
# 4. Own parameterized tests: boundary, negative, RESET, GET TEMP, PING, timeout
# =====================================================================



# ---------- 1. PWM: additional valid values (req 5) ----------
@pytest.mark.parametrize("value", [2, 10, 98])
def test_pwm_extra_valid_values_are_applied(device, value):
    r = device.command(f"SET PWM {value}")
    assert r["status"] == "OK"
    assert r["pwm"] == value
    assert device.command("STATUS")["pwm"] == raw_duty(value)


# ---------- 2. PWM: out-of-range values (req 6) ----------
@pytest.mark.parametrize("value", [-100, -2, 102, 1000, 2147483648])
def test_pwm_extra_out_of_range_is_invalid_value(device, value):
    r = device.command(f"SET PWM {value}")
    assert r["status"] == "ERROR"
    assert r["code"] == "INVALID_VALUE"


# ---------- 3. PWM: non-numeric / malformed argument (negative) ----------
# Requirements do not name the error code here, so only "ERROR" is checked.
@pytest.mark.parametrize("arg", ["abc", "1.5", "5x", "--1", "0x10"])
def test_pwm_malformed_argument_is_rejected(device, arg):
    r = device.command(f"SET PWM {arg}")
    assert r["status"] == "ERROR"


# ---------- 4. Rejected value must not change the state ----------
@pytest.mark.parametrize("bad", [-1, 101, 999])
def test_pwm_rejected_value_keeps_previous_state(device, bad):
    assert device.command("SET PWM 10")["status"] == "OK"
    device.command(f"SET PWM {bad}")
    assert device.command("STATUS")["pwm"] == raw_duty(10)


# ---------- 5. Unknown commands (req 8) ----------
@pytest.mark.parametrize("cmd", ["FOO", "HELLO", "SET FAN ON", "PINGG"])
def test_unknown_command_is_structured_error(device, cmd):
    r = device.command(cmd)
    assert r["status"] == "ERROR"
    assert r["code"] == "UNKNOWN_COMMAND"


# ---------- 6. LED state (req 4) ----------
@pytest.mark.parametrize("state, expected", [("ON", True), ("OFF", False)])
def test_led_state_matches_command(device, state, expected):
    assert device.command(f"SET LED {state}")["led"] is expected
    assert device.command("STATUS")["led"] is expected


# ---------- 7. RESET (req 7): one test per symptom, so each has its own evidence ----------
# Values 1, 50, 99 avoid the PWM=100 problem, so a failure points to RESET itself.
@pytest.mark.parametrize("pwm", [1, 50, 99])
def test_reset_clears_pwm(device, pwm):
    device.command(f"SET PWM {pwm}")
    assert device.command("RESET")["status"] == "OK"
    assert device.command("STATUS")["pwm"] == 0


def test_reset_turns_led_off(device):
    device.command("SET LED ON")
    assert device.command("RESET")["status"] == "OK"
    assert device.command("STATUS")["led"] is False


# ---------- 8. The fixture precondition really gives a safe state ----------
def test_precondition_starts_from_safe_state(device):
    status = device.command("STATUS")
    assert status["pwm"] == 0
    assert status["led"] is False


# ---------- 9. GET TEMP: valid value or explicit sensor error (req 3) ----------
@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_temp_is_valid_value_or_sensor_error(device, attempt):
    r = device.command("GET TEMP")
    if r["status"] == "OK":
        assert isinstance(r["temperature"], (int, float))
        assert -40 <= r["temperature"] <= 80
    else:
        assert r["status"] == "ERROR"
        assert r["code"] == "SENSOR_UNAVAILABLE"


# ---------- 10. PING latency (OBS-1) ----------
# Measured in manual_check.log: PING always answers OK, but after 1.31-1.36 s,
# while every other command answers in 0.18-0.37 s. The 1.0 s harness timeout
# is therefore too short for PING. Requirements 1 and 9 give no latency limit.
PING_TIMEOUT = 3.0   # chosen from the measurement above (~2x the measured 1.3 s)
PING_XFAIL = pytest.mark.xfail(
    reason="OBS-1: PING answers in ~1.3 s, harness timeout is 1.0 s (no latency requirement)",
    strict=False,
)


def test_ping_answers_with_extended_timeout(device, record_property):
    r = device.command("PING", timeout=5.0)
    record_property("ping_latency_s", round(device.last_elapsed, 2))
    assert r["status"] == "OK"
    assert r["command"] == "PING"


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5])
def test_ping_is_stable(device, attempt, record_property):
    r = device.command("PING", timeout=PING_TIMEOUT)
    record_property("ping_latency_s", round(device.last_elapsed, 2))
    assert r["status"] == "OK"
    assert r["command"] == "PING"


@PING_XFAIL
@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_ping_latency_is_below_harness_timeout(device, attempt):
    r = device.command("PING")  # harness timeout 1.0 s
    assert r["status"] == "OK"


# ---------- 11. Timeout behaviour (req 9) ----------
@pytest.mark.parametrize("cmd", [
    pytest.param("PING", marks=PING_XFAIL),
    "STATUS",
    "GET TEMP",
])
def test_command_answers_within_timeout(device, cmd):
    start = time.monotonic()
    try:
        device.command_raw(cmd)
    except TimeoutError:
        pytest.fail(f"{cmd}: no answer within {device.ser.timeout}s")
    assert time.monotonic() - start <= device.ser.timeout + 0.5


def test_no_response_command_times_out_in_finite_time(device):
    """NO_RESPONSE is silent by design: must end with TimeoutError, not hang."""
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        device.command("NO_RESPONSE")
    assert time.monotonic() - start < device.ser.timeout + 1.0


def test_incomplete_command_gives_finite_timeout(device):
    """Send a command without newline: the device waits for line end,
    so readline() must return empty after the timeout, not block forever."""
    device.ser.timeout = 0.5
    device.ser.reset_input_buffer()
    device.log("TX", "PING (without newline)")
    device.ser.write(b"PING")
    device.ser.flush()
    start = time.monotonic()
    line = device.ser.readline()
    elapsed = time.monotonic() - start
    device.log("RX", f"{line!r} after {elapsed:.2f}s")
    assert line == b""
    assert elapsed < 2.0
    # finish the command so the device buffer is clean for cleanup
    device.ser.write(b"\n")
    device.ser.flush()
    device.timed_out = True   # the late PING reply will be drained in cleanup


class _SilentSerial:
    """Fake port that never answers (no hardware needed)."""
    timeout = 1.0

    def reset_input_buffer(self):
        pass

    def write(self, data):
        pass

    def flush(self):
        pass

    def readline(self):
        return b""


def test_device_raises_timeout_when_port_is_silent():
    dev = Device.__new__(Device)  # skip __init__: no real port
    dev.ser = _SilentSerial()
    dev.transcript = []
    dev.timed_out = False
    with pytest.raises(TimeoutError):
        dev.command_raw("PING")


# =====================================================================
# 5. Course tests: LED and RESET
# =====================================================================

def test_led_on_off(device):
    assert device.command("SET LED ON")["led"] is True
    assert device.command("SET LED OFF")["led"] is False

def test_reset_restores_safe_state(device):
    device.command("SET LED ON")
    device.command("RESET")
    status = device.command("STATUS")
    assert status["pwm"] == 0
    assert status["led"] is False


# =====================================================================
# 6. Course tests: sensor
# =====================================================================

def test_temperature_is_valid_json(device):
    r = device.command("GET TEMP")
    assert r["status"] in {"OK", "ERROR"}
    if r["status"] == "OK":
        assert -40 <= r["temperature"] <= 80


# =====================================================================
# 7. Course tests: PING and STATUS
# =====================================================================

def test_ping(device):
    r = device.command("PING")
    assert r["status"] == "OK"
    assert r["command"] == "PING"

def test_status(device):
    r = device.command("STATUS")
    assert r["status"] == "OK"
    assert "heap" in r
    assert "pwm" in r


# =====================================================================
# 8. Course tests: timeout
# =====================================================================

def test_no_response_becomes_timeout(device):
    with pytest.raises(TimeoutError):
        device.command("NO_RESPONSE")
