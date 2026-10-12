#!/usr/bin/env python3
"""analyze.py —— 对 scope.py wave 导出的 CSV 做信号完整性分析（PC 侧，不碰示波器）。

为什么在 PC 侧算而不用示波器内置测量：阈值、迟滞、异常值剔除都能自己控制，
而且结果可复算、可比对。对「量化在固定时隙上」的信号尤其重要。

    analyze.py wave.csv                    # 全套分析
    analyze.py wave.csv --probe-cap 15     # 改探头电容假设（影响回路电感估算）
    analyze.py wave.csv --quiet            # 只输出一行摘要，便于多次测量横向对比

输出分四块：电平 / 时序 / 边沿 / 振铃。振铃那块用来定位接地回路问题 ——
f_ring 低、衰减慢 = 回流路径电感大，通常是探头地线夹太长或没接到被测点的地。
"""

import argparse
import sys

import numpy as np


def load(path):
    """支持 scope.py 产出的 CSV 与 .npz。长记录一律用 npz —— 4M 点 CSV 要 120MB。"""
    if path.endswith(".npz"):
        z = np.load(path)
        v = z["v"]
        return np.arange(len(v)) * float(z["dt"]) + float(z["t0"]), v
    d = np.loadtxt(path, delimiter=",", skiprows=1)
    if d.ndim != 2 or d.shape[1] < 2:
        sys.exit(f"analyze: {path} 不是 time,volt 两列 CSV")
    return d[:, 0], d[:, 1]


def levels(v):
    """用直方图双峰定高低电平，再取各自邻域的中位数 —— 比 min/max 抗振铃。"""
    hist, edges = np.histogram(v, bins=400)
    ctr = (edges[:-1] + edges[1:]) / 2
    mid = len(hist) // 2
    lo_pk = ctr[np.argmax(hist[:mid])]
    hi_pk = ctr[mid + np.argmax(hist[mid:])]
    band = max(0.05, (hi_pk - lo_pk) * 0.05)
    lo = np.median(v[np.abs(v - lo_pk) < band])
    hi = np.median(v[np.abs(v - hi_pk) < band])
    return float(lo), float(hi)


def hist_peaks(x, nbins=200, min_frac=0.02):
    """在一维样本上找聚簇峰：返回 [(中心, 计数, 占比), ...]。

    用于验证"周期量化在固定时隙格点上"这类假设 —— 注入 ±1 时隙抖动应出现 3 个峰，
    ±2 应出现 5 个。直接看 mean/std 看不出多峰，必须看分布。
    """
    if len(x) < 10:
        return [], None, None
    hist, edges = np.histogram(x, bins=nbins)
    ctr = (edges[:-1] + edges[1:]) / 2
    thr = max(hist.max() * min_frac, 2)
    peaks = []
    i = 0
    while i < len(hist):
        if hist[i] < thr:
            i += 1
            continue
        j = i
        while j + 1 < len(hist) and hist[j + 1] >= thr:
            j += 1
        seg = hist[i:j + 1]
        # 簇内按计数加权求中心，比取 argmax 稳
        center = float(np.average(ctr[i:j + 1], weights=seg))
        peaks.append((center, int(seg.sum()), seg.sum() / len(x) * 100))
        i = j + 1
    return peaks, hist, ctr


def noise_floor(v, lo, hi, amp):
    """高/低平台各自的噪声 std。用于判断振铃指标是否已掉进噪声里。

    必须**分开**算 —— 把两个平台拼起来算 std 得到的是 amp/2，不是噪声。
    """
    band = amp * 0.015
    nhi = v[np.abs(v - hi) < band]
    nlo = v[np.abs(v - lo) < band]
    return (float(np.std(nhi)) if len(nhi) > 10 else float("nan"),
            float(np.std(nlo)) if len(nlo) > 10 else float("nan"))


def crossings(t, v, th):
    """50% 阈值穿越点，线性插值到亚采样精度。"""
    ab = v > th
    up = np.where(~ab[:-1] & ab[1:])[0]
    dn = np.where(ab[:-1] & ~ab[1:])[0]
    def interp(i):
        dv = v[i + 1] - v[i]
        return t[i] + (t[i + 1] - t[i]) * (th - v[i]) / dv if dv else t[i]
    return up, dn, np.array([interp(i) for i in up]), np.array([interp(i) for i in dn])


