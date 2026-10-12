#!/usr/bin/env python3
"""scope.py —— Siglent SDS800X HD 系列示波器控制（raw socket SCPI，仅标准库）。

默认目标 192.168.2.139:5025，可用 --host / 环境变量 SCOPE_HOST 覆盖。

子命令：
    idn                     连通性 + 身份自检
    state                   一次抓全配置快照
    shot <out.png>          屏幕截图
    wave <CH> <out.csv>     读波形（自动 WORD + 自动乘探头倍率）
    meas                    列出已配置的测量槽位及当前值
    errors                  循环排空错误队列（排查第一步）
    q <SCPI?>               任意查询
    w <SCPI>                任意写入（发完自动查错误队列）

两个静默错误陷阱已在本脚本内封死，不依赖调用方记得：
  * wave 强制 :WAVeform:WIDTh WORD —— 否则 HD 机型按 8-bit 传，白瞎 ADC 精度
  * wave 自动乘 :CHANnel<n>:PROBe? —— preamble 的 vertical_gain 不含探头倍率，
    直接用会小 10 倍且不报错
"""

import argparse
import os
import socket
import struct
import sys
import time

DEFAULT_HOST = os.environ.get("SCOPE_HOST", "192.168.2.139")
DEFAULT_PORT = int(os.environ.get("SCOPE_PORT", "5025"))

# 不支持的命令不会报错，而是静默无响应直到超时。超时即「命令名可能拼错了」。
TIMEOUT = 5.0
TIMEOUT_BULK = 30.0     # 读波形/截图这类大块数据
SETTLE = 0.4            # 写操作后断开重连的间隔，给固件时间落地设置


class ScopeTimeout(Exception):
    pass


class Scope:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        self._open()

    def _open(self):
        try:
            self.s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            sys.exit(f"scope: 连不上 {self.host}:{self.port} —— {e}")
        self.s.settimeout(self.timeout)

    def reconnect(self, settle=SETTLE):
        """断开重连 —— 写操作落地的必要步骤。

        实测：在同一连接上写完后立即发查询并读响应，写命令会被静默丢弃，
        而错误队列仍报 0,"No error"。断开重连后写才生效。
        成功率：写→等待→断开 6/6；写→立即查错误→断开 1/6。
        """
        self.close()
        time.sleep(settle)
        self._open()

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def write(self, cmd):
        self.s.sendall((cmd + "\n").encode())

    def ask(self, cmd, timeout=None):
        """文本查询。超时抛 ScopeTimeout —— 通常意味着命令不被支持。"""
        self.s.settimeout(timeout or TIMEOUT)
        self.write(cmd)
        buf = b""
        try:
            while not buf.endswith(b"\n"):
                chunk = self.s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            raise ScopeTimeout(cmd)
        return buf.decode(errors="replace").strip()

    def ask_safe(self, cmd, default="<不支持>"):
        try:
            return self.ask(cmd)
        except ScopeTimeout:
            return default

    def ask_block(self, cmd, timeout=TIMEOUT_BULK, max_bytes=64 << 20):
        """读 IEEE 488.2 定长块 `#<nd><len><payload>`，返回 payload。"""
        self.s.settimeout(timeout)
        self.write(cmd)
        buf = b""
        header_len = None
        total = None
        try:
            while True:
                chunk = self.s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
                if total is None and len(buf) >= 2 and buf[:1] == b"#":
                    nd = int(chr(buf[1]))
                    if len(buf) >= 2 + nd:
                        header_len = 2 + nd
                        total = header_len + int(buf[2:2 + nd])
                if total is not None and len(buf) >= total:
                    return buf[header_len:total]
                if len(buf) > max_bytes:
                    break
        except socket.timeout:
            raise ScopeTimeout(cmd)
        if buf[:1] != b"#":
            raise RuntimeError(f"{cmd}: 返回的不是 IEEE488.2 块，前 16 字节 {buf[:16]!r}")
        raise RuntimeError(f"{cmd}: 块不完整，收到 {len(buf)} / 期望 {total}")

    def ask_png(self, cmd=":PRINt? PNG", timeout=TIMEOUT_BULK):
        """SDS800X HD 的截图是裸 PNG 流，没有 IEEE488.2 块头。读到 IEND 为止。"""
        self.s.settimeout(timeout)
        self.write(cmd)
        buf = b""
        end = b"IEND\xaeB`\x82"
        try:
            while end not in buf:
                chunk = self.s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            if not buf:
                raise ScopeTimeout(cmd)
        start = buf.find(b"\x89PNG")
        if start < 0:
            raise RuntimeError(f"返回的不是 PNG，前 16 字节 {buf[:16]!r}")
        return buf[start:buf.find(end) + len(end)] if end in buf else buf[start:]

    # ---- 错误队列 ---------------------------------------------------------
    # 队列是 FIFO，深度实测约 16 条，溢出后早期错误被直接丢弃。
    # 所以每条写操作后立刻查，不要攒一批再查。
    def drain_errors(self, limit=40):
        out = []
        for _ in range(limit):
            try:
                e = self.ask(":SYSTem:ERRor?")
            except ScopeTimeout:
                out.append("<查询错误队列本身超时>")
                break
            if e.startswith("0,"):
                break
            out.append(e)
        return out


