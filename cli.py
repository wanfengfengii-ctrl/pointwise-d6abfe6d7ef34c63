#!/usr/bin/env python3
"""history.Index 的命令行工具：准备数据、核账核验、更正演示。

配合 .pointwise-runtime.json（版本 1）使用：

  prepare           生成确定性数据集（10 万修订、10 万查询、抽样查询）及完整性元信息
  prepare-expected  用独立重放实现计算抽样查询的期望结果（对账基准）
  verify            核账核验：给定样例 / 边界 / 随机小例重放对照 / 不变量巡检 /
                    抽样对账 / 10 万修订 + 10 万查询的耗时与峰值内存实测
  demo              更正演示：覆盖、撤销（None）、改值、0 值与按历史版本核算
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import random
import resource
import sys
import time
import tracemalloc
from hashlib import sha256

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history import Index

SEED = 20261115
N_REVISIONS = 100_000
N_QUERIES = 100_000
N_SAMPLE = 8
SAMPLE_MAX_COMMIT = 1200   # 抽样查询的 commit 上限，保证独立重放代价可控


# ============================================================ 独立重放（对账基准）
# 与线段树完全无关的实现：把修订依次裁剪进查询窗口，维护互不重叠的区间列表，
# 后到的修订切断并覆盖先到者，None 只切断不补回。仅用于小规模对账。

def replay_query(revisions, commit, start, end):
    """独立重放：返回 {"covered":..., "total":...}，复杂度 O(适用修订数 x 段数)。"""
    segs = []  # 窗口内互不重叠的 (s, e, value)
    for c, s, e, v in revisions:
        if c > commit:
            break
        s2 = s if s > start else start
        e2 = e if e < end else end
        if s2 >= e2:
            continue
        out = []
        for a, b, val in segs:
            if b <= s2 or a >= e2:
                out.append((a, b, val))
            else:
                if a < s2:
                    out.append((a, s2, val))
                if e2 < b:
                    out.append((e2, b, val))
        if v is not None:
            out.append((s2, e2, v))
        segs = out
    covered = 0
    total = 0
    for a, b, val in segs:
        covered += b - a
        total += val * (b - a)
    return {"covered": covered, "total": total}


# ============================================================ 不变量巡检（仅小规模）

def check_invariants(idx):
    """沿每个版本根递归核对不变量 I1-I3（I4/I5 由实现结构保证）。"""
    xs, m = idx._xs, idx._m
    L, R, COV, TOT, TAG = idx._L, idx._R, idx._cov, idx._tot, idx._tag
    values = idx._values

    def expect(node, a, b, seen):
        if node == 0:
            return (0, 0)
        if node in seen:
            return seen[node]
        t = TAG[node]
        if t:  # I2：懒标记节点按整段物化
            v = values[t - 1]
            ln = xs[b] - xs[a]
            exp = (0, 0) if v is None else (ln, v * ln)
        else:  # I3：聚合等于孩子之和
            mid = (a + b) // 2
            e1 = expect(L[node], a, mid, seen)
            e2 = expect(R[node], mid, b, seen)
            exp = (e1[0] + e2[0], e1[1] + e2[1])
        if COV[node] != exp[0] or TOT[node] != exp[1]:
            raise AssertionError(
                f"不变量被破坏: node={node} 段[{a},{b}) "
                f"cov={COV[node]} 应为 {exp[0]}, tot={TOT[node]} 应为 {exp[1]}")
        seen[node] = exp
        return exp

    for root in idx._roots:
        expect(root, 0, m, {})


# ============================================================ 数据集

def gen_dataset(seed, n_rev, n_q):
    rng = random.Random(seed)
    revs = []
    for i in range(1, n_rev + 1):
        s = rng.randrange(0, 1_000_000)
        e = s + rng.randrange(1, 2001)
        r = rng.random()
        if r < 0.20:
            v = None          # 撤销覆盖
        elif r < 0.30:
            v = 0             # 0 仍计有效时长
        else:
            v = rng.randrange(-1000, 1001)
        revs.append((i, s, e, v))
    qs = []
    for _ in range(n_q):
        c = rng.randrange(0, n_rev + 1)
        s = rng.randrange(-1000, 1_001_000)
        qs.append((c, s, s + rng.randrange(1, 5001)))
    sample = []
    for _ in range(N_SAMPLE):
        c = rng.randrange(1, SAMPLE_MAX_COMMIT + 1)
        s = rng.randrange(0, 1_000_000)
        sample.append((c, s, s + rng.randrange(1, 301)))
    return revs, qs, sample


def _sha256(path):
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_prepare(args):
    out = args.dir
    os.makedirs(out, exist_ok=True)
    revs, qs, sample = gen_dataset(SEED, args.revisions, args.queries)
    paths = {}
    for name, obj in (("revisions", revs), ("queries", qs), ("sample_queries", sample)):
        p = os.path.join(out, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        paths[name] = p
    meta = {"seed": SEED, "revisions": len(revs), "queries": len(qs),
            "sample_queries": len(sample),
            "sha256": {name: _sha256(p) for name, p in paths.items()}}
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"[prepare] 已生成 {len(revs)} 条修订、{len(qs)} 条查询、"
          f"{len(sample)} 条抽样查询 -> {out}/（含 meta.json 完整性摘要）")


def load_dataset(d):
    with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    data = {}
    for name in ("revisions", "queries", "sample_queries"):
        p = os.path.join(d, name + ".json")
        if _sha256(p) != meta["sha256"][name]:
            raise RuntimeError(f"{p} 摘要与 meta.json 不符，请重新运行 prepare")
        with open(p, encoding="utf-8") as f:
            data[name] = json.load(f)
    return data["revisions"], data["queries"], data["sample_queries"]


def cmd_prepare_expected(args):
    revs, _, sample = load_dataset(args.dir)
    answers = [replay_query(revs, c, s, e) for c, s, e in sample]
    out = os.path.join(args.dir, "expected_sample.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"queries": sample, "answers": answers}, f, ensure_ascii=False, indent=2)
    print(f"[prepare-expected] 已用独立重放计算 {len(answers)} 条抽样查询的期望结果 -> {out}")


# ============================================================ 核账核验

def _random_case(rng, n, coord):
    revs = []
    for i in range(1, n + 1):
        s = rng.randrange(-5, coord)
        e = s + rng.randrange(1, coord)
        r = rng.random()
        v = None if r < 0.25 else (0 if r < 0.40 else rng.randrange(-20, 21))
        revs.append((i, s, e, v))
    return revs


def cmd_verify(args):
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # 1. 题目给定样例
    idx = Index([(1, 0, 10, 2), (2, 3, 7, None)])
    check("给定样例 query(1,0,10)",
          idx.query(1, 0, 10) == {"covered": 10, "total": 20})
    check("给定样例 query(2,0,10)",
          idx.query(2, 0, 10) == {"covered": 6, "total": 12})

    # 2. 边界情形
    edge_ok = True
    edge_ok &= Index([]).query(5, 0, 10) == {"covered": 0, "total": 0}          # 空索引
    one = Index([(3, 10, 20, 4)])
    edge_ok &= one.query(2, 0, 100) == {"covered": 0, "total": 0}               # commit 早于所有修订
    edge_ok &= one.query(3, 0, 10) == {"covered": 0, "total": 0}                # 窗口在坐标范围左侧
    edge_ok &= one.query(3, 20, 30) == {"covered": 0, "total": 0}               # 窗口在右侧
    edge_ok &= one.query(10**9, 10, 20) == {"covered": 10, "total": 40}         # commit 超过全部
    edge_ok &= one.query(3, -50, 15) == {"covered": 5, "total": 20}             # 窗口越界裁剪
    nones = Index([(1, 0, 10, None), (2, 5, 15, None)])
    edge_ok &= nones.query(2, 0, 20) == {"covered": 0, "total": 0}              # 仅 None
    zero = Index([(1, 0, 10, 0)])
    edge_ok &= zero.query(1, 0, 10) == {"covered": 10, "total": 0}              # 0 计时长不计值
    neg = Index([(1, -10, -2, -3), (2, -6, 4, 5)])
    edge_ok &= neg.query(2, -10, 4) == {"covered": 14, "total": -3 * 4 + 5 * 10}  # 负坐标负值
    edge_ok &= neg.query(1, -10, 4) == {"covered": 8, "total": -24}             # 历史版本不受 #2 影响
    check("边界情形（空索引/版本越界/窗口裁剪/None/0/负数/历史版本）", edge_ok)

    # 3. 随机小例：Index vs 独立重放 + 不变量巡检
    rng = random.Random(12345)
    n_q = 0
    try:
        for _ in range(300):
            revs = _random_case(rng, rng.randrange(0, 61), rng.randrange(2, 80))
            idx = Index(revs)
            check_invariants(idx)
            for _ in range(4):
                c = rng.randrange(0, len(revs) + 3)
                s = rng.randrange(-8, 88)
                q = (c, s, s + rng.randrange(1, 160))
                got, want = idx.query(*q), replay_query(revs, *q)
                n_q += 1
                if got != want:
                    raise AssertionError(f"q={q} got={got} want={want} revs={revs}")
        check(f"随机小例重放对照（300 例 x 4 查询 = {n_q} 次）+ 不变量巡检", True)
    except AssertionError as ex:
        check("随机小例重放对照 + 不变量巡检", False, str(ex)[:200])

    # 4. 中规模对照
    try:
        rng2 = random.Random(777)
        revs = _random_case(rng2, 350, 250)
        idx = Index(revs)
        check_invariants(idx)
        for _ in range(100):
            c = rng2.randrange(0, 360)
            s = rng2.randrange(-10, 260)
            q = (c, s, s + rng2.randrange(1, 500))
            assert idx.query(*q) == replay_query(revs, *q), f"q={q}"
        check("中规模重放对照（350 修订 x 100 查询）", True)
    except AssertionError as ex:
        check("中规模重放对照", False, str(ex)[:200])

    # 5. 大数据实测 + 抽样对账
    if args.skip_perf:
        print("[SKIP] 10 万修订 + 10 万查询实测（--skip-perf）")
    else:
        try:
            revs, qs, sample = load_dataset(args.dir)
        except (FileNotFoundError, RuntimeError) as ex:
            check("数据集完整性", False, f"{ex}；请先运行 prepare")
            revs = None
        if revs is not None:
            check("数据集完整性（sha256 对账）", True)
            n, nq = len(revs), len(qs)
            gc.collect()
            t0 = time.perf_counter()
            idx = Index(revs)
            t1 = time.perf_counter()
            acc_c = acc_t = 0
            for c, s, e in qs:
                r = idx.query(c, s, e)
                acc_c += r["covered"]
                acc_t += r["total"]
            t2 = time.perf_counter()
            del idx
            gc.collect()
            tracemalloc.start()
            idx2 = Index(revs)          # 第二次构建专用于内存测量
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            nodes = idx2.node_count
            m = len(idx2._xs) - 1
            print(f"    构建: {n} 修订, {t1 - t0:.3f}s（{(t1 - t0) / n * 1e6:.1f} µs/修订）")
            print(f"    节点: {nodes} 个（叶数 m={m}，同阶参考 n·log2(m+1)≈"
                  f"{int(n * (m + 1).bit_length())}）")
            print(f"    查询: {nq} 条, {t2 - t1:.3f}s（{(t2 - t1) / nq * 1e6:.2f} µs/条）"
                  f"（covered 合计 {acc_c}，total 合计 {acc_t}）")
            print(f"    峰值内存: tracemalloc {peak / 2**20:.1f} MiB；"
                  f"进程 ru_maxrss {rss_mib:.1f} MiB")
            # 抽样对账：Index(10 万全量) vs 独立重放期望
            try:
                with open(os.path.join(args.dir, "expected_sample.json"), encoding="utf-8") as f:
                    expected = json.load(f)
                if expected["queries"] != [list(q) for q in sample]:
                    raise RuntimeError("expected_sample.json 与抽样查询不一致，请重跑 prepare-expected")
                bad = [q for q, exp in zip(sample, expected["answers"])
                       if idx2.query(*q) != exp]
                check(f"抽样对账（{len(sample)} 条，Index vs 独立重放期望）", not bad,
                      f"不符: {bad[:3]}")
            except (FileNotFoundError, RuntimeError) as ex:
                check("抽样对账", False, f"{ex}；请先运行 prepare-expected")

    print()
    if failures:
        print(f"核验未通过：{len(failures)} 项 -> {failures}")
        return 1
    print("核验全部通过。")
    return 0


# ============================================================ 更正演示

def cmd_demo(_args):
    revs = [(1, 0, 10, 2),      # 初始登记
            (2, 3, 7, None),    # 更正：撤销 [3,7) 的覆盖
            (3, 5, 9, 7),       # 更正：[5,9) 改值为 7（晚于撤销，以它为准）
            (4, 0, 2, 0)]       # 更正：[0,2) 改值为 0（0 仍计有效时长）
    idx = Index(revs)
    print("提交历史（数仓加工员的更正流水）：")
    print("  #1 [0,10) := 2      初始登记")
    print("  #2 [3,7)  := None   更正：撤销该段覆盖")
    print("  #3 [5,9)  := 7      更正：改值为 7（提交号大，覆盖 #2 的撤销）")
    print("  #4 [0,2)  := 0      更正：改值为 0（0 仍计有效时长）")
    print()
    print("按历史版本核算 query(commit, 0, 10)：")
    notes = ["尚无修订", "初始登记", "[3,7) 被撤销", "[5,9) 改值 7", "[0,2) 改值 0"]
    for c in range(5):
        r = idx.query(c, 0, 10)
        assert r == replay_query(revs, c, 0, 10)  # 演示结果同时经独立重放核对
        print(f"  commit={c}  covered={r['covered']:>2}  total={r['total']:>3}   {notes[c]}")
    print()
    print("同一窗口、不同历史版本（窗口汇总按版本各算各的账）：")
    for q in [(4, 4, 6), (1, 4, 6), (2, 0, 4), (3, 0, 4)]:
        r = idx.query(*q)
        assert r == replay_query(revs, *q)
        print(f"  query{q} -> covered={r['covered']}  total={r['total']}")
    print()
    print("演示通过：覆盖、撤销（None）、改值、0 值与历史版本核算均与独立重放一致。")
    return 0


# ============================================================ 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="生成确定性数据集")
    p.add_argument("--dir", default="artifacts")
    p.add_argument("--revisions", type=int, default=N_REVISIONS)
    p.add_argument("--queries", type=int, default=N_QUERIES)
    p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("prepare-expected", help="独立重放计算抽样期望")
    p.add_argument("--dir", default="artifacts")
    p.set_defaults(fn=cmd_prepare_expected)
    p = sub.add_parser("verify", help="核账核验")
    p.add_argument("--dir", default="artifacts")
    p.add_argument("--skip-perf", action="store_true")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("demo", help="更正演示")
    p.set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
