"""Bounded line reads and disk-backed duplicate indexes; inputs remain untouched."""
from collections import Counter
import csv
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import re
from time import perf_counter
import unicodedata

MISSING = object()
CSV_FIELDS = ["file", "line", "code", "field", "message", "first_file", "first_line"]
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
CONTENT_CHECK_KEYS = {"prompt_fields", "response_field", "conflicting_answer", "echo_response", "ai_boilerplate", "min_cjk_ratio"}
# Template phrases that usually mean a model-generated answer slipped into training data unedited.
BOILERPLATE = re.compile(r"作为一?[个名]?(人工智能|AI)(语言)?(模型|助手)|我(只)?是一[个名](人工智能|AI)(语言)?(模型|助手)|as an ai language model", re.IGNORECASE)
CJK = re.compile(r"[一-鿿]")
LATIN = re.compile(r"[A-Za-z]")
# Code answers are legitimately mostly ASCII, so they are excluded from the CJK-ratio check.
CODE_HINT = re.compile(r"```|^\s*(def |class |import |from \S+ import|#include|function |public |SELECT |INSERT |<\w+[ >])|=>|\{\s*$|;\s*$", re.MULTILINE | re.IGNORECASE)
# Prompts that explicitly ask for English output (translation, spelling) are exempt from the CJK-ratio check.
ENGLISH_EXPECTED = re.compile(r"英文|英语|english|翻译|单词|拼写|缩写", re.IGNORECASE)


def normalize_text(value, fold_case=True):
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    return text.casefold() if fold_case else text


def cjk_ratio(text):
    cjk, latin = len(CJK.findall(text)), len(LATIN.findall(text))
    return None if cjk + latin < 20 else cjk / (cjk + latin)


def strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def load_rules(path: Path | None = None):
    rules = json.loads((path or Path(__file__).with_name("default_rules.json")).read_text(encoding="utf-8"), object_pairs_hook=strict_pairs, parse_constant=reject_constant)
    if not isinstance(rules, dict) or not isinstance(rules.get("fields"), dict) or not rules["fields"]:
        raise ValueError("Rules require a non-empty fields object")
    allowed = {"fields", "duplicate_key_fields", "content_fields", "normalize_content", "content_checks"}
    if set(rules) - allowed:
        raise ValueError(f"Unknown rule options: {sorted(set(rules) - allowed)}")
    for field, rule in rules["fields"].items():
        if not field or any(not part for part in field.split(".")) or not isinstance(rule, dict):
            raise ValueError("Each field needs a dotted path and rule object")
        if rule.get("type") not in TYPES:
            raise ValueError(f"Unsupported type for {field}")
        if set(rule) - {"type", "required", "min_length", "max_length", "enum", "allow_blank"}:
            raise ValueError(f"Unknown field rule for {field}")
        if "required" in rule and not isinstance(rule["required"], bool):
            raise ValueError(f"required must be boolean: {field}")
        if "allow_blank" in rule and (not isinstance(rule["allow_blank"], bool) or rule["type"] != "string"):
            raise ValueError(f"allow_blank must be boolean on a string field: {field}")
        for key in ("min_length", "max_length"):
            if key in rule and (type(rule[key]) is not int or rule[key] < 0 or rule["type"] not in {"string", "array"}):
                raise ValueError(f"Invalid {key}: {field}")
        if rule.get("min_length", 0) > rule.get("max_length", float("inf")):
            raise ValueError(f"Length range reversed: {field}")
        if "enum" in rule and (not isinstance(rule["enum"], list) or not rule["enum"]):
            raise ValueError(f"enum must be a non-empty list: {field}")
    for key in ("duplicate_key_fields", "content_fields"):
        values = rules.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or v not in rules["fields"] for v in values) or len(values) != len(set(values)):
            raise ValueError(f"{key} must list distinct configured fields")
    if not isinstance(rules.get("normalize_content", True), bool):
        raise ValueError("normalize_content must be boolean")
    checks = rules.get("content_checks")
    if checks is not None:
        if not isinstance(checks, dict) or set(checks) - CONTENT_CHECK_KEYS:
            raise ValueError(f"content_checks accepts only {sorted(CONTENT_CHECK_KEYS)}")
        prompts, response = checks.get("prompt_fields"), checks.get("response_field")
        if not isinstance(prompts, list) or not prompts or any(not isinstance(p, str) or not p for p in prompts):
            raise ValueError("content_checks.prompt_fields must be a non-empty list of field paths")
        if not isinstance(response, str) or not response or response in prompts:
            raise ValueError("content_checks.response_field must be a field path not listed in prompt_fields")
        for key in ("conflicting_answer", "echo_response", "ai_boilerplate"):
            if not isinstance(checks.get(key, True), bool):
                raise ValueError(f"content_checks.{key} must be boolean")
        ratio = checks.get("min_cjk_ratio")
        if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1):
            raise ValueError("content_checks.min_cjk_ratio must be a number between 0 and 1")
    return rules


