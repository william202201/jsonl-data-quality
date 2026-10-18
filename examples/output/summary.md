# JSONL 数据质检结果

输入 1 个文件，共 94 个物理行。通过 80 行，问题行 14，问题项 14。

成功解析对象 89 行，非法/空白行 5。同一行可有多个问题项。

| 问题代码 | 次数 |
| --- | ---: |
| blank_line | 1 |
| duplicate_content | 2 |
| duplicate_id | 1 |
| invalid_enum | 1 |
| invalid_json | 3 |
| invalid_type | 1 |
| missing_field | 2 |
| not_object | 1 |
| too_long | 1 |
| too_short | 1 |

长度按 Unicode 字符或数组元素计数，不是 token 数。重复只标记后出现的行，并关联首条来源。质检不修改、不删除输入数据；结构检查不判断答案真实性或语义质量。