def _cross_at(w, wt, i_before, i_after, level):
    """在 [i_before, i_after] 这一跨越处线性插值求达到 level 的精确时刻。"""
    dv = w[i_after] - w[i_before]
    if dv == 0:
        return wt[i_after]
    return wt[i_before] + (wt[i_after] - wt[i_before]) * (level - w[i_before]) / dv


def edge_time(t, v, idx, rising, lo, amp, span=120):
    """10–90% 边沿时间，线性插值到亚采样精度。

    **必须插值**：2GSa/s 下一个 2.5ns 的边沿只有 5 个采样点，直接取采样点时刻
    的量化误差是 ±0.5ns（20%），足以掩盖 fast/slow slew 这类真实差异。
    插值后分辨率约 50ps。
    """
    t10, t90 = lo + amp * 0.1, lo + amp * 0.9
    lvl_a, lvl_b = (t10, t90) if rising else (t90, t10)
    out = []
    n = len(v)
    for i in idx:
        off = max(0, i - span)
        w, wt = v[off:min(n, i + span)], t[off:min(n, i + span)]
        c = i - off                     # 边沿在窗口中的**真实**索引
        # 不能用 len(w)//2 当边沿位置 —— 窗口在记录首尾会被截断，中点就不是边沿了，
        # 插值会落在两个同电平采样点之间，得到荒谬值或被过滤掉（下降沿曾因此全成 nan）。
        if c < 1 or c >= len(w) - 1:
            continue
        if rising:
            a = np.where(w[:c + 1] <= t10)[0]; b = np.where(w[c:] >= t90)[0] + c
        else:
            a = np.where(w[:c + 1] >= t90)[0]; b = np.where(w[c:] <= t10)[0] + c
        if not len(a) or not len(b):
            continue
        ia, ib = int(a[-1]), int(b[0])
        if ia + 1 >= len(w) or ib < 1:
            continue
        t_a = _cross_at(w, wt, ia, ia + 1, lvl_a)
        t_b = _cross_at(w, wt, ib - 1, ib, lvl_b)
        if t_b > t_a:
            out.append(t_b - t_a)
    return np.array(out)


