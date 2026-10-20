import os
import re
import time
from pathlib import Path

import pytest
from device import Device

EVIDENCE_DIR = Path(os.getenv("EVIDENCE_DIR", "evidence"))
READY_WAIT = float(os.getenv("DEVICE_READY_WAIT", "1.0"))


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Keep setup/call/teardown reports on the test item so the fixture
    can see whether the test failed."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


def _save_evidence(node, rep, dev):
    """Write TX/RX transcript + pytest failure text for a failed test."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]+", "_", node.nodeid)
    path = EVIDENCE_DIR / f"{name}.log"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Test: {node.nodeid}\n")
        f.write(f"Saved: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Port: {os.getenv('DEVICE_PORT')}\n\n")
        f.write("=== Device transcript (TX = to device, RX = from device) ===\n")
        f.write("\n".join(dev.transcript) + "\n\n")
        f.write("=== pytest failure ===\n")
        f.write(str(rep.longrepr) + "\n")


def force_safe_state(dev):
    """RESET, then set the safe state explicitly.

    Measured on the device: RESET answers {"reset":true} but leaves LED/PWM
    unchanged (defect BUG-2). The fixture therefore must not trust RESET alone,
    otherwise state leaks from one test into the next and unrelated tests fail.
    Tests that check RESET itself call device.command("RESET") directly."""
    dev.command("RESET")
    dev.command("SET LED OFF")
    dev.command("SET PWM 0")


@pytest.fixture
def device(request):
    port = os.getenv("DEVICE_PORT")
    if not port:
        pytest.skip("Set DEVICE_PORT, e.g. COM5, /dev/ttyUSB0 or rfc2217://localhost:4000 (Wokwi)")
    dev = Device(port, timeout=1.0)
    try:
        # Controlled precondition: finish boot, then known safe state.
        dev.wait_ready(READY_WAIT)
        try:
            force_safe_state(dev)
        except (TimeoutError, AssertionError) as e:
            pytest.fail(f"Precondition failed (environment/communication): {e}")
        yield dev
    finally:
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.failed:
            _save_evidence(request.node, rep, dev)
        # Cleanup: leave the device safe even if the test failed.
        try:
            if dev.timed_out:      # a late reply may be on its way: avoid desync
                dev.drain(2.0)
            force_safe_state(dev)
        except Exception as e:  # cleanup must never hide the real test result
            dev.log("INFO", f"cleanup failed: {e!r}")
        dev.close()
