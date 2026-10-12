"""可审性风格检查：符号朝向惯例、器件旋转、对齐。对应 skill 的 R2e / R2f。

这三条**网表和几何检查都查不出来**，但它们决定图好不好读。
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/data/hardware/refs/eda-design-practice/lib")
from build_page import FLAG, PAGES
from gridcheck import STORED_TO_BODY
from live import open_page, run
from terrain_layout import symmetric


def check(page: str) -> None:
    open_page(PAGES[page])
    time.sleep(1.5)
    cs = run("return (await eda.sch_PrimitiveComponent.getAll()||[]).map(c=>({"
             "d:c.designator||null,t:c.componentType||null,net:c.net||null,"
             "fp:((c.footprint||{}).name)||'',x:Math.round(c.x),y:Math.round(c.y),"
             "r:c.rotation||0,id:c.primitiveId}));") or []
    flags = [c for c in cs if c["t"] == "netflag" and FLAG.get(c["net"])]
    ok = 0
    for c in flags:
        kind = FLAG[c["net"]]
        d = STORED_TO_BODY[kind].get(int(c["r"]) % 360, (0, 1))
        ok += (d == ((0, -1) if kind == "Ground" else (0, 1)))
    parts = [c for c in cs if c["t"] == "part" and c["d"]]
    rots = Counter(int(c["r"]) % 360 for c in parts)
    bad270 = []
    for c in parts:
        p = run(f'return (await eda.sch_PrimitiveComponent.getAllPinsByPrimitiveId'
                f'("{c["id"]}")||[]).length;')
        if int(c["r"]) % 360 in (180, 270) and symmetric(c["d"], {"loc": [0] * (p or 0),
                                                                  "fp": c["fp"]}):
            bad270.append(f'{c["d"]}@{int(c["r"])%360}')
    xs, ys = Counter(c["x"] for c in parts), Counter(c["y"] for c in parts)
    al = sum(v for v in xs.values() if v > 1) + sum(v for v in ys.values() if v > 1)
    print(f"{page}: 电源地符号 {len(flags)} 个 → 合朝向 {ok} ({ok/max(len(flags),1):.0%})"
          f"   旋转 {dict(sorted(rots.items()))}"
          f"   对称件用 180/270 的 {len(bad270)} 个 {bad270[:5]}"
          f"   对齐 {al}/{len(parts)}")


if __name__ == "__main__":
    for pg in (sys.argv[1:] or list(PAGES)):
        check(pg)
