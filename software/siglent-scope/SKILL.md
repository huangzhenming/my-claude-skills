---
name: siglent-scope
description: >-
  用 SCPI 控制 Siglent SDS800X HD 系列示波器（默认 192.168.2.139:5025）：抓屏看波形、
  读通道/时基/触发配置、把原始采样读成 CSV 做分析、改仪器设置、排查 SCPI 不响应。
  Trigger on: "示波器", "示波器截图", "看波形", "抓波形", "SCPI", "Siglent", "SDS824X",
  "SDS800X", "探头", "触发电平", "时基", "oscilloscope", "scope".
license: MIT
metadata:
  version: "0.1.0"
  device: "SDS824X HD @ 192.168.2.139  (实测固件 3.8.12.1.1.3.8)"
---

# Siglent SDS800X HD 示波器控制

全部操作走 `scripts/scope.py`（raw socket 5025，仅 Python 标准库，无需 pyvisa/NI-VISA）。

```bash
S=~/.claude/skills/siglent-scope/scripts/scope.py      # 安装后的路径

$S idn                        # 连通性 + 身份自检 ← 每次会话第一步
$S state                      # 全配置快照：4 通道 + 时基 + 触发 + 采集
$S shot /tmp/scope.png        # 截图，然后用 Read 工具看图
$S wave C2 /tmp/w.csv         # 读波形到 CSV（已自动处理两个陷阱，见下）
$S meas                       # 当前测量槽位及读数
$S errors                     # 排空错误队列 ← 出任何问题的第一步
$S q ":CHANnel1:SCALe?"       # 任意查询
$S w ":TIMebase:SCALe 1E-6"   # 任意写入，自动断开重连+读回验证+重试

D=~/.claude/skills/siglent-scope/scripts/analyze.py
$D /tmp/w.csv                 # 分析 wave 导出的 CSV（见「读数据」一节）
```

换设备用 `--host 192.168.x.x` 或环境变量 `SCOPE_HOST`。

## 看波形：截图 + Read

`shot` 存 PNG 后**用 Read 工具直接看图**。这是最高带宽的通道 —— 屏幕上有波形形状、
所有档位、触发设置、测量栏读数，一眼全在。用户问「现在波形什么样」「这个振铃正常吗」，
先 `shot` 再 Read，不要只报数字。

截图是 **40~65 KB 裸 PNG，没有 IEEE 488.2 块头**（和手册描述不同）。

## 读数据：CSV 落盘，再用 analyze.py 分析

`wave C2 out.csv` 产出 `time_s,volt_V` 两列，并打印点数/采样率/电压范围摘要。
接着用同目录的 `analyze.py` 做信号完整性分析，**不要每次手搓一次性脚本** ——
多次测量之间算法必须一致才能横向对比：

```bash
$S wave C1 /tmp/w.csv
$D /tmp/w.csv                  # D=.../scripts/analyze.py，全套分析
$D /tmp/w.csv --quiet          # 单行摘要，多次测量做对比表用
$D /tmp/w.csv --probe-cap 15   # 改探头电容假设（影响回路电感估算）
```

输出四块：**电平**（直方图双峰定高低电平，抗振铃）、**时序**（50% 阈值 + 线性插值，
周期 mean/std/min/max、脉宽、占空比）、**边沿**（10–90% 上升/下降）、
**振铃**（峰值、f_ring、衰减时间、回流路径电感估算）。

振铃那块是定位接地问题的关键：**f_ring 低 + 衰减慢 = 回流路径电感大**。
参照值：弹簧地针 5–20 nH，10 cm 长地线夹 100–150 nH。实测到几百 nH 就说明
地线夹悬空或接到了很远的地，回流被迫绕机壳/市电地走。

### 长记录与抖动分析

要统计量（周期抖动、毛刺率、直方图）就拉长时基取长记录，**不要拿几个周期下结论**：

