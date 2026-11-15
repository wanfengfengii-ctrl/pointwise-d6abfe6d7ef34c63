"""按历史版本核算有效期事实的窗口汇总。

接口
----
    idx = history.Index(revisions)      # revisions: [(commit, start, end, value), ...]
    idx.query(commit, start, end)       # -> {"covered": int, "total": int}

语义
----
只应用提交号 <= 查询 commit 的修订；同一时刻被多次覆盖时以提交号大者为准；
value 为 None 表示撤销该段覆盖；value 为 0 仍计有效时长。
返回 {"covered": 有效覆盖时长, "total": 值 x 有效时长之和}，无覆盖两者均为 0。

实现：坐标压缩 + 持久化（路径复制）懒标记线段树
----------------------------------------------
所有修订端点排序去重得坐标数组 X，叶 i 对应时间段 [X[i], X[i+1])，叶数 m <= 2n-1。
每个修订是一次"区间覆盖赋值"，产生一个新版本根；版本之间只路径复制、旧节点永不
改写，因此可在任意历史版本上 O(log m) 查询，无需重放历史。

不变量（对任意版本的任意节点，节点覆盖叶子区间 [a, b)，即时间段 [X[a], X[b])）
  I1 空节点 0 表示整段无覆盖：COVER = TOTAL = 0，TAG = 0，孩子为 0。
  I2 带懒标记（TAG = 修订序号+1）的节点，其整段值统一为该修订的 value
     （None 表示整段无覆盖）；COVER/TOTAL 已按整段长度物化，孩子不再具有语义。
  I3 无懒标记的节点满足 COVER = COVER[左] + COVER[右]，TOTAL = TOTAL[左] + TOTAL[右]。
  I4 更新只做路径复制：每次修订新增 O(log m) 个节点，历史版本节点保持不变。
  I5 查询只读：区间求和与点查自根向下，遇懒标记按整段折算后即停，不再下探。

复杂度（n = 修订数，m = 叶数 <= 2n - 1）
  构建：每次修订访问并复制 O(log m) 个节点，总时间 O((n+1) log(n+2))，
       总空间 O((n+1) log(n+2))。
  查询：二分定位版本 O(log n)；答案 = F(end) - F(start)，前缀函数 F(t) 由一次
       区间求和加一次点查组成，各 O(log m)；合计 O(log(n+2))。
  全程不逐时间单位展开、不重放历史。

存储说明：孩子下标与懒标记用 32 位数组；COVER/TOTAL 在坐标跨度与
|value|*跨度 均不超过 2**63-1 时用 64 位数组，否则自动退化为 Python 列表，
保证任意大整数下结果仍然精确。
"""

from array import array
from bisect import bisect_left, bisect_right

__all__ = ["Index"]

_I32 = "i" if array("i").itemsize >= 4 else "l"
_I64 = "q"
_INT64_LIMIT = 2**63 - 1