def content_issues(record, checks, db, file, line, issue):
    """LLM-data heuristics. They flag rows for human review; they do not prove an answer is wrong."""
    response = lookup(record, checks["response_field"])
    if not isinstance(response, str) or not response.strip():
        return
    prompt_values = [lookup(record, f) for f in checks["prompt_fields"]]
    prompt_texts = [v for v in prompt_values if isinstance(v, str) and v.strip()]
    if not prompt_texts:
        return
    field = checks["response_field"]
    norm_response = normalize_text(response)
    if checks.get("conflicting_answer", True):
        fingerprint = digest([normalize_text(v) if isinstance(v, str) else v for v in prompt_values])
        answer = digest(norm_response)
        first = db.execute("SELECT file,line,record_hash FROM seen WHERE kind='prompt' AND fingerprint=?", (fingerprint,)).fetchone()
        if not first:
            db.execute("INSERT INTO seen VALUES ('prompt',?,?,?,?)", (fingerprint, file, line, answer))
        elif first[2] != answer:
            issue("conflicting_answer", field, "Same prompt as an earlier record but a different answer; needs adjudication", first[0], first[1])
    # Case is kept here: a capitalisation-fix task legitimately returns the input with only case changed.
    if checks.get("echo_response", True) and any(normalize_text(response, False) == normalize_text(v, False) for v in prompt_texts):
        issue("echo_response", field, "Answer repeats the prompt or input verbatim")
    if checks.get("ai_boilerplate", True):
        match = BOILERPLATE.search(response)
        if match:
            issue("ai_boilerplate", field, f"Template phrase: {match.group(0)}")
    threshold = checks.get("min_cjk_ratio")
    if threshold is not None:
        ratio = cjk_ratio(response)
        # Only a Chinese-only prompt makes a mostly-English answer suspicious; prompts about English text are exempt.
        prompt_latin = sum(len(LATIN.findall(v)) for v in prompt_texts)
        english_expected = any(ENGLISH_EXPECTED.search(v) for v in prompt_texts)
        if ratio is not None and ratio < threshold and prompt_latin < 3 and not english_expected and not CODE_HINT.search(response):
            issue("low_cjk_ratio", field, f"Chinese prompt but answer is {ratio:.0%} Chinese; possibly untranslated")


def lookup(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def is_type(value, name):
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[name]) and not (isinstance(value, float) and not math.isfinite(value))


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def has_nonfinite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(has_nonfinite(v) for v in value.values())
    if isinstance(value, list):
        return any(has_nonfinite(v) for v in value)
    return False


def iter_lines(path, maximum):
    with path.open("rb") as handle:
        index = 0
        while True:
            raw = handle.readline(maximum + 1)
            if not raw:
                break
            index += 1
            if len(raw) > maximum:
                # Discard the rest of an oversized physical line in bounded chunks.
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(maximum + 1)
                yield index, None
            else:
                yield index, raw


