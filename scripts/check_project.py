from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "project.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    commands = [
        [sys.executable, "-m", "unittest", "discover", "-s", config["tests"], "-v"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests/analysis_context", "-v"],
        [sys.executable, "-m", "unittest", "discover", "-s", config["statistics_tests"], "-v"],
        [sys.executable, "-m", "compileall", "-q", config["package"], "tests", "scripts"],
    ]
    for command in commands:
        print("执行：", " ".join(command), flush=True)
        subprocess.run(command, cwd=root, check=True, timeout=65)
    with tempfile.TemporaryDirectory(prefix="project-smoke-") as directory:
        command = [
            sys.executable, "-m", config["entrypoint"],
            *config["fixture_prefix"], "fixtures/demo.json",
            config["database_flag"], str(Path(directory) / "state.sqlite3"),
        ]
        print("执行：", " ".join(command), flush=True)
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True, timeout=15)
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or not response:
            raise RuntimeError("服务没有输出有效结果")
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
        statistics_smoke = _statistics_smoke(root, Path(directory), config)
        print(json.dumps(statistics_smoke, ensure_ascii=False, sort_keys=True), flush=True)
    print(json.dumps({"结果": "通过", "耗时秒": round(time.monotonic() - started, 3)}, ensure_ascii=False))
    return 0


def _statistics_smoke(root: Path, directory: Path, config: dict) -> dict:
    """在独立临时目录里串起 summarize -> merge，验证只交换摘要的联合统计入口。"""
    fixture = json.loads((root / config["analysis_fixture"]).read_text(encoding="utf-8"))
    contract = directory / "contract.json"
    contract.write_text(json.dumps(
        {"analysis": fixture["analysis"],
         "partition_contract": fixture["partition_contract"]}, ensure_ascii=False),
        encoding="utf-8")
    site_a = fixture["sites"][0]
    site_input = directory / "site-a.json"
    site_input.write_text(json.dumps(
        {"site": site_a["site"], "batch": site_a["batch"],
         "revision": site_a["revision"], "rows": site_a["rows"]}, ensure_ascii=False),
        encoding="utf-8")
    envelope_path = directory / "site-a.envelopes.json"
    summarize = [sys.executable, "-m", config["statistics_entrypoint"], "summarize",
                 "--input", str(site_input), "--output", str(envelope_path),
                 "--contract", str(contract)]
    print("执行：", " ".join(summarize), flush=True)
    subprocess.run(summarize, cwd=root, check=True, capture_output=True, text=True, timeout=15)
    envelopes = json.loads(envelope_path.read_text(encoding="utf-8"))
    if "envelopes" not in envelopes or not envelopes["envelopes"]:
        raise RuntimeError("summarize 没有产出摘要信封")
    # private_note 只允许作为 removed_fields 里的字段名痕迹出现一次都不能有私有值。
    serialized = json.dumps(envelopes, ensure_ascii=False)
    if "仅站内" in serialized:
        raise RuntimeError("摘要信封里出现了站内私有字段的值")
    merge_output = directory / "merge.json"
    merge_db = directory / "statistics.sqlite3"
    merge = [sys.executable, "-m", config["statistics_entrypoint"], "merge",
             "--input", str(envelope_path), "--output", str(merge_output),
             config["database_flag"], str(merge_db), "--contract", str(contract)]
    print("执行：", " ".join(merge), flush=True)
    subprocess.run(merge, cwd=root, check=True, capture_output=True, text=True, timeout=15)
    merged = json.loads(merge_output.read_text(encoding="utf-8"))
    if not merged["merge"]["statistics"]["scopes"]:
        raise RuntimeError("merge 没有产出统计 scope")
    return {"统计冒烟": "通过",
            "信封数": len(envelopes["envelopes"]),
            "scope数": len(merged["merge"]["statistics"]["scopes"])}


if __name__ == "__main__":
    raise SystemExit(main())
