"""几何审查：有没有导线穿过**器件本体**、或穿过**网络符号**的占位区。

为什么要有这个脚本：

- 这类问题**网表查不出来** —— 电气上完全正确，只是图难看、人难审
- 而「人眼看一遍」在多页工程上**不可重复**，改一轮就得重看一轮

daikin-rcu 2026-09-28 实测：用户指出两处重叠，我改完自认为修好了，
他复查说「一点没变」。一跑这个脚本才看清 —— **元件本体穿越 0 处、
网络符号占位穿越 4 处**：我改的全是本体，符号一点没碰。
**有判据才能吵清楚。**

用法::

    from live import run, open_page       # 各项目自己的通道
    from overlapcheck import check_page
    check_page(run, open_page, "页uuid", "P1")

对应 `references/schematic-readability.md` 的 R2 修订。
"""

from __future__ import annotations

from typing import Any, Callable

from gridcheck import (STORED_TO_BODY, body_box, crosses_box, label_box,
                       read_pins)


def _wire_segments(run: Callable[[str], Any]) -> list[tuple[int, int, int, int]]:
    """读回全部导线并切成线段。

    ⚠ 回读的 `wire.line` 是**线段表**（每 4 个数一段），不是折线 —— 步进 4。
    """
    wires = run("return (await eda.sch_PrimitiveWire.getAll()||[]).map(w=>w.line);") or []
    segs = []
    for line in wires:
        flat = [round(v) for v in ([x for pt in line for x in pt]
                                   if line and isinstance(line[0], list) else line)]
        segs += [(flat[i], flat[i + 1], flat[i + 2], flat[i + 3])
                 for i in range(0, len(flat) - 3, 4)]
    return segs


def check_page(run: Callable[[str], Any], open_page: Callable[[str], Any],
               page_uuid: str, page_name: str = "",
               ground_nets: set[str] | None = None,
               power_nets: set[str] | None = None) -> list[str]:
    """回问题清单（空列表 = 干净）。

    `ground_nets` / `power_nets` 用来判断符号本体的朝向查哪张表
    （地/电源/端口三种约定不同，见 R4）。不传就按网名猜。
    """
    import time
    open_page(page_uuid)
    time.sleep(2)
    gnd = ground_nets or {"GND", "AGND", "PGND", "DGND"}
    pwr = power_nets or set()

    info = read_pins(run)
    segs = _wire_segments(run)
    syms = run("return (await eda.sch_PrimitiveComponent.getAll()||[])"
               ".filter(c=>c.componentType==='netflag'||c.componentType==='netport')"
               ".map(c=>({net:c.net||null,x:Math.round(c.x),y:Math.round(c.y),"
               "r:c.rotation||0,t:c.componentType}));") or []

    bad: list[str] = []
    for ref, d in info.items():
        bx = body_box(d["pins"])
        for s in segs:
            if crosses_box(list(s), [bx]):
                bad.append(f"{page_name} 本体 {ref}{bx} ← 导线{s}")
    for c in syms:
        net = c["net"] or ""
        kind = ("Ground" if net in gnd else
                "Power" if (net in pwr or c["t"] == "netflag") else "BI")
        d = STORED_TO_BODY[kind].get(int(c["r"]) % 360, (0, 1))
        lb = label_box(c["x"], c["y"], d)
        for s in segs:
            # 符号自己的支线接在它的原点上，不算压
            if (s[0], s[1]) == (c["x"], c["y"]) or (s[2], s[3]) == (c["x"], c["y"]):
                continue
            if crosses_box(list(s), [lb]):
                bad.append(f"{page_name} 符号 {net}@({c['x']},{c['y']}) "
                           f"rot={c['r']} {kind} 占位{lb} ← 导线{s}")
    return bad


# ── 剩下的重叠什么时候该收手 ────────────────────────────────────────
#
# ⚠ **有些重叠是布局问题，不是连线问题，路由改不动。**
#
# daikin-rcu P1 最后剩 2 个符号无解。量一下就清楚：U1 左侧 **12 个引脚全部朝左**、
# y 从 490 到 630、间距只有 10~20，而一个符号占位半高 15 ——
# **一个符号要吃掉 3 条脚道**，而那 12 条线全都要往左出。
#
# → 这时正确的做法是**报清楚「剩几处、为什么」，把决定权交回人**
#   （器件摆放归人，见 `human-ai-handoff.md`），不要硬凑、更不要假装修好了。
