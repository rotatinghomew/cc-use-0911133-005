# registry_privacy_gateway

登记资料交换网关：用途校验、最小字段集、匿名键、隔离和传输回执。当前入口是 `registry_privacy_gateway.gateway.transfer`，它使用本地 JSON 资料和 SQLite 状态；目录 `registry_privacy_gateway` 存放业务模块，`tests/privacy_batches` 验证既有业务行为。

`registry_privacy_gateway.statistics` 提供只交换摘要的联合统计能力：站点侧 `summarize` 把授权明细压缩成按互斥分区拆分的加权摘要信封，中央侧 `merge` 只接收信封、维护修订/撤销状态并全量重算合并统计。逐人明细永远不进入中央接口。

## 环境

使用 Python 3.11，全部依赖来自标准库。`requirements.txt` 为空，不需要下载数据、模型或第三方包。数据库在运行时创建，可以删除临时数据库重新开始。

## 检查

```bash
python3 -m unittest discover -s tests/privacy_batches -v
python3 -m unittest discover -s tests/analysis_context -v
python3 -m unittest discover -s tests/statistics_merge -v
python3 -m compileall -q registry_privacy_gateway tests scripts
```

## 运行

```bash
python3 -m registry_privacy_gateway.gateway.transfer  fixtures/demo.json --db /tmp/registry_privacy_gateway-demo.sqlite3
python3 scripts/check_project.py
```

`check_project.py` 串行执行测试、编译和真实业务入口，将本次业务输出完整打印到终端；状态文件使用独立临时目录，重复运行不沿用上次状态。

### 只交换摘要的联合统计

两端通过 `--input`、`--output` 和必要状态参数串起来，会话契约（`analysis` 加 `partition_contract`）由 `--contract` 传入：

```bash
# 站点侧：站内文件（含 rows）只在本地读取，输出可交换的摘要信封
python3 -m registry_privacy_gateway.statistics summarize \
  --input /path/to/site-transmission.json \
  --output /path/to/summary-envelopes.json \
  --contract fixtures/merge_statistics.json

# 中央侧：输入只能是摘要信封，SQLite 状态保留重发、修订、补传与撤销
python3 -m registry_privacy_gateway.statistics merge \
  --input /path/to/summary-envelopes.json \
  --output /path/to/merge-result.json \
  --db /tmp/registry_privacy_gateway-statistics.sqlite3 \
  --contract fixtures/merge_statistics.json
```

撤销传输用 `{"withdraw": true, "partitions": [...]}` 的站内文件走同一个 `summarize` 入口。中央结果只取决于当前有效摘要集合：重发幂等、修订替换、补传新分区、撤销登记墓碑，乱序到达不改变最终统计，旧 revision 会被 `stale-revision` 拒绝。

统计口径：频数权重；每个列的均值与方差基于该列非缺失的有效观测集合，交叉矩（协方差、相关系数）只基于两列同时非缺失的共同观测集合，边际矩和交叉矩的分母不同、互不冒充。总体二阶矩用分母 n，样本估计用 n-1 且有效频数不足 2 时明确未定义；零方差或交叉信息不足时相关系数返回未定义原因，而不是虚假的零相关。

批量分析资料位于 `fixtures/merge_statistics.json`，字段含义见 `docs/data.md`。它与当前单笔流程的输入格式不同，不能直接交给原来的入口处理。
