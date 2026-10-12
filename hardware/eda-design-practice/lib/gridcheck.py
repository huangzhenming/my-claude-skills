"""原理图几何检查：栅格对齐、路径碰撞、器件间距、标记朝向。

这些规则**能被代码检查**，就不要只写在文档里 ——
写在文档里靠自觉，写进这里每次都会跑。

对应 `references/schematic-readability.md` 的 R4 / R5 / R6 / R7。
"""

from __future__ import annotations

import time
from typing import Any, Callable

GRID = 10

# 实测符号尺寸（EasyEDA Pro，单位 0.01 inch）。见 R6。
PIN_SPAN = {"竖放R/C": 40, "横放R": 40, "横放C(1206)": 30}
MIN_CENTER_DIST = 60   # 同列/同排竖放件的最小中心距；建议 80

# ★★ 网络标记的朝向约定。**2026-09-28 用户目视逐个确认，不要凭推测改。**
#
# 两个独立的事实，缺一不可：
#
# 1) **每种符号的约定不同**，不能共用一张表。
#    下表是「传入 createNetFlag/createNetPort 的 rotation → 本体伸出方向」
#    （原理图 API 坐标，y 向上；引脚在符号原点，本体朝引脚的反方向伸）。
# 2) **存储值 = (360 − 传入值) % 360**（五种符号一致）。
#    所以**生成时查 PASS_TO_BODY，回读检查时查 STORED_TO_BODY**，两者不能混。
#
# ⚠ 教训：此前只测了 GND 就把结论推广到所有符号，而且生成器和检查器共用同一张
#   凭感觉写的表 —— 自己验自己，「修好了」只是自洽，实际越修越错。
#   **符号本体的包围盒 API 读不到，这个约定在 API 侧无法自证，只能靠人眼测一次。**
PASS_TO_BODY: dict[str, dict[int, tuple[int, int]]] = {
    # 传入 rot:  0          90         180        270
    "Ground": {0: (0, -1), 90: (-1, 0), 180: (0, 1), 270: (1, 0)},
    "Power":  {0: (0, 1),  90: (1, 0),  180: (0, -1), 270: (-1, 0)},
    "BI":     {0: (1, 0),  90: (0, -1), 180: (-1, 0), 270: (0, 1)},
    "OUT":    {0: (1, 0),  90: (0, -1), 180: (-1, 0), 270: (0, 1)},
    "IN":     {0: (-1, 0), 90: (0, 1),  180: (1, 0),  270: (0, -1)},
}
#: 本体想朝哪 → 该**传入**的 rotation
ROT_FOR_BODY = {k: {b: r for r, b in m.items()} for k, m in PASS_TO_BODY.items()}
#: **回读**到的 rotation → 本体方向（存储值是传入值取负）
STORED_TO_BODY = {k: {(360 - r) % 360: b for r, b in m.items()}
                  for k, m in PASS_TO_BODY.items()}


def _kind(sym: dict) -> str:
    """从回读对象判断符号类型。netflag 分 Ground / Power，netport 统一按 BI。"""
    if sym.get("t") == "netflag" or sym.get("componentType") == "netflag":
        return "Ground" if (sym.get("net") or "").upper() in ("GND", "AGND", "PGND") else "Power"
    return "BI"


# ── 引脚朝向与几何盒（R2 修订用）──────────────────────────────────
#: 引脚 `rotation` → **向外方向**（原理图 API 坐标，y 向上；逆时针）。
#: ⚠ 这和**网络标记**的朝向是两套不同约定，别和上面的 PASS_TO_BODY 混：
#:   标记那边「存储值 = (360 − 传入值) % 360」，且每种符号一张表。
#: 实测标定（daikin-rcu P1 2026-09-28）：R13 原点 x=1000，脚2 在 x=1020 且 rot=0、
#: 脚1 在 x=980 且 rot=180；Q1 的 S 在下方 rot=270、D 在上方 rot=90。
PIN_DIR: dict[int, tuple[int, int]] = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}

BODY_MIN = 20                      #: 本体盒退化方向的最小厚度
LABEL_LEN, LABEL_HALF = 60, 15     #: 网络符号占位：长度、半宽

Box = tuple[int, int, int, int]    # (x0, y0, x1, y1)


def pin_dir(rot: int | None) -> tuple[int, int]:
    return PIN_DIR.get(int(rot or 0) % 360, (1, 0))


def poly_segs(path: list[int]) -> list[tuple[int, int, int, int]]:
    """把**折线**（`create` 用的格式）切成线段 —— 步进 2。

    ⚠ 别和 `_segments()` 混：**回读**的 `wire.line` 是「线段表」，步进 4。
    """
    return [(path[i], path[i + 1], path[i + 2], path[i + 3])
            for i in range(0, len(path) - 2, 2)]


