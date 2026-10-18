import argparse
import json
from pathlib import Path
import sys

from .checker import check, load_rules


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查 JSONL 字段、重复和长度，输出 CSV 问题清单")
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/demo"))
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--max-line-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--fail-on-issues", action="store_true", help="发现问题返回 1；运行错误返回 2")
    args = parser.parse_args(argv)
    try:
        summary = check(args.input, args.output, load_rules(args.rules), args.max_line_bytes)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("input_files", "total_lines", "clean_records", "problem_lines", "total_issues", "issues_by_code", "elapsed_seconds")}, ensure_ascii=False, indent=2))
    return 1 if args.fail_on_issues and summary["problem_lines"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
