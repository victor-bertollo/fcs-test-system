#!/usr/bin/env python3
import argparse
import glob
import time
from pathlib import Path

import serial
import yaml

PIN_COUNT = 16
ALLOWED_FRAME_CHARS = {"z", "0", "1", "v", "g", "c"}
ALLOWED_EXPECT_CHARS = {"0", "1", "x", "X", "?", "-", "z", "Z"}


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: YAML root must be a mapping")
    if "tests" not in data or not isinstance(data["tests"], list):
        raise ValueError(f"{path}: YAML must contain a list field: tests")
    return data


def normalize_expect(expect) -> str:
    if isinstance(expect, str):
        s = expect.strip()
        if len(s) != PIN_COUNT:
            raise ValueError(f"expect string must have {PIN_COUNT} chars, got {len(s)}: {s!r}")
        for ch in s:
            if ch not in ALLOWED_EXPECT_CHARS:
                raise ValueError(f"bad expect char {ch!r} in {s!r}")
        return s.lower().replace("?", "x").replace("-", "x").replace("z", "x")
    if isinstance(expect, dict):
        result = ["x"] * PIN_COUNT
        for key, value in expect.items():
            if isinstance(key, str) and key.lower().startswith("p"):
                pin = int(key[1:])
            else:
                pin = int(key)
            if not 1 <= pin <= PIN_COUNT:
                raise ValueError(f"bad expected pin number {pin}")
            if value in (0, "0", False):
                result[pin - 1] = "0"
            elif value in (1, "1", True):
                result[pin - 1] = "1"
            else:
                raise ValueError(f"bad expected value for pin {pin}: {value!r}")
        return "".join(result)
    raise ValueError(f"expect must be string or mapping, got {type(expect).__name__}")


def validate_frame(frame: str) -> str:
    frame = frame.strip().lower()
    if len(frame) != PIN_COUNT:
        raise ValueError(f"frame must have {PIN_COUNT} chars, got {len(frame)}: {frame!r}")
    for ch in frame:
        if ch not in ALLOWED_FRAME_CHARS:
            raise ValueError(f"bad frame char {ch!r} in {frame!r}")
    return frame


def reset_esp_protocol(ser: serial.Serial) -> None:
    ser.write(b"r\n")
    ser.flush()
    end = time.time() + 0.35
    while time.time() < end:
        line = ser.readline()
        if not line:
            continue


def parse_read_line(line: str) -> str | None:
    line = line.strip()

    if len(line) == PIN_COUNT and all(ch in "01" for ch in line):
        return line
    return None


def send_frame_and_read(ser: serial.Serial, frame: str, timeout: float, raw: bool = False) -> str:
    ser.write(frame.encode("ascii"))
    ser.flush()
    time.sleep(timeout)
    
    line = ser.readline().decode(errors="replace").strip()
    
    if raw:
        print(f"ESP> {line}")
    
    bits = parse_read_line(line)
    
    if bits is not None:
        return bits

    raise TimeoutError(f"timeout waiting for READ after frame {frame!r}; last line={last_line!r}")


def compare_bits(read_bits: str, expect: str) -> tuple[bool, list[str]]:
    errors = []
    for i, exp in enumerate(expect):
        if exp == "x":
            continue
        got = read_bits[i]
        if got != exp:
            errors.append(f"pin {i + 1}: expected {exp}, got {got}")
    return len(errors) == 0, errors


def expected_to_text(expect: str) -> str:
    parts = [f"p{i + 1}={ch}" for i, ch in enumerate(expect) if ch != "x"]
    return " ".join(parts) if parts else "(nothing checked)"


def run_config(ser: serial.Serial, path: str, args) -> bool:
    cfg = load_yaml(path)
    chip_name = cfg.get("chip", Path(path).stem)
    stop_on_fail = bool(cfg.get("stop_on_fail", True))
    reset_before_run = bool(cfg.get("reset_before_run", True))
    print()
    print("=" * 72)
    print(f"TEST {chip_name}  ({path})")
    print("=" * 72)
    if reset_before_run:
        reset_esp_protocol(ser)
    total = 0
    failed = 0
    for test in cfg["tests"]:
        total += 1
        name = str(test.get("name", f"test #{total}"))
        frame = validate_frame(str(test["frame"]))
        expect = normalize_expect(test["expect"])
        read_bits = send_frame_and_read(ser, frame, timeout=args.timeout, raw=args.raw)
        ok, errors = compare_bits(read_bits, expect)
        if ok:
            if args.show_passes:
                print(f"PASS {total:04d}: {name}")
                print(f"  frame = {frame}")
                print(f"  read  = {read_bits}")
        else:
            failed += 1
            print(f"FAIL {total:04d}: {name}")
            print(f"  frame    = {frame}")
            print(f"  read     = {read_bits}")
            print(f"  expected = {expected_to_text(expect)}")
            print(f"  errors   = {'; '.join(errors)}")
            if stop_on_fail:
                print("  stop_on_fail=true -> stopping this chip test")
                break
    passed = failed == 0
    print()
    if passed:
        print(f"RESULT {chip_name}: PASS ({total} tests)")
    else:
        print(f"RESULT {chip_name}: FAIL ({failed} failed, {total} tests executed)")
    return passed


def expand_config_paths(paths: list[str]) -> list[str]:
    result = []
    for p in paths:
        matches = sorted(glob.glob(p))
        result.extend(matches if matches else [p])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="ESP32/MCP23017 explicit YAML chip tester")
    parser.add_argument("configs", nargs="+", help="YAML files, e.g. configs/not.yaml or configs/*.yaml")
    parser.add_argument("--port", default="/dev/ttyACM0", help="serial port, default: /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200, help="baud rate, default: 115200")
    parser.add_argument("--timeout", type=float, default=0.007, help="milliseconds to wait for READ response")
    parser.add_argument("--show-passes", action="store_true", help="print every passed test row")
    parser.add_argument("--raw", action="store_true", help="print raw ESP lines")
    args = parser.parse_args()
    config_paths = expand_config_paths(args.configs)
    print(f"Opening serial port {args.port} @ {args.baud}...")
    serial_timeout = 10 * (10 ** -6)
    with serial.Serial(args.port, args.baud, timeout=serial_timeout) as ser:
        time.sleep(2.0)
        ser.reset_input_buffer()
        results = []
        for path in config_paths:
            results.append((path, run_config(ser, path, args)))
    print()
    print("=" * 72)
    print("GLOBAL SUMMARY")
    print("=" * 72)
    for path, ok in results:
        print(f"{Path(path).name:16s} : {'PASS' if ok else 'FAIL'}")
    passed = [Path(path).stem for path, ok in results if ok]
    print()
    if len(passed) == 1:
        print(f"VERDICT: CHIP MATCHES {passed[0]}")
    elif len(passed) == 0:
        print("VERDICT: NO CONFIG PASSED")
    else:
        print("VERDICT: AMBIGUOUS")
        print("Passed configs:", ", ".join(passed))
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
