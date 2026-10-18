import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonl_quality.checker import check, load_rules
from jsonl_quality.cli import main


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "data.jsonl"
        self.out = self.root / "out"

    def write(self, records):
        self.source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    def row(self, key="one", **changes):
        return {"id": key, "input": "这是模拟输入文本", "output": "这是模拟输出文本", **changes}

    def issues(self):
        with (self.out / "issues.csv").open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_valid_input_and_no_mutation(self):
        self.write([self.row()])
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        s = check([self.source], self.out)
        self.assertEqual((s["clean_records"], s["total_issues"]), (1, 0))
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(self.issues(), [])

    def test_missing_null_blank_and_type(self):
        row = self.row()
        del row["output"]
        self.write([row, self.row("two", input=None), self.row("three", input="  "), self.row("four", input=7)])
        s = check([self.source], self.out)
        self.assertEqual(s["issues_by_code"]["missing_field"], 3)
        self.assertEqual(s["issues_by_code"]["invalid_type"], 1)
        self.assertEqual(s["problem_lines"], 4)

    def test_length_boundaries_use_unicode_codepoints(self):
        rules = load_rules()
        rules["content_fields"] = []
        self.write([self.row(str(i), input="字"*n) for i, n in enumerate([4, 5, 2000, 2001])])
        s = check([self.source], self.out, rules)
        self.assertEqual(s["issues_by_code"], {"too_long": 1, "too_short": 1})
        self.assertEqual(s["clean_records"], 2)

    def test_duplicate_id_and_normalized_content_across_files(self):
        self.write([self.row()])
        second = self.root / "second.jsonl"
        second.write_text(json.dumps(self.row(input=" 这是模拟输入文本 "), ensure_ascii=False) + "\n")
        s = check([self.source, second], self.out)
        self.assertEqual(s["problem_lines"], 1)
        self.assertEqual(s["total_issues"], 2)
        self.assertTrue(all(r["first_file"] == "data.jsonl" and r["first_line"] == "1" for r in self.issues()))

    def test_malformed_lines_do_not_stop_following_record(self):
        self.source.write_bytes(b'{bad}\n[]\n\n{"x":NaN}\n{"x":1,"x":2}\n{"x":1e999}\n\xff\n' + json.dumps(self.row()).encode() + b'\n')
        s = check([self.source], self.out)
        self.assertEqual((s["total_lines"], s["invalid_lines"], s["clean_records"]), (8, 7, 1))
        self.assertEqual(s["issues_by_code"]["invalid_json"], 4)
        self.assertEqual(self.issues()[-1]["line"], "7")

    def test_oversized_line_is_bounded_and_line_numbers_continue(self):
        self.source.write_bytes(b'x'*300 + b'\n' + json.dumps(self.row()).encode() + b'\n')
        s = check([self.source], self.out, max_line_bytes=200)
        self.assertEqual((s["total_lines"], s["clean_records"]), (2, 1))
        self.assertEqual(s["issues_by_code"], {"line_too_large": 1})

    def test_nested_field_and_boolean_not_integer(self):
        rules = {"fields": {"meta.count": {"type": "integer", "required": True}}}
        self.write([{"meta": {"count": True}}, {"meta": {"count": 3}}, {}])
        s = check([self.source], self.out, rules)
        self.assertEqual(s["issues_by_code"], {"invalid_type": 1, "missing_field": 1})

    def test_utf8_bom_and_final_line_without_newline(self):
        self.source.write_bytes(b'\xef\xbb\xbf' + json.dumps(self.row()).encode())
        self.assertEqual(check([self.source], self.out)["clean_records"], 1)

    def test_empty_file_has_zero_rows(self):
        self.source.write_bytes(b'')
        s = check([self.source], self.out)
        self.assertEqual((s["total_lines"], s["total_issues"]), (0, 0))

    def test_optional_present_value_must_follow_contract(self):
        self.write([self.row(label="其他"), self.row("two", label=None)])
        s = check([self.source], self.out)
        self.assertEqual(s["issues_by_code"]["invalid_enum"], 1)
        self.assertEqual(s["issues_by_code"]["invalid_type"], 1)

    def test_cli_exit_codes(self):
        self.write([self.row(input="短")])
        args = ["--input", str(self.source), "--output", str(self.out)]
        self.assertEqual(main(args), 0)
        self.assertEqual(main(args + ["--fail-on-issues"]), 1)
        self.assertEqual(main(["--input", str(self.root / "absent")]), 2)

    def test_invalid_rules_fail_early(self):
        path = self.root / "rules.json"
        for rules in [{"fields": {}}, {"fields": {"id": {"type": "unknown"}}}, {"fields": {"id": {"type": "string", "min_length": 9, "max_length": 2}}}, {"fields": {"id": {"type": "string"}}, "typo": True}]:
            path.write_text(json.dumps(rules))
            with self.assertRaises(ValueError):
                load_rules(path)

    def test_repeated_input_file_rejected(self):
        self.write([self.row()])
        with self.assertRaisesRegex(ValueError, "repeat"):
            check([self.source, self.source], self.out)

    def test_csv_formula_prefix_and_html_escaping(self):
        self.source = self.root / '=danger.jsonl'
        self.write([self.row(input="短")])
        check([self.source], self.out)
        self.assertEqual(self.issues()[0]["file"], "'=danger.jsonl")
        self.assertFalse(any(p.name.endswith('.sqlite') for p in self.out.iterdir()))

    def test_surrogate_escape_does_not_crash_duplicate_hashing(self):
        self.source.write_text('{"id":"x","input":"abcde\\ud800","output":"12345"}\n')
        self.assertEqual(check([self.source], self.out)["parsed_records"], 1)

    def test_same_basename_is_disambiguated_without_absolute_path(self):
        self.write([self.row()])
        other = self.root / 'sub' / 'data.jsonl'
        other.parent.mkdir()
        other.write_bytes(self.source.read_bytes())
        check([self.source, other], self.out)
        self.assertEqual(self.issues()[0]["first_file"], 'input1:data.jsonl')
        self.assertEqual(self.issues()[0]["file"], 'input2:data.jsonl')


if __name__ == "__main__":
    unittest.main()