def ringing(t, v, i0, plateau, amp, cap_f, npts=600, next_edge=None, rising=True,
            noise=None):
    """边沿之后围绕平台电平的阻尼振荡：频率、衰减时间、回路电感估算。

    两个必须做的截断，否则结果完全失真：

    1. **起点**要推到边沿首次到达平台电平处，而不是 50% 阈值穿越点。否则窗口里
       含边沿本身的爬升段 —— 边沿只有 2ns(4 个采样点) 时 v[i0] 可能还在 0.5V，
       减去平台 3.24V 就得到 -2.7V 的假"峰值"，并把衰减判据的 10% 阈值一起抬高。
    2. **终点**要截在下一个边沿之前。否则高频信号(半周期短于窗口)会把后续整个方波
       跳变当成振铃 —— 实测 24MHz 用固定 600 点窗口报出"峰值 124%、衰减 170ns"。
    """
    look = min(i0 + 400, len(v))
    hit = (v[i0:look] >= plateau) if rising else (v[i0:look] <= plateau)
    if hit.any():
        i0 = i0 + int(np.argmax(hit))
    if next_edge is not None and next_edge > i0:
        npts = min(npts, max(8, next_edge - i0 - 2))
    seg = v[i0:i0 + npts]
    st = t[i0:i0 + npts] - t[i0]
    if len(seg) < 20:
        return None
    res = seg - plateau
    pk = float(np.abs(res).max())

    # 只在振铃仍显著的区间内统计过零。衰减完之后信号在平台电平附近随机抖动，
    # 那些噪声过零会把频率估算拉高 —— 实测不做限制会从 84MHz 虚高到 110MHz。
    #
    # 显著性阈值必须同时相对**峰值**和**噪声底**取大者。只用 0.15*peak 的话，
    # 振铃一小（如驱动从 12mA 降到 4mA），0.15*peak 自己就掉进噪声里，limit 延伸到
    # 纯噪声区，过零次数虚增 -> 周期虚短 -> f_ring 虚高。实测 4mA 时虚高到 135MHz，
    # 而同一物理回路 12mA 测得 80MHz；阻尼变大只会让振荡频率略降，不可能升 70%。
    thresh = 0.15 * pk
    if noise and noise == noise:
        thresh = max(thresh, 3 * noise)
    sig = np.where(np.abs(res) > thresh)[0]
    limit = sig[-1] if len(sig) else 0
    zc = np.where(np.diff(np.signbit(res)))[0]
    zc = zc[zc <= limit]

    out = {"peak_v": pk, "peak_pct": pk / amp * 100, "n_zc": len(zc)}
    if len(zc) >= 4:        # 至少 2 个完整振荡周期才给频率
        period = float(np.mean(np.diff(st[zc])) * 2)
        out["f_ring"] = 1 / period if period > 0 else None
        # L ≈ 1/((2*pi*f)^2 * C)
        out["l_loop"] = 1 / ((2 * np.pi / period) ** 2 * cap_f) if period > 0 else None
    env = np.abs(res)
    for j in range(len(env)):
        if env[j] < 0.1 * pk and np.all(env[j:j + 40] < 0.1 * pk):
            out["settle_10pct"] = float(st[j])
            break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--probe-cap", type=float, default=12.0,
                    help="探头输入电容 pF，用于回路电感估算（默认 12）")
    ap.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    ap.add_argument("--slot", type=float, metavar="NS",
                    help="信号源的时隙宽度 ns。给出后检验周期直方图各峰是否对齐到"
                         "该时隙的整数倍（如 RP2040 PIO @192MHz 为 5.208）")
    a = ap.parse_args()

    t, v = load(a.csv)
    dt = float(t[1] - t[0])
    lo, hi = levels(v)
    amp = hi - lo
    if amp <= 0:
        sys.exit("analyze: 找不到两个电平 —— 信号可能是直流、或通道上没有信号")
    up, dn, tu, td = crossings(t, v, lo + amp * 0.5)

    # 周期一律优先用**上升沿**间隔，不能按"哪组边沿多"来选。
    #
    # 对占空比被调制的信号，两者的分布完全不同：上升沿间隔 = 一个完整周期；
    # 下降沿间隔 = 本周期的低电平 + 下周期的高电平，是跨周期的混合量。
    # 实测 RP2040 ±1 时隙抖动（高电平取 floor(L/2) 个时隙）：
    #   上升沿间隔 -> 25% / 50% / 25%   （正确，三个时隙格点）
    #   下降沿间隔 -> 9.4% / 81.2% / 9.4%（错误，低+高的组合把分布抹平了）
    # 早先按 len() 取多数，恰好选中下降沿，险些把错误比例当成结论上报。
    ref = tu if len(tu) >= 3 else td
    per = np.diff(ref) if len(ref) >= 2 else np.array([])
    rt = edge_time(t, v, up, True, lo, amp)
    ft = edge_time(t, v, dn, False, lo, amp)

    if a.quiet:
        f = 1 / per.mean() if len(per) else float("nan")
        print(f"{a.csv}: V_amp={amp:.4f}V f={f/1e3:.4f}kHz "
              f"tr={rt.mean()*1e9 if len(rt) else float('nan'):.2f}ns "
              f"tf={ft.mean()*1e9 if len(ft) else float('nan'):.2f}ns "
              f"OS={(v.max()-hi)/amp*100:.1f}% US={(lo-v.min())/amp*100:.1f}%")
        return

    print(f"# {a.csv}")
    print(f"  {len(v)} 点, dt={dt*1e12:.1f} ps ({1/dt/1e9:.3f} GSa/s), "
          f"跨度 {(t[-1]-t[0])*1e6:.3f} us\n")

    print("## 电平")
    print(f"  V_base {lo*1000:>10.2f} mV      V_top {hi:>8.4f} V      V_amp {amp:>8.4f} V")
    print(f"  Vmin   {v.min():>10.4f} V       Vmax  {v.max():>8.4f} V      Vpp   {np.ptp(v):>8.4f} V")
    print(f"  过冲   {(v.max()-hi)/amp*100:>10.1f} %  ({v.max()-hi:+.3f} V)")
    print(f"  下冲   {(lo-v.min())/amp*100:>10.1f} %  ({v.min()-lo:+.3f} V)")

    print("\n## 时序")
    if len(per):
        print(f"  周期   n={len(per):<4} mean={per.mean()*1e9:.4f} ns  -> f = {1/per.mean()/1e6:.6f} MHz")
        if len(per) > 1:
            print(f"         std={per.std()*1e12:.1f} ps   min={per.min()*1e9:.4f}   "
                  f"max={per.max()*1e9:.4f} ns   pk-pk={np.ptp(per)*1e12:.1f} ps")
    else:
        print(f"  周期   同向边沿不足 2 个，无法测量 —— 加大时基窗口再采")

    if len(per) >= 50 and a.slot:
        # 给了时隙宽度时，直接按时隙归类统计 —— 比固定 bin 的直方图准得多。
        # hist_peaks 用 200 个等宽 bin + min_frac 阈值，峰的尾巴会被截掉，
        # 实测把真实的 25%/50%/25% report 成 9.4%/81%/9.4%。
        slot = a.slot * 1e-9
        kk = np.round((per - np.median(per)) / slot).astype(int)
        print(f"\n  周期按时隙({a.slot:g} ns)归类:")
        for k in sorted(set(kk.tolist())):
            sel = per[kk == k]
            print(f"    {k:+d} 时隙  n={len(sel):<7} {len(sel)/len(per)*100:6.2f}%   "
                  f"mean={sel.mean()*1e9:9.4f} ns   std={sel.std()*1e12:6.1f} ps")
        nz = int((kk != 0).sum())
        print(f"    受扰周期 {nz}/{len(per)} = {nz/len(per)*100:.2f}%")

        # 配对和：验证"长短成对补偿"。对边界未知，奇偶两种起点都算，
        # 对齐的那个应为单峰。
        print(f"  配对和 T[i]+T[i+1]:")
        for off, nm in ((0, "偶起点"), (1, "奇起点")):
            body = per[off:]
            body = body[:len(body) - len(body) % 2]
            if len(body) < 4:
                continue
            ssum = body.reshape(-1, 2).sum(axis=1)
            ks = np.round((ssum - np.median(ssum)) / slot).astype(int)
            n_cl = len(set(ks.tolist()))
            tag = "  <- 单峰，成对补偿成立" if n_cl == 1 else ""
            print(f"    {nm}  mean={ssum.mean()*1e9:9.4f} ns  "
                  f"std={ssum.std()*1e12:8.1f} ps  簇数={n_cl}{tag}")
    elif len(per) >= 50:
        peaks, _, _ = hist_peaks(per)
        if len(peaks) > 1:
            print(f"\n  周期直方图: 发现 {len(peaks)} 个峰 —— 周期是量化的，不是连续分布")
            base = peaks[len(peaks) // 2][0]
            for c, n, pct in peaks:
                d = (c - base) * 1e12
                print(f"    {c*1e9:>11.4f} ns   n={n:<7} {pct:>5.1f}%"
                      f"   相对中心峰 {d:+9.1f} ps")
            if a.slot:
                slot = a.slot * 1e-9
                print(f"    按时隙 {a.slot:g} ns 检验:")
                for c, _, _ in peaks:
                    k = (c - base) / slot
                    print(f"      {c*1e9:>11.4f} ns -> {k:+.3f} 个时隙"
                          f"   {'✓ 对齐' if abs(k - round(k)) < 0.15 else '✗ 不在格点上'}")
        else:
            print(f"\n  周期直方图: 单峰（无量化抖动），"
                  f"半高宽内 std={per.std()*1e12:.1f} ps")
    # 对每个上升沿找它**之后**的第一个下降沿。不能按下标配对 —— 当记录的第一个
    # 边沿是下降沿时，td[j] 恒早于 tu[j]，按下标配对会全部失配得到 n=0。
    pw = np.array([td[td > x][0] - x for x in tu if np.any(td > x)])
    if len(pw) and len(per):
        print(f"  正脉宽 n={len(pw):<4} mean={pw.mean()*1e9:.4f} ns   "
              f"占空比 = {pw.mean()/per.mean()*100:.3f} %")
        if a.slot and len(pw) >= 50:
            slot = a.slot * 1e-9
            kp = np.round((pw - np.median(pw)) / slot).astype(int)
            if len(set(kp.tolist())) > 1:
                print(f"  正脉宽按时隙归类:")
                for k in sorted(set(kp.tolist())):
                    sel = pw[kp == k]
                    print(f"    {k:+d} 时隙  n={len(sel):<7} "
                          f"{len(sel)/len(pw)*100:6.2f}%   mean={sel.mean()*1e9:9.4f} ns")

    print("\n## 边沿 (10–90%)")
    for name, arr in [("上升", rt), ("下降", ft)]:
        if len(arr):
            print(f"  {name}   n={len(arr):<4} mean={arr.mean()*1e9:.3f} ns"
                  f"   (每边沿仅 {arr.mean()/dt:.1f} 个采样点)")
        else:
            print(f"  {name}   未测到完整边沿")
    print(f"  注: 200MHz 带宽示波器自身上升时间约 1.75 ns，实测值已含此限制")

    n_hi, n_lo = noise_floor(v, lo, hi, amp)
    print(f"\n## 振铃  (C_probe = {a.probe_cap:g} pF，平台噪声 std: "
          f"高 {n_hi*1000:.2f} mV / 低 {n_lo*1000:.2f} mV)")
    truncated = False
    noisy = []
    for name, idx, other, plateau in [("上升沿后", up, dn, hi), ("下降沿后", dn, up, lo)]:
        if not len(idx):
            continue
        nxt = other[other > idx[0]]
        r = ringing(t, v, idx[0], plateau, amp, a.probe_cap * 1e-12,
                    next_edge=int(nxt[0]) if len(nxt) else None,
                    rising=(name == "上升沿后"),
                    noise=(n_hi if plateau == hi else n_lo))
        if not r:
            continue
        if len(nxt) and nxt[0] - idx[0] < 600:
            truncated = True
        nf = n_hi if plateau == hi else n_lo
        ratio = (r["peak_v"] * 0.1) / nf if nf and nf == nf else float("inf")
        flag = "  ⚠噪声限制" if ratio < 3 else ""
        line = f"  {name}  峰值偏离 {r['peak_v']:+.3f} V ({r['peak_pct']:.1f}%)"
        if r.get("f_ring"):
            line += f"   f_ring = {r['f_ring']/1e6:.1f} MHz"
        print(line + flag)
        if r.get("l_loop"):
            print(f"            回路电感 L ≈ {r['l_loop']*1e9:.0f} nH"
                  f"   (弹簧地针 5–20nH / 10cm 地线夹 100–150nH)")
        if r.get("settle_10pct"):
            print(f"            衰减到 <10% 用时 {r['settle_10pct']*1e9:.1f} ns")
        if ratio < 3:
            noisy.append((name, r["peak_v"] * 100, nf * 1000, ratio))
    if len(per):
        half = per.mean() / 2
        print(f"\n  参考: 本信号半周期 = {half*1e9:.1f} ns。振铃衰减时间若接近或超过它，"
              f"\n        下一个边沿到来时振铃尚未衰减完，平顶会被吃掉、波形趋于正弦。")
        if truncated:
            print(f"\n  ⚠ 分析窗口被下一个边沿截断（半周期仅 {half*1e9:.1f} ns）。"
                  f"\n    振铃没衰减完就撞上下一个边沿，f_ring 和衰减时间都只是下限、不可全信。"
                  f"\n    要准确测振铃，请让信号源降频（平台期需 >200ns），再测一次。")
    for name, th_mv, nf_mv, ratio in noisy:
        print(f"\n  ⚠ {name}：10% 判据阈值 {th_mv:.1f} mV 仅为噪声 std {nf_mv:.2f} mV 的 "
              f"{ratio:.1f} 倍。"
              f"\n    振铃已小到接近底噪，**f_ring / 回路电感 / 衰减时间都不可信**"
              f"（过冲百分比仍有效，它是绝对值）。"
              f"\n    自洽性检查：接地不变时 L_loop 必须不变，变了就是本条在作祟。"
              f"\n    要继续量化，开示波器averaging降噪：:ACQuire:TYPE AVERage")


if __name__ == "__main__":
    main()
