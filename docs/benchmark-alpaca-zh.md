# 真实数据实测：5.1 万条公开中文指令数据

用公开的中文指令微调数据集检验工具在真实规模下的表现。数据不随仓库分发，脚本自动下载。

## 复现

```bash
python scripts/download_alpaca_zh.py
python -m jsonl_quality --input data/alpaca_zh_51k.jsonl --rules examples/rules_alpaca_zh.json --output outputs/alpaca_zh
```

- 数据：[ymcui/Chinese-LLaMA-Alpaca](https://github.com/ymcui/Chinese-LLaMA-Alpaca) 的 `alpaca_data_zh_51k.json`（Stanford Alpaca 中文翻译版，研究用途），转换为 JSONL。
- 规则：[`examples/rules_alpaca_zh.json`](../examples/rules_alpaca_zh.json)。`input` 为空在 Alpaca 格式里是正常的，用 `allow_blank` 放行。
- 环境：Python 3.13 / Linux x86_64 / 2 核云端容器，2026-10-19。换一台电脑耗时会不同。

## 结果

| 指标 | 数值 |
| --- | ---: |
| 记录数 | 51,179 |
| 通过 | 50,997 |
| 问题行 | 182（0.36%） |
| 5 次运行耗时中位数 | 5.2 秒 |

| 问题代码 | 次数 | 典型情况 |
| --- | ---: | --- |
| `echo_response` 复读问题 | 141 | “改正语病”类题目，回答与原句一字不差；需要确认原句是否本来就正确 |
| `missing_field` 回答为空 | 28 | 计算、作图类指令的 `output` 是空字符串，应剔除或补写 |
| `conflicting_answer` 同题多答 | 12 | 同一道开放问答出现两种回答，如第 5 行和第 15691 行都在回答“解释神经网络如何学习” |
| `too_short` 指令过短 | 1 | 指令只有“填空”两个字，缺少上下文 |

完整问题清单：[alpaca-zh-issues.csv](alpaca-zh-issues.csv)（只含行号与问题代码，不含原始问答正文）。运行记录：[benchmark-alpaca-zh.json](benchmark-alpaca-zh.json)。

## 规则调整记录

规则不是一次写对的，而是边跑真实数据边收紧的：

1. **回答最短字数**：一开始设为 5 字，标出 2,000 多条，抽看发现几乎都是分类、计算题的正确短答案（如“积极”“17”）。改为只检查空回答。
2. **疑似未翻译**：中文指令配全英文回答，标出 288 条。抽看多为歌名、书名、代码、参考文献等正常英文。在这份配置里关闭，规则保留供其他数据集使用。
3. **复读问题**：比较时原本忽略大小写，结果把“改正大小写”题的正确答案误判成复读。改为保留大小写后，从 162 条降到 141 条。

这些规则是**复核线索**，命中不等于数据一定错误，入库前仍需人工抽检。
