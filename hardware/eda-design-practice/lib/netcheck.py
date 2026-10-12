"""网表验收：导出 EDA 自己的网表并按硬判据检查。

铁律：**改完必须用 EDA 自己导出的网表验收，不看自己的账。**

判据：

- **不许有单节点的命名网络**（单节点 = 悬空）
- 点对点的网**恰好 2 个节点**
- 未接引脚清单里**只允许出现 NC 和明确不用的脚**
- 自动命名的网络（`$1N146` 这类）**不是错误** —— 那是直连线连出来的无名网，
  只要节点数 ≥2 就是对的
- ⚠ **导线条数不是判据**：编辑器会合并共线同宽段（建 84 读回 81 是正常的）

用法::

    from netcheck import fetch_netlist, analyse, report
    raw = fetch_netlist(run, open_page, page_uuid)
    r = analyse(raw)
    report(r, expect_nc={"U1.6", "U1.13"})

## 网表格式（实测）

顶层没有 `nets` 键！网络存在**每个引脚**上：

    components[gge<N>].props.Designator
    components[gge<N>].pinInfoMap[<引脚号>].net        ← 网络名在这里
    components[gge<N>].pinInfoMap[<引脚号>].name       ← 引脚名

⚠ `getNetlistFile(name, type)` 的 name 参数是幌子，它导的是**当前激活的那块板**，
不先 `open_page` 会拿到上一块板的网表。
⚠ 有缓存：改完属性立刻导会看到上一版，**等 1~2 秒再导，或导两次对比**。
"""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

_FETCH_JS = """
const T = {JLCEDA_PRO: "JLCEDA"};
const f = await eda.sch_ManufactureData.getNetlistFile("x", T.JLCEDA_PRO);
if (!f) return null;
const b = new Uint8Array(await f.arrayBuffer());
let s = ""; const CH = 0x8000;
for (let i = 0; i < b.length; i += CH) s += String.fromCharCode.apply(null, b.subarray(i, i + CH));
return btoa(s);
"""


def fetch_netlist(run: Callable[[str], Any], open_page: Callable[[str], Any],
                  page_uuid: str, settle_s: float = 2.5) -> dict:
    """切到该板任一页后导出网表。`settle_s` 是为了绕开导出缓存。"""
    open_page(page_uuid)
    time.sleep(settle_s)
    b64 = run(_FETCH_JS)
    if not b64:
        raise RuntimeError("getNetlistFile 返回空 —— 页面可能没打开")
    return json.loads(base64.b64decode(b64))


@dataclass
class Result:
    nets: dict[str, list[str]] = field(default_factory=dict)
    unconnected: list[str] = field(default_factory=list)
    single_node: list[str] = field(default_factory=list)
    anonymous: dict[str, list[str]] = field(default_factory=dict)
    n_components: int = 0


def analyse(raw: dict) -> Result:
    nets: dict[str, list[str]] = defaultdict(list)
    unconn: list[str] = []
    for comp in raw.get("components", {}).values():
        ref = comp.get("props", {}).get("Designator", "?")
        for num, pi in (comp.get("pinInfoMap") or {}).items():
            net = (pi.get("net") or "").strip()
            label = f"{ref}.{num}({pi.get('name')})"
            if net:
                nets[net].append(f"{ref}.{num}")
            else:
                unconn.append(label)
    return Result(
        nets=dict(nets),
        unconnected=sorted(unconn),
        # 自动命名的网（$xNyyy）是直连线连出来的，不算「命名网络」
        single_node=sorted(n for n, p in nets.items()
                           if len(p) < 2 and not n.startswith("$")),
        anonymous={n: p for n, p in nets.items() if n.startswith("$")},
        n_components=len(raw.get("components", {})),
    )


def report(r: Result, expect_nc: set[str] | None = None,
           expect_single: set[str] | None = None, verbose: bool = False) -> bool:
    """打印并返回是否通过。

    `expect_nc`       允许未接的引脚（形如 "U1.6"），NC 脚写在这里
    `expect_single`   允许单节点的网络（跨页端口在单页导出时会这样）
    """
    expect_nc = expect_nc or set()
    expect_single = expect_single or set()
    ok = True
    print(f"器件 {r.n_components}  网络 {len(r.nets)}")

    if verbose:
        for n in sorted(r.nets):
            print(f"  {n:<12} {len(r.nets[n]):>2}  {' '.join(sorted(r.nets[n]))}")

    bad_single = [n for n in r.single_node if n not in expect_single]
    print("★ 单节点网络:", bad_single or "无 ✓")
    ok &= not bad_single

    bad_unconn = [u for u in r.unconnected
                  if u.split("(")[0] not in expect_nc and "(NC)" not in u]
    print("★ 非预期的未接引脚:", " ".join(bad_unconn) or "无 ✓")
    ok &= not bad_unconn

    thin = {n: p for n, p in r.anonymous.items() if len(p) < 2}
    if thin:
        print("★ ⚠ 自动命名网络只有 1 个节点（有线没接上）:", thin)
        ok = False
    elif r.anonymous:
        print(f"  （{len(r.anonymous)} 条无名网络来自直连线，节点数都 ≥2，正常）")

    print("=> " + ("通过 ✓" if ok else "**不通过**"))
    return ok


def diff(before: Result, after: Result) -> None:
    """比较两次网表，专门用来抓「意外并网」。

    ⚠ 典型事故：直连线的 L 形路径压到别的引脚 → 被判成 T 形连接 →
    整条网络被静默并进 GND。**只有对比节点数才看得出来。**
    """
    keys = set(before.nets) | set(after.nets)
    for k in sorted(keys):
        b, a = len(before.nets.get(k, [])), len(after.nets.get(k, []))
        if b != a:
            mark = "  ⚠ 变多了，检查是否误并网" if a > b else ""
            print(f"  {k:<12} {b:>2} → {a:>2}{mark}")
            if a > b:
                print(f"      新增: {sorted(set(after.nets.get(k, [])) - set(before.nets.get(k, [])))}")
