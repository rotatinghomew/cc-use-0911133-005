"""摘要交换契约。

信封是站点与中央之间唯一允许交换的载荷：它只包含加权汇总数、契约声明和
行级隔离记录，不包含逐人原始行。中央在接收任何信封前都要在这里完成字段版本、
群体、口径、重叠分区和权限的检查；无法满足合作约定时返回带明确原因码的
:class:`SummaryContractError`，而不是默默产出一个看起来合理的结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from registry_privacy_gateway.runtime import DomainError, fingerprint

SCHEMA_VERSION = 1
ENVELOPE_TYPE = "statistics-summary-v1"

# 信封骨架允许出现的键。任何站点明细字段（包括 private_note、observation_id
# 的原值等）都不能进入信封；隔离记录只保留观测指纹而不是行内容。
ENVELOPE_KEYS = {
    "envelope_type",
    "schema_version",
    "digest",
    "site",
    "batch",
    "partition",
    "revision",
    "withdrawn",
    "contract",
    "scopes",
    "excluded",
}
CONTRACT_KEYS = {
    "purpose",
    "observation_unit",
    "weight_kind",
    "field_version",
    "fields",
    "group_by",
    "allowed_fields",
    "partition_contract_version",
    "partition",
    "authoritative_site",
}

# 结构字段是信封的信封信息，不属于可分析的数据列。
STRUCTURAL_FIELDS = {"observation_id", "partition", "purpose", "weight"}


class SummaryContractError(DomainError):
    """摘要信封违反交换契约。``code`` 是稳定的机器可读原因码。"""

    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


def reason_code(error: BaseException) -> str:
    """从异常中取出原因码，未知异常统一归为 internal-error。"""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "internal-error"


def _require(value: Any, predicate: bool, code: str, message: str,
             detail: Mapping[str, Any] | None = None) -> None:
    if not predicate:
        raise SummaryContractError(code, message, detail)


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    """对除 digest 外的完整信封内容取指纹，用于重发识别与防篡改。"""
    body = {key: value for key, value in envelope.items() if key != "digest"}
    return fingerprint(body)


def _validate_partition_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    partition_contract = contract.get("partition_contract")
    _require(
        contract,
        isinstance(partition_contract, Mapping),
        "partition-contract-missing",
        "摘要必须声明经各方协商的互斥分区契约，无法解释的重叠不能精确去重",
    )
    disjoint = partition_contract.get("disjoint_keys")
    authority = partition_contract.get("authoritative_site")
    _require(
        contract,
        isinstance(disjoint, list) and disjoint and all(isinstance(k, str) and k for k in disjoint),
        "partition-contract-invalid",
        "分区契约的 disjoint_keys 必须是非空字符串列表",
    )
    _require(
        contract,
        isinstance(authority, Mapping) and set(disjoint) <= set(authority),
        "partition-contract-invalid",
        "每个互斥分区都必须指定 authoritative_site",
    )
    if len(set(disjoint)) != len(disjoint):
        raise SummaryContractError(
            "partition-contract-invalid", "disjoint_keys 中存在重复分区",
            {"disjoint_keys": disjoint},
        )
    return {"version": partition_contract.get("version"), "disjoint": list(disjoint),
            "authority": {key: authority[key] for key in disjoint}}


@dataclass(frozen=True, slots=True)
class SessionContract:
    """中央一次联合分析所锚定的合作约定。"""

    purpose: str
    observation_unit: str
    weight_kind: str
    field_version: str
    fields: tuple[str, ...]
    group_by: tuple[str, ...]
    allowed_fields: frozenset[str]
    partition_version: str | None
    disjoint_partitions: frozenset[str]
    authority: Mapping[str, str]

    @classmethod
    def from_analysis(cls, analysis: Mapping[str, Any],
                      partition_contract: Mapping[str, Any]) -> "SessionContract":
        for key in ("purpose", "observation_unit", "weight_kind", "field_version", "fields"):
            _require(analysis, key in analysis, "session-contract-invalid",
                     f"analysis 缺少必填声明 {key}")
        fields = analysis["fields"]
        group_by = tuple(analysis.get("group_by", []))
        allowed = frozenset(analysis.get("allowed_fields", []) + list(group_by))
        _require(analysis, isinstance(fields, list) and len(fields) >= 1,
                 "session-contract-invalid", "fields 至少需要一个分析列")
        _require(analysis, all(isinstance(f, str) and f for f in fields),
                 "session-contract-invalid", "fields 必须是非空字符串")
        _require(analysis, len(set(fields)) == len(fields),
                 "session-contract-invalid", "fields 中存在重复列")
        _require(analysis, set(fields).issubset(allowed),
                 "allowed-fields-mismatch", "分析列必须全部出现在 allowed_fields 中",
                 {"fields": list(fields), "allowed_fields": sorted(allowed)})
        _require(analysis, all(column not in STRUCTURAL_FIELDS for column in fields),
                 "allowed-fields-mismatch", "结构字段不能作为分析列")
        _require(analysis, analysis.get("weight_kind") == "frequency",
                 "weight-kind-mismatch", "当前契约仅支持频数权重 weight_kind=frequency")
        disjoint = partition_contract.get("disjoint_keys")
        authority = partition_contract.get("authoritative_site")
        _require(analysis, isinstance(disjoint, list) and isinstance(authority, Mapping),
                 "partition-contract-invalid", "分区契约必须包含 disjoint_keys 与 authoritative_site")
        return cls(
            purpose=str(analysis["purpose"]),
            observation_unit=str(analysis["observation_unit"]),
            weight_kind=str(analysis["weight_kind"]),
            field_version=str(analysis["field_version"]),
            fields=tuple(fields),
            group_by=group_by,
            allowed_fields=allowed,
            partition_version=partition_contract.get("version"),
            disjoint_partitions=frozenset(disjoint),
            authority={key: authority[key] for key in disjoint},
        )

    def envelope_contract(self, partition: str, site: str) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "observation_unit": self.observation_unit,
            "weight_kind": self.weight_kind,
            "field_version": self.field_version,
            "fields": list(self.fields),
            "group_by": list(self.group_by),
            "allowed_fields": sorted(self.allowed_fields),
            "partition_contract_version": self.partition_version,
            "partition": partition,
            "authoritative_site": self.authority[partition],
        }


def validate_envelope_shape(envelope: Mapping[str, Any]) -> None:
    """检查信封的物理结构，防止原始行或未知字段混进交换载荷。"""
    _require(envelope, isinstance(envelope, Mapping), "envelope-malformed", "信封必须是 JSON 对象")
    extra = set(envelope) - ENVELOPE_KEYS
    _require(envelope, not extra, "envelope-field-not-allowed",
             "信封包含契约之外的键，原始行或私有字段不允许进入交换包",
             {"unexpected_keys": sorted(extra)})
    _require(envelope, envelope.get("envelope_type") == ENVELOPE_TYPE,
             "envelope-type-mismatch", "envelope_type 不被支持",
             {"envelope_type": envelope.get("envelope_type")})
    _require(envelope, envelope.get("schema_version") == SCHEMA_VERSION,
             "schema-version-mismatch", "摘要 schema 版本不受支持",
             {"schema_version": envelope.get("schema_version"), "expected": SCHEMA_VERSION})
    for key in ("site", "batch", "partition", "revision"):
        _require(envelope, isinstance(envelope.get(key), str) if key != "revision"
                 else isinstance(envelope.get("revision"), int),
                 "envelope-malformed", f"信封字段 {key} 类型不正确")
    _require(envelope, isinstance(envelope.get("revision"), int) and envelope["revision"] >= 1,
             "revision-invalid", "revision 必须是不小于 1 的整数")
    _require(envelope, isinstance(envelope.get("withdrawn"), bool),
             "envelope-malformed", "withdrawn 必须是布尔值")
    contract = envelope.get("contract")
    _require(envelope, isinstance(contract, Mapping), "envelope-malformed", "contract 必须是对象")
    _require(envelope, not (set(contract) - CONTRACT_KEYS),
             "envelope-field-not-allowed", "contract 包含未知声明键",
             {"unexpected_keys": sorted(set(contract) - CONTRACT_KEYS)})
    if not envelope["withdrawn"]:
        _require(envelope, isinstance(envelope.get("scopes"), list) and envelope["scopes"],
                 "envelope-malformed", "有效摘要至少需要一个统计 scope（含总体 scope）")
    if "excluded" in envelope:
        _require(envelope, isinstance(envelope["excluded"], list),
                 "envelope-malformed", "excluded 必须是列表")
        for item in envelope["excluded"]:
            _require(envelope, isinstance(item, Mapping)
                     and isinstance(item.get("reason_code"), str)
                     and isinstance(item.get("observation_fingerprint"), str),
                     "envelope-malformed", "隔离记录只能包含原因码与观测指纹")
            _require(envelope, set(item) <= {"reason_code", "observation_fingerprint", "detail"},
                     "envelope-field-not-allowed",
                     "隔离记录不能携带原始行内容",
                     {"unexpected_keys": sorted(set(item) - {"reason_code", "observation_fingerprint", "detail"})})


def _same_list(left: Any, right: Any) -> bool:
    return isinstance(left, list) and isinstance(right, list) and left == right


def check_intake_compatibility(envelope: Mapping[str, Any], session: SessionContract,
                               active_partition_owners: Mapping[str, str]) -> None:
    """中央接收前的合作约定检查。

    ``active_partition_owners`` 给出当前已生效摘要中每个分区由哪个 batch 占用，
    用于发现没有分区契约支持、无法分离的重叠。
    """
    contract = envelope["contract"]
    site = envelope["site"]
    partition = envelope["partition"]
    batch = envelope["batch"]

    if contract.get("purpose") != session.purpose:
        raise SummaryContractError(
            "purpose-mismatch", "摘要用途与本次联合分析授权用途不符",
            {"envelope_purpose": contract.get("purpose"), "session_purpose": session.purpose})
    if contract.get("observation_unit") != session.observation_unit:
        raise SummaryContractError(
            "observation-unit-mismatch", "观测单位口径不一致，无法判断同一居民的重复贡献",
            {"envelope_unit": contract.get("observation_unit"),
             "session_unit": session.observation_unit})
    if contract.get("weight_kind") != session.weight_kind:
        raise SummaryContractError(
            "weight-kind-mismatch", "权重口径不一致，仅支持频数权重",
            {"envelope_weight_kind": contract.get("weight_kind")})
    if contract.get("field_version") != session.field_version:
        raise SummaryContractError(
            "field-version-mismatch", "字段版本/计量单位不一致，合并均值与相关系数会失真",
            {"envelope_field_version": contract.get("field_version"),
             "session_field_version": session.field_version})
    if not _same_list(contract.get("fields"), list(session.fields)):
        raise SummaryContractError(
            "field-set-mismatch", "分析列集合与会话契约不一致",
            {"envelope_fields": contract.get("fields"), "session_fields": list(session.fields)})
    if not _same_list(contract.get("group_by"), list(session.group_by)):
        raise SummaryContractError(
            "group-dimension-mismatch", "分群维度不一致，分群统计不能对齐",
            {"envelope_group_by": contract.get("group_by"),
             "session_group_by": list(session.group_by)})
    if contract.get("partition_contract_version") != session.partition_version:
        raise SummaryContractError(
            "partition-contract-version-mismatch", "分区契约版本不一致",
            {"envelope_version": contract.get("partition_contract_version"),
             "session_version": session.partition_version})
    if partition not in session.disjoint_partitions:
        raise SummaryContractError(
            "partition-not-contracted", f"分区 {partition} 不在互斥分区契约内，"
            "任意重叠的汇总数无法精确去重，请先协商互斥分区或补充契约",
            {"partition": partition, "contracted_partitions": sorted(session.disjoint_partitions)})
    owner = session.authority[partition]
    if contract.get("authoritative_site") != owner:
        raise SummaryContractError(
            "authority-declaration-mismatch", "信封声明的负责站与分区契约不一致",
            {"declared": contract.get("authoritative_site"), "contract_authority": owner})
    if site != owner:
        raise SummaryContractError(
            "site-not-authoritative", "只有分区负责站可以提交该分区的摘要",
            {"site": site, "partition": partition, "authoritative_site": owner})
    current_owner = active_partition_owners.get(partition)
    if current_owner is not None and current_owner != batch:
        raise SummaryContractError(
            "inseparable-overlap", f"分区 {partition} 已被 batch {current_owner} 占用，"
            f"batch {batch} 又提交了同一分区；缺少可分离的互斥声明，不能精确去重",
            {"partition": partition, "held_by": current_owner, "incoming": batch})
    if not envelope["withdrawn"]:
        _check_scope_contract(envelope, session)


def _check_scope_contract(envelope: Mapping[str, Any], session: SessionContract) -> None:
    pairs = [tuple(sorted((left, right))) for index, left in enumerate(session.fields)
             for right in session.fields[index + 1:]]
    for scope in envelope["scopes"]:
        # 同一列对在每个 scope（总体/各分群）里各声明一次，去重集合按 scope 重置。
        declared_pairs: set[tuple[str, str]] = set()
        _require(envelope, isinstance(scope, Mapping) and "group" in scope,
                 "envelope-malformed", "每个 scope 必须声明 group")
        group = scope["group"]
        _require(envelope, group is None or isinstance(group, Mapping),
                 "envelope-malformed", "group 必须是 null（总体）或分群键值对象")
        if isinstance(group, Mapping):
            _require(envelope, set(group) == set(session.group_by),
                     "group-dimension-mismatch", "分群键与 group_by 契约不一致",
                     {"group_keys": sorted(group), "expected": list(session.group_by)})
        marginals = scope.get("marginals", [])
        _require(envelope, isinstance(marginals, list), "envelope-malformed",
                 "marginals 必须是列表")
        seen_fields: set[str] = set()
        for marginal in marginals:
            _require(envelope, isinstance(marginal, Mapping), "envelope-malformed",
                     "marginal 必须是对象")
            field_name = marginal.get("field")
            _require(envelope, field_name in session.fields and field_name not in seen_fields,
                     "field-set-mismatch", "边际统计列缺失或重复",
                     {"field": field_name})
            seen_fields.add(field_name)
            _require(envelope, set(marginal) == {"field", "n", "sum", "sum_squares"},
                     "envelope-field-not-allowed", "marginal 只允许 n/sum/sum_squares 三个矩")
            _require(envelope, all(isinstance(marginal[key], (int, float))
                                   and marginal[key] >= 0 for key in ("n", "sum_squares")),
                     "envelope-malformed", "n 与 sum_squares 必须非负")
        _require(envelope, seen_fields == set(session.fields),
                 "field-set-mismatch", "每个 scope 必须覆盖全部分析列的边际矩",
                 {"missing": sorted(set(session.fields) - seen_fields)})
        pair_blocks = scope.get("pairs", [])
        _require(envelope, isinstance(pair_blocks, list), "envelope-malformed",
                 "pairs 必须是列表")
        for block in pair_blocks:
            _require(envelope, isinstance(block, Mapping), "envelope-malformed",
                     "pair 块必须是对象")
            pair = (block.get("left"), block.get("right"))
            ordered = tuple(sorted(pair)) if all(isinstance(x, str) for x in pair) else pair
            _require(envelope, ordered in pairs, "field-set-mismatch",
                     "交叉矩的列对不属于契约分析列", {"pair": list(pair)})
            _require(envelope, ordered not in declared_pairs, "field-set-mismatch",
                     "同一 scope 内列对的交叉矩重复声明", {"pair": list(ordered)})
            declared_pairs.add(ordered)
            allowed = {"left", "right", "pair_n", "sum_x", "sum_y", "sum_xy",
                       "sum_x2", "sum_y2"}
            _require(envelope, set(block) == allowed, "envelope-field-not-allowed",
                     "pair 块只允许共同观测集合上的一阶、交叉与平方矩",
                     {"unexpected_keys": sorted(set(block) - allowed)})
            _require(envelope, isinstance(block["pair_n"], (int, float))
                     and block["pair_n"] >= 0, "envelope-malformed",
                     "pair_n 必须非负")
            for squared_key in ("sum_x2", "sum_y2"):
                _require(envelope, isinstance(block[squared_key], (int, float))
                         and block[squared_key] >= 0, "envelope-malformed",
                         f"{squared_key} 必须非负")
    # 交叉矩覆盖必须在所有 scope 间保持完整且一致：只要会话存在多列，每个 scope
    # 都要声明全部列对的共同观测矩。空 pairs 不等于零协方差，而是“信息不足”，
    # 不能用边际矩冒充共同观测矩，更不能产出虚假的零相关。
    expected_pairs = set(pairs)
    if expected_pairs:
        for scope in envelope["scopes"]:
            present = {
                tuple(sorted((block["left"], block["right"])))
                for block in scope["pairs"]
            }
            if present != expected_pairs:
                raise SummaryContractError(
                    "cross-moment-coverage-gap",
                    "交叉矩覆盖不完整：共同观测信息不足，不能计算该相关性",
                    {"group": scope["group"],
                     "missing_pairs": [list(p) for p in sorted(expected_pairs - present)]})


def assert_no_raw_rows(envelope: Mapping[str, Any], forbidden_fields: Sequence[str]) -> None:
    """序列化前的最后一道防线：确保明细字段不会作为数据列进入信封。

    ``excluded[].detail.removed_fields`` 里出现被剥离字段名是预期的最小化痕迹，
    不属于泄漏；除此之外，任何 scope 矩块或顶层键都不允许携带原始列。
    """
    forbidden = set(forbidden_fields)

    def walk(value: Any, path: str) -> list[str]:
        hits: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in forbidden and not path.endswith("removed_fields"):
                    hits.append(f"{path}.{key}" if path else str(key))
                hits.extend(walk(item, f"{path}.{key}" if path else str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                hits.extend(walk(item, f"{path}[{index}]"))
        return hits

    leaked = sorted(set(walk(envelope, "")))
    if leaked:
        raise SummaryContractError(
            "raw-row-in-envelope", "原始字段被写进摘要包，违反只交换摘要的合作约定",
            {"leaked_paths": leaked})
