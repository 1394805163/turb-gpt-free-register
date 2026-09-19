# -*- coding: utf-8 -*-
"""CLI：从下游 chatgpt2api 只读同步账号状态（零凭据触碰）。逻辑见 core/downstream_status.py。

用法：
    python tools/sync_downstream_status.py --dry-run   # 只报告
    python tools/sync_downstream_status.py             # 写入本地库
"""
from __future__ import annotations

import argparse
import os
import sys

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WT)
sys.path.insert(0, WT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from core.downstream_status import sync_downstream_status

    result = sync_downstream_status(dry_run=args.dry_run)
    print(f"下游账号: {result['downstream_total']}")
    print(f"同步: 匹配 {result['matched']} / 本地缺档 {result['missed']} "
          f"{'(dry-run, 未写库)' if args.dry_run else '(已写库)'}")
    print("下游状态分布:", result["status_counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
