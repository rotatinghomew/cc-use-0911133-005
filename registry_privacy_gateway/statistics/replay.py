"""本地虚构资料上的完整交换重放。

重放严格模拟真实分工：站点侧只能看到本站明细，先在站内压缩成摘要信封；中央侧
只接收信封，从不接触 ``rows`` 里的逐人数据。:func:`reference_statistics` 是测试
专用的“上帝视角”参照，直接从授权明细全量重算，用于证明中央合并结果正确，
它本身不是中央接口的一部分。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Any, Mapping, Sequence

from registry_privacy_gateway.runtime import stable_json
from registry_privacy_gateway.statistics.contract import SessionContract
from registry_privacy_gateway.statistics.merge import (
    IngestReceipt,
    MergeCentral,
    MergeResult,
)
from registry_privacy_gateway.statistics.summarize import (
    build_summary_envelopes,
    build_withdraw_envelopes,
)
from registry_privacy_gateway.statistics.store import SummaryStore


@dataclass(slots=True)
class ReplayOutcome:
    receipts: list[IngestReceipt]
    result: MergeResult
    envelopes_emitted: int
    withdrawn_batches: list[str]
    reference: dict[str, Any]
    rejected: list[dict[str, Any]] = field(default_factory=list)


def session_from_fixture(fixture: Mapping[str, Any]) -> SessionContract:
    return SessionContract.from_analysis(fixture["analysis"], fixture["partition_contract"])


def _site_payloads(fixture: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {site["site"]: site for site in fixture["sites"]}


def _transmission_payload(transmission: Mapping[str, Any],
                          sites: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    site = sites[transmission["site"]]
    payload: dict[str, Any] = {
        "site": site["site"],
        "batch": site["batch"],
        "revision": transmission["revision"],
        "rows": site.get("rows", []),
    }
    if "replace_values" in transmission:
        payload["replace_values"] = transmission["replace_values"]
    if transmission.get("withdraw"):
        payload["withdraw"] = True
    return payload


def _envelopes_for(transmission: Mapping[str, Any],
                   sites: Mapping[str, Mapping[str, Any]],
                   session: SessionContract) -> list[dict[str, Any]]:
    site = sites[transmission["site"]]
    if transmission.get("withdraw"):
        # 只能撤销本站负责的分区；持有的他站共享副本不属于本站可撤销范围。
        partitions = sorted({
            row["partition"] for row in site.get("rows", [])
            if session.authority.get(row["partition"]) == site["site"]
        })
        return build_withdraw_envelopes(
            site["site"], site["batch"], transmission["revision"], partitions, session)
    return build_summary_envelopes(_transmission_payload(transmission, sites), session)


def replay_fixture(fixture: Mapping[str, Any],
                   store: str | Path | SummaryStore) -> ReplayOutcome:
    """按 fixture 声明的传输顺序跑完整流程，返回中央回执与全量重算结果。"""
    session = session_from_fixture(fixture)
    central = MergeCentral(store, session)
    sites = _site_payloads(fixture)
    receipts: list[IngestReceipt] = []
    emitted = 0
    withdrawn: list[str] = []
    rejected: list[dict[str, Any]] = []

    for index, transmission in enumerate(fixture["transmissions"]):
        envelopes = _envelopes_for(transmission, sites, session)
        emitted += len(envelopes)
        for envelope in envelopes:
            receipt = central.ingest(envelope)
            receipts.append(receipt)
            if not receipt.accepted:
                rejected.append({"transmission_index": index,
                                 "reason_code": receipt.reason_code,
                                 "message": receipt.message, "detail": receipt.detail})
            if receipt.action == "withdraw":
                withdrawn.append(receipt.batch)
    result = central.recompute()
    reference = reference_statistics(fixture)
    return ReplayOutcome(receipts=receipts, result=result, envelopes_emitted=emitted,
                         withdrawn_batches=sorted(set(withdrawn)),
                         reference=reference, rejected=rejected)


# ------------------------------------------------------------- 测试专用参照
def effective_rows(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按传输历史计算当前仍然有效的授权明细行（仅测试参照使用）。

    规则与中央有效信封集合保持一致：被撤销 batch 的全部行失效；其余行按分区契约
    只保留负责站提交的版本；修订替换在站内先应用。
    """
    session = session_from_fixture(fixture)
    sites = _site_payloads(fixture)
    withdrawn_batches: set[str] = set()
    replaced: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for transmission in fixture["transmissions"]:
        site = sites[transmission["site"]]
        if transmission.get("withdraw"):
            withdrawn_batches.add(site["batch"])
        if "replace_values" in transmission:
            # 替换指令按“站点 + 传输 revision”登记，revision 不等于站点资料的基线号。
            replaced[(transmission["site"], transmission["revision"])] = transmission["replace_values"]

    effective: list[dict[str, Any]] = []
    for site in fixture["sites"]:
        if site["batch"] in withdrawn_batches:
            continue
        rows = [dict(row) for row in site["rows"]]
        replacements: list[Mapping[str, Any]] = []
        for (candidate_site, revision), instructions in replaced.items():
            if candidate_site == site["site"]:
                replacements = instructions
        by_id = {row.get("observation_id"): row for row in rows}
        for replacement in replacements:
            target = by_id[replacement["observation_id"]]
            for key, value in replacement.items():
                if key != "observation_id":
                    target[key] = value
        for row in rows:
            if row.get("partition") not in session.disjoint_partitions:
                continue
            if session.authority[row["partition"]] != site["site"]:
                continue
            if row.get("purpose") != session.purpose:
                continue
            effective.append(row)
    return effective


