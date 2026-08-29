#!/usr/bin/env python3

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAKET_TESTER_DIR = PROJECT_ROOT / "maket_tester"
CONFIG_DIR = MAKET_TESTER_DIR / "configs"
sys.path.insert(0, str(MAKET_TESTER_DIR))

import tester


class FakeSerial:
    def __init__(self, response=b""):
        self.response = bytearray(response)
        self.writes = []
        self.read_counts = []
        self.reset_count = 0

    def reset_input_buffer(self):
        self.reset_count += 1

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def read(self, count):
        self.read_counts.append(count)
        result = self.response[:count]
        del self.response[:count]
        return bytes(result)


class DynamicMcpTesterTests(unittest.TestCase):
    def write_yaml(self, data):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "test.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return str(path)

    def args(self):
        return SimpleNamespace(
            timeout=0.02,
            raw=False,
            show_passes=False,
            no_color=True,
            stop_on_fail=None,
        )

    def test_loads_valid_config_and_normalizes_expected(self):
        path = self.write_yaml(
            {
                "board": "mixed",
                "pins_type": "gvici" + "z" * 3 + "o" + "z" * 23,
                "tests": [{"input": "01", "expected": "?"}],
            }
        )

        config = tester.load_config(path)

        self.assertEqual(config.input_count, 2)
        self.assertEqual(config.input_positions, (2, 4))
        self.assertEqual(config.input_labels, ("3", "5"))
        self.assertEqual(config.output_positions, (8,))
        self.assertEqual(config.output_labels, ("9",))
        self.assertEqual(config.tests[0].expected, "x")

    def test_protocol_writes_reset_config_and_compact_input(self):
        pins_type = "gvici" + "z" * 3 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [{"input": "01", "expected": "1"}],
            }
        )
        fake = FakeSerial(b"1")

        with redirect_stdout(io.StringIO()):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertTrue(passed)
        self.assertEqual(fake.writes, [b"r", pins_type.encode("ascii"), b"01"])
        self.assertEqual(fake.read_counts, [1])

    def test_no_inputs_uses_trigger_and_multiple_clocks_are_allowed(self):
        pins_type = "gvcc" + "z" * 4 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [{"input": "", "expected": "0"}],
            }
        )
        fake = FakeSerial(b"0")

        with redirect_stdout(io.StringIO()):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertTrue(passed)
        self.assertEqual(fake.writes[-1], b"0")

    def test_no_outputs_does_not_read(self):
        pins_type = "gvic" + "z" * 28
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [{"input": "1", "expected": ""}],
            }
        )
        fake = FakeSerial()

        with redirect_stdout(io.StringIO()):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertTrue(passed)
        self.assertEqual(fake.read_counts, [])

    def test_i2c_error_marker_reports_mcp_address_and_bank(self):
        fake = FakeSerial(b"EE")

        with self.assertRaisesRegex(
            RuntimeError,
            r"ESP reported I2C read error: MCP 0x21 GPIOA",
        ):
            tester.send_test_vector(
                fake,
                "1",
                (16, 20),
                timeout=0.02,
            )

    def test_ignored_expected_output_passes(self):
        self.assertEqual(
            tester.compare_outputs("1", "x", (16,)),
            (True, []),
        )

    def test_z_pin_is_not_part_of_expected_output(self):
        path = self.write_yaml(
            {
                "pins_type": "gvi" + "z" * 5 + "o" + "z" * 23,
                "tests": [{"input": "1", "expected": "0"}],
            }
        )

        config = tester.load_config(path)

        self.assertEqual(config.input_count, 1)
        self.assertEqual(config.output_positions, (8,))

    def test_pins_type_does_not_require_gv_prefix(self):
        path = self.write_yaml(
            {
                "pins_type": "zvi" + "z" * 29,
                "tests": [{"input": "1", "expected": ""}],
            }
        )

        config = tester.load_config(path)

        self.assertEqual(config.pins_type[:3], "zvi")
        self.assertEqual(config.input_positions, (2,))

    def test_mismatch_reports_physical_mcp_pin(self):
        ok, errors = tester.compare_outputs("0", "1", (17,))

        self.assertFalse(ok)
        self.assertEqual(
            errors,
            ["MCP 0x21 pin 2: expected 1, received 0"],
        )

    def test_mismatch_reports_semantic_and_physical_pin(self):
        ok, errors = tester.compare_outputs(
            "0",
            "1",
            (18,),
            ("S2",),
        )

        self.assertFalse(ok)
        self.assertEqual(
            errors,
            ["S2 (MCP 0x21 pin 3): expected 1, received 0"],
        )

    def test_failure_table_colors_correct_and_wrong_pins(self):
        table = tester.render_failure_tables(
            "10",
            "11",
            (16, 17),
            color=True,
        )

        self.assertIn(f"{tester.ANSI_GREEN}  1  ", table)
        self.assertIn(f"{tester.ANSI_RED}  2  ", table)
        self.assertIn(f"{tester.ANSI_GREEN}  1  ", table)
        self.assertIn(f"{tester.ANSI_RED}  0  ", table)
        self.assertIn(tester.ANSI_BRIGHT_WHITE, table)

    def test_failure_table_splits_after_sixteen_pins(self):
        positions = tuple(range(15, 32))
        table = tester.render_failure_tables(
            "0" * 17,
            "1" * 17,
            positions,
            color=False,
        )

        tables = table.split("\n\n")
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].splitlines()[1].count("│"), 18)
        self.assertEqual(tables[1].splitlines()[1], "│PIN     │  16 │")

    def test_input_table_uses_plain_pin_numbers_and_values(self):
        table = tester.render_input_tables(
            "101",
            (2, 8, 15),
            color=False,
        )

        self.assertIn("│PIN     │  3  │  9  │  16 │", table)
        self.assertIn("│VALUE   │  1  │  0  │  1  │", table)
        self.assertNotIn("20:", table)

    def test_tables_use_semantic_labels_and_dynamic_width(self):
        input_table = tester.render_input_tables(
            "10",
            (2, 3),
            color=False,
            input_labels=("Cin", "LongInput"),
        )
        output_table = tester.render_failure_tables(
            "01",
            "11",
            (16, 17),
            color=False,
            output_labels=("S0", "Cout"),
        )

        self.assertIn("│PIN     │ Cin │LongInput│", input_table)
        self.assertIn("│VALUE   │  1  │    0    │", input_table)
        self.assertIn("│PIN     │  S0 │ Cout│", output_table)

    def test_failure_table_marks_ignored_output_neutrally(self):
        table = tester.render_failure_tables("1", "x", (16,), color=True)

        self.assertIn("SKIP", table)
        self.assertNotIn(tester.ANSI_GREEN, table)
        self.assertNotIn(tester.ANSI_RED, table)

    def test_failure_table_without_color_contains_no_ansi(self):
        table = tester.render_failure_tables("0", "1", (16,), color=False)

        self.assertNotIn("\033[", table)
        self.assertIn("┌", table)
        self.assertIn("FAIL", table)

    def test_no_color_flag_disables_colors_for_tty(self):
        stream = SimpleNamespace(isatty=lambda: True)
        args = tester.parse_args(["test.yaml", "--no-color"])

        self.assertTrue(args.no_color)
        self.assertFalse(tester.colors_enabled(args.no_color, stream=stream))

    def test_stop_on_fail_cli_flags_are_mutually_exclusive(self):
        self.assertIsNone(tester.parse_args(["test.yaml"]).stop_on_fail)
        self.assertTrue(
            tester.parse_args(["test.yaml", "--stop-on-fail"]).stop_on_fail
        )
        self.assertFalse(
            tester.parse_args(
                ["test.yaml", "--not-stop-on-fail"]
            ).stop_on_fail
        )
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tester.parse_args(
                    [
                        "test.yaml",
                        "--stop-on-fail",
                        "--not-stop-on-fail",
                    ]
                )

    def test_stop_on_fail_cli_overrides_yaml(self):
        vectors = [
            {"name": "first", "input": "0", "expected": "1"},
            {"name": "second", "input": "1", "expected": "1"},
        ]

        yaml_continue = self.write_yaml(
            {
                "pins_type": "i" + "z" * 7 + "o" + "z" * 23,
                "stop_on_fail": False,
                "tests": vectors,
            }
        )
        force_stop_args = self.args()
        force_stop_args.stop_on_fail = True
        force_stop_serial = FakeSerial(b"00")
        with redirect_stdout(io.StringIO()):
            tester.run_config(
                force_stop_serial,
                tester.load_config(yaml_continue),
                force_stop_args,
            )
        self.assertEqual(force_stop_serial.writes[2:], [b"0"])

        yaml_stop = self.write_yaml(
            {
                "pins_type": "i" + "z" * 7 + "o" + "z" * 23,
                "stop_on_fail": True,
                "tests": vectors,
            }
        )
        force_continue_args = self.args()
        force_continue_args.stop_on_fail = False
        force_continue_serial = FakeSerial(b"00")
        with redirect_stdout(io.StringIO()):
            tester.run_config(
                force_continue_serial,
                tester.load_config(yaml_stop),
                force_continue_args,
            )
        self.assertEqual(force_continue_serial.writes[2:], [b"0", b"1"])

    def test_live_dashboard_replaces_passed_test_tables(self):
        pins_type = "gvi" + "z" * 5 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [
                    {"input": "0", "expected": "0"},
                    {"input": "1", "expected": "1"},
                ],
            }
        )
        fake = FakeSerial(b"01")
        output = io.StringIO()
        output.isatty = lambda: True

        with redirect_stdout(output):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertTrue(passed)
        self.assertIn("Passed tests: 0/2", output.getvalue())
        self.assertIn("Passed tests: 1/2", output.getvalue())
        self.assertIn("Passed tests: 2/2", output.getvalue())
        self.assertIn("\033[1A", output.getvalue())
        self.assertNotIn("\033[s", output.getvalue())
        self.assertNotIn("\033[u", output.getvalue())
        self.assertIn("PASS 0001", output.getvalue())
        self.assertIn("PASS 0002", output.getvalue())
        self.assertIn("INPUT:", output.getvalue())
        self.assertIn("OUTPUT:", output.getvalue())
        self.assertIn("EXPECTED", output.getvalue())
        self.assertIn("RECEIVED", output.getvalue())
        self.assertLess(
            output.getvalue().index("Passed tests: 0/2"),
            output.getvalue().index("Passed tests: 2/2"),
        )

    def test_live_dashboard_is_cleared_before_failure(self):
        pins_type = "gvi" + "z" * 5 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [{"input": "1", "expected": "1"}],
            }
        )
        fake = FakeSerial(b"0")
        output = io.StringIO()
        output.isatty = lambda: True

        with redirect_stdout(output):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertFalse(passed)
        clear_sequence = "\r\033[2K\r"
        self.assertIn("Passed tests: 0/1", output.getvalue())
        self.assertIn(clear_sequence, output.getvalue())
        self.assertLess(
            output.getvalue().rindex(clear_sequence),
            output.getvalue().index("FAIL 0001"),
        )

    def test_clear_live_dashboard_erases_every_line(self):
        output = io.StringIO()

        tester.clear_live_dashboard(3, stream=output)

        self.assertEqual(
            output.getvalue(),
            "\r\033[2K\033[1A\r\033[2K\033[1A\r\033[2K\r",
        )

    def test_live_progress_text_is_readable(self):
        self.assertEqual(tester.live_progress_text(127, 512), "Passed tests: 127/512")

    def test_show_passes_prints_input_and_output_tables(self):
        pins_type = "i" + "z" * 7 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "input_labels": ["A"],
                "output_labels": ["Y"],
                "tests": [{"name": "A=1", "input": "1", "expected": "1"}],
            }
        )
        fake = FakeSerial(b"1")
        args = self.args()
        args.show_passes = True
        output = io.StringIO()

        with redirect_stdout(output):
            passed = tester.run_config(fake, tester.load_config(path), args)

        text = output.getvalue()
        self.assertTrue(passed)
        self.assertIn("PASS 0001: A=1", text)
        self.assertIn("INPUT:", text)
        self.assertIn("│PIN     │  A  │", text)
        self.assertIn("│VALUE   │  1  │", text)
        self.assertIn("OUTPUT:", text)
        self.assertIn("│PIN     │  Y  │", text)
        self.assertIn("│EXPECTED│  1  │", text)
        self.assertIn("│RECEIVED│  1  │", text)
        self.assertIn("│RESULT  │  OK │", text)
        self.assertNotIn("  input    =", text)
        self.assertNotIn("  read     =", text)

    def test_communication_error_does_not_print_pin_table(self):
        pins_type = "gvi" + "z" * 5 + "o" + "z" * 23
        path = self.write_yaml(
            {
                "pins_type": pins_type,
                "tests": [{"input": "1", "expected": "0"}],
            }
        )
        fake = FakeSerial(b"E")
        output = io.StringIO()

        with redirect_stdout(output):
            passed = tester.run_config(fake, tester.load_config(path), self.args())

        self.assertFalse(passed)
        self.assertIn("communication error", output.getvalue())
        self.assertIn("INPUT:", output.getvalue())
        self.assertIn("VALUE", output.getvalue())
        self.assertNotIn("OUTPUT:", output.getvalue())
        self.assertNotIn("EXPECTED", output.getvalue())

    def test_verdict_is_green_for_success_and_red_for_failure(self):
        passed = tester.render_verdict("VERDICT: BOARD PASSED", True, color=True)
        failed = tester.render_verdict("VERDICT: BOARD FAILED", False, color=True)

        self.assertTrue(passed.startswith(tester.ANSI_GREEN))
        self.assertTrue(failed.startswith(tester.ANSI_RED))
        self.assertIn("╔", passed)
        self.assertIn("╚", failed)

    def test_rejects_invalid_lengths_and_characters(self):
        cases = [
            {
                "pins_type": "gv" + "i" * 29,
                "tests": [{"input": "0" * 29, "expected": ""}],
            },
            {
                "pins_type": "gvq" + "z" * 29,
                "tests": [{"input": "", "expected": ""}],
            },
            {
                "pins_type": "gvio" + "z" * 28,
                "tests": [{"input": "0", "expected": "0"}],
            },
            {
                "pins_type": "gvi" + "z" * 6 + "o" + "z" * 22,
                "tests": [{"input": "0", "expected": "0"}],
            },
            {
                "pins_type": "gvi" + "z" * 5 + "o" + "z" * 23,
                "tests": [{"input": "", "expected": "0"}],
            },
            {
                "pins_type": "gvi" + "z" * 5 + "o" + "z" * 23,
                "tests": [{"input": "2", "expected": "0"}],
            },
            {
                "pins_type": "gvi" + "z" * 5 + "o" + "z" * 23,
                "tests": [{"input": "0", "expected": ""}],
            },
            {
                "pins_type": "gvi" + "z" * 5 + "o" + "z" * 23,
                "tests": [{"input": "0", "expected": "q"}],
            },
        ]

        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    tester.load_config(self.write_yaml(data))

    def test_rejects_invalid_pin_labels(self):
        pins_type = "gvi" + "z" * 5 + "o" + "z" * 23
        cases = [
            {"input_labels": [], "output_labels": ["Y"]},
            {"input_labels": ["A"], "output_labels": []},
            {"input_labels": [""], "output_labels": ["Y"]},
            {"input_labels": [1], "output_labels": ["Y"]},
            {"input_labels": "A", "output_labels": ["Y"]},
            {"input_labels": None, "output_labels": ["Y"]},
        ]

        for labels in cases:
            with self.subTest(labels=labels):
                data = {
                    "pins_type": pins_type,
                    "tests": [{"input": "0", "expected": "0"}],
                    **labels,
                }
                with self.assertRaises(ValueError):
                    tester.load_config(self.write_yaml(data))

    def test_all_current_yaml_configs_load(self):
        config_paths = sorted(CONFIG_DIR.glob("*.yaml"))

        self.assertTrue(config_paths)
        for path in config_paths:
            with self.subTest(path=path.name):
                config = tester.load_config(str(path))
                self.assertEqual(len(config.pins_type), tester.PIN_COUNT)
                self.assertTrue(config.tests)
                for start, end in tester.SIGNAL_BLOCKS:
                    block = config.pins_type[start:end]
                    self.assertFalse(set(block) & set("ic") and "o" in block)

    def test_adder_labels_use_msb_first_order(self):
        config = tester.load_config(
            str(CONFIG_DIR / "adder_4bit_with_carry.yaml")
        )
        expected_inputs = ("Cin", "A3", "A2", "A1", "A0", "B3", "B2", "B1", "B0")
        expected_outputs = ("Cout", "S3", "S2", "S1", "S0")

        self.assertEqual(config.input_labels, expected_inputs)
        self.assertEqual(config.output_labels, expected_outputs)

    def test_adder_vectors_use_msb_first_order(self):
        config = tester.load_config(
            str(CONFIG_DIR / "adder_4bit_with_carry.yaml")
        )

        self.assertEqual(len(config.tests), 512)

        for test in config.tests:
            expression = test.name.split(" = ", 1)[0]
            a, b, carry_in = (
                int(value.strip())
                for value in expression.split("+")
            )
            self.assertEqual(
                test.input_values,
                f"{carry_in}{a:04b}{b:04b}",
                test.name,
            )
            self.assertEqual(
                test.expected,
                f"{a + b + carry_in:05b}",
                test.name,
            )

    def test_first_8_pins_sweep_is_output_only_diagnostic(self):
        config = tester.load_config(
            str(CONFIG_DIR / "first_8_pins_sweep.yaml")
        )

        self.assertEqual(
            config.pins_type,
            "iiiiiiiizzzzzzzzoooooooozzzzzzzz",
        )
        self.assertEqual(config.output_positions, tuple(range(16, 24)))
        self.assertEqual(len(config.tests), 256)
        self.assertEqual(
            [test.input_values for test in config.tests],
            [f"{number:08b}" for number in range(256)],
        )
        self.assertTrue(
            all(test.expected == "xxxxxxxx" for test in config.tests)
        )

    def test_divider_vectors_contain_quotient_and_remainder(self):
        config = tester.load_config(
            str(CONFIG_DIR / "divider_4bit.yaml")
        )

        self.assertEqual(
            config.pins_type,
            "iiiiiiiizzzzzzzzoooooooozzzzzzzz",
        )
        self.assertEqual(
            config.input_labels,
            ("A3", "A2", "A1", "A0", "B3", "B2", "B1", "B0"),
        )
        self.assertEqual(
            config.output_labels,
            ("I3", "I2", "I1", "I0", "R3", "R2", "R1", "R0"),
        )
        self.assertEqual(len(config.tests), 256)

        for test in config.tests:
            dividend = int(test.input_values[:4], 2)
            divisor = int(test.input_values[4:], 2)
            quotient, remainder = (
                (0xF, dividend)
                if divisor == 0
                else divmod(dividend, divisor)
            )
            self.assertEqual(
                test.expected,
                f"{quotient:04b}{remainder:04b}",
                test.name,
            )

    def test_multiplier_vectors_contain_full_product(self):
        config = tester.load_config(
            str(CONFIG_DIR / "multiplier_4x4.yaml")
        )

        self.assertEqual(
            config.input_labels,
            ("A3", "A2", "A1", "A0", "B3", "B2", "B1", "B0"),
        )
        self.assertEqual(
            config.output_labels,
            ("P7", "P6", "P5", "P4", "P3", "P2", "P1", "P0"),
        )
        self.assertEqual(len(config.tests), 256)

        for test in config.tests:
            factor_a = int(test.input_values[:4], 2)
            factor_b = int(test.input_values[4:], 2)
            self.assertEqual(
                test.expected,
                f"{factor_a * factor_b:08b}",
                test.name,
            )


if __name__ == "__main__":
    unittest.main()