```bash
$S w ":TIMebase:SCALe 200E-6"      # 2ms 窗口 @2GSa/s = 4M 点 ≈ 48000 个 24MHz 周期
$S wave C1 /tmp/w.npz              # 输出 .npz 走二进制：全程 2.5 秒
$D /tmp/w.npz --slot 5.208         # 给出时隙宽度后做量化检验
```

**长记录是本 skill 最有价值的用法**。单次采集同时拿到数量、间隔分布、幅度分布，
往往比配 runt/脉宽触发更省事、信息更全，还不用赌触发条件设对没有。
实测 4M 点读取 1.2 秒（6.7 MB/s），写 .npz 32MB；同样数据写 CSV 要 120MB 且慢得多。

给了 `--slot` 后 `analyze.py` 会：
- **按时隙归类**周期和正脉宽（比固定 bin 的直方图准，见陷阱 9）
- **配对和校验** `T[i]+T[i+1]`，奇偶两种起点分别算 —— 验证"长短成对补偿"这类机制时，
  对齐的那个起点应为单峰，另一个散成多簇

统计精度参考（实测 Siglent SDS824X HD + 2ms 记录）：周期抖动底噪 **9.6 ps**，
长记录线性拟合测频随机误差 **±0.22 ppm**（n=187 周期；n 越多越好）。

**波形数据绝不要读进上下文**——10 M 点约 20 MB。要分析就用 numpy 在文件上做：
FFT、找毛刺、测上升时间、比对两次采集，都比示波器内置功能灵活。
**串行协议（I2C/SPI/UART）也在 PC 上自己解码**，比示波器内置解码可控得多。

单次最多 5 M 点（`:WAVeform:MAXPoint?`），存储深度 10 M；读满需用 `--start/--points` 分段
（**分段读取未实测，首次用请验证点数连续**）。

## 写操作

**允许自由改配置**，不必每次征求同意。但守住四条：

1. **一律用 `scope.py w`，不要手搓 socket 写。** 这个固件会**静默丢弃**写命令
   （错误队列照样报 `No error`），必须「写 → 断开重连 → 读回验证」才可靠。
   `w` 已封装这套流程并自动重试，成功率从 1/6 变成 6/6。细节见陷阱 1。
2. **改之前先 `state` 存原值**，在报告里带上「原值 → 新值」，方便用户让你改回去。
3. **绝不碰**：`*RST`、校准（`*CAL?`）、固件升级、网络配置。
4. **诊断问题时不要拿活跃测量通道当试验品。** 要试探写操作行为，用
   `:WAVeform:STARt` 这类只影响数据传输、不碰显示的参数。拿 `:CHANnel<n>:SCALe`
   反复试会把用户正在看的档位打到最小档（5mV/div），屏幕直接废掉。

用户可能正盯着屏幕调试，改完**主动说明你改了什么**。
发现设置和你上次留下的不一致时，**那多半是用户在面板上操作** —— 不要自动「恢复」，先问。

## 会导致静默错误的陷阱

全部实测撞出来的。前 5 条 `scope.py` 已在工具层封死，第 9 条 `analyze.py` 已加防护，
但你手搓 SCPI 或自己写分析代码时仍会中招。

### 1. 写命令被静默丢弃 —— 最坑

在同一连接上写完后**立即发查询并读响应**，写命令会被丢弃，而错误队列仍报
`0,"No error"`。没有任何信号表明设置没生效。实测成功率：

| 序列 | 成功率 |
|---|---|
| 写 → 等待 → 断开（不读响应） | 6/6 |
| 写 → **断开重连** → 读回验证 | **6/6** |
| 写 → 立即查错误队列 → 断开 | 1/6 |
| 写 → `*OPC?` 同步 → 查错误 → 断开 | 0/6 |

`*OPC?` 同步**在这台固件上不管用**（这是其他 Siglent 型号的常见建议，别照搬）。
唯一可靠的是**断开重连后读回实际值**。永远不要用错误队列判断写是否成功。

### 2. 连接数要省着用

