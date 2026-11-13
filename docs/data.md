# 业务样例数据

`fixtures/merge_statistics.json` 使用本地合成数据，不含真实个人资料。

weight 为正整数频数。partition 是协作站认可的互斥分区，不是个人键；observation_id 和 private_note 均限站内。authoritative_site 指明共享分区的负责站。

`fixtures/demo.json` 是当前单笔业务流程的样例，和批量分析资料分开保存。

## 摘要交换契约（statistics-summary-v1）

站点不共享逐人明细，只交换按 partition 拆分的摘要信封。信封结构由 `registry_privacy_gateway.statistics.contract` 校验，顶层只允许信封元数据、`contract` 声明、`scopes` 矩块和 `excluded` 隔离记录：

- `contract` 锚定合作约定：`purpose` 用途、`observation_unit` 观测单位、`weight_kind` 权重口径、`field_version` 字段/计量单位版本、`fields` 分析列、`group_by` 分群维度、分区契约版本与该分区的 `authoritative_site`。
- 每个 scope（总体 `group: null` 加各分群）的 `marginals` 携带每列的 `n / sum / sum_squares`，只统计该列非缺失的有效观测；`pairs` 携带列对在共同观测集合上的 `pair_n / sum_x / sum_y / sum_xy / sum_x2 / sum_y2`。边际矩与交叉矩分母不同，缺交叉矩的信封会被 `cross-moment-coverage-gap` 拒绝，不能用边际数冒充共同观测矩。
- `excluded` 只保留原因码和观测指纹（含 `field-minimized` 剥离痕迹），不含原始行值；`private_note` 等未授权字段在站内剥离，信封携带指纹而不是 observation_id 原值。

中央接收时检查字段版本、群体维度、口径、分区重叠和权限，原因码包括 `field-version-mismatch`、`group-dimension-mismatch`、`observation-unit-mismatch`、`purpose-mismatch`、`partition-not-contracted`、`site-not-authoritative`、`inseparable-overlap`、`digest-mismatch`、`stale-revision`、`revision-conflict` 等。无法分离的重叠要求各方先协商互斥分区，中央不会声称任意重叠都能精确去重。

观测单位按声明解释：`service-visit` 下同一居民的多次服务是多个合法观测，中央不按人去重，重复贡献由站点在站内负责。
