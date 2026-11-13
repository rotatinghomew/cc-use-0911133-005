"""站点侧摘要计算。

输入是站点本地的授权明细（含正整数频数权重），输出是按互斥分区拆分的摘要信封。
站点负责在站内完成去重与口径解释：当 ``observation_unit`` 声明为 service-visit 时，
同一居民的多次服务就是多个合法观测，中央不会也不能按人去重；但同一站点载荷里
重复出现的 observation_id 会被当作站内数据问题隔离，避免样本量被意外夸大。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from registry_privacy_gateway.runtime import fingerprint
from registry_privacy_gateway.statistics.contract import (
    ENVELOPE_TYPE,
    SCHEMA_VERSION,
    SessionContract,
    SummaryContractError,
    assert_no_raw_rows,
    envelope_digest,
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _observation_ref(site: str, observation_id: Any) -> str:
    # observation_id 限站内使用，交换时只给指纹，中央无法回溯到个人。
    return fingerprint({"site": site, "observation_id": str(observation_id)})


class _Marginal:
    __slots__ = ("field", "n", "sum", "sum_squares")

    def __init__(self, field: str):
        self.field = field
        self.n = 0
        self.sum = 0.0
        self.sum_squares = 0.0

    def add(self, weight: int, value: float) -> None:
        self.n += weight
        self.sum += weight * value
        self.sum_squares += weight * value * value

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "n": self.n, "sum": self.sum, "sum_squares": self.sum_squares}


class _Pair:
    __slots__ = ("left", "right", "pair_n", "sum_x", "sum_y", "sum_xy", "sum_x2", "sum_y2")

    def __init__(self, left: str, right: str):
        self.left = left
        self.right = right
        self.pair_n = 0
        self.sum_x = 0.0
        self.sum_y = 0.0
        self.sum_xy = 0.0
        self.sum_x2 = 0.0
        self.sum_y2 = 0.0

    def add(self, weight: int, x: float, y: float) -> None:
        self.pair_n += weight
        self.sum_x += weight * x
        self.sum_y += weight * y
        self.sum_xy += weight * x * y
        # 平方和与交叉矩同属共同观测集合，保证相关系数分母与协方差同口径。
        self.sum_x2 += weight * x * x
        self.sum_y2 += weight * y * y

    def to_dict(self) -> dict[str, Any]:
        return {"left": self.left, "right": self.right, "pair_n": self.pair_n,
                "sum_x": self.sum_x, "sum_y": self.sum_y, "sum_xy": self.sum_xy,
                "sum_x2": self.sum_x2, "sum_y2": self.sum_y2}


class _ScopeAccumulator:
    """单个 scope（总体或一个分群）上的加权矩累加器。"""

    def __init__(self, session: SessionContract):
        self.marginals = {field: _Marginal(field) for field in session.fields}
        self.pairs = {}
        for index, left in enumerate(session.fields):
            for right in session.fields[index + 1:]:
                ordered = tuple(sorted((left, right)))
                self.pairs[ordered] = _Pair(ordered[0], ordered[1])

    def add(self, row: Mapping[str, Any], weight: int) -> None:
        # 每个列各自基于“该列非缺失”的有效观测集合；缺失值不进入对应矩。
        for field, marginal in self.marginals.items():
            value = row.get(field)
            if _is_number(value):
                marginal.add(weight, float(value))
        # 交叉矩只基于两列同时非缺失的共同观测集合，分母与边际矩不同。
        for (left, right), pair in self.pairs.items():
            x = row.get(left)
            y = row.get(right)
            if _is_number(x) and _is_number(y):
                pair.add(weight, float(x), float(y))

    def to_dict(self, group: dict[str, str] | None) -> dict[str, Any]:
        return {
            "group": group,
            "marginals": [self.marginals[field].to_dict() for field in self.marginals],
            "pairs": [self.pairs[key].to_dict() for key in sorted(self.pairs)],
        }


class _PartitionAccumulator:
    def __init__(self, session: SessionContract):
        self.session = session
        self.overall = _ScopeAccumulator(session)
        self.groups: dict[str, _ScopeAccumulator] = {}
        self.group_values: dict[str, dict[str, str]] = {}
        self.excluded: list[dict[str, Any]] = []
        self.minimized: list[dict[str, Any]] = []

    def _group_key(self, row: Mapping[str, Any]) -> dict[str, str] | None:
        if not self.session.group_by:
            return None
        key = {column: row.get(column) for column in self.session.group_by}
        if any(not isinstance(value, str) or not value.strip() for value in key.values()):
            return None
        return key

    def add_row(self, row: Mapping[str, Any], site: str) -> None:
        ref = _observation_ref(site, row.get("observation_id"))
        if row.get("purpose") != self.session.purpose:
            self.excluded.append({"reason_code": "purpose-mismatch", "observation_fingerprint": ref,
                                  "detail": {"row_purpose": row.get("purpose")}})
            return
        partition = row.get("partition")
        if partition not in self.session.disjoint_partitions:
            self.excluded.append({"reason_code": "partition-not-contracted",
                                  "observation_fingerprint": ref, "detail": {"partition": partition}})
            return
        if self.session.authority[partition] != site:
            self.excluded.append({"reason_code": "site-not-authoritative",
                                  "observation_fingerprint": ref,
                                  "detail": {"partition": partition,
                                             "authoritative_site": self.session.authority[partition]}})
            return
        weight = row.get("weight")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            self.excluded.append({"reason_code": "weight-invalid", "observation_fingerprint": ref,
                                  "detail": {"weight": weight}})
            return
        bad_fields = [field for field in self.session.fields
                      if row.get(field) is not None and not _is_number(row.get(field))]
        if bad_fields:
            self.excluded.append({"reason_code": "measurement-invalid", "observation_fingerprint": ref,
                                  "detail": {"fields": bad_fields}})
            return
        group_key = self._group_key(row)
        if self.session.group_by and group_key is None:
            self.excluded.append({"reason_code": "group-value-missing", "observation_fingerprint": ref})
            return
        self.overall.add(row, weight)
        if group_key is not None:
            marker = fingerprint(group_key)
            self.group_values.setdefault(marker, group_key)
            self.groups.setdefault(marker, _ScopeAccumulator(self.session)).add(row, weight)


def _apply_replacements(rows: Sequence[Mapping[str, Any]],
                        replacements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    materialized = [dict(row) for row in rows]
    by_id: dict[Any, dict[str, Any]] = {}
    for row in materialized:
        by_id.setdefault(row.get("observation_id"), row)
    for replacement in replacements:
        target = by_id.get(replacement.get("observation_id"))
        if target is None:
            raise SummaryContractError(
                "replace-target-missing",
                "修订指令引用的 observation_id 在本站当前明细中不存在",
                {"observation_id": replacement.get("observation_id")})
        for key, value in replacement.items():
            if key != "observation_id":
                target[key] = value
    return materialized


def _minimize_row(row: Mapping[str, Any], session: SessionContract, site: str,
                  notes: list[dict[str, Any]]) -> dict[str, Any]:
    # 结构字段 + 会话授权字段之外的一切列（典型如 private_note）在站内剥离。
    permitted = frozenset(session.allowed_fields) | frozenset(
        {"observation_id", "partition", "purpose", "weight"})
    extra = sorted(set(row) - permitted)
    if extra:
        notes.append({"reason_code": "field-minimized",
                      "observation_fingerprint": _observation_ref(site, row.get("observation_id")),
                      "detail": {"removed_fields": extra}})
    return {key: value for key, value in row.items() if key in permitted}


def build_summary_envelopes(site_payload: Mapping[str, Any],
                            session: SessionContract) -> list[dict[str, Any]]:
    """把一个站点的一次传输压缩成每分区一个摘要信封。

    ``site_payload`` 是站内文件，允许包含授权明细；返回值已经是可交换的摘要，
    调用方不得再把站内文件发给中央。
    """
    site = site_payload.get("site")
    batch = site_payload.get("batch")
    revision = site_payload.get("revision")
    if not isinstance(site, str) or not site or not isinstance(batch, str) or not batch:
        raise SummaryContractError("envelope-malformed", "site 与 batch 必须是非空字符串")
    if not isinstance(revision, int) or revision < 1:
        raise SummaryContractError("revision-invalid", "revision 必须是不小于 1 的整数")
    if site_payload.get("withdraw"):
        raise SummaryContractError(
            "withdraw-not-supported-here", "撤销传输请使用 build_withdraw_envelopes")

    raw_rows = site_payload.get("rows", [])
    if not isinstance(raw_rows, list) or not raw_rows:
        raise SummaryContractError("summary-empty", "站点传输没有任何可汇总的明细行")
    replacements = site_payload.get("replace_values", [])
    if not isinstance(replacements, list):
        raise SummaryContractError("envelope-malformed", "replace_values 必须是列表")
    rows = _apply_replacements(raw_rows, replacements)

    partitions: dict[str, _PartitionAccumulator] = {}
    seen_ids: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise SummaryContractError("envelope-malformed", "明细行必须是对象")
        clean = _minimize_row(row, session, site, [])
        partition = clean.get("partition")
        marker = (str(partition), str(clean.get("observation_id")))
        ref = _observation_ref(site, clean.get("observation_id"))
        accumulator = partitions.setdefault(str(partition), _PartitionAccumulator(session))
        if marker in seen_ids:
            accumulator.excluded.append(
                {"reason_code": "duplicate-observation", "observation_fingerprint": ref,
                 "detail": {"partition": partition}})
            continue
        seen_ids.add(marker)
        # 字段最小化记录挂到所属分区，保持“行不离开站点、只留痕迹”。
        extra = sorted(set(row) - set(clean))
        if extra:
            accumulator.minimized.append(
                {"reason_code": "field-minimized", "observation_fingerprint": ref,
                 "detail": {"removed_fields": extra}})
        accumulator.add_row(clean, site)

    envelopes: list[dict[str, Any]] = []
    for partition in sorted(partitions):
        accumulator = partitions[partition]
        # 非负责站持有的共享分区副本只会留下隔离记录，不产生该分区信封，
        # 避免同一批观测在中央被重复计数。
        if session.authority[partition] != site:
            continue
        scopes = [accumulator.overall.to_dict(None)]
        for marker in sorted(accumulator.group_values):
            scopes.append(accumulator.groups[marker].to_dict(accumulator.group_values[marker]))
        accepted = max(marginal.n for marginal in accumulator.overall.marginals.values())
        pair_support = max(pair.pair_n for pair in accumulator.overall.pairs.values())
        if accepted == 0 and pair_support == 0:
            raise SummaryContractError(
                "summary-empty", f"分区 {partition} 的全部明细都被隔离，无法形成有效摘要",
                {"partition": partition, "excluded": accumulator.minimized + accumulator.excluded})
        envelope = {
            "envelope_type": ENVELOPE_TYPE,
            "schema_version": SCHEMA_VERSION,
            "site": site,
            "batch": batch,
            "partition": partition,
            "revision": revision,
            "withdrawn": False,
            "contract": session.envelope_contract(partition, site),
            "scopes": scopes,
            "excluded": accumulator.minimized + accumulator.excluded,
        }
        envelope["digest"] = envelope_digest(envelope)
        assert_no_raw_rows(envelope, ["private_note"])
        envelopes.append(envelope)
    if not envelopes:
        excluded: list[dict[str, Any]] = []
        for partition_name in sorted(partitions):
            excluded.extend(partitions[partition_name].minimized)
            excluded.extend(partitions[partition_name].excluded)
        raise SummaryContractError(
            "summary-empty", "站点在其负责的分区上没有任何有效观测，无法形成摘要",
            {"site": site, "partitions": sorted(partitions), "excluded": excluded})
    return envelopes


def build_withdraw_envelopes(site: str, batch: str, revision: int,
                             partitions: Sequence[str],
                             session: SessionContract) -> list[dict[str, Any]]:
    """生成撤销信封：中央收到后把对应 batch/分区的摘要全部标记为失效。"""
    if not isinstance(site, str) or not site or not isinstance(batch, str) or not batch:
        raise SummaryContractError("envelope-malformed", "site 与 batch 必须是非空字符串")
    if not isinstance(revision, int) or revision < 1:
        raise SummaryContractError("revision-invalid", "revision 必须是不小于 1 的整数")
    unknown = sorted(set(partitions) - session.disjoint_partitions)
    if unknown:
        raise SummaryContractError("partition-not-contracted",
                                   "撤销引用了契约之外的分区", {"partitions": unknown})
    envelopes = []
    for partition in sorted(set(partitions)):
        if session.authority[partition] != site:
            raise SummaryContractError("site-not-authoritative",
                                       "只有分区负责站可以撤销该分区摘要",
                                       {"site": site, "partition": partition})
        envelope = {
            "envelope_type": ENVELOPE_TYPE,
            "schema_version": SCHEMA_VERSION,
            "site": site,
            "batch": batch,
            "partition": partition,
            "revision": revision,
            "withdrawn": True,
            "contract": session.envelope_contract(partition, site),
            "scopes": [],
            "excluded": [],
        }
        envelope["digest"] = envelope_digest(envelope)
        envelopes.append(envelope)
    return envelopes
