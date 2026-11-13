"""只交换摘要的联合统计能力。

站点侧使用 :mod:`registry_privacy_gateway.statistics.summarize` 把本地授权明细
压缩成加权摘要信封；中央侧使用 :mod:`registry_privacy_gateway.statistics.merge`
校验信封、维护修订状态并全量重算合并统计。

中央接口只处理摘要信封，任何逐人原始行都不是该接口的输入或输出。
"""
from __future__ import annotations

from registry_privacy_gateway.statistics.contract import (
    ENVELOPE_TYPE,
    SCHEMA_VERSION,
    SummaryContractError,
    reason_code,
)
from registry_privacy_gateway.statistics.merge import MergeCentral, MergeResult
from registry_privacy_gateway.statistics.summarize import build_summary_envelopes, build_withdraw_envelopes
from registry_privacy_gateway.statistics.replay import ReplayOutcome, replay_fixture

__all__ = [
    "ENVELOPE_TYPE",
    "SCHEMA_VERSION",
    "SummaryContractError",
    "reason_code",
    "MergeCentral",
    "MergeResult",
    "build_summary_envelopes",
    "build_withdraw_envelopes",
    "ReplayOutcome",
    "replay_fixture",
]
