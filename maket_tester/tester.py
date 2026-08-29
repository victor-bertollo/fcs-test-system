#!/usr/bin/env python3

import argparse
import glob
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial
import yaml


PIN_COUNT = 32
ALLOWED_PIN_TYPES = set("iocgvz")
ALLOWED_EXPECTED_CHARS = set("01xX?-zZ")
SIGNAL_BLOCKS = ((0, 8), (8, 16), (16, 24), (24, 32))
TABLE_CHUNK_SIZE = 16

ANSI_RESET = "\033[0m"
ANSI_BRIGHT_WHITE = "\033[97m"
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"


@dataclass(frozen=True)
class TestVector:
    name: str
    input_values: str
    expected: str


@dataclass(frozen=True)
class TestConfig:
    path: str
    board_name: str
    pins_type: str
    input_count: int
    input_positions: tuple[int, ...]
    input_labels: tuple[str, ...]
    output_positions: tuple[int, ...]
    output_labels: tuple[str, ...]
    stop_on_fail: bool
    tests: tuple[TestVector, ...]


def _require_string(value, field: str, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{path}: '{field}' must be a quoted string, "
            f"got {type(value).__name__}"
        )

    return value.strip()


def normalize_pins_type(value, path: str) -> str:
    pins_type = _require_string(value, "pins_type", path).lower()

    if len(pins_type) != PIN_COUNT:
        raise ValueError(
            f"{path}: pins_type must contain exactly {PIN_COUNT} chars, "
            f"got {len(pins_type)}: {pins_type!r}"
        )

    invalid = sorted(set(pins_type) - ALLOWED_PIN_TYPES)
    if invalid:
        raise ValueError(
            f"{path}: invalid pins_type chars: {''.join(invalid)!r}"
        )

    for start, end in SIGNAL_BLOCKS:
        block = pins_type[start:end]
        if set(block) & set("ic") and "o" in block:
            raise ValueError(
                f"{path}: pins {start + 1}-{end} mix input/clock pins "
                "with output pins; outputs must start in a new block"
            )

    if "o" in pins_type and pins_type.index("o") % 8 != 0:
        first_output = pins_type.index("o") + 1
        raise ValueError(
            f"{path}: first output is pin {first_output}; "
            "an output region must start at pin 1, 9, 17, or 25"
        )

    return pins_type


def normalize_input(value, expected_length: int, path: str, test_number: int) -> str:
    input_values = _require_string(value, "input", path)

    if len(input_values) != expected_length:
        raise ValueError(
            f"{path}: test {test_number} input must contain exactly "
            f"{expected_length} chars, got {len(input_values)}: {input_values!r}"
        )

    if any(ch not in "01" for ch in input_values):
        raise ValueError(
            f"{path}: test {test_number} input may contain only '0' and '1': "
            f"{input_values!r}"
        )

    return input_values


def normalize_expected(value, expected_length: int, path: str, test_number: int) -> str:
    expected = _require_string(value, "expected", path)

    if len(expected) != expected_length:
        raise ValueError(
            f"{path}: test {test_number} expected must contain exactly "
            f"{expected_length} chars, got {len(expected)}: {expected!r}"
        )

    invalid = sorted(set(expected) - ALLOWED_EXPECTED_CHARS)
    if invalid:
        raise ValueError(
            f"{path}: test {test_number} has invalid expected chars: "
            f"{''.join(invalid)!r}"
        )

    return (
        expected.lower()
        .replace("?", "x")
        .replace("-", "x")
        .replace("z", "x")
    )


def normalize_pin_labels(
    value,
    field: str,
    expected_length: int,
    path: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: '{field}' must be a list of strings")

    if len(value) != expected_length:
        raise ValueError(
            f"{path}: '{field}' must contain exactly {expected_length} labels, "
            f"got {len(value)}"
        )

    labels = []
    for index, label in enumerate(value, start=1):
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"{path}: '{field}' label {index} must be a non-empty string"
            )
        labels.append(label.strip())

    return tuple(labels)