def pad(s, width):
    """按显示宽度补空格 —— 中日韩字符占两列，%-Ns 对它们算不准。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


def parse_preamble(b):
    """解析 :WAVeform:PREamble? 的 346 字节 WAVEDESC。偏移量已在 SDS824X HD 实测验证。"""
    if len(b) < 188:
        raise RuntimeError(f"preamble 只有 {len(b)} 字节，短于预期 346")
    return {
        "points":        struct.unpack("<i", b[116:120])[0],
        "vertical_gain": struct.unpack("<f", b[156:160])[0],   # 不含探头倍率
        "vertical_off":  struct.unpack("<f", b[160:164])[0],   # 不含探头倍率
        "code_per_div":  struct.unpack("<f", b[164:168])[0],
        "adc_bit":       struct.unpack("<h", b[172:174])[0],
        "dt":            struct.unpack("<f", b[176:180])[0],
        "t0":            struct.unpack("<d", b[180:188])[0],
    }


# ======================================================================= 子命令

def cmd_idn(_, sc):
    print(f"主机      : {sc.host}:{DEFAULT_PORT}")
    print(f"*IDN?     : {sc.ask('*IDN?')}")
    print(f"*OPC?     : {sc.ask_safe('*OPC?')}")
    errs = sc.drain_errors()
    print(f"错误队列  : {'干净' if not errs else f'排空了 {len(errs)} 条: ' + '; '.join(errs)}")


CHAN_FIELDS = [("SWITch", "开关"), ("SCALe", "V/div"), ("OFFSet", "偏置"),
               ("COUPling", "耦合"), ("PROBe", "探头"), ("BWLimit", "带宽限制"),
               ("UNIT", "单位")]


def cmd_state(_, sc):
    print(f"# {sc.ask('*IDN?')}\n")
    print("## 通道")
    hdr = "  {:<4}" + "".join("{:>10}" for _ in CHAN_FIELDS)
    print(hdr.format("CH", *[n for _, n in CHAN_FIELDS]))
    for ch in range(1, 5):
        vals = [sc.ask_safe(f":CHANnel{ch}:{f}?", "-") for f, _ in CHAN_FIELDS]
        print(hdr.format(f"C{ch}", *vals))

    print("\n## 时基 / 触发 / 采集")
    for label, cmd in [
        ("时基 s/div",    ":TIMebase:SCALe?"),
        ("时基延迟",      ":TIMebase:DELay?"),
        ("触发状态",      ":TRIGger:STATus?"),
        ("触发类型",      ":TRIGger:TYPE?"),
        ("触发模式",      ":TRIGger:MODE?"),
        ("边沿源",        ":TRIGger:EDGE:SOURce?"),
        ("边沿电平",      ":TRIGger:EDGE:LEVel?"),
        ("边沿斜率",      ":TRIGger:EDGE:SLOPe?"),
        ("采集模式",      ":ACQuire:MODE?"),
        ("采集类型",      ":ACQuire:TYPE?"),
        ("存储深度",      ":ACQuire:MDEPth?"),
        ("采样率",        ":ACQuire:SRATe?"),
        ("屏幕点数",      ":ACQuire:POINts?"),
        ("分辨率",        ":ACQuire:RESolution?"),
        ("采集运行中",    ":ACQuire:STATe?"),
        ("波形取数通道",  ":WAVeform:SOURce?"),
        ("波形传输位宽",  ":WAVeform:WIDTh?"),
        ("单次最大点数",  ":WAVeform:MAXPoint?"),
    ]:
        print(f"  {pad(label, 16)}: {sc.ask_safe(cmd, '-')}")

    errs = sc.drain_errors()
    if errs:
        print(f"\n## 错误队列（已排空 {len(errs)} 条）")
        for e in errs:
            print(f"  {e}")


def cmd_shot(a, sc):
    png = sc.ask_png()
    with open(a.out, "wb") as f:
        f.write(png)
    print(f"已保存 {a.out}  ({len(png)} 字节)")
    errs = sc.drain_errors()
    if errs:
        print("错误队列:", "; ".join(errs), file=sys.stderr)


def cmd_wave(a, sc):
    ch = a.channel.upper()
    if not ch.startswith("C"):
        ch = "C" + ch
    n = ch[1:]

    probe = float(sc.ask(f":CHANnel{n}:PROBe?"))     # 坑：preamble 不含探头倍率

    # 这几条都是写操作，必须走 set_verified —— 直接 write 再读 preamble 会被静默
    # 丢弃，导致读错通道或拿到 8-bit 数据，而且没有任何报错。
    # STARt/POINt 必须**显式**设定，不能沿用示波器的残留状态：实测残留的
    # POINt=5000000 会让 :WAVeform:DATA? 在只有 500 点可用时直接挂死到超时。
    # POINt 0 = 取当前采集的全部可用点。
    cfg = [f":WAVeform:SOURce {ch}",
           ":WAVeform:WIDTh WORD",
           f":WAVeform:STARt {a.start or 0}",
           f":WAVeform:POINt {a.points if a.points is not None else 0}"]
    bad = [(s, got) for s, ok, got in set_many_verified(sc, cfg) if ok is False]
    if bad:
        for s, got in bad:
            print(f"  设置失败: {s}  读回 {got}", file=sys.stderr)
        sys.exit("scope: 取数参数未生效，读出的数据会不可信，已中止。")

    pre = parse_preamble(sc.ask_block(":WAVeform:PREamble?"))
    raw = sc.ask_block(":WAVeform:DATA?")

    ncode = len(raw) // 2
    codes = struct.unpack(f"<{ncode}h", raw[:ncode * 2])
    gain, off, cpd = pre["vertical_gain"], pre["vertical_off"], pre["code_per_div"]
    dt, t0 = pre["dt"], pre["t0"]

    if a.out.endswith(".npz"):
        # 长记录走二进制：4M 点写 CSV 要 ~120MB 且耗时几十秒，npz 不到 1 秒。
        import numpy as _np
        _v = (_np.asarray(codes, dtype=_np.float64) * gain / cpd - off) * probe
        _np.savez(a.out, v=_v, dt=dt, t0=t0)
    else:
        with open(a.out, "w") as f:
            f.write("time_s,volt_V\n")
            for i, c in enumerate(codes):
                v = (c * gain / cpd - off) * probe    # 乘探头倍率 = 探针尖端真实电压
                f.write(f"{t0 + i * dt:.12g},{v:.9g}\n")

    volts = [(c * gain / cpd - off) * probe for c in codes]
    print(f"通道        : {ch}  (探头 {probe:g}X)")
    print(f"点数        : {len(codes)}")
    print(f"采样间隔    : {dt:.6g} s  ({1/dt/1e9:.3f} GSa/s)")
    print(f"时间跨度    : {len(codes)*dt*1e6:.6g} us  (t0={t0:.6g} s)")
    print(f"ADC 位宽    : {pre['adc_bit']} bit   传输位宽 WORD(16bit)")
    print(f"电压范围    : {min(volts):.6g} ~ {max(volts):.6g} V   Vpp={max(volts)-min(volts):.6g} V")
    print(f"已保存      : {a.out}")
    errs = sc.drain_errors()
    if errs:
        print("错误队列:", "; ".join(errs), file=sys.stderr)


def cmd_meas(_, sc):
    mode = sc.ask_safe(":MEASure:MODE?", "-")
    print(f"测量模式: {mode}")
    if not mode.upper().startswith("ADV"):
        print(f"  简单测量源: {sc.ask_safe(':MEASure:SIMPle:SOURce?', '-')}")
        print("  注：SIMPle 模式下未配置的测量项返回 'The number of measurements is zero'")
        return

    # 固件没有 per-slot 的启用状态查询（:P<n>:STATe? 实测不支持），
    # 所以无法区分「未配置」和「配置了但信号无效」—— 两者都返回 ****。
    live, dead = [], []
    for i in range(1, 13):
        t = sc.ask_safe(f":MEASure:ADVanced:P{i}:TYPE?", None)
        if t in (None, "<不支持>"):
            break
        v = sc.ask_safe(f":MEASure:ADVanced:P{i}:VALue?", "-")
        src = sc.ask_safe(f":MEASure:ADVanced:P{i}:SOURce1?", "")
        (dead if v.strip("*") == "" else live).append((i, t, v, src))

    if live:
        print("有效读数:")
        for i, t, v, src in live:
            print(f"  P{i:<3} {t:<16} {v:<16} {src}")
    else:
        print("有效读数: (无)")
    if dead:
        slots = ", ".join(f"P{i}({t}/{src})" for i, t, _, src in dead)
        print(f"无读数 (****): {slots}")
        print("  —— 通道关闭、无信号、或该槽位本就没用上；固件不提供槽位启用状态查询")
    errs = sc.drain_errors()
    if errs:
        print("错误队列:", "; ".join(errs), file=sys.stderr)


def cmd_errors(_, sc):
    errs = sc.drain_errors()
    if not errs:
        print('0,"No error"  —— 队列已空')
    else:
        for e in errs:
            print(e)
        print(f"--- 共排空 {len(errs)} 条 ---")
        if any("Queue overflow" in e for e in errs):
            print("注意: 出现 Queue overflow，更早的错误已被丢弃。", file=sys.stderr)


def cmd_q(a, sc):
    try:
        print(sc.ask(a.scpi, timeout=a.timeout))
    except ScopeTimeout:
        sys.exit(f"scope: {a.scpi} 超时无响应 —— 该命令多半不被支持（拼写？）。\n"
                 f"       用 `scope.py errors` 看错误队列确认。")


def _same_value(got, want):
    """比较读回值与期望值。先按数值比，失败再按字符串（忽略大小写/空白）比。"""
    if got is None:
        return False
    try:
        return abs(float(got) - float(want)) <= abs(float(want)) * 1e-6 + 1e-12
    except (TypeError, ValueError):
        return got.strip().upper() == str(want).strip().upper()


def set_many_verified(sc, scpi_list, attempts=3):
    """批量设置并一次性验证。

    为什么不是逐条调用 set_verified：每条都 reconnect + 查错误 + 读回，连接数会翻几倍，
    而这台固件对短时间内的大量 TCP 连接很敏感 —— 实测逐条验证 4 个 WAVeform 参数后，
    紧接着的 :WAVeform:DATA? 会直接挂死到超时。批量写 + 单次 reconnect 可靠得多。

    返回 [(scpi, ok, got), ...]。
    """
    pending = list(scpi_list)
    results = {}
    for _ in range(attempts):
        for scpi in pending:
            sc.write(scpi)              # 连续写，中间不读任何响应
        sc.reconnect()                  # 只断开重连一次，写才落地
        still = []
        for scpi in pending:
            parts = scpi.strip().split(None, 1)
            if len(parts) != 2:
                results[scpi] = (None, None)
                continue
            query, want = parts[0] + "?", parts[1]
            got = sc.ask_safe(query, None)
            if _same_value(got, want):
                results[scpi] = (True, got)
            else:
                results[scpi] = (False, got)
                still.append(scpi)
        pending = still
        if not pending:
            break
    return [(s, *results.get(s, (False, None))) for s in scpi_list]


def set_verified(sc, scpi, attempts=4):
    """写入并读回确认，必要时重试。返回 (成功?, 原值, 现值, 读回查询, 错误列表)。

    写操作**必须**配合 reconnect，否则会被静默丢弃（见 Scope.reconnect 注释）。
    """
    parts = scpi.strip().split(None, 1)
    query = parts[0] + "?" if len(parts) == 2 and not parts[0].endswith("?") else None
    want = parts[1] if len(parts) == 2 else None

    before = sc.ask_safe(query, None) if query else None
    errs, got = [], None
    for _ in range(attempts):
        sc.write(scpi)
        sc.reconnect()                      # ← 关键：断开重连，写才落地
        errs += sc.drain_errors()
        if query is None:
            return None, before, None, None, errs   # 无法读回验证
        got = sc.ask_safe(query, None)
        if _same_value(got, want):
            return True, before, got, query, errs
    return False, before, got, query, errs


def cmd_w(a, sc):
    if a.no_verify:
        sc.write(a.scpi)
        sc.reconnect()
        errs = sc.drain_errors()
        print(f"已发送（未验证）: {a.scpi}")
        for e in errs:
            print(f"  错误: {e}", file=sys.stderr)
        sys.exit(1 if errs else 0)

    ok, before, got, query, errs = set_verified(sc, a.scpi)
    for e in errs:
        print(f"  错误: {e}", file=sys.stderr)

    if ok is None:
        print(f"已发送: {a.scpi}")
        print(f"  该命令没有对应的查询形式，无法读回验证。"
              f"请自行用 `scope.py q` 确认。")
        sys.exit(1 if errs else 0)
    if ok:
        print(f"已生效: {a.scpi}")
        print(f"  {query}  {before} -> {got}")
        return
    print(f"写入失败: {a.scpi}", file=sys.stderr)
    print(f"  {query} 仍为 {got}（原值 {before}），重试 4 次均未生效。", file=sys.stderr)
    print(f"  错误队列是干净的 —— 该固件会静默丢弃写命令，别指望它报错。\n"
          f"  检查：数值是否超出该档位的合法范围？示波器是否被面板操作占用？",
          file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        prog="scope.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"默认 {DEFAULT_HOST}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("idn", help="连通性与身份自检").set_defaults(func=cmd_idn)
    sub.add_parser("state", help="抓全配置快照").set_defaults(func=cmd_state)
    sub.add_parser("meas", help="列出测量槽位及值").set_defaults(func=cmd_meas)
    sub.add_parser("errors", help="排空错误队列").set_defaults(func=cmd_errors)

    p = sub.add_parser("shot", help="屏幕截图")
    p.add_argument("out")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("wave", help="读波形到 CSV")
    p.add_argument("channel", help="C1/C2/C3/C4，也可只写数字")
    p.add_argument("out")
    p.add_argument("--start", type=int, help=":WAVeform:STARt 起始点")
    p.add_argument("--points", type=int, help=":WAVeform:POINt 点数，0=全部")
    p.set_defaults(func=cmd_wave)

    p = sub.add_parser("q", help="任意查询")
    p.add_argument("scpi")
    p.add_argument("--timeout", type=float, default=TIMEOUT)
    p.set_defaults(func=cmd_q)

    p = sub.add_parser("w", help="任意写入（默认写后读回验证，失败自动重试）")
    p.add_argument("scpi")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过读回验证。只用于没有查询形式的命令（如 :RUN）")
    p.set_defaults(func=cmd_w)

    a = ap.parse_args()
    with Scope(a.host) as sc:
        a.func(a, sc)


if __name__ == "__main__":
    main()
