"""E124b：同一条哈希记录在行尾统一前后的变化，以及证据引用证据的那部分。

只读两个检出目录，不调用也不修改项目代码。记录的识别规则与 E124 相同（直接复用 e124 的 records）。
以“记录所在文件、路径、哈希”为键，取两个检出共有的记录，统计：
  - 统一前对上、统一后对不上（broken_by_commit），其中多少只要把目标换回 CRLF 就能对上；
  - 统一前对不上、统一后对上（fixed_by_commit）；
  - 目标本身也在 evidence/ 下的记录（历史证据引用历史证据）逐条列出统一前后状态。
代码文件哈希随代码修改而失配属于正常情况；需要关注的是只因行尾改写而失配的记录，尤其是证据引用证据。
用法：python e124b_evidence_reference_diff.py <统一前检出> <统一后检出>
"""
import collections
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e124_evidence_hash_references import records  # noqa: E402


def collect(root):
    found = {}
    for path in sorted((root / "evidence").rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        recorded_in = str(path.relative_to(root))
        for relative, digest in records(data):
            relative = relative.replace("\\", "/").lstrip("./")
            target = root / relative
            if target.is_file():
                raw = target.read_bytes()
                crlf = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                state = ("match" if hashlib.sha256(raw).hexdigest() == digest else
                         "crlf_would_match" if hashlib.sha256(crlf).hexdigest() == digest else "mismatch")
            elif "/" in relative:
                state = "missing"
            else:
                continue
            found[(recorded_in, relative, digest)] = state
    return found


def main():
    before_root, after_root = (Path(p).resolve() for p in sys.argv[1:3])
    before, after = collect(before_root), collect(after_root)
    common = sorted(set(before) & set(after))
    broken = [key for key in common if before[key] == "match" and after[key] != "match"]
    fixed = [key for key in common if before[key] != "match" and after[key] == "match"]
    evidence_refs = [key for key in common if key[1].startswith("evidence/")]
    report = {
        "common_records": len(common),
        "matched_before": sum(before[key] == "match" for key in common),
        "matched_after": sum(after[key] == "match" for key in common),
        "broken_by_commit": len(broken),
        "broken_by_commit_crlf_would_match": sum(after[key] == "crlf_would_match" for key in broken),
        "fixed_by_commit": len(fixed),
        "broken_target_top_dirs": collections.Counter(key[1].split("/")[0] for key in broken).most_common(),
        "broken_eol_only_target_top_dirs": collections.Counter(
            key[1].split("/")[0] for key in broken if after[key] == "crlf_would_match").most_common(),
        "evidence_to_evidence": {
            "records": len(evidence_refs),
            "matched_before": sum(before[key] == "match" for key in evidence_refs),
            "matched_after": sum(after[key] == "match" for key in evidence_refs),
            "changed": [{"recorded_in": key[0], "path": key[1], "before": before[key], "after": after[key]}
                        for key in evidence_refs if before[key] != after[key]],
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