def body_box(pins: dict) -> Box:
    """器件**本体**的近似矩形。

    ★ 符号本体的包围盒 API 读不到（见 R6），但可以算：
      引脚的 `x,y` 是**接线点（外端）**，沿引脚朝向**回缩 `pinLength`** 即本体边缘，
      对内端点取包围盒。只有一排引脚时某方向退化成 0，补 `BODY_MIN`。

    `pins` 的值形如 `(x, y, 引脚名, rotation, pinLength)`。
    实测：R13 两脚 x=980/1020、pinLength=10 → 本体 x[990,1010]、y 补成 [520,540]。
    """
    pts = []
    for v in pins.values():
        dx, dy = pin_dir(v[3])
        ln = v[4] if len(v) > 4 else 10
        pts.append((v[0] - dx * ln, v[1] - dy * ln))
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    if x1 - x0 < BODY_MIN:
        c = (x0 + x1) // 2
        x0, x1 = c - BODY_MIN // 2, c + BODY_MIN // 2
    if y1 - y0 < BODY_MIN:
        c = (y0 + y1) // 2
        y0, y1 = c - BODY_MIN // 2, c + BODY_MIN // 2
    return (x0, y0, x1, y1)


def label_box(ex: int, ey: int, d: tuple[int, int], length: int = LABEL_LEN) -> Box:
    """网络符号的占位矩形，从 (ex,ey) 沿本体方向 `d` 伸出 `length`。

    ⚠ **连线前预留位置时要从「引脚」起算**（`length = STUB + LABEL_LEN`），
      不能只圈符号本体 —— 否则引脚到符号之间那段支线是敞开的，长导线从那儿穿过去，
      放符号时就「找不到支线位」，**网络当场悬空**。
    """
    x2, y2 = ex + d[0] * length, ey + d[1] * length
    hx, hy = (LABEL_HALF, 0) if d[0] == 0 else (0, LABEL_HALF)
    return (min(ex, x2) - hx, min(ey, y2) - hy, max(ex, x2) + hx, max(ey, y2) + hy)


def crosses_box(path: list[int], boxes: list[Box]) -> bool:
    """折线是否穿过任何矩形的**内部**（只擦边界不算）。

    ⚠ 判内部不判边界：导线本来就从引脚出发，而引脚正好在本体边缘上，
      按闭区间判会把所有正常连线都否掉。
    """
    for x1, y1, x2, y2 in poly_segs(path):
        for (bx0, by0, bx1, by1) in boxes:
            if x1 == x2:
                if bx0 < x1 < bx1 and min(y1, y2) < by1 and max(y1, y2) > by0:
                    return True
            elif by0 < y1 < by1 and min(x1, x2) < bx1 and max(x1, x2) > bx0:
                return True
    return False


def wire_conflict(path: list[int], blocked: list[Box]) -> bool:
    """这条折线会不会被编辑器判成和**别的网**连上？

    ★ **X 形穿越是安全的**，人画图也躲不开交叉。会被判成连接的只有三种：
      ① 和别的网的线**共线**且有交集（哪怕只重合一个点）→ 被合并
      ② 我的**拐点或端点**落在别的网的线上 → T 形连接
      ③ 别的网的线的**端点**落在我的线上 → T 形连接

    ⚠ 只躲引脚是不够的：daikin-rcu P1 实测 `SRA_N` 并进 `MS_DOUT`、
      `TERM_N` 并进 `BIO_BUS`，两处都**没压到任何引脚**，全是共线重叠。
    """
    mine = poly_segs(path)
    mypts = [(path[i], path[i + 1]) for i in range(0, len(path), 2)]

    def _on(px, py, s):
        x1, y1, x2, y2 = s
        return ((x1 == x2 == px and min(y1, y2) <= py <= max(y1, y2))
                or (y1 == y2 == py and min(x1, x2) <= px <= max(x1, x2)))

    for s in blocked:
        for m in mine:
            if (m[0] == m[2] == s[0] == s[2]
                    and max(min(m[1], m[3]), min(s[1], s[3]))
                    <= min(max(m[1], m[3]), max(s[1], s[3]))):
                return True
            if (m[1] == m[3] == s[1] == s[3]
                    and max(min(m[0], m[2]), min(s[0], s[2]))
                    <= min(max(m[0], m[2]), max(s[0], s[2]))):
                return True
        if any(_on(px, py, s) for px, py in mypts):
            return True
        if any(_on(e[0], e[1], m) for m in mine for e in ((s[0], s[1]), (s[2], s[3]))):
            return True
    return False