def load_config(path: str) -> TestConfig:
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: YAML root must be a mapping")

    if "pins_type" not in data:
        raise ValueError(f"{path}: YAML must contain field 'pins_type'")

    if "tests" not in data or not isinstance(data["tests"], list):
        raise ValueError(f"{path}: YAML must contain a list field 'tests'")

    pins_type = normalize_pins_type(data["pins_type"], path)
    input_positions = tuple(
        position
        for position, pin_type in enumerate(pins_type)
        if pin_type == "i"
    )
    input_count = len(input_positions)
    output_positions = tuple(
        position
        for position, pin_type in enumerate(pins_type)
        if pin_type == "o"
    )
    input_labels = (
        normalize_pin_labels(
            data["input_labels"],
            "input_labels",
            input_count,
            path,
        )
        if "input_labels" in data
        else tuple(table_pin_text(position) for position in input_positions)
    )
    output_labels = (
        normalize_pin_labels(
            data["output_labels"],
            "output_labels",
            len(output_positions),
            path,
        )
        if "output_labels" in data
        else tuple(table_pin_text(position) for position in output_positions)
    )

    tests = []
    for test_number, test in enumerate(data["tests"], start=1):
        if not isinstance(test, dict):
            raise ValueError(f"{path}: test {test_number} must be a mapping")
        if "input" not in test:
            raise ValueError(f"{path}: test {test_number} does not contain 'input'")
        if "expected" not in test:
            raise ValueError(f"{path}: test {test_number} does not contain 'expected'")

        tests.append(
            TestVector(
                name=str(test.get("name", f"test #{test_number}")),
                input_values=normalize_input(
                    test["input"], input_count, path, test_number
                ),
                expected=normalize_expected(
                    test["expected"], len(output_positions), path, test_number
                ),
            )
        )

    board_name = str(data.get("board", data.get("name", Path(path).stem)))

    return TestConfig(
        path=path,
        board_name=board_name,
        pins_type=pins_type,
        input_count=input_count,
        input_positions=input_positions,
        input_labels=input_labels,
        output_positions=output_positions,
        output_labels=output_labels,
        stop_on_fail=bool(data.get("stop_on_fail", True)),
        tests=tuple(tests),
    )


def read_exactly(ser: serial.Serial, count: int, timeout: float) -> bytes:
    result = bytearray()
    deadline = time.monotonic() + timeout

    while len(result) < count:
        if time.monotonic() >= deadline:
            break

        data = ser.read(count - len(result))
        if data:
            result.extend(data)

    if len(result) != count:
        raise TimeoutError(
            f"expected {count} bytes from ESP, got {len(result)}: {bytes(result)!r}"
        )

    return bytes(result)


def configure_esp(ser: serial.Serial, pins_type: str, raw: bool = False) -> None:
    ser.reset_input_buffer()
    ser.write(b"r")
    ser.flush()
    time.sleep(0.05)
    ser.reset_input_buffer()

    if raw:
        print("PC  > r")
        print(f"PC  > pins_type={pins_type}")

    ser.write(pins_type.encode("ascii"))
    ser.flush()


def send_test_vector(
    ser: serial.Serial,
    input_values: str,
    output_positions: tuple[int, ...],
    timeout: float,
    raw: bool = False,
) -> str:
    payload = input_values if input_values else "0"

    if raw:
        suffix = " (trigger)" if not input_values else ""
        print(f"PC  > input={payload}{suffix}")

    ser.write(payload.encode("ascii"))
    ser.flush()

    raw_answer = read_exactly(ser, len(output_positions), timeout)

    try:
        answer = raw_answer.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"ESP returned non-ASCII data: {raw_answer!r}") from error

    if raw:
        print(f"ESP > output={answer}")

    if "E" in answer:
        failed_banks = []
        for index, value in enumerate(answer):
            if value != "E":
                continue

            position = output_positions[index]
            address = 0x20 + position // 16
            bank = "A" if position % 16 < 8 else "B"
            label = f"MCP 0x{address:02X} GPIO{bank}"
            if label not in failed_banks:
                failed_banks.append(label)

        raise RuntimeError(
            "ESP reported I2C read error: " + ", ".join(failed_banks)
        )

    if any(ch not in "01" for ch in answer):
        raise RuntimeError(f"bad response from ESP: {answer!r}")

    return answer


