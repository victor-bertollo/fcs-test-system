#!/usr/bin/env python3

import argparse
import glob
import time
from pathlib import Path

import serial
import yaml


PIN_COUNT = 16

ALLOWED_FRAME_CHARS = {
    "z",
    "0",
    "1",
    "v",
    "g",
    "c",
}

ALLOWED_EXPECT_CHARS = {
    "0",
    "1",
    "x",
    "X",
    "?",
    "-",
    "z",
    "Z",
}


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: YAML root must be a mapping"
        )

    if "tests" not in data:
        raise ValueError(
            f"{path}: YAML must contain field 'tests'"
        )

    if not isinstance(data["tests"], list):
        raise ValueError(
            f"{path}: 'tests' must be a list"
        )

    return data


def validate_frame(frame: str) -> str:
    frame = frame.strip().lower()

    if len(frame) != PIN_COUNT:
        raise ValueError(
            f"frame must contain exactly {PIN_COUNT} chars, "
            f"got {len(frame)}: {frame!r}"
        )

    for ch in frame:
        if ch not in ALLOWED_FRAME_CHARS:
            raise ValueError(
                f"invalid frame char {ch!r} in {frame!r}"
            )

    return frame


def normalize_expect(expect) -> str:
    # Form:
    #
    # expect: "xx01xxxx..."
    #
    if isinstance(expect, str):
        s = expect.strip()

        if len(s) != PIN_COUNT:
            raise ValueError(
                f"expect must contain exactly {PIN_COUNT} chars, "
                f"got {len(s)}: {s!r}"
            )

        for ch in s:
            if ch not in ALLOWED_EXPECT_CHARS:
                raise ValueError(
                    f"invalid expect char {ch!r} in {s!r}"
                )

        s = s.lower()

        s = s.replace("?", "x")
        s = s.replace("-", "x")
        s = s.replace("z", "x")

        return s

    # Alternative form:
    #
    # expect:
    #   3: 1
    #   7: 0
    #
    # or:
    #
    # expect:
    #   p3: 1
    #   p7: 0
    #
    if isinstance(expect, dict):
        result = ["x"] * PIN_COUNT

        for key, value in expect.items():
            if (
                isinstance(key, str)
                and key.lower().startswith("p")
            ):
                pin = int(key[1:])
            else:
                pin = int(key)

            if not 1 <= pin <= PIN_COUNT:
                raise ValueError(
                    f"invalid expected pin: {pin}"
                )

            if value in (0, "0", False):
                result[pin - 1] = "0"

            elif value in (1, "1", True):
                result[pin - 1] = "1"

            else:
                raise ValueError(
                    f"invalid expected value "
                    f"for pin {pin}: {value!r}"
                )

        return "".join(result)

    raise ValueError(
        f"expect must be string or mapping, "
        f"got {type(expect).__name__}"
    )


def reset_esp_protocol(
    ser: serial.Serial,
) -> None:
    # 'r' resets the current 16-byte frame on ESP.
    #
    # ESP does not send any answer for r.
    ser.reset_input_buffer()

    ser.write(b"r")
    ser.flush()

    time.sleep(0.05)

    # Anything left from an interrupted previous test
    # must not become the result of the next test.
    ser.reset_input_buffer()


def read_exactly(
    ser: serial.Serial,
    count: int,
    timeout: float,
) -> bytes:
    result = bytearray()
    deadline = time.monotonic() + timeout

    while len(result) < count:
        remaining_time = deadline - time.monotonic()

        if remaining_time <= 0:
            break

        data = ser.read(count - len(result))

        if data:
            result.extend(data)

    if len(result) != count:
        raise TimeoutError(
            f"expected {count} bytes from ESP, "
            f"got {len(result)}: {bytes(result)!r}"
        )

    return bytes(result)


def send_frame_and_read(
    ser: serial.Serial,
    frame: str,
    timeout: float,
    raw: bool = False,
) -> str:
    if raw:
        print(f"PC  > {frame}")

    # Exactly 16 bytes.
    #
    # No newline.
    ser.write(frame.encode("ascii"))
    ser.flush()

    # ESP answers with exactly 16 chars:
    #
    # 0101011010100101
    #
    # There is deliberately no '\n'.
    raw_answer = read_exactly(
        ser,
        PIN_COUNT,
        timeout,
    )

    try:
        bits = raw_answer.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeError(
            f"ESP returned non-ASCII data: "
            f"{raw_answer!r}"
        )

    if (
        len(bits) != PIN_COUNT
        or any(ch not in "01" for ch in bits)
    ):
        raise RuntimeError(
            f"bad response from ESP: {bits!r}"
        )

    if raw:
        print(f"ESP > {bits}")

    return bits


def compare_bits(
    read_bits: str,
    expect: str,
) -> tuple[bool, list[str]]:
    errors = []

    for i, expected_bit in enumerate(expect):
        # x means:
        # we don't care what this output line contains.
        if expected_bit == "x":
            continue

        actual_bit = read_bits[i]

        if actual_bit != expected_bit:
            errors.append(
                f"line {i + 1}: "
                f"expected {expected_bit}, "
                f"got {actual_bit}"
            )

    return len(errors) == 0, errors


def expected_to_text(expect: str) -> str:
    result = []

    for i, value in enumerate(expect):
        if value == "x":
            continue

        result.append(
            f"p{i + 1}={value}"
        )

    if not result:
        return "(nothing checked)"

    return " ".join(result)


