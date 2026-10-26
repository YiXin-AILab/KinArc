# Tau3 Banking 数据集

本文件仅记录 [Tau3 Banking](../README.md) 的训练、验证和测试集划分。
完整 Case 从 Tau2 Bench `v1.0.1` 官方仓库的
`data/tau2/domains/banking_knowledge/tasks/task_*.json` 获取，
按以下 ID 清单及顺序分别整理为 `train.json`、`validation.json` 和 `test.json`。
请使用单独的 Case 文件，不使用同级汇总文件 `tasks.json`，两者内容存在差异。

## 数据集与 Case ID

| 文件 | Case 数 | 用途 |
| --- | ---: | --- |
| `train.json` | 34 | 为自进化优化器提供训练反馈 |
| `validation.json` | 23 | 比较候选并选择最佳版本 |
| `test.json` | 40 | 候选确定后，由用户手动执行测试 |

三个集合共 97 个 Case，集合内及集合之间均无重复 ID。以下按 JSON 文件中的原始顺序列出，
不是执行结果或评分排名。ID 保留 `task_` 前缀和三位数字，不重新编号。

### train.json

```text
task_098 task_095 task_094 task_090 task_087 task_084 task_081 task_078
task_077 task_072 task_068 task_065 task_064 task_060 task_057 task_056
task_054 task_050 task_046 task_045 task_044 task_039 task_038 task_036
task_033 task_025 task_024 task_021 task_018 task_016 task_015 task_008
task_007 task_002
```

### validation.json

```text
task_100 task_096 task_091 task_088 task_085 task_079 task_075 task_073
task_071 task_066 task_061 task_059 task_058 task_055 task_052 task_047
task_040 task_037 task_032 task_020 task_012 task_010 task_003
```

### test.json

```text
task_102 task_101 task_099 task_097 task_093 task_092 task_089 task_086
task_083 task_082 task_080 task_076 task_074 task_070 task_069 task_067
task_063 task_062 task_053 task_051 task_049 task_048 task_043 task_041
task_035 task_034 task_031 task_029 task_028 task_027 task_026 task_023
task_022 task_019 task_017 task_014 task_006 task_005 task_004 task_001
```