这台固件对短时间内的大量 TCP 连接很敏感。实测：对 4 个 `:WAVeform:*` 参数**逐条**
做「写+重连+验证」后，紧接着的 `:WAVeform:DATA?` 会直接挂死到 30 秒超时；
改成**批量写 + 单次重连 + 批量验证**后，同样的操作 0.66 秒完成且连跑稳定。

要连设多个参数时用 `set_many_verified()`，不要循环调 `set_verified()`。

### 3. `:WAVeform:STARt` / `:POINt` 必须显式设定

不能沿用示波器的残留状态。实测残留的 `POINt=5000000` 会让 `:WAVeform:DATA?`
在只有 500 点可用时挂死。`POINt 0` = 取当前采集的全部可用点。

### 4. 探头倍率 —— 数据最容易悄悄错

| 来源 | 值 | 含探头倍率 |
|---|---|---|
| `:CHANnel2:SCALe?` | `1.00E+00` | ✅ 含 |
| preamble `vertical_gain` | `0.1` | ❌ 不含 |
| `:CHANnel2:PROBe?` | `1.00E+01` | 比值正好是它 |

直接用 preamble 算电压会**小 10 倍且不报任何错**。正确公式（已实测验证）：

```
V_探针尖端 = (code × vertical_gain / code_per_div − vertical_offset) × probe_ratio
```

注意 preamble 的 `vertical_offset` 与 `:CHANnel:OFFSet?` **符号相反**。

### 5. HD 机型默认按 8-bit 传波形

`:WAVeform:WIDTh?` 默认 `BYTE`。不设 `WORD` 就白瞎了 12-bit ADC。

### 6. 不支持的命令静默超时，不报错

实测 `:DECode:BUS1:SWITch?`、`:FUNCtion1:SWITch?`、`:TRIGger:I2C:*`、`:SAVE:*`、`:PRINt?`（不带参数）
等十余条**全部无响应直到超时**。**不要猜命令拼写**——猜错就卡 5 秒。
需要本文没列的命令时，查官方 SDS800X HD Programming Guide，或先用 `q --timeout 2` 试探。

同一命令组里部分子命令可用、部分不可用是常态：`:DECode:BUS1:RESult?` 和 `:FORMat?` 能用，
但同组的 `:SWITch?` `:TYPE?` 不能。

### 7. 错误队列是 FIFO，会溢出

深度实测约 16 条，溢出后返回 `-350,"Queue overflow"` 并**丢弃更早的错误**。
排查前先 `errors` 排空到 `0,"No error"` 再复现问题，否则会被陈旧错误误导。

### 8. 测量值依赖当前测量模式

`:MEASure:MODE?` 为 `ADVanced` 时，`:MEASure:SIMPle:VALue? PKPK` 返回
`'The number of measurements is zero'`——这**不是通信失败**，是「该测量项没配置」。
想测任意参数要先配槽位。固件**没有** per-slot 启用状态查询（`:P<n>:STATe?` 不支持），
所以「未配置」和「配置了但信号无效」都显示 `****`，无法区分。

### 9. 分析类错误不会报错，只会给一个看起来合理的数

这是本 skill 最该记住的一条。一次 24MHz 抖动验证里，分析代码产出过
"突发密度 74%""振铃峰值 124%""抖动比例 9.4%""互补性 std 3716ps" ——
期望值分别是 25% / 20% / 25% / 20ps。**没有一个会抛异常、打警告或崩溃。**

实际踩过的坑，每条都只表现为"数不对"：

| 错法 | 后果 |
|---|---|
| 振铃窗口从 50% 阈值点开始 | 边沿爬升段被当成振铃，峰值虚高 3 倍 |
| 振铃窗口不按下一个边沿截断 | 高频信号把后续整个方波当振铃，报 124% / 170ns |
| 过零显著性阈值只用 `0.15×peak` | 振铃一小就掉进噪声，f_ring 虚高 70% |
| 边沿时间不做插值 | 2GSa/s 下 2ns 边沿只有 4 点，量化误差 ±0.5ns (20%) |
| 边沿窗口用 `len(w)//2` 定位 | 记录首尾截断时错位，下降沿全成 nan |
| 周期参考按"哪组边沿多"选 | 选中下降沿，占空比调制信号的分布被抹平（9.4% vs 25%） |
| 固定 bin 直方图统计比例 | 峰的尾巴被阈值截掉，25% 报成 9.4% |
| 用中间量推算目标量 | 短段数、短间隔数到毛刺周期数的倍率搞错，密度虚高 2–3 倍 |