def run_config(
    ser: serial.Serial,
    path: str,
    args,
) -> bool:
    cfg = load_yaml(path)

    board_name = cfg.get(
        "board",
        cfg.get(
            "name",
            Path(path).stem,
        ),
    )

    stop_on_fail = bool(
        cfg.get("stop_on_fail", True)
    )

    reset_before_run = bool(
        cfg.get("reset_before_run", True)
    )

    print()
    print("=" * 72)
    print(
        f"TEST BOARD {board_name}  ({path})"
    )
    print("=" * 72)

    if reset_before_run:
        reset_esp_protocol(ser)

    total = 0
    failed = 0

    for test in cfg["tests"]:
        total += 1

        name = str(
            test.get(
                "name",
                f"test #{total}",
            )
        )

        if "frame" not in test:
            raise ValueError(
                f"{path}: test {total} "
                f"does not contain 'frame'"
            )

        if "expect" not in test:
            raise ValueError(
                f"{path}: test {total} "
                f"does not contain 'expect'"
            )

        frame = validate_frame(
            str(test["frame"])
        )

        expect = normalize_expect(
            test["expect"]
        )

        try:
            read_bits = send_frame_and_read(
                ser,
                frame,
                timeout=args.timeout,
                raw=args.raw,
            )

        except Exception as e:
            failed += 1

            print(
                f"FAIL {total:04d}: {name}"
            )
            print(
                f"  frame = {frame}"
            )
            print(
                f"  communication error: {e}"
            )

            if stop_on_fail:
                print(
                    "  stop_on_fail=true "
                    "-> stopping this board test"
                )
                break

            continue

        ok, errors = compare_bits(
            read_bits,
            expect,
        )

        if ok:
            if args.show_passes:
                print(
                    f"PASS {total:04d}: {name}"
                )
                print(
                    f"  frame    = {frame}"
                )
                print(
                    f"  read     = {read_bits}"
                )
                print(
                    f"  expected = {expected_to_text(expect)}"
                )

        else:
            failed += 1

            print(
                f"FAIL {total:04d}: {name}"
            )
            print(
                f"  frame    = {frame}"
            )
            print(
                f"  read     = {read_bits}"
            )
            print(
                f"  expected = {expected_to_text(expect)}"
            )
            print(
                f"  errors   = {'; '.join(errors)}"
            )

            if stop_on_fail:
                print(
                    "  stop_on_fail=true "
                    "-> stopping this board test"
                )
                break

    passed = failed == 0

    print()

    if passed:
        print(
            f"RESULT {board_name}: "
            f"PASS ({total} tests)"
        )

    else:
        print(
            f"RESULT {board_name}: "
            f"FAIL "
            f"({failed} failed, "
            f"{total} tests executed)"
        )

    return passed


def expand_config_paths(
    paths: list[str],
) -> list[str]:
    result = []

    for path in paths:
        matches = sorted(
            glob.glob(path)
        )

        if matches:
            result.extend(matches)
        else:
            result.append(path)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ESP32/MCP23017 YAML "
            "breadboard tester"
        )
    )

    parser.add_argument(
        "configs",
        nargs="+",
        help=(
            "YAML test files, e.g. "
            "configs/board1.yaml "
            "or configs/*.yaml"
        ),
    )

    parser.add_argument(
        "--port",
        default="/dev/cu.usbmodem1101",
        help=(
            "serial port, default: "
            "/dev/cu.usbmodem1101"
        ),
    )

    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="baud rate",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=0.5,
        help=(
            "maximum seconds to wait "
            "for the 16-byte ESP response"
        ),
    )

    parser.add_argument(
        "--show-passes",
        action="store_true",
        help="print passed tests",
    )

    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "print raw PC -> ESP "
            "and ESP -> PC traffic"
        ),
    )

    args = parser.parse_args()

    config_paths = expand_config_paths(
        args.configs
    )

    print(
        f"Opening serial port "
        f"{args.port} @ {args.baud}..."
    )

    with serial.Serial(
        args.port,
        args.baud,
        timeout=0.01,
    ) as ser:

        # ESP32-C3 usually resets when the
        # serial connection is opened.
        time.sleep(2.0)

        ser.reset_input_buffer()

        results = []

        for path in config_paths:
            ok = run_config(
                ser,
                path,
                args,
            )

            results.append(
                (path, ok)
            )

    print()
    print("=" * 72)
    print("GLOBAL SUMMARY")
    print("=" * 72)

    for path, ok in results:
        print(
            f"{Path(path).name:24s} : "
            f"{'PASS' if ok else 'FAIL'}"
        )

    passed = [
        Path(path).stem
        for path, ok in results
        if ok
    ]

    print()

    if len(results) == 1:
        if results[0][1]:
            print("VERDICT: BOARD PASSED")
        else:
            print("VERDICT: BOARD FAILED")

    elif len(passed) == 1:
        print(
            f"VERDICT: BOARD MATCHES "
            f"{passed[0]}"
        )

    elif len(passed) == 0:
        print(
            "VERDICT: NO BOARD CONFIG PASSED"
        )

    else:
        print("VERDICT: AMBIGUOUS")
        print(
            "Passed configs: "
            + ", ".join(passed)
        )

    return (
        0
        if all(ok for _, ok in results)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
