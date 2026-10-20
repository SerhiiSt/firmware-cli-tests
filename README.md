# Firmware CLI tests (pytest)

Automated tests for the device serial CLI: `PING`, `STATUS`, `GET TEMP`,
`SET LED`, `SET PWM`, `RESET`, unknown commands and timeout behaviour.

## Files
- `test_firmware_cli.py` - all 96 tests in one module: course tests, own boundary/negative tests, candidate-bug checks and exploratory checks
- `conftest.py` - fixture `device`: RESET before and after every test, evidence log for every failure
- `device.py` - serial helper (`command`, `command_raw`) with a finite timeout

- `evidence/` - logs of the last run and `evidence/INDEX.md` (finding -> evidence files)

## Run
```bash
pip install -r requirements.txt
export DEVICE_PORT=rfc2217://localhost:4000   # Wokwi, or COM5 / /dev/ttyUSB0
python -m pytest -v test_firmware_cli.py
```
Optional: `RUN_SLOW=1` (heap sampling, full PWM sweep) and `SENSOR_DISCONNECTED=1` (sensor error test).
Failed tests save a TX/RX log into `evidence/`. A failure is evidence of a
mismatch, not automatic proof of a firmware defect (requirement 10).