**唯一有效的防线是这四样，不是仔细检查代码：**

1. **物理自洽性** —— 找一个物理上必须不变的量。接地没动过，回流路径电感就不可能
   从 330nH 变成 109nH；变了就是测量方法失效，而不是物理现象。
2. **独立复算** —— 对同一份数据另写一段代码，用不同的路径算同一个量，比对结果。
3. **对照组** —— 设计一个"应该什么都检不到"的实验。只测检出率说明不了问题，
   配上零假阳性验证才完整。
4. **跨实现交叉验证** —— 让完全独立的一侧（如被测设备的固件逻辑）预测你的测量结果。
   两边不共享代码和假设时，吻合才有说服力。

遇到对不上的数，**先怀疑分析代码，再怀疑被测物**。本次 10 个异常里 10 个都是前者。

## 实测可用的命令清单

本文列出的都在 SDS824X HD 固件 3.8.12.1.1.3.8 上真跑通过。

```
通道   :CHANnel<1-4>:  SWITch? SCALe? OFFSet? COUPling? PROBe? BWLimit? UNIT?
时基   :TIMebase:SCALe? :TIMebase:DELay?
触发   :TRIGger:STATus? TYPE? MODE?   :TRIGger:EDGE:SOURce? LEVel? SLOPe?
       :TRIGger:PULSe:SOURce?  :TRIGger:RUNT:SOURce?  :TRIGger:UART:BAUD?
采集   :ACQuire:MODE? TYPE? MDEPth? SRATe? POINts? RESolution? STATe?
       :ACQuire:INTerpolation? :ACQuire:SEQuence?
波形   :WAVeform:SOURce? STARt? POINt? MAXPoint? WIDTh? INTerval?
       :WAVeform:PREamble?  :WAVeform:DATA?
测量   :MEASure:MODE?  :MEASure:ADVanced:P<1-12>:TYPE? VALue? SOURce1?
       :MEASure:ADVanced:LINenumber?  :MEASure:SIMPle:SOURce?
显示   :DISPlay:TYPE? GRID?        截图 :PRINt? PNG   ← 参数必需
系统   *IDN? *OPC? :SYSTem:ERRor? :SYSTem:DATE?
       :SYSTem:COMMunicate:LAN:IPADdress?
其他   :CURSor:MODE?  :HISTORy:FRAMe? :HISTORy:LIST?
       :FUNCtion1:OPERation? :FUNCtion1:FFT:WINDow? :FUNCtion1:FFT:UNIT?
```

**两种 SCPI 语法并存，一律用上面的标准语法。** 旧式 `C1:VDIV?` 也能跑，但返回
`C1:VDIV 1.00E+00V`（回显命令头 + 带单位），解析脏；标准语法返回纯值 `1.00E+00`。

## 排查

| 现象 | 做什么 |
|---|---|
| 连不上 | `ping 192.168.2.139`；5025/111/80 应开放；确认没有别的程序占着连接 |
| 某命令无响应卡 5 秒 | 命令不被支持。`errors` 看是否 `-113,"Undefined header"` |
| 读出的电压差 10 倍 | 没乘探头倍率。用 `wave` 而不是手搓 |
| 波形精度只有 8 bit | `:WAVeform:WIDTh?` 是 `BYTE`，设 `WORD` |
| 测量返回 `****` 或 `measurements is zero` | 通道关着 / 没配测量槽位，不是通信问题 |
| 错误信息对不上刚发的命令 | 队列滞后或溢出。先 `errors` 排空再复现 |

## 范围外

不做固件升级、校准自检、系统网络配置；不通过 80 端口的 Web 界面操作（raw socket 更直接）。