def csv_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def check(paths: list[Path], output: Path, rules=None, max_line_bytes=4 * 1024 * 1024):
    rules = rules or load_rules()
    if type(max_line_bytes) is not int or max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be a positive integer")
    paths = [p.resolve() for p in paths]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Provide at least one input and do not repeat the same file")
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Input is not a file: {path}")
        if path in [output.resolve() / name for name in ("issues.csv", "summary.json", "summary.md", "report.html")]:
            raise ValueError("Output would overwrite an input")
    start = perf_counter()
    counters = Counter(total_lines=0, parsed_records=0, invalid_lines=0, problem_lines=0, clean_records=0, total_issues=0)
    codes = Counter()
    previews, per_file = [], []
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output, prefix=".stage-") as temp:
        stage = Path(temp)
        db = sqlite3.connect(stage / "dedup.sqlite")
        try:
            db.execute("CREATE TABLE seen (kind TEXT, fingerprint TEXT, file TEXT, line INTEGER, record_hash TEXT, PRIMARY KEY(kind, fingerprint))")
            with (stage / "issues.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for path in paths:
                    before = counters.copy()
                    # Relative input labels avoid publishing full local paths in reports.
                    file = path.name if sum(p.name == path.name for p in paths) == 1 else f"input{paths.index(path)+1}:{path.name}"
                    for line, raw in iter_lines(path, max_line_bytes):
                        counters["total_lines"] += 1
                        issues = []

                        def issue(code, field, message, first_file="", first_line=""):
                            issues.append(dict(zip(CSV_FIELDS, [file, line, code, field, message, first_file, first_line])))

                        record = None
                        if raw is None:
                            issue("line_too_large", "", f"Physical line exceeds {max_line_bytes} bytes including newline")
                        else:
                            try:
                                text = raw.decode("utf-8-sig" if line == 1 else "utf-8")
                                if not text.strip():
                                    issue("blank_line", "", "Blank JSONL physical line")
                                else:
                                    record = json.loads(text, object_pairs_hook=strict_pairs, parse_constant=reject_constant)
                                    if not isinstance(record, dict):
                                        issue("not_object", "", "Each line must be a JSON object")
                                        record = None
                                    elif has_nonfinite(record):
                                        raise ValueError("Non-finite numeric value")
                            except UnicodeDecodeError:
                                issue("invalid_utf8", "", "Input line is not UTF-8")
                            except (ValueError, RecursionError):
                                issue("invalid_json", "", "Malformed JSON, duplicate object key, non-finite number or excessive nesting")
                                record = None
                        if record is None:
                            counters["invalid_lines"] += 1
                        else:
                            counters["parsed_records"] += 1
                            valid_fields = set()
                            for field, rule in rules["fields"].items():
                                value = lookup(record, field)
                                if isinstance(value, str) and not value.strip() and rule.get("allow_blank", False):
                                    valid_fields.add(field)  # e.g. Alpaca-style "input": "" is a legitimate empty context
                                    continue
                                if value is MISSING or value is None or (isinstance(value, str) and not value.strip()):
                                    if rule.get("required", False):
                                        issue("missing_field", field, "Required value is absent, null or blank")
                                    elif value is not MISSING:
                                        issue("invalid_type", field, "Optional values may be omitted; present values cannot be null or blank")
                                    continue
                                if not is_type(value, rule["type"]):
                                    issue("invalid_type", field, f"Expected {rule['type']}")
                                    continue
                                valid_fields.add(field)
                                if "min_length" in rule and len(value) < rule["min_length"]:
                                    issue("too_short", field, f"Length {len(value)} < {rule['min_length']}")
                                if "max_length" in rule and len(value) > rule["max_length"]:
                                    issue("too_long", field, f"Length {len(value)} > {rule['max_length']}")
                                if "enum" in rule and value not in rule["enum"]:
                                    issue("invalid_enum", field, "Value is outside configured enum")
                            record_hash = digest(record)
                            for kind, fields in (("id", rules.get("duplicate_key_fields", [])), ("content", rules.get("content_fields", []))):
                                if not fields or not set(fields).issubset(valid_fields):
                                    continue
                                values = [lookup(record, f) for f in fields]
                                if kind == "content" and rules.get("normalize_content", True):
                                    values = [" ".join(unicodedata.normalize("NFKC", v).split()).casefold() if isinstance(v, str) else v for v in values]
                                fingerprint = digest(values)
                                first = db.execute("SELECT file,line,record_hash FROM seen WHERE kind=? AND fingerprint=?", (kind, fingerprint)).fetchone()
                                if first:
                                    code = "duplicate_id" if kind == "id" else "duplicate_content"
                                    note = "Same complete record" if first[2] == record_hash else "Different complete record"
                                    issue(code, ",".join(fields), note + "; later occurrence refers to first occurrence", first[0], first[1])
                                else:
                                    db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (kind, fingerprint, file, line, record_hash))
                            if rules.get("content_checks") and not any(i["code"] == "duplicate_content" for i in issues):
                                content_issues(record, rules["content_checks"], db, file, line, issue)
                        if issues:
                            counters["problem_lines"] += 1
                            counters["total_issues"] += len(issues)
                            for item in issues:
                                codes[item["code"]] += 1
                                writer.writerow({k: csv_safe(v) for k, v in item.items()})
                                if len(previews) < 12:
                                    previews.append(item)
                        else:
                            counters["clean_records"] += 1
                    per_file.append({"file": file, **{k: counters[k] - before[k] for k in counters}})
            db.commit()
        finally:
            db.close()
        summary = {"input_files": len(paths), **dict(counters), "issues_by_code": dict(sorted(codes.items())),
                   "files": per_file, "max_line_bytes": max_line_bytes,
                   "duplicate_index": "temporary SQLite; SHA-256 fingerprints; later occurrences only",
                   "length_unit": "Unicode code points for strings; items for arrays; not tokens or bytes",
                   "rules": rules, "elapsed_seconds": round(perf_counter() - start, 4)}
        assert summary["total_lines"] == summary["parsed_records"] + summary["invalid_lines"]
        assert summary["total_lines"] == summary["clean_records"] + summary["problem_lines"]
        (stage / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "summary.md").write_text(markdown(summary), encoding="utf-8")
        (stage / "report.html").write_text(html_report(summary, previews), encoding="utf-8")
        for name in ("issues.csv", "summary.md", "report.html", "summary.json"):
            os.replace(stage / name, output / name)
    return summary


