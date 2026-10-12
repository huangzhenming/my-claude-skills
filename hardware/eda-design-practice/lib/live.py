"""嘉立创EDA 专业版运行态通道（Run API Gateway bridge）。

链路::

    本模块 ─HTTP→ bridge-server (127.0.0.1:49620~49629)
              ─WebSocket→ 「Run API Gateway」扩展（装在 EasyEDA Pro 里）
              → eda.* 运行时 API

比 `finish_live_schematics.run()` 多两件事：端口自动扫描、windowId 自动取
（那个脚本里写死的 `WINDOW=8c9301ea-…` 是 2026-09-18 那次会话的窗口，
重启客户端就失效）。

用法::

    python3 scripts/live.py --status
    python3 scripts/live.py --exec 'return await eda.dmt_Project.getCurrentProjectInfo();'

    from live import run, project_pages
    run('return await eda.sch_PrimitiveComponent.getAll();')

⚠ HTTP 500 / 超时 = **未知状态**，不代表写操作没生效；重试前必须 read back。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PORT_RANGE = range(49620, 49630)
SERVICE_ID = "easyeda-bridge"
TIMEOUT_S = 40  # bridge 对 EDA 的超时是 30s，外面留余量

_cache: dict[str, Any] = {}


def health() -> tuple[int, dict]:
    """扫端口找 bridge，回 (端口, /health)。找不到或 EDA 没连上就抛。"""
    if "health" in _cache:
        return _cache["health"]
    for port in PORT_RANGE:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                h = json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
        if h.get("service") == SERVICE_ID:
            _cache["health"] = (port, h)
            return port, h
    raise RuntimeError(
        "bridge 没在跑。启动：\n"
        "  cd /data/hardware/refs/easyeda-api-skill && node scripts/bridge-server.mjs"
    )


def window_id() -> str | None:
    """当前活动窗口 id。只有一个窗口时返回 None（让 bridge 自己选）。"""
    if "window" in _cache:
        return _cache["window"]
    port, h = health()
    wid = None
    if h.get("edaWindowCount", 0) > 1:
        # ⚠ /eda-windows/select 有未定义变量 bug 会崩 bridge，只读列表是安全的
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/eda-windows", timeout=5) as r:
            data = json.load(r)
        wins = data.get("windows") or data.get("data") or []
        # ⚠ /eda-windows 回的键是 `windowId`，不是 `id`（canmv_serdes 那份写错了，
        #   多窗口时恒返回 None，等于没选窗口）。且 `connected:false` 的是重启前
        #   留下的僵尸条目，选中它会直接报「no longer connected」。
        live = [w for w in wins if w.get("connected")]
        active = [w for w in live if w.get("active")]
        pick = (active or live or [None])[0]
        wid = pick.get("windowId") if pick else data.get("activeWindowId")
    _cache["window"] = wid
    return wid


def _run_once(code: str) -> Any:
    """在 EDA 运行时里执行 JS 片段，回 result。失败抛 RuntimeError（带原始响应）。

    片段是异步函数体，用 `return await eda.xxx(...)` 收尾。
    `globalThis` 在同窗口的多次调用之间共享，可以拿来暂存大对象。
    """
    port, _ = health()
    payload: dict[str, Any] = {"code": code}
    wid = window_id()
    if wid:
        payload["windowId"] = wid
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/execute",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    if not resp.get("success"):
        raise RuntimeError(json.dumps(resp, ensure_ascii=False))
    return resp.get("result")


RETRY_ON = ("disconnected", "no longer connected", "timed out")


def run(code: str, retries: int = 4) -> Any:
    """执行 JS，**断线自动重连重试**。

    ⚠ 本机的 EDA 扩展 WebSocket 每隔几十秒会断开一次（close 1005）再自动重连，
    每次换一个新的 windowId。批量写操作必然被它打断，所以这里统一兜底：
    命中「disconnected / timed out」就清掉缓存的端口和 windowId，等一下重试。

    ⚠ 重试**不等于幂等**：HTTP 500 是「未知状态」，写操作可能已经生效。
    调用方仍然要能重入（例如连线前先清线），并且**建完读回核对**。
    """
    last: Exception | None = None
    for i in range(retries):
        try:
            return _run_once(code)
        except RuntimeError as e:
            last = e
            if not any(k in str(e) for k in RETRY_ON):
                raise
            _cache.pop("health", None)
            _cache.pop("window", None)
            time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("run 失败")


def project() -> dict:
    """当前工程信息（`dmt_Project.getCurrentProjectInfo`）。"""
    return run("return await eda.dmt_Project.getCurrentProjectInfo();")


def project_pages(name_filter: str = "") -> list[tuple[str, str, str]]:
    """回 [(板名, 页名, 页uuid)]，按板名/页名排序。"""
    out = []
    for board in project().get("data", []):
        name = board.get("name", "")
        if name_filter and name_filter not in name:
            continue
        for page in sorted(board.get("schematic", {}).get("page", []), key=lambda p: p["name"]):
            out.append((name, page["name"], page["uuid"]))
    return sorted(out)


def open_page(uuid: str) -> Any:
    """切到某一图页并等它成为当前文档。图元域加载有滞后，读之前先确认。"""
    return run(f"return await eda.dmt_EditorControl.openDocument({json.dumps(uuid)});")


def save() -> bool:
    ok = run("return await eda.sch_Document.save();")
    if not ok:
        raise RuntimeError("sch_Document.save() 返回 false")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--status", action="store_true", help="bridge / EDA 连接状态")
    ap.add_argument("--pages", action="store_true", help="列出当前工程的板和图页")
    ap.add_argument("--exec", dest="code", help="执行 JS 片段")
    args = ap.parse_args()

    if args.status or not (args.pages or args.code):
        port, h = health()
        print(f"✓ bridge 127.0.0.1:{port}  EDA已连接={h['edaConnected']}  窗口={h['edaWindowCount']}")
        if not h["edaConnected"]:
            print("  在 EDA 里：扩展 → Run API Gateway → 重新连接")
            return 1
    if args.pages:
        for board, page, uuid in project_pages():
            print(f"{board:28} {page:24} {uuid}")
    if args.code:
        print(json.dumps(run(args.code), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