# ── R7 栅格 ──────────────────────────────────────────────────────────
def read_pins(run: Callable[[str], Any]) -> dict[str, dict]:
    """回 {位号: {"__id": pid, "pins": {引脚号: (x, y, 引脚名, rotation, pinLength)}}}。"""
    cs = run("return (await eda.sch_PrimitiveComponent.getAll()||[])"
             ".filter(c=>c.designator).map(c=>({d:c.designator,id:c.primitiveId}));") or []
    out: dict[str, dict] = {}
    for c in cs:
        p = run(f'return (await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId("{c["id"]}")||[])'
                f'.map(p=>[p.pinNumber,Math.round(p.x),Math.round(p.y),p.pinName,'
                f'p.rotation||0,p.pinLength||10]);') or []
        # v = (x, y, 引脚名, 引脚朝向 rotation, 引脚长度)
        out[c["d"]] = {"__id": c["id"],
                       "pins": {str(a[0]): (a[1], a[2], a[3], a[4], a[5]) for a in p}}
    return out


def off_grid(info: dict[str, dict]) -> dict[str, tuple[bool, bool]]:
    """回 {位号: (x 方向不对齐, y 方向不对齐)}。

    ⚠ **必须同时查 x 和 y**。daikin-rcu 曾只查 y，漏掉了 MB10S 的 x=965
    （它的引脚 x 偏移是 ±25，原点必须落在半格上）。
    """
    bad = {}
    for ref, d in info.items():
        dx = any(v[0] % GRID for v in d["pins"].values())
        dy = any(v[1] % GRID for v in d["pins"].values())
        if dx or dy:
            bad[ref] = (dx, dy)
    return bad


def nudge_to_grid(run: Callable[[str], Any], info: dict[str, dict],
                  rounds: int = 2) -> dict[str, dict]:
    """把引脚不落栅格的器件原点在对应轴上挪 5，复查，最多 `rounds` 轮。

    器件引脚偏移常见 ±5/±15/±25，这时**原点要落在半格上**引脚才落 10 栅格。
    不落栅格 = 网表里就是断的。
    """
    for _ in range(rounds):
        bad = off_grid(info)
        if not bad:
            break
        for ref, (dx, dy) in bad.items():
            c = run(f'return await eda.sch_PrimitiveComponent.get("{info[ref]["__id"]}");')
            nx = round(c["x"]) + (5 if dx else 0)
            ny = round(c["y"]) + (5 if dy else 0)
            print(f"  ↳ 微调 {ref} → ({nx},{ny})")
            run(f'return await eda.sch_PrimitiveComponent.modify('
                f'"{info[ref]["__id"]}", {{x:{nx}, y:{ny}}});')
            time.sleep(0.2)
        run("return await eda.sch_Document.save();")
        time.sleep(1)
        info = read_pins(run)
    return info


# ── R5 直连线的路径碰撞 ──────────────────────────────────────────────
def seg_hits(path: list[int], pins: set[tuple[int, int]]) -> bool:
    """折线路径是否压到了**不属于本网络**的引脚。

    ⚠ 这个检查不能省。EasyEDA 里**端点落在另一条线上 = T 形连接**（连通），
    X 形穿越才是不连接。L 形路径很容易从第三个引脚上压过去。

    实测事故（daikin-rcu 2026-09-28）：IO12_N / LED_A / LED_N 三条网络被静默并进 GND，
    GND 从 43 个节点涨到 48 —— **图上完全看不出来，只有导网表才暴露**。
    """
    for i in range(0, len(path) - 2, 2):
        x1, y1, x2, y2 = path[i], path[i + 1], path[i + 2], path[i + 3]
        for (px, py) in pins:
            if x1 == x2 == px and min(y1, y2) <= py <= max(y1, y2):
                return True
            if y1 == y2 == py and min(x1, x2) <= px <= max(x1, x2):
                return True
    return False


def direct_path(a: tuple[int, int], b: tuple[int, int],
                others: set[tuple[int, int]]) -> list[int] | None:
    """给两个引脚找一条不压到别人的路径；找不到回 None（调用方应退回用标记）。"""
    (x1, y1), (x2, y2) = a, b
    cands = ([[x1, y1, x2, y2]] if (x1 == x2 or y1 == y2)
             else [[x1, y1, x2, y1, x2, y2], [x1, y1, x1, y2, x2, y2]])
    return next((c for c in cands if not seg_hits(c, others)), None)


