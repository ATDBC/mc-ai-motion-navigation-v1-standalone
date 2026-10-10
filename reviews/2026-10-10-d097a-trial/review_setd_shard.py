"""审查方用：把 G8 的集合 D（M1 接受的全部验证候选）分片并行跑等价比较。

只调用 m2/run_m2.py 的 run_set 与 m2 的 set_d 读取函数，比较逻辑不变；每片单独写结果文件。
运行：PYTHONPATH=<项目根>:<本目录>:<本目录>/m2 python review_setd_shard.py <shard> <shards>
"""
import json
import sys
from pathlib import Path

import run_m2 as R

shard, shards = int(sys.argv[1]), int(sys.argv[2])
cands = R.K.set_d(R.K.M1_ACCEPTED)[shard::shards]
result = R.run_set(cands, f"D{shard}")
out = Path(__file__).parent / "setd_shards" / f"setd_shard_{shard}_of_{shards}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"shard": shard, "shards": shards, "platform": R.platform_info(),
                           "summary": result["summary"], "seconds": result["seconds"],
                           "mismatches": result["mismatches"]}, indent=1, default=str), encoding="utf-8")
print(json.dumps(result["summary"], indent=1))
