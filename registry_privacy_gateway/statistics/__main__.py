"""命令行入口：python3 -m registry_privacy_gateway.statistics ...

两个子命令对应交换两端：

* ``summarize``：站点侧，读取站内传输文件（含授权明细），输出每分区摘要信封；
* ``merge``：中央侧，读取信封文件（可为信封数组或信封流文件），维护 SQLite
  状态并全量重算合并统计。

两端通过 ``--input`` / ``--output`` 与状态参数串起来；中央的输入永远是摘要，
逐人 CSV/JSON 不会被中央接口接受。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from registry_privacy_gateway.statistics.contract import (
    SummaryContractError,
    reason_code,
)
from registry_privacy_gateway.statistics.merge import MergeCentral
from registry_privacy_gateway.statistics.replay import session_from_fixture
from registry_privacy_gateway.statistics.summarize import (
    build_summary_envelopes,
    build_withdraw_envelopes,
)


def _load_json(path: str | Path) -> Any:
    # 约定只读取显式传入的路径；不依赖当前工作目录的任何模块。
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str | Path, value: Any) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _load_session(contract_path: str | Path):
    contract_doc = _load_json(contract_path)
    if "analysis" in contract_doc and "partition_contract" in contract_doc:
        return session_from_fixture(contract_doc)
    if "analysis" in contract_doc:
        return session_from_fixture(
            {"analysis": contract_doc["analysis"],
             "partition_contract": contract_doc.get("partition_contract", {})})
    raise SummaryContractError("session-contract-invalid",
                               "会话契约文件必须包含 analysis 与 partition_contract")


def _summarize(args: argparse.Namespace) -> int:
    session = _load_session(args.contract)
    payload = _load_json(args.input)
    if payload.get("withdraw"):
        partitions = payload.get("partitions")
        if not isinstance(partitions, list) or not partitions:
            raise SummaryContractError(
                "envelope-malformed",
                "撤销输入必须通过 partitions 声明要撤销的分区列表")
        envelopes = build_withdraw_envelopes(
            str(payload["site"]), str(payload["batch"]), int(payload["revision"]),
            partitions, session)
    else:
        envelopes = build_summary_envelopes(payload, session)
    _write_json(args.output, {"envelopes": envelopes, "count": len(envelopes)})
    print(json.dumps({"action": "summarize", "envelopes": len(envelopes),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


def _load_envelopes(doc: Any) -> list[Mapping[str, Any]]:
    if isinstance(doc, dict) and isinstance(doc.get("envelopes"), list):
        return doc["envelopes"]
    if isinstance(doc, list):
        return doc
    raise SummaryContractError("envelope-malformed",
                               "中央输入必须是摘要信封或信封数组，不接受逐人明细")


def _merge(args: argparse.Namespace) -> int:
    session = _load_session(args.contract)
    envelopes = _load_envelopes(_load_json(args.input))
    central = MergeCentral(args.db, session)
    receipts = []
    for envelope in envelopes:
        receipt = central.ingest(envelope)
        receipts.append({
            "accepted": receipt.accepted, "action": receipt.action,
            "batch": receipt.batch, "partition": receipt.partition,
            "revision": receipt.revision, "digest": receipt.digest,
            "reason_code": receipt.reason_code, "message": receipt.message,
            "detail": receipt.detail,
        })
    result = central.recompute()
    output = {"receipts": receipts, "merge": central.to_dict(result)}
    _write_json(args.output, output)
    rejected = [receipt for receipt in receipts if not receipt["accepted"]]
    print(json.dumps({"action": "merge", "ingested": len(receipts) - len(rejected),
                      "rejected": len(rejected),
                      "rejection_codes": [r["reason_code"] for r in rejected],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0 if not rejected or args.allow_rejections else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="registry_privacy_gateway.statistics",
        description="只交换摘要的联合统计：站点 summarize，中央 merge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summarize_parser = subparsers.add_parser(
        "summarize", help="站点侧：把站内授权明细压缩成摘要信封")
    summarize_parser.add_argument("--input", required=True,
                                  help="站内传输 JSON（含 rows，仅限站点本地）")
    summarize_parser.add_argument("--output", required=True,
                                  help="摘要信封输出路径（可交给中央）")
    summarize_parser.add_argument("--contract", required=True,
                                  help="会话契约（analysis + partition_contract）")
    summarize_parser.set_defaults(handler=_summarize)

    merge_parser = subparsers.add_parser(
        "merge", help="中央侧：接收摘要信封并全量重算合并统计")
    merge_parser.add_argument("--input", required=True,
                              help="摘要信封 JSON（信封数组或 {envelopes: [...]}）")
    merge_parser.add_argument("--output", required=True,
                              help="合并统计与接收回执输出路径")
    merge_parser.add_argument("--db", required=True,
                              help="中央 SQLite 状态文件，保留修订/撤销状态")
    merge_parser.add_argument("--contract", required=True,
                              help="会话契约（analysis + partition_contract）")
    merge_parser.add_argument("--allow-rejections", action="store_true",
                              help="存在被拒绝信封时仍以 0 退出（默认退出码 2）")
    merge_parser.set_defaults(handler=_merge)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except SummaryContractError as exc:
        print(json.dumps({"accepted": False, "reason_code": reason_code(exc),
                          "message": str(exc), "detail": exc.detail},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"accepted": False, "reason_code": "io-error",
                          "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
