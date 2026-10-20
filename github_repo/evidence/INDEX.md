# Evidence index

Run: Wokwi (ESP32), `rfc2217://localhost:4000`, Python 3.9.12, pytest 8.4.2, pyserial 3.5.
Result of the last full run (`pytest_output.txt`): 96 tests, 72 passed, 17 failed, 1 error,
2 skipped, 4 xfailed, 623.14 s.

## Findings and their evidence

| ID | Finding | Requirement | Evidence |
|---|---|---|---|
| BUG-1 | `SET PWM 100` returns `INVALID_VALUE` instead of `OK` | 5 | `tests_test_boundaries.py_test_pwm_valid_boundaries_100_.log`; raw serial: `manual_check_476e74.log` 12:34:18, `manual_check_1.log` 19:23:45, `manual_check.log` 19:04:42 |
| BUG-2 | `RESET` answers `reset:true` but leaves LED and PWM unchanged | 7 | `tests_test_extra.py_test_reset_clears_pwm_{1,50,99}_.log`, `tests_test_extra.py_test_reset_turns_led_off.log`, `tests_test_relay.py_test_reset_restores_safe_state.log`; raw serial: `manual_check_476e74.log` 12:34:29-30 |
| BUG-3 | `GET TEMP` returns JSON without the closing `}` | 3 | `tests_test_sensor.py_test_temperature_is_valid_json.log`, `tests_test_extra.py_test_temp_is_valid_value_or_sensor_error_{1,2,3}_.log`; raw serial: `manual_check_476e74.log` 12:34:13-14 |
| BUG-4 | `PING` blocks command processing for about 1.3 s (`STATUS` right after it answers in 1.45-1.46 s instead of ~0.2 s) | 1, 9 (no latency limit defined) | `tests_test_candidates_bugs.py_test_status_right_after_ping_is_not_blocked.log`, `tests_test_candidates_bugs.py_test_ping_is_not_an_outlier_vs_status.log`, `tests_test_status.py_test_ping.log`; raw serial: PING 1.31-1.40 s in `manual_check_476e74.log` 12:34:03-11, `manual_check_1.log`, `manual_check.log`, `manual_check_fa4e79.log` |
| QUESTION-1 | `SET PWM` uses percent, `STATUS` shows 0..255 (1 -> 2, 50 -> 127, 99 -> 252) | 2 (unit not defined) | `manual_check_476e74.log` 12:34:21-25; `test_pwm_percent_to_status_mapping` passes in `pytest_output.txt` |
| QUESTION-2 | LED and PWM behave as one output: `led = (pwm > 0)`, `SET LED ON` -> pwm 255, `SET LED OFF` -> pwm 0 | 2, 4, 7 | `tests_test_candidates_bugs.py_test_led_on_and_off_keep_pwm_value_{0,30,99}_.log`, `tests_test_exploratory.py_test_set_pwm_does_not_turn_led_on.log` |
| ERROR | `test_no_response_becomes_timeout` errored in setup: no answer to `RESET` within 1.0 s. Not reproduced (passed in the previous run): environment / communication | - | `setup_error_test_no_response_becomes_timeout.txt` (excerpt of `pytest_output.txt`; setup errors have no per-test log) |

XFAIL tests (PING answered after the 1.0 s timeout) have no per-test log; see
`pytest_output.txt` and the PING lines in the manual logs.

## Files

| File | What it is |
|---|---|
| `pytest_output.txt` | full pytest output of the last run |
| `manual_check_476e74.log` | last manual run (12:34) |
| `manual_check_1.log`, `manual_check.log`, `manual_check_fa4e79.log` | earlier manual runs (19:23, 19:04, 11:29) with the same results |
| `tests_test_*.log` | one TX/RX transcript plus the pytest failure text per failed test |

## Provenance

- `pytest_output.txt` and `manual_check_476e74.log` come from the last run.
- The 17 per-test logs come from the previous full run (96 tests, identical set of
  17 failures), because the last run's per-test logs were not saved. Measured values differ
  by at most 0.02 s (for example `STATUS` after `PING`: 1.45 s in the log, 1.46 s in
  `pytest_output.txt`).
- File names start with `tests_test_...` because the tests were run as separate files in
  `tests/`. Running the merged `test_firmware_cli.py` creates names starting with
  `test_firmware_cli.py_...`.
- Stale logs from earlier runs were removed on purpose. Clear `evidence/` before each run.