def physical_pin_text(position: int) -> str:
    mcp_address = 0x20 + position // 16
    pin_number = position % 16 + 1
    return f"MCP 0x{mcp_address:02X} pin {pin_number}"


def table_pin_text(position: int) -> str:
    return str(position % 16 + 1)


def colors_enabled(no_color: bool = False, stream=None) -> bool:
    stream = stream or sys.stdout
    return (
        not no_color
        and "NO_COLOR" not in os.environ
        and hasattr(stream, "isatty")
        and stream.isatty()
    )


def live_progress_enabled(raw: bool = False, stream=None) -> bool:
    stream = stream or sys.stdout
    return (
        not raw
        and hasattr(stream, "isatty")
        and stream.isatty()
    )


def live_progress_text(passed: int, total: int) -> str:
    return f"Passed tests: {passed}/{total}"


def show_live_progress(
    passed: int,
    total: int,
    color: bool = False,
    stream=None,
) -> None:
    stream = stream or sys.stdout
    text = live_progress_text(passed, total)
    stream.write("\r" + color_text(text, ANSI_GREEN, color))
    stream.flush()


def clear_live_progress(total: int, stream=None) -> None:
    stream = stream or sys.stdout
    width = len(live_progress_text(total, total))
    stream.write("\r" + " " * width + "\r")
    stream.flush()