class Index:
    """有效期事实的多版本窗口汇总索引。不变量与复杂度见模块 docstring。"""

    __slots__ = ("_xs", "_m", "_commits", "_values", "_roots",
                 "_L", "_R", "_cov", "_tot", "_tag")

    def __init__(self, revisions):
        revs = [tuple(r) for r in revisions]
        commits = [r[0] for r in revs]
        values = [r[3] for r in revs]
        xs = sorted({r[1] for r in revs} | {r[2] for r in revs})
        m = (len(xs) - 1) if xs else 0
        span = (xs[-1] - xs[0]) if xs else 0
        vmax = 0
        for v in values:
            if v is not None and abs(v) > vmax:
                vmax = abs(v)
        # 任意节点的 COVER <= span，|TOTAL| <= vmax*span；据此选择紧凑存储
        small = span <= _INT64_LIMIT and vmax * span <= _INT64_LIMIT

        self._xs = xs
        self._m = m
        self._commits = commits
        self._values = values
        self._L = array(_I32, [0])   # 左孩子下标，0 为空节点
        self._R = array(_I32, [0])   # 右孩子下标
        self._tag = array(_I32, [0])  # 懒标记：修订序号+1，0 为无标记
        if small:
            self._cov = array(_I64, [0])
            self._tot = array(_I64, [0])
        else:
            self._cov = [0]
            self._tot = [0]

        roots = [0]  # 版本 0：空树
        root = 0
        bl = bisect_left
        for i, (_, s, e, _) in enumerate(revs):
            root = self._update(root, 0, m, bl(xs, s), bl(xs, e), i + 1)
            roots.append(root)
        self._roots = roots

    # ---------------------------------------------------------------- 内部

    @property
    def node_count(self):
        """已分配的线段树节点总数（含空节点 0）。"""
        return len(self._tag)

    def _node(self, left, right, cov, tot, tag):
        self._L.append(left)
        self._R.append(right)
        self._cov.append(cov)
        self._tot.append(tot)
        self._tag.append(tag)
        return len(self._tag) - 1

    def _tagged(self, a, b, ref):
        """新建整段 [a,b) 被修订 ref 覆盖赋值的节点（不变量 I2）。"""
        v = self._values[ref - 1]
        if v is None:
            return self._node(0, 0, 0, 0, ref)
        ln = self._xs[b] - self._xs[a]
        return self._node(0, 0, ln, v * ln, ref)

    def _update(self, node, a, b, l, r, ref):
        """区间覆盖赋值 [l,r) := 修订 ref 的值，返回新节点（路径复制，I4）。"""
        if r <= a or b <= l:
            return node
        if l <= a and b <= r:
            t = self._tag[node]
            if t and self._values[t - 1] == self._values[ref - 1]:
                return node  # 赋值同值，状态不变，直接复用
            return self._tagged(a, b, ref)
        mid = (a + b) >> 1
        t = self._tag[node]
        if t:
            # 下推懒标记：为孩子物化当前统一值（I2），再在其上继续更新
            left = self._tagged(a, mid, t)
            right = self._tagged(mid, b, t)
        else:
            left = self._L[node]
            right = self._R[node]
        nl = self._update(left, a, mid, l, r, ref)
        nr = self._update(right, mid, b, l, r, ref)
        return self._node(nl, nr, self._cov[nl] + self._cov[nr],
                          self._tot[nl] + self._tot[nr], 0)

    def _range_query(self, node, a, b, l, r):
        """求叶子区间 [l,r) 与节点段 [a,b) 交集的 (COVER, TOTAL)（只读，I5）。"""
        if node == 0 or r <= a or b <= l:
            return 0, 0
        if l <= a and b <= r:
            return self._cov[node], self._tot[node]
        t = self._tag[node]
        if t:
            v = self._values[t - 1]
            if v is None:
                return 0, 0
            lo = l if l > a else a
            hi = r if r < b else b
            ln = self._xs[hi] - self._xs[lo]
            return ln, v * ln
        mid = (a + b) >> 1
        c1, t1 = self._range_query(self._L[node], a, mid, l, r)
        c2, t2 = self._range_query(self._R[node], mid, b, l, r)
        return c1 + c2, t1 + t2

    def _point_value(self, node, a, b, i):
        """叶 i 处的 (是否覆盖, 值)：路径上最深层懒标记即最新赋值（I5）。"""
        while node:
            t = self._tag[node]
            if t:
                v = self._values[t - 1]
                return (v is not None), v
            if b - a <= 1:
                return False, None
            mid = (a + b) >> 1
            if i < mid:
                node = self._L[node]
                b = mid
            else:
                node = self._R[node]
                a = mid
        return False, None

    def _prefix(self, root, t):
        """F(t) = 时间段 [X[0], t) 的 (覆盖时长, 值x时长)。"""
        xs = self._xs
        i = bisect_right(xs, t) - 1  # X[i] <= t < X[i+1]；t == X[-1] 时 i == m
        if i > 0:
            cov, tot = self._range_query(root, 0, self._m, 0, i)
        else:
            cov = tot = 0
        if 0 <= i < self._m:
            covered, v = self._point_value(root, 0, self._m, i)
            if covered:
                d = t - xs[i]
                cov += d
                tot += v * d
        return cov, tot

    # ---------------------------------------------------------------- 接口

    def query(self, commit, start, end):
        """按提交号 <= commit 的历史版本，核算窗口 [start, end) 的汇总。"""
        xs = self._xs
        if not xs:
            return {"covered": 0, "total": 0}
        # 窗口裁剪到坐标范围内：范围外任何版本都无覆盖
        s = start if start > xs[0] else xs[0]
        e = end if end < xs[-1] else xs[-1]
        if s >= e:
            return {"covered": 0, "total": 0}
        v = bisect_right(self._commits, commit)  # 版本 = 提交号 <= commit 的修订数
        root = self._roots[v]
        c1, t1 = self._prefix(root, s)
        c2, t2 = self._prefix(root, e)
        return {"covered": c2 - c1, "total": t2 - t1}