def markdown(s):
    rows = "\n".join(f"| {k} | {v} |" for k, v in s["issues_by_code"].items())
    return f"""# JSONL 数据质检结果

输入 {s['input_files']} 个文件，共 {s['total_lines']} 个物理行。通过 {s['clean_records']} 行，问题行 {s['problem_lines']}，问题项 {s['total_issues']}。

成功解析对象 {s['parsed_records']} 行，非法/空白行 {s['invalid_lines']}。同一行可有多个问题项。

| 问题代码 | 次数 |
| --- | ---: |
{rows}

长度按 Unicode 字符或数组元素计数，不是 token 数。重复只标记后出现的行，并关联首条来源。质检不修改、不删除输入数据；结构检查不判断答案真实性或语义质量。
"""


def html_report(s, previews):
    bars = "".join(f'<div class="bar"><span>{escape(k)}</span><i style="width:{max(2,v/max(s["issues_by_code"].values(),default=1)*100):.1f}%"></i><b>{v}</b></div>' for k, v in s["issues_by_code"].items())
    rows = "".join(f'<tr><td>{escape(r["file"])}</td><td>{r["line"]}</td><td>{escape(r["code"])}</td><td>{escape(r["field"])}</td></tr>' for r in previews)
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JSONL 数据质检报告</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#202a43;font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:1120px;margin:auto;padding:38px 28px}}.eyebrow{{font-size:12px;letter-spacing:2px;color:#6d7388}}h1{{font-size:32px;margin:8px 0}}p{{color:#788197}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:25px 0}}article,.panel{{background:white;border:1px solid #e0e5ee;border-radius:12px;padding:20px}}article span{{color:#788197}}strong{{display:block;font-size:30px;margin-top:6px}}.grid{{display:grid;grid-template-columns:1fr 1.45fr;gap:18px}}h2{{font-size:16px;margin:0 0 16px}}.bar{{display:grid;grid-template-columns:142px 1fr 26px;align-items:center;gap:8px;margin:10px 0;font-size:11px}}.bar i{{display:block;height:8px;background:#6875d9;border-radius:4px}}b{{font-weight:500}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{text-align:left;border-bottom:1px solid #edf0f5;padding:9px 6px;word-break:break-word}}th{{color:#788197;font-weight:500}}footer{{margin-top:22px;color:#788197;font-size:12px}}a{{color:#545fc0}}@media(max-width:760px){{.cards{{grid-template-columns:1fr 1fr}}.grid{{grid-template-columns:1fr}}}}
</style><main><div class="eyebrow">JSONL DATA QUALITY / AUDIT REPORT</div><h1>把数据问题定位到每一行</h1><p>字段检查 · 重复检测 · 长度规则 · UTF-8 与 JSON 校验</p><section class="cards"><article><span>扫描行数</span><strong>{s['total_lines']:,}</strong></article><article><span>通过记录</span><strong style="color:#16876d">{s['clean_records']:,}</strong></article><article><span>问题行数</span><strong style="color:#b96b24">{s['problem_lines']:,}</strong></article><article><span>问题项数</span><strong>{s['total_issues']:,}</strong></article></section><section class="grid"><div class="panel"><h2>问题类型分布</h2>{bars}<p>一行可命中多个检查项，次数不等于问题行数。</p></div><div class="panel"><h2>问题清单预览 / 前 {len(previews)} 项</h2><table><thead><tr><th>文件</th><th>行</th><th>问题</th><th>字段</th></tr></thead><tbody>{rows}</tbody></table></div></section><footer>输入不修改 · 重复索引使用临时 SQLite · 长度按 Unicode 字符计数 · <a href="issues.csv">下载 CSV</a> · <a href="summary.json">查看 JSON 汇总</a></footer></main></html>'''
