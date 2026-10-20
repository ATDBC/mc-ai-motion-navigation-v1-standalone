"""E124：行尾统一之后，证据里记录的文件哈希还对不对。

只读检出目录，不调用也不修改项目代码。扫描 evidence/ 下所有 JSON 文件，找出“仓库内相对路径 → 64 位
十六进制 sha256”的记录（对象里 path/file 字段配 sha256 字段，或以路径为键、以哈希为值），再用检出里
该路径当前的字节重新计算 sha256。分别统计：记录数、路径存在且哈希相同、路径存在但哈希不同、路径不存在。
同一脚本在 3889d10 与 dfaa0b4f 两个检出上运行，比较行尾统一前后的差别；另报告不同的记录里，
有多少只要把文件换回 CRLF 就能对上（说明只是行尾被改写）。
用法：python e124_evidence_hash_references.py <检出目录>
"""
import hashlib
import json
import re
import sys
from pathlib import Path

HEX = re.compile(r"^[0-9a-f]{64}$")


def records(value):
    if isinstance(value, dict):
        path = next((value[k] for k in ("path", "file", "relative_path", "source") if isinstance(value.get(k), str)), None)
        digest = next((value[k] for k in ("sha256", "sha256_hex", "digest") if isinstance(value.get(k), str)), None)
        if path and digest and HEX.match(digest):
            yield path, digest
        for key, item in value.items():
            if isinstance(item, str) and HEX.match(item) and ("/" in key or key.endswith((".py", ".json", ".md", ".java", ".txt", ".jsonl"))):
                yield key, item
            else:
                yield from records(item)
    elif isinstance(value, list):
        for item in value:
            yield from records(item)


def main():
    root = Path(sys.argv[1]).resolve()
    stats = {"evidence_json_files": 0, "records": 0, "match": 0, "mismatch": 0, "missing": 0,
             "mismatch_fixed_by_crlf": 0}
    examples = []
    for path in sorted((root / "evidence").rglob("*.json")):
        stats["evidence_json_files"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        for relative, digest in records(data):
            relative = relative.replace("\\", "/").lstrip("./")
            target = root / relative
            if not target.is_file():
                if "/" in relative:
                    stats["records"] += 1
                    stats["missing"] += 1
                continue
            stats["records"] += 1
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() == digest:
                stats["match"] += 1
                continue
            stats["mismatch"] += 1
            crlf = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if hashlib.sha256(crlf).hexdigest() == digest:
                stats["mismatch_fixed_by_crlf"] += 1
            if len(examples) < 8:
                examples.append({"recorded_in": str(path.relative_to(root)), "path": relative})
    print(json.dumps({"root_head": (root / ".git").exists() or True, **stats, "examples": examples},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
