# history.Index —— 按历史版本核算有效期事实的窗口汇总

纯 Python 标准库实现。`history.Index(revisions)` 构建多版本索引，
`query(commit, start, end)` 在提交号 ≤ commit 的历史版本上核算窗口
`[start, end)` 的 `{"covered": 有效覆盖时长, "total": 值×有效时长之和}`。
重叠处以提交号大者为准；`None` 撤销覆盖；`0` 仍计有效时长；无覆盖两者均为 0。

## 实现：坐标压缩 + 持久化（路径复制）懒标记线段树

所有修订端点排序去重得坐标数组 X，叶 i 对应时间段 `[X[i], X[i+1])`，
叶数 m ≤ 2n−1。每个修订是一次"区间覆盖赋值"，产生一个新版本根；
版本间只路径复制、旧节点永不改写，因此任意历史版本都可直接查询，
不逐时间单位展开、不重放历史。查询窗口先裁剪到 `[X[0], X[-1]]`，
答案 = F(end) − F(start)，前缀函数 F(t) = 一次区间求和 + 一次点查。

## 不变量（对任意版本的任意节点，节点覆盖叶子区间 [a,b)，即时间段 [X[a],X[b])）

- **I1** 空节点 0 表示整段无覆盖：COVER = TOTAL = 0，TAG = 0，孩子为 0。
- **I2** 带懒标记（TAG = 修订序号+1）的节点，其整段值统一为该修订的 value
  （None 表示整段无覆盖）；COVER/TOTAL 已按整段长度物化，孩子不再具有语义。
- **I3** 无懒标记的节点满足 COVER = COVER[左]+COVER[右]，TOTAL = TOTAL[左]+TOTAL[右]。
- **I4** 更新只做路径复制：每次修订新增 O(log m) 个节点，历史版本节点保持不变。
- **I5** 查询只读：区间求和与点查自根向下，遇懒标记按整段折算后即停，不再下探。

`cli.py verify` 中的 `check_invariants` 会沿每个版本根递归核对 I1–I3
（I4/I5 由实现结构保证）。

## 复杂度（n = 修订数，m = 叶数 ≤ 2n−1）

| 阶段 | 时间 | 空间 |
| --- | --- | --- |
| 构建 | O((n+1)·log(n+2))：每次修订访问并复制 O(log m) 个节点 | O((n+1)·log(n+2)) |
| 查询 | O(log(n+2))：二分定位版本 O(log n) + 区间求和与点查各 O(log m) | — |

存储：孩子下标与懒标记用 32 位数组；COVER/TOTAL 在坐标跨度与 |value|×跨度
均 ≤ 2⁶³−1 时用 64 位数组，否则自动退化为 Python 列表，任意大整数下结果仍精确。

## 独立重放对照

`cli.py` 内置与线段树完全无关的对账实现 `replay_query`：把适用修订依次裁剪进
查询窗口，维护互不重叠的区间列表，后来者切断并覆盖先到者，None 只切断不补回。
核验内容：题目给定样例；边界情形（空索引、commit 越界、窗口裁剪、仅 None、
0 值、负坐标负值、历史版本不受后续修订影响）；300 个随机小例 × 4 查询共
1200 次对照 + 不变量巡检；350 修订 × 100 查询中规模对照；以及 10 万全量索引
对 8 条抽样查询（commit ≤ 1200）与 `prepare-expected` 独立重放期望的对账。

## 实测（10 万修订 + 10 万查询，Python 3.11.2，本仓库 `verify` 输出）

- 构建：**2.95 s**（29.5 µs/修订），线段树节点 4,772,781 个（叶数 m=181,471，
  同阶参考 n·log2(m+1) ≈ 1.8×10⁶，实测常数约 2.7）
- 查询：**1.98 s / 10 万条**（19.8 µs/条）
- 峰值内存：tracemalloc **146.7 MiB**；进程 ru_maxrss **261.3 MiB**
  （含数据集与两次构建的峰值驻留）

## CLI 运行清单（.pointwise-runtime.json，版本 1）

```json
{
  "version": 1,
  "prepare": [
    ["python3", "-I", "cli.py", "prepare", "--dir", "artifacts"],
    ["python3", "-I", "cli.py", "prepare-expected", "--dir", "artifacts"]
  ],
  "verify": ["python3", "-I", "cli.py", "verify", "--dir", "artifacts"],
  "demo": ["python3", "-I", "cli.py", "demo"]
}
```

- `prepare`（参数数组列表，两条命令先后执行）：生成确定性数据集
  （10 万修订、10 万查询、8 条抽样查询，含 sha256 完整性摘要），
  再用独立重放计算抽样查询的期望结果作为对账基准。
- `verify`（参数数组）：核账核验，全部通过时退出码 0。
- `demo`（参数数组）：更正演示——覆盖、撤销（None）、改值、0 值，
  以及同一窗口在不同历史版本上的核算对照。

## 文件

- `history.py` —— 库：`Index` 类（不变量与复杂度亦见模块 docstring）
- `cli.py` —— 命令行：prepare / prepare-expected / verify / demo
- `.pointwise-runtime.json` —— 版本 1 CLI 运行清单
- `artifacts/` —— `prepare` 生成的数据集与对账基准（不入库）
