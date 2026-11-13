"""中央侧摘要接收与合并。

合并分两步：:meth:`MergeCentral.ingest` 对每个信封做契约检查并写入状态；
:meth:`MergeCentral.recompute` 从当前全部有效信封重新累加并推导统计量。
因为加权矩满足可加性，重算结果只取决于“哪些信封有效”，与接收顺序无关——
重发、修订、补传分区和撤销都会落到同一组有效信封上。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from registry_privacy_gateway.runtime import stable_json
from registry_privacy_gateway.statistics.contract import (
    SessionContract,
    SummaryContractError,
    check_intake_compatibility,
    envelope_digest,
    reason_code,
    validate_envelope_shape,
)
from registry_privacy_gateway.statistics.store import (
    JOURNAL_REPLAY,
    JOURNAL_REPLACE,
    JOURNAL_SUPPLEMENT,
    JOURNAL_WITHDRAW,
    SummaryStore,
)

_EPS = 1e-12


@dataclass(slots=True)
class IngestReceipt:
    accepted: bool
    action: str | None
    batch: str
    partition: str
    revision: int
    digest: str | None = None
    reason_code: str | None = None
    message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MergeResult:
    contract: dict[str, Any]
    statistics: dict[str, Any]
    active_batches: list[dict[str, Any]]
    journal_count: int
    recomputed_from_scratch: bool = True


class MergeCentral:
    def __init__(self, store: str | Path | SummaryStore, session: SessionContract):
        self.store = store if isinstance(store, SummaryStore) else SummaryStore(store)
        self.session = session

    # ------------------------------------------------------------------ 接收
    def ingest(self, envelope: Mapping[str, Any]) -> IngestReceipt:
        try:
            validate_envelope_shape(envelope)
            if envelope.get("digest") != envelope_digest(envelope):
                raise SummaryContractError(
                    "digest-mismatch", "信封摘要指纹不一致，载荷可能被篡改或损坏")
            check_intake_compatibility(envelope, self.session,
                                       self.store.active_partition_owners())
            current = self.store.current(envelope["batch"], envelope["partition"])
            action = self._classify_action(envelope, current)
            record = self.store.apply(envelope, action)
            return IngestReceipt(True, action, envelope["batch"], envelope["partition"],
                                 envelope["revision"], digest=record["digest"])
        except SummaryContractError as exc:
            revision = envelope.get("revision")
            digest = envelope.get("digest")
            return IngestReceipt(
                False, None, str(envelope.get("batch", "?")),
                str(envelope.get("partition", "?")),
                int(revision) if isinstance(revision, int) else 0,
                digest=digest if isinstance(digest, str) else None,
                reason_code=reason_code(exc), message=str(exc), detail=exc.detail)

    @staticmethod
    def _classify_action(envelope: Mapping[str, Any],
                         current: Mapping[str, Any] | None) -> str:
        revision = envelope["revision"]
        if current is not None and revision < current["revision"]:
            # 乱序到达的旧 revision 一律拒绝：最终状态只由最高 revision 决定，
            # 迟到的旧信封（无论内容还是撤销）都不能回退当前状态。
            raise SummaryContractError(
                "stale-revision", "收到旧 revision 的信封，当前状态已经更新",
                {"current_revision": current["revision"], "incoming_revision": revision})
        if envelope["withdrawn"]:
            if current is not None and current["status"] == "withdrawn" \
                    and revision == current["revision"]:
                # 同一撤销信封的重发，幂等记为重发而不是重复撤销。
                return JOURNAL_REPLAY
            # 允许“先到的撤销”：即使摘要尚未到达也登记墓碑，随后迟到的低 revision
            # 摘要会被上面的版本规则挡住，保证顺序无关。
            return JOURNAL_WITHDRAW
        if current is None:
            return JOURNAL_SUPPLEMENT
        if current["status"] == "withdrawn":
            raise SummaryContractError(
                "batch-withdrawn", "该 batch/分区已撤销，重新启用需要更高 revision 的"
                "恢复信封或新的 batch 标识",
                {"batch": envelope["batch"], "partition": envelope["partition"],
                 "current_revision": current["revision"]})
        if revision == current["revision"]:
            if envelope["digest"] == current["digest"]:
                return JOURNAL_REPLAY
            raise SummaryContractError(
                "revision-conflict", "同一 revision 出现内容不同的信封，无法确定权威版本",
                {"batch": envelope["batch"], "partition": envelope["partition"],
                 "revision": revision})
        return JOURNAL_REPLACE

    # -------------------------------------------------------------- 全量重算
    def recompute(self) -> MergeResult:
        envelopes = self.store.active_envelopes()
        merged = _merge_envelopes(envelopes, self.session)
        statistics = _derive_statistics(merged, self.session)
        active = [
            {"batch": envelope["batch"], "partition": envelope["partition"],
             "site": envelope["site"], "revision": envelope["revision"]}
            for envelope in envelopes
        ]
        return MergeResult(
            contract={
                "purpose": self.session.purpose,
                "observation_unit": self.session.observation_unit,
                "weight_kind": self.session.weight_kind,
                "field_version": self.session.field_version,
                "fields": list(self.session.fields),
                "group_by": list(self.session.group_by),
                "partition_contract_version": self.session.partition_version,
            },
            statistics=statistics,
            active_batches=active,
            journal_count=len(self.store.journal()),
        )

    def to_dict(self, result: MergeResult) -> dict[str, Any]:
        return {
            "contract": result.contract,
            "statistics": result.statistics,
            "active_batches": result.active_batches,
            "journal_count": result.journal_count,
            "recomputed_from_scratch": result.recomputed_from_scratch,
        }


# ---------------------------------------------------------------------- 累加
class _MarginalSum:
    __slots__ = ("field", "n", "sum", "sum_squares")

    def __init__(self, field: str):
        self.field = field
        self.n = 0
        self.sum = 0.0
        self.sum_squares = 0.0

    def add(self, block: Mapping[str, Any]) -> None:
        self.n += block["n"]
        self.sum += block["sum"]
        self.sum_squares += block["sum_squares"]


class _PairSum:
    __slots__ = ("left", "right", "pair_n", "sum_x", "sum_y", "sum_xy",
                 "sum_x2", "sum_y2", "covered")

    def __init__(self, left: str, right: str):
        self.left = left
        self.right = right
        self.pair_n = 0
        self.sum_x = 0.0
        self.sum_y = 0.0
        self.sum_xy = 0.0
        self.sum_x2 = 0.0
        self.sum_y2 = 0.0
        self.covered = False

    def add(self, block: Mapping[str, Any]) -> None:
        self.pair_n += block["pair_n"]
        self.sum_x += block["sum_x"]
        self.sum_y += block["sum_y"]
        self.sum_xy += block["sum_xy"]
        self.sum_x2 += block["sum_x2"]
        self.sum_y2 += block["sum_y2"]
        self.covered = True


class _MergedScope:
    def __init__(self, session: SessionContract):
        self.marginals = {field: _MarginalSum(field) for field in session.fields}
        self.pairs = {}
        for index, left in enumerate(session.fields):
            for right in session.fields[index + 1:]:
                ordered = tuple(sorted((left, right)))
                self.pairs[ordered] = _PairSum(ordered[0], ordered[1])


def _merge_envelopes(envelopes: Sequence[Mapping[str, Any]],
                     session: SessionContract) -> dict[str, _MergedScope]:
    merged: dict[str, _MergedScope] = {}

    def scope_for(group: Any) -> _MergedScope:
        key = stable_json(group)
        if key not in merged:
            merged[key] = _MergedScope(session)
        return merged[key]

    for envelope in envelopes:
        for scope in envelope["scopes"]:
            target = scope_for(scope["group"])
            for block in scope["marginals"]:
                target.marginals[block["field"]].add(block)
            for block in scope["pairs"]:
                key = tuple(sorted((block["left"], block["right"])))
                target.pairs[key].add(block)
    return merged


def _round(value: float) -> float:
    rounded = round(value, 10)
    return 0.0 if rounded == 0 else rounded


def _undefined(reason: str, **detail: Any) -> dict[str, Any]:
    return {"defined": False, "reason": reason, "detail": detail}


def _marginal_stats(block: _MarginalSum) -> dict[str, Any]:
    n = block.n
    if n <= 0:
        undefined = _undefined("no-valid-observations")
        return {"field": block.field, "n": 0, "mean": undefined,
                "population_variance": undefined, "sample_variance": undefined}
    mean = block.sum / n
    # 总体二阶矩口径：E[X²] - E[X]²，负的微小浮点误差钳到 0。
    population_variance = max(0.0, block.sum_squares / n - mean * mean)
    entry: dict[str, Any] = {
        "field": block.field,
        "n": n,
        "mean": _round(mean),
        "population_variance": _round(population_variance),
    }
    if n - 1 <= 0:
        # 有效频数减一的样本估计：频数不足时明确未定义，而不是退化成总体值。
        entry["sample_variance"] = _undefined("sample-estimate-undefined",
                                              effective_frequency=n, denominator="n-1")
    else:
        entry["sample_variance"] = _round(population_variance * n / (n - 1))
    return entry


def _pair_stats(block: _PairSum) -> dict[str, Any]:
    pair = [block.left, block.right]
    if not block.covered:
        # 没有任何站点声明该列对的共同观测矩：宁可不计算，也不拿边际矩冒充。
        undefined = _undefined("cross-moment-unavailable")
        return {"pair": pair, "pair_n": 0,
                "population_covariance": undefined,
                "sample_covariance": undefined, "correlation": undefined}
    n = block.pair_n
    if n <= 0:
        undefined = _undefined("no-valid-paired-observations")
        return {"pair": pair, "pair_n": 0,
                "population_covariance": undefined,
                "sample_covariance": undefined, "correlation": undefined}
    mean_x = block.sum_x / n
    mean_y = block.sum_y / n
    # 全部矩都基于同一个共同观测集合，分母一致。
    population_cov = block.sum_xy / n - mean_x * mean_y
    var_x = max(0.0, block.sum_x2 / n - mean_x * mean_x)
    var_y = max(0.0, block.sum_y2 / n - mean_y * mean_y)
    entry: dict[str, Any] = {
        "pair": pair,
        "pair_n": n,
        "population_covariance": _round(population_cov),
    }
    if n - 1 <= 0:
        entry["sample_covariance"] = _undefined("sample-estimate-undefined",
                                                effective_frequency=n, denominator="n-1")
    else:
        entry["sample_covariance"] = _round(population_cov * n / (n - 1))
    if var_x <= _EPS or var_y <= _EPS:
        # 零方差时相关系数在数学上没有定义，不能输出虚假的零相关。
        entry["correlation"] = _undefined("zero-variance",
                                          variance_x=_round(var_x), variance_y=_round(var_y))
    else:
        entry["correlation"] = _round(population_cov / math.sqrt(var_x * var_y))
    return entry


def _derive_statistics(merged: Mapping[str, _MergedScope],
                       session: SessionContract) -> dict[str, Any]:
    scopes_out: list[dict[str, Any]] = []
    for key in sorted(merged):
        group = json.loads(key)
        scope = merged[key]
        marginals = [_marginal_stats(scope.marginals[field]) for field in session.fields]
        pairs = [_pair_stats(scope.pairs[key_pair]) for key_pair in sorted(scope.pairs)]
        scopes_out.append({"group": group, "marginals": marginals, "pairs": pairs})
    return {"observation_unit": session.observation_unit, "weight_kind": session.weight_kind,
            "variance_convention": {
                "population": "second-moment divisor n",
                "sample": "divisor n-1, undefined when effective frequency < 2"},
            "scopes": scopes_out}
