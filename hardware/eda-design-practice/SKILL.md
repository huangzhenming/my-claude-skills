---
name: eda-design-practice
description: >-
  用 AI 做电路板设计时的**工程实践规则与可复用工具**：原理图的可审性规则、
  元器件选型与校验、PCB 的人机分工流程、验收判据、以及 EasyEDA 运行态 API 的实测坑。
  和官方的 easyeda-api skill 互补 —— 那个讲「API 怎么调」，这个讲「怎么调得对、
  怎么画得让人能审、踩过什么坑」。
  Trigger on: "画原理图", "原理图", "PCB", "PCB布局", "布局", "元器件选型", "选型",
  "网表", "网表验收", "封装", "立创EDA", "嘉立创EDA", "EasyEDA", "schematic",
  "netlist", "footprint", "placement", "layout".
  在开始任何一块新板子的原理图或 PCB 工作前，**先读本 skill**。
license: MIT
metadata:
  version: "0.1.0"
  origin: 从 ML307H_LCEDA / canmv_serdes / daikin-rcu 三个项目累积
---

# EDA 设计实践

## 这个 skill 解决什么问题

同一个错在不同项目里重复犯。例如「GND 就近连接」这条，在 `canmv_serdes` 被用户纠正过，
但知识留在了那次对话里，没进任何文档 —— 于是 `daikin-rcu` 的 P1 页上又出现了
23 个独立堆叠的地符号。

**知识必须离开对话，进入可复用的载体。**

## 五条最高优先级的铁律

违反了就是返工，排在所有其它规则之前。

### 1. 改图前先探，别凭记忆或生成器代码写坐标和位号

运行态是唯一真源。读回器件、引脚绝对坐标、导线端点，再动手。

### 2. 改完必须用 EDA 自己导出的网表验收，不看自己的账

判据要硬：

- **不许有单节点的命名网络**（单节点 = 悬空）
- 点对点的网**恰好 2 个节点**
- **未接引脚清单里只允许出现 NC 和明确不用的脚**
- ⚠ **导线条数不是判据** —— 编辑器会合并共线段（建 84 读回 81 是正常的）

`lib/netcheck.py` 做这件事。

### 3. 不要覆盖人的工作

生成脚本天然是破坏性重建器。**布局和画法归人、网表规格归 AI**；人调完跑
`--snapshot` 把位置**和导线**一起回流成规格，之后生成原样重放、只补增量。
破坏性操作要专门开关 + 事前告知。详见 `references/human-ai-handoff.md`。

⚠ 判断归属别问「能不能重算」，要问「**重算出来的是不是同一个东西**」——
网表只约束「连到一起」，不约束「怎么画」。

### 4. 动手之前先有回滚点，而且是**版本节点**，不是导出的文件

**请人在 GUI 里建一个版本节点**（AI 建不了，这是个交接点），别拿 AI 导的
`.epro2` 当唯一安全网 —— 那是离线文件，恢复要重新导入成新工程，
而且它依赖的路径授权**会随客户端重启失效**。详见 `references/backup-and-versioning.md`。

### 5. HTTP 500 / 超时 = 未知状态，不代表没生效

必须 read back 再决定重试。批量写操作**必须可重入**（先清后建），
否则断线重试会产生重复图元 —— **网表看不出来，但 PCB 会乱**。

---

## 工作流

### 原理图

1. 读需求 → 出**网表规格**（`(位号, 引脚) → 网络名` 的表），先给人评审
2. 选型 → 见 `references/component-selection.md`（**选型和校验是两件事，都要做**）
3. 放器件 → 栅格检查 + 重叠检查（`lib/gridcheck.py`）
4. 连线 → 按 `references/schematic-readability.md`：
   **信号网画实线全连**（交叉不用躲），**电源地就近分簇引符号**
5. **导网表验收**（`lib/netcheck.py`）—— 电气判据，硬的
6. **料校验** —— 每个器件有 `supplierId`、`Value` 非空、
   **值和厂商料号解码结果一致**。⚠ 网表**不带值**，这一维度网表查不出来
7. **几何审查**（`lib/overlapcheck.py`）—— 导线穿元件本体 / 穿符号占位。
   **网表查不出来**，必须单独跑
   ⚠ 交叉数也要量（`references/radial-layout.md`）：它不是对错判据，
   但**任何布局/布线改动都必须量它的前后值，没变好就回退**
8. **请人打开看** —— 最终判据始终是人眼

### PCB（人机分工契约）

```
① AI 估面积      器件数 + 裸面积 + 经验密度系数，给出建议板框尺寸
② 人定板框 + lock 关键器件        ← 交接点
③ AI 自动布局    重力/气泡求解 → 区域夹取 → 贪心格点合法化 → 预览
④ 布线   ⚠ `autoRouting()` API **在 4.1.60 是哑的**（Promise 永不返回）；
         自写布线器的经验和失败点见 `references/pcb-workflow.md` 末节
```

★ **② 之后 AI 不越界**：板框和锁定件是人的决定，AI 只在给定边界内求解。
详见 `references/pcb-workflow.md`。

---

## 目录

| 文件 | 内容 |
|---|---|
| `references/schematic-readability.md` | 原理图可审性规则（画给人看，不只是网表对） |
| `references/component-selection.md` | 选型 + **器件校验**（封装/3D模型/引脚方位） |
| `references/terrain-layout.md` | **地形布局**：质量→高度场→峰谷→分水岭→方位等距投影；目标是「自然」不是「少交叉」 |
| `references/radial-layout.md` | **辐射式原理图布局**：主芯片作圆心、卫星按引脚角排开；何时该用、何时别用 |
| `references/pcb-workflow.md` | PCB 四步法与各步的已知陷阱 |
| `references/easyeda-gotchas.md` | 运行态 API 实测坑（**带客户端版本**） |
| `references/acceptance.md` | 验收判据清单 |
| `references/human-ai-handoff.md` | **人机协作协议**：布局和画法归人、调整如何回流 |
| `references/backup-and-versioning.md` | 备份用**版本节点**不用 `.epro2`；两个版本怎么比 |
| `GOVERNANCE.md` | **什么能进这个 skill、什么不能** —— 加新规则前先读 |
| `lib/` | 可复用工具：通道、网表验收、栅格/朝向/几何盒、导线-符号重叠检查 |
| `candidates/` | 候选区：只命中过一次、或机理还不清楚的观察 |

## 加新规则前

**先读 `GOVERNANCE.md`。** 错误提升一条通用规则，比漏掉一条更危险 ——
漏掉只是让人重新推一遍，错误的规则会被自信地照做。