def color_text(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{ANSI_RESET}"


def update_live_dashboard(text: str, previous_lines: int, stream=None) -> int:
    stream = stream or sys.stdout
    clear_live_dashboard(previous_lines, stream=stream)
    stream.write(text)
    stream.flush()
    return max(1, len(text.splitlines()))


def clear_live_dashboard(line_count: int, stream=None) -> None:
    if line_count <= 0:
        return
    stream = stream or sys.stdout
    # The cursor is at the end of the dashboard's last line. Clear that line,
    # then move upwards and clear every preceding dashboard line.
    stream.write("\r\033[2K")
    for _ in range(line_count - 1):
        stream.write("\033[1A\r\033[2K")
    stream.write("\r")
    stream.flush()


def render_pin_tables(
    positions: tuple[int, ...],
    labels: tuple[str, ...],
    rows: list[tuple[str, list[str]]],
    styles: list[str | None],
    color: bool,
) -> str:
    if len(labels) != len(positions):
        raise ValueError("labels and positions lengths must match")
    if len(styles) != len(positions):
        raise ValueError("styles and positions lengths must match")
    if any(len(values) != len(positions) for _, values in rows):
        raise ValueError("table row and positions lengths must match")

    tables = []
    for start in range(0, len(positions), TABLE_CHUNK_SIZE):
        chunk_labels = labels[start:start + TABLE_CHUNK_SIZE]
        chunk_styles = styles[start:start + TABLE_CHUNK_SIZE]
        widths = [8]
        for index, label in enumerate(chunk_labels, start=start):
            widths.append(
                max(
                    5,
                    len(label),
                    *(len(values[index]) for _, values in rows),
                )
            )

        def border(character: str) -> str:
            return color_text(character, ANSI_BRIGHT_WHITE, color)

        def horizontal(left: str, middle: str, right: str) -> str:
            pieces = ["─" * width for width in widths]
            return border(left + middle.join(pieces) + right)

        def row(label: str, values: list[str], styles: list[str | None]) -> str:
            cells = [label.ljust(widths[0])]
            for value, width, style in zip(values, widths[1:], styles):
                padded = value.center(width)
                cells.append(color_text(padded, style, color) if style else padded)
            return border("│") + border("│").join(cells) + border("│")

        lines = [horizontal("┌", "┬", "┐")]
        lines.append(
            row(
                "PIN",
                list(chunk_labels),
                chunk_styles,
            )
        )
        for label, values in rows:
            chunk_values = values[start:start + TABLE_CHUNK_SIZE]
            lines.append(horizontal("├", "┼", "┤"))
            lines.append(row(label, chunk_values, chunk_styles))
        lines.append(horizontal("└", "┴", "┘"))
        tables.append("\n".join(lines))

    return "\n\n".join(tables)


def render_input_tables(
    input_values: str,
    input_positions: tuple[int, ...],
    color: bool,
    input_labels: tuple[str, ...] | None = None,
) -> str:
    if len(input_values) != len(input_positions):
        raise ValueError("input_values and input_positions lengths must match")
    if not input_positions:
        return "(trigger only)"
    labels = input_labels or tuple(
        table_pin_text(position) for position in input_positions
    )
    if len(labels) != len(input_positions):
        raise ValueError("input_labels and input_positions lengths must match")

    return render_pin_tables(
        input_positions,
        labels,
        [("VALUE", list(input_values))],
        [ANSI_BRIGHT_WHITE] * len(input_positions),
        color,
    )


def render_failure_tables(
    actual: str,
    expected: str,
    output_positions: tuple[int, ...],
    color: bool,
    output_labels: tuple[str, ...] | None = None,
) -> str:
    if not (len(actual) == len(expected) == len(output_positions)):
        raise ValueError("actual, expected, and output_positions lengths must match")
    labels = output_labels or tuple(
        table_pin_text(position) for position in output_positions
    )
    if len(labels) != len(output_positions):
        raise ValueError("output_labels and output_positions lengths must match")

    styles = []
    results = []
    for expected_bit, actual_bit in zip(expected, actual):
        if expected_bit == "x":
            styles.append(None)
            results.append("SKIP")
        elif expected_bit == actual_bit:
            styles.append(ANSI_GREEN)
            results.append("OK")
        else:
            styles.append(ANSI_RED)
            results.append("FAIL")

    return render_pin_tables(
        output_positions,
        labels,
        [
            ("EXPECTED", list(expected)),
            ("RECEIVED", list(actual)),
            ("RESULT", results),
        ],
        styles,
        color,
    )


def render_test_tables(
    test: TestVector,
    config: TestConfig,
    actual: str,
    color: bool,
) -> str:
    parts = [
        color_text("  INPUT:", ANSI_BRIGHT_WHITE, color),
        render_input_tables(
            test.input_values,
            config.input_positions,
            color=color,
            input_labels=config.input_labels,
        ),
        color_text("  OUTPUT:", ANSI_BRIGHT_WHITE, color),
    ]
    if config.output_positions:
        parts.append(
            render_failure_tables(
                actual,
                test.expected,
                config.output_positions,
                color=color,
                output_labels=config.output_labels,
            )
        )
    else:
        parts.append("(no outputs)")
    return "\n".join(parts)


def render_pass_dashboard(
    passed: int,
    total: int,
    test_number: int,
    test: TestVector,
    config: TestConfig,
    actual: str,
    color: bool,
) -> str:
    return "\n".join(
        [
            color_text(live_progress_text(passed, total), ANSI_GREEN, color),
            color_text(
                f"PASS {test_number:04d}: {test.name}",
                ANSI_GREEN,
                color,
            ),
            render_test_tables(test, config, actual, color),
        ]
    )


def render_verdict(message: str, success: bool, color: bool) -> str:
    content = f" {message} "
    lines = [
        "╔" + "═" * len(content) + "╗",
        "║" + content + "║",
        "╚" + "═" * len(content) + "╝",
    ]
    return color_text(
        "\n".join(lines),
        ANSI_GREEN if success else ANSI_RED,
        color,
    )


def compare_outputs(
    actual: str,
    expected: str,
    output_positions: tuple[int, ...],
    output_labels: tuple[str, ...] | None = None,
) -> tuple[bool, list[str]]:
    errors = []

    for index, expected_bit in enumerate(expected):
        if expected_bit == "x":
            continue

        actual_bit = actual[index]
        if actual_bit != expected_bit:
            pin_text = physical_pin_text(output_positions[index])
            if output_labels is not None:
                pin_text = f"{output_labels[index]} ({pin_text})"
            errors.append(
                f"{pin_text}: "
                f"expected {expected_bit}, received {actual_bit}"
            )

    return not errors, errors


def run_config(ser: serial.Serial, config: TestConfig, args) -> bool:
    use_color = colors_enabled(getattr(args, "no_color", False))
    stop_on_fail_override = getattr(args, "stop_on_fail", None)
    stop_on_fail = (
        config.stop_on_fail
        if stop_on_fail_override is None
        else stop_on_fail_override
    )
    use_live_terminal = live_progress_enabled(raw=args.raw)
    use_live_dashboard = use_live_terminal and not args.show_passes
    use_live_progress = use_live_terminal and args.show_passes

    print()
    print("=" * 72)
    print(f"TEST BOARD {config.board_name}  ({config.path})")
    print("=" * 72)

    configure_esp(ser, config.pins_type, raw=args.raw)

    total = 0
    failed = 0
    configured_total = len(config.tests)
    passed_count = 0
    progress_visible = False
    dashboard_lines = 0

    if use_live_dashboard:
        dashboard_lines = update_live_dashboard(
            color_text(
                live_progress_text(passed_count, configured_total),
                ANSI_GREEN,
                use_color,
            ),
            dashboard_lines,
        )
    elif use_live_progress:
        show_live_progress(passed_count, configured_total, color=use_color)
        progress_visible = True

    for total, test in enumerate(config.tests, start=1):
        if use_live_dashboard and dashboard_lines == 0:
            dashboard_lines = update_live_dashboard(
                color_text(
                    live_progress_text(passed_count, configured_total),
                    ANSI_GREEN,
                    use_color,
                ),
                dashboard_lines,
            )
        elif use_live_progress and not progress_visible:
            show_live_progress(passed_count, configured_total, color=use_color)
            progress_visible = True

        try:
            actual = send_test_vector(
                ser,
                test.input_values,
                config.output_positions,
                timeout=args.timeout,
                raw=args.raw,
            )
        except Exception as error:
            failed += 1
            if dashboard_lines:
                clear_live_dashboard(dashboard_lines)
                dashboard_lines = 0
            if progress_visible:
                clear_live_progress(configured_total)
                progress_visible = False
            print(color_text(f"FAIL {total:04d}: {test.name}", ANSI_RED, use_color))
            print(color_text("  INPUT:", ANSI_BRIGHT_WHITE, use_color))
            print(
                render_input_tables(
                    test.input_values,
                    config.input_positions,
                    color=use_color,
                    input_labels=config.input_labels,
                )
            )
            print(
                color_text(
                    f"  communication error: {error}",
                    ANSI_RED,
                    use_color,
                )
            )

            if stop_on_fail:
                print("  stop_on_fail=true -> stopping this board test")
                break
            continue

        ok, errors = compare_outputs(
            actual,
            test.expected,
            config.output_positions,
            config.output_labels,
        )

        if ok:
            passed_count += 1
            if use_live_dashboard:
                dashboard_lines = update_live_dashboard(
                    render_pass_dashboard(
                        passed_count,
                        configured_total,
                        total,
                        test,
                        config,
                        actual,
                        use_color,
                    ),
                    dashboard_lines,
                )
            elif use_live_progress:
                show_live_progress(passed_count, configured_total, color=use_color)
                progress_visible = True
            if args.show_passes:
                if progress_visible:
                    clear_live_progress(configured_total)
                    progress_visible = False
                print(
                    color_text(
                        f"PASS {total:04d}: {test.name}",
                        ANSI_GREEN,
                        use_color,
                    )
                )
                print(render_test_tables(test, config, actual, use_color))
        else:
            failed += 1
            if dashboard_lines:
                clear_live_dashboard(dashboard_lines)
                dashboard_lines = 0
            if progress_visible:
                clear_live_progress(configured_total)
                progress_visible = False
            print(color_text(f"FAIL {total:04d}: {test.name}", ANSI_RED, use_color))
            print(render_test_tables(test, config, actual, use_color))
            print(color_text("  ERRORS:", ANSI_RED, use_color))
            for error in errors:
                print(color_text(f"    {error}", ANSI_RED, use_color))

            if stop_on_fail:
                print("  stop_on_fail=true -> stopping this board test")
                break

    if dashboard_lines or progress_visible:
        print()

    passed = failed == 0
    print()
    if passed:
        print(f"RESULT {config.board_name}: PASS ({total} tests)")
    else:
        print(
            f"RESULT {config.board_name}: FAIL "
            f"({failed} failed, {total} tests executed)"
        )

    return passed


def expand_config_paths(paths: list[str]) -> list[str]:
    result = []
    for path in paths:
        matches = sorted(glob.glob(path))
        result.extend(matches if matches else [path])
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="ESP32 dynamic MCP23017 YAML tester"
    )
    parser.add_argument(
        "configs",
        nargs="+",
        help="YAML test files, e.g. configs/board.yaml or configs/*.yaml",
    )
    parser.add_argument(
        "--port",
        default="/dev/cu.usbmodem1101",
        help="serial port, default: /dev/cu.usbmodem1101",
    )
    parser.add_argument("--baud", type=int, default=115200, help="baud rate")
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.5,
        help="maximum seconds to wait for each ESP response",
    )
    parser.add_argument("--show-passes", action="store_true", help="print passed tests")
    parser.add_argument(
        "--raw", action="store_true", help="print raw PC -> ESP and ESP -> PC traffic"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colors in terminal output",
    )
    stop_group = parser.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--stop-on-fail",
        dest="stop_on_fail",
        action="store_const",
        const=True,
        default=None,
        help="stop after the first failed test, overriding YAML",
    )
    stop_group.add_argument(
        "--not-stop-on-fail",
        dest="stop_on_fail",
        action="store_const",
        const=False,
        help="run all tests after failures, overriding YAML",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    use_color = colors_enabled(args.no_color)

    try:
        configs = [
            load_config(path)
            for path in expand_config_paths(args.configs)
        ]
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"CONFIG ERROR: {error}")
        return 2

    print(f"Opening serial port {args.port} @ {args.baud}...")

    try:
        with serial.Serial(args.port, args.baud, timeout=0.01) as ser:
            time.sleep(2.0)
            ser.reset_input_buffer()
            results = [(config, run_config(ser, config, args)) for config in configs]
    except serial.SerialException as error:
        print(f"SERIAL ERROR: {error}")
        return 2

    print()
    print("=" * 72)
    print("GLOBAL SUMMARY")
    print("=" * 72)
    for config, ok in results:
        print(f"{Path(config.path).name:24s} : {'PASS' if ok else 'FAIL'}")

    passed = [Path(config.path).stem for config, ok in results if ok]
    print()
    if len(results) == 1:
        verdict = "VERDICT: BOARD PASSED" if results[0][1] else "VERDICT: BOARD FAILED"
        verdict_success = results[0][1]
    elif len(passed) == 1:
        verdict = f"VERDICT: BOARD MATCHES {passed[0]}"
        verdict_success = True
    elif not passed:
        verdict = "VERDICT: NO BOARD CONFIG PASSED"
        verdict_success = False
    else:
        verdict = "VERDICT: AMBIGUOUS"
        verdict_success = False

    print(render_verdict(verdict, verdict_success, use_color))

    if len(passed) > 1:
        print("Passed configs: " + ", ".join(passed))

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