def reference_statistics(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """直接在授权明细上计算加权统计，作为中央输出的对照真值。"""
    session = session_from_fixture(fixture)
    rows = effective_rows(fixture)

    def keep(row: Mapping[str, Any], field: str) -> bool:
        value = row.get(field)
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def scope_stats(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        marginals = []
        for field_name in session.fields:
            valid = [row for row in subset if keep(row, field_name)]
            n = sum(int(row["weight"]) for row in valid)
            if n == 0:
                marginals.append({"field": field_name, "n": 0})
                continue
            total = sum(int(row["weight"]) * float(row[field_name]) for row in valid)
            total_sq = sum(int(row["weight"]) * float(row[field_name]) ** 2 for row in valid)
            mean = total / n
            pop_var = max(0.0, total_sq / n - mean * mean)
            marginals.append({
                "field": field_name, "n": n, "mean": mean,
                "population_variance": pop_var,
                "sample_variance": pop_var * n / (n - 1) if n > 1 else None,
            })
        pairs = []
        for index, left in enumerate(session.fields):
            for right in session.fields[index + 1:]:
                paired = [row for row in subset if keep(row, left) and keep(row, right)]
                n = sum(int(row["weight"]) for row in paired)
                if n == 0:
                    pairs.append({"pair": [left, right], "pair_n": 0})
                    continue
                sx = sum(int(row["weight"]) * float(row[left]) for row in paired)
                sy = sum(int(row["weight"]) * float(row[right]) for row in paired)
                sxy = sum(int(row["weight"]) * float(row[left]) * float(row[right])
                          for row in paired)
                sx2 = sum(int(row["weight"]) * float(row[left]) ** 2 for row in paired)
                sy2 = sum(int(row["weight"]) * float(row[right]) ** 2 for row in paired)
                mx, my = sx / n, sy / n
                pop_cov = sxy / n - mx * my
                vx = max(0.0, sx2 / n - mx * mx)
                vy = max(0.0, sy2 / n - my * my)
                correlation = pop_cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else None
                pairs.append({
                    "pair": [left, right], "pair_n": n,
                    "population_covariance": pop_cov,
                    "sample_covariance": pop_cov * n / (n - 1) if n > 1 else None,
                    "correlation": correlation,
                })
        return {"marginals": marginals, "pairs": pairs}

    scopes = [{"group": None, **scope_stats(rows)}]
    if session.group_by:
        group_values = sorted({stable_json({col: row[col] for col in session.group_by})
                               for row in rows
                               if all(isinstance(row.get(col), str) and row[col].strip()
                                      for col in session.group_by)})
        for marker in group_values:
            key = json.loads(marker)
            subset = [row for row in rows
                      if all(row.get(col) == value for col, value in key.items())]
            scopes.append({"group": key, **scope_stats(subset)})
    return {"observation_unit": session.observation_unit,
            "weight_kind": session.weight_kind, "scopes": scopes,
            "effective_row_count": len(rows)}