# ── R6 器件间距 ──────────────────────────────────────────────────────
def crowded(info: dict[str, dict], min_dist: int = MIN_CENTER_DIST) -> list[str]:
    """按引脚包围盒找挨得过近的器件对。

    ⚠ 这只是**下限筛查**：API 读不到符号本体和位号/数值文字的包围盒，
    所以「器件叠没叠」最终**只能靠人打开看**。本函数只抓明显的。
    """
    boxes = {}
    for ref, d in info.items():
        xs = [v[0] for v in d["pins"].values()]
        ys = [v[1] for v in d["pins"].values()]
        boxes[ref] = (min(xs), min(ys), max(xs), max(ys))
    out = []
    refs = sorted(boxes)
    for i, a in enumerate(refs):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in refs[i + 1:]:
            bx0, by0, bx1, by1 = boxes[b]
            gap_x = max(bx0 - ax1, ax0 - bx1)
            gap_y = max(by0 - ay1, ay0 - by1)
            if max(gap_x, gap_y) < min_dist - max(ax1 - ax0, ay1 - ay0, 0) * 0:
                if gap_x < 0 and gap_y < 0:
                    out.append(f"{a} ↔ {b} 包围盒重叠")
                elif max(gap_x, gap_y) < 20:
                    out.append(f"{a} ↔ {b} 间距仅 {max(gap_x, gap_y)}")
    return out


# ── R4 标记朝向 ──────────────────────────────────────────────────────
def check_label_orientation(run: Callable[[str], Any]) -> list[str]:
    """找出压在自己支线上的网络标记（地/电源符号、跨页端口）。

    **机理**：标记有方向。若朝向与支线方向相同，符号本体就压在导线上；
    镜像（H Mirror）或转到背离方向就不压。

    做法：标记的引脚在它自己的原点上；找出从该点出发的支线方向，
    与标记的 rotation 对比。方向一致 = 压住了。
    """
    syms = run("return (await eda.sch_PrimitiveComponent.getAll()||[])"
               ".filter(c=>c.componentType==='netflag'||c.componentType==='netport')"
               ".map(c=>({id:c.primitiveId,x:Math.round(c.x),y:Math.round(c.y),"
               "rot:c.rotation,t:c.componentType,net:c.net}));") or []
    segs = _segments(run)
    bad = []
    for s in syms:
        here = (s["x"], s["y"])
        for (x1, y1, x2, y2) in segs:
            if (x1, y1) == here:
                wd = (_sgn(x2 - x1), _sgn(y2 - y1))
            elif (x2, y2) == here:
                wd = (_sgn(x1 - x2), _sgn(y1 - y2))
            else:
                continue
            bd = STORED_TO_BODY[_kind(s)].get(int(s["rot"] or 0) % 360)
            if wd == bd:   # 本体朝着导线伸 → 压住
                bad.append(f"{s['net']}@({s['x']},{s['y']}) rot={s['rot']} "
                           f"本体朝 {bd} 与支线同向 → 该镜像")
            break
    return bad


# ⚠ **没有 fix_label_orientation()**：`sch_PrimitiveComponent.modify` 只认
#   componentType==="part"，对 netflag/netport 报「仅当器件类型为元件时允许使用该函数进行修改」。
#   **朝向只能在 createNetFlag/createNetPort 的参数里定，事后改不了，要改只能删了重建。**


def _segments(run: Callable[[str], Any]) -> list[tuple[int, int, int, int]]:
    """读回全部导线并切成线段。

    ★★ **回读的 `wire.line` 是「线段表」，不是折线** —— 每 **4 个数**一段
    `[x1,y1,x2,y2]`，段间没有连续性。而 `create()` 收的是**折线**（L 形 6 个数）。
    两种格式**只在单段时长得一样**，所以极易看错。

    判据（daikin-rcu P4 2026-09-28）：按 4 切，62 段**全部**轴对齐；
    按 2 当折线切，85 段里有 9 段是斜的 —— 原理图里根本没有斜线。

    此前本函数按 2 步进，于是 `check_label_orientation()` 拿着**虚构的斜线段**
    去比对朝向 —— 那正是 R4 那轮「越修越错」的隐藏帮凶之一。
    """
    wires = run("return (await eda.sch_PrimitiveWire.getAll()||[]).map(w=>w.line);") or []
    segs = []
    for line in wires:
        flat = [v for pt in line for v in pt] if line and isinstance(line[0], list) else line
        for i in range(0, len(flat) - 3, 4):      # ← 4，不是 2
            segs.append((round(flat[i]), round(flat[i + 1]),
                         round(flat[i + 2]), round(flat[i + 3])))
    return segs


def _sgn(v: int) -> int:
    return (v > 0) - (v < 0)
