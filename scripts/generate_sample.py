"""Generate a small, deterministic and deliberately imperfect synthetic dataset."""
import json
from pathlib import Path


def generate(path):
    records = [json.dumps({"id": f"item-{i:04d}", "input": f"请解释第 {i} 条模拟记录的用途。", "output": f"这是第 {i} 条虚构问答，用于演示数据结构检查。", "label": "问答"}, ensure_ascii=False) for i in range(1, 81)]
    samples = [
        {"id": "missing-output", "input": "这条记录没有输出字段。"},
        {"id": "null-value", "input": None, "output": "空值应该被显式报告。"},
        {"id": "bad-type", "input": 42, "output": "类型错误的模拟记录。"},
        {"id": "short", "input": "短", "output": "长度不足的模拟记录。"},
        {"id": "long", "input": "很长的模拟输入" * 300, "output": "输入长度超过预设上限。"},
        {"id": "bad-label", "input": "演示不在标签枚举内的记录。", "output": "标签值需要根据规则进行检查。", "label": "其他"},
        {"id": "item-0001", "input": "相同主键但内容发生了变化。", "output": "这条记录用于演示主键重复。"},
        {"id": "copy-content", "input": "请解释第 2 条模拟记录的用途。", "output": "这是第 2 条虚构问答，用于演示数据结构检查。"},
        {"id": "space-copy", "input": " 请解释第 3 条模拟记录的用途。 ", "output": "这是第 3 条虚构问答，用于演示数据结构检查。"},
    ]
    records.extend(json.dumps(r, ensure_ascii=False) for r in samples)
    records += ['{"id": "broken",', '[]', '', '{"id":"a","id":"b"}', '{"value":NaN}']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "examples/sample.jsonl")
