# EasyEDA 运行态 API 实测坑

⚠ **每条都标客户端版本。** 版本变了行为可能变 —— 见下面「已被证伪」那几条。

来源：ML307H_LCEDA / canmv_serdes（3.2.186）、daikin-rcu（4.1.60）

---

## 通道与连接

### 批量写必须可重入 + 断线自动重连（4.1.60）

**现象**：EDA 扩展的 WebSocket 每隔几十秒断一次（close code **1005**）再自动重连，
每次换一个新的 windowId。daikin-rcu 一晚断了 **7 次**，长批量必被打断。

**对策**（两条缺一不可）：

1. `run()` 层做**断线重连重试**：命中 `disconnected` / `timed out` 就清掉缓存的端口和
   windowId，等一下重试
2. 调用方**必须可重入**：连线前**循环清线直到为零**再重建

⚠ **重试 ≠ 幂等。** HTTP 500 是「未知状态」，写操作可能已经生效。
不清就重试 → 重复图元 → **网表看不出来，但 PCB 会乱**。

⚠ 清线要**循环到零**，删除有滞后，跑一遍不够（daikin-rcu 实测残留 50 根）。

### 导线回读是「线段表」，写入是「折线」（4.1.60）

**两种格式，方向相反，且单段时长得一模一样** —— 这是最容易看错的一条：

| | 格式 | 例 |
|---|---|---|
| `getAll()` 回的 `w.line` | **线段表**，每 **4 个数**一段 `[x1,y1,x2,y2]`，段间不连续 | 12 个数 = 3 段 |
| `create(path)` 收的 | **折线**，连续点 | L 形 = 6 个数 |

判据（daikin-rcu P4 2026-09-28）：按 4 切，62 段**全部**轴对齐；
按 2 当折线切，85 段里有 9 段是斜的 —— **原理图里根本不存在斜线**，
所以「出现斜线」就是把格式读错了。

后果两条：

1. 把回读的 line 原样丢回 `create()`，**凡是分叉的线一律 `create failed!`**
   （T 形连接、一点引出多路的地线都是分叉的）。→ **逐段建**。
2. 按 2 步进算出来的线段是**虚构的**，拿去做碰撞/朝向判断会自信地判错。

### `create failed!` 的两个真因（4.1.60）

这个错**不告诉你原因**。已知两种：

1. **坐标和已有图元重合** —— 清线没清干净时必现
   （清的时候要删**所有非 part** 图元，只挑 netflag/netport 会留残渣）
2. **器件刚被移动，编辑器还在更新** —— `modify` 位置之后要 **sleep ≥3s** 再建线
3. **连续批量写时的瞬时状态**（daikin-rcu 2026-09-28 新增）——
   **和几何无关**。同一份 62 条线段：单独跑 **62/62 成功**；
   跟在 `place()` + 清线后面跑只成 4 条，而且形态是「某条开始往后**全挂**」，
   每次跑断点还不一样（4 / 10 / 15）。
   ⚠ 别去找坐标的毛病 —— 我为此查了三轮几何，全是白查。
   **对策：逐条退避重试**（1s、2s、3s）。加上重试后同一份数据 **62/62 一次过**。

（canmv_serdes 3.2.186 还记过第三种：引脚带「不连接」标记时往上建线必报此错，
且不提示是 NC 挡的。摘标记走 `pin.toAsync()` → `a.noConnected=false` → `await a.done()`）

### 窗口选择（4.1.60）

`/eda-windows` 回的键是 **`windowId`**，不是 `id`。
且 `connected:false` 的是重启前留下的**僵尸条目**，选中它会报「no longer connected」。

**僵尸窗口占着 active 槽时，在 EDA 里点「重新连接」没用** —— 症状是另一个窗口的 uuid
每次轮询都在变（反复重连又被踢）。**解法是重启 bridge 进程**。

### 读之前必须先 `openDocument`（4.1.60）

工程信息里明明有图页，但 `dmt_Schematic.getCurrentSchematicInfo()` 回 **null**、
`getCurrentSchematicAllSchematicPagesInfo()` 回 **[]** —— 因为编辑器还没打开那张图。

`pcb_*` 命名空间在工程没打开时**整个消失**；图元域加载有滞后，
`getAll()` 返回 null 或 0 就是还没好 —— 读之前先轮询到器件数非零。

---

## 原理图

### 坐标系

原理图 API **y 向上为正**，文件格式 **y 向下为正**（`api_y = -file_y`）。
单位 0.01 inch，栅格 10，**不落栅格网表里就是断的**。
PCB 侧两个坐标系一致，别混。

**引脚的 `x,y` 是接线点（外端），不是符号中心。**

### 引脚自带 `rotation`，就是它的向外方向（4.1.60）

`getAllPinsByPrimitiveId()` 回的引脚对象有 **`rotation`** 和 `pinLength`，
**不用按几何去猜朝向**：

| pin rotation | 0 | 90 | 180 | 270 |
|---|---|---|---|---|
| 向外方向（y 向上） | 右 (1,0) | 上 (0,1) | 左 (−1,0) | 下 (0,−1) |

实测标定（daikin-rcu P1 2026-09-28）：R13 原点 x=1000，脚 2 在 x=1020 且 rot=0、
脚 1 在 x=980 且 rot=180；Q1 的 S 在下方 rot=270、D 在上方 rot=90；U1 左排全 180。

⚠ 这和**网络标记**（netflag/netport）的朝向是**两套不同的约定**，别混：
标记那边「存储值 = (360 − 传入值) % 360」，且每种符号一张表（见 R4）。

### `sch_PrimitiveComponent.create` 的签名（4.1.60）

第一个参数是**器件描述对象**，不是两个字符串（传两个字符串报 HTTP 500「数据不符合规范」）：

```js
create({libraryUuid, uuid}, x, y, subPartName?, rotation?, mirror?, addIntoBom?, addIntoPcb?)
```

传 `rotation:90` 读回是 `270`（不影响结果，对照时别当成没生效）。

### ~~netflag/netport 只能用带网络名的短导线代替~~（3.2.186 记录，**4.1.60 证伪**）

**canmv_serdes 记的原文**：「netport / netflag 器件 `sch_PrimitiveComponent.create` 会挂死，
要新加网络标记只能用带网络名的短导线」。

**daikin-rcu 证伪**：有**专用入口**，工作正常：

```js
sch_PrimitiveComponent.createNetFlag(ident, net, x, y, rot?, mirror?)
   ident ∈ 'Power' | 'Ground' | 'AnalogGround' | 'ProtectGround'
sch_PrimitiveComponent.createNetPort(dir, net, x, y, rot?)
   dir ∈ 'IN' | 'OUT' | 'BI'
```

**netflag / netport 的引脚就在它自己的原点上**，直接放到支线末端就连上。

→ 教训：**「不能 X」型的禁令如果说不出机理，很可能只是没找到正确入口。**
见 `GOVERNANCE.md`。

### 网络标记的 rotation：存储值是传入值取负（4.1.60）

`createNetFlag` / `createNetPort` 实测：**存储 rotation = (360 − 传入) % 360**
（传 0→0、90→270、180→180、270→90，五种符号一致）。
生成和回读检查必须用两张表，混用会自相矛盾。完整朝向表见
`schematic-readability.md` 的 R4。

### `modify` 的边界（3.2.186）

- 只认 `componentType === "part"`。对 sheet / netflag / netport 报
  「仅当器件类型为元件时允许使用该函数进行修改」
  → **后果：网络标记的朝向只能在 `createNetFlag/createNetPort` 的参数里定，
    事后改不了，要改只能删了重建。**
- 能改 `designator` / `name` / `otherProperty` / `x` / `y` / `rotation`，**改不了符号**
- `otherProperty` 要**自己先读出来合并再整份写回**，别只塞新键
- 要改图框标题栏的「版本」这类字段，从 `getDocumentSource()` 找到那条 ATTR 的 id，
  走 `sch_PrimitiveAttribute.modify(id, {value})`

### 页面管理（4.1.60）

`dmt_Schematic.createSchematicPage()` 的参数是**图纸 uuid，不是页名**。
传页名或 `{name:…}` 都**静默返回 None、什么也不建**（不报错，最坑）。

正确用法：`createSchematicPage()` → 返回新页 uuid（页名自动分配，会和已有页重名）
→ `modifySchematicPageName(pageUuid, name)` 改名。
**建了先读回 `getCurrentSchematicAllSchematicPagesInfo()` 核对，别信返回值。**

### `sch_PrimitiveText` 的正文字段叫 `content`

不是 `value`；但同一段文字在**文件格式**里的 TEXT 记录用的是 `value`。两边别混。

文字落位前要**先量空白带**：按「行数 × 行距」算一遍再写，别写完才发现盖住器件。

---

## 库

### 自建符号：目前走不通（4.1.60）

| 入口 | 结果 |
|---|---|
| `sch_PrimitivePin.create` 等 | 是**图页**的 API，符号编辑态用不了 |
| `lib_Symbol.openInEditor()` | **恒返回 False** |
| `sys_FileManager.getDocumentSource()` | **arity 0，忽略参数，只回当前打开的文档** |
| `lib_Symbol.copy()` | **恒返回 None**（3/4/5 个参数都试过） |
| `lib_Symbol.create()` | ✅ 能建空符号 |
| `lib_Symbol.updateDocumentSource()` | **恒返回 False**，不报原因 |
| `lib_Device.copy()` | ✅ 可用，但目标库要传**个人库 uuid** |

⚠ `getProjectLibraryUuid()` 回的是**字面量 `"project"`** 而不是 uuid，
拿它当 `copy()` 的目标会报 `[object Object]`。

**读符号源码的可行办法**：把母版器件**放到图页上** → 导出 `.epro2` → 解包 `.epru`
→ 按 `DOCHEAD` 切段，找 `docType:"SYMBOL"` 且含 `"type":"PIN"` 的那段。
格式是行式记录 `{头JSON}||{体JSON}|`。**读得到、改得对，但写不回去。**

→ **结论：符号目前只能在 GUI 里建**（符号向导，24 脚 IC 约 2 分钟）。

### `lib_Footprint.get()` 不回焊盘几何

只回 name / classification / description。
**封装尺寸只能从名字里编码的字段读**（`QFN-24_L4.0-W4.0-P0.50-BL-EP2.6`）。
`getRenderImage()` 只给 PNG Blob。

### `lib_Device.search()` 不能用来选型

它只搜器件库的**名字**，搜 "SMA" 只回一个二极管，漏得离谱。
**选型要用商城接口**，见 `component-selection.md`。

---

## 文件系统

### `sys_FileSystem` 的路径授权会随客户端重启丢失

表现：`saveFileToFileSystem` 回 `{'error':'路径不允许'}`、
`existsPathInFileSystem` 对确实存在的目录回 False、`getEdaPath()` 回空串。

**绕法**（实测 2.4MB 的 .epro2 能过）：

```js
const b = new Uint8Array(await f.arrayBuffer());
let s=""; const CH=0x8000;
for(let i=0;i<b.length;i+=CH) s += String.fromCharCode.apply(null, b.subarray(i,i+CH));
return btoa(s);   // Python 侧 base64 解码写盘
```

⚠ 必须分块 `String.fromCharCode.apply`，否则爆栈。

### 网表导出必须**一次调用内取完**（4.1.60）

分两步、用 `globalThis.__nl` 传递会挂：两次调用之间 WebSocket 会重连
（本机每隔几十秒一次），`globalThis` 就不是同一个上下文了，第二步报
`Cannot read properties of undefined (reading 'arrayBuffer')`。
→ `getNetlistFile` + `arrayBuffer` + `btoa` 写在同一个片段里。

⚠ 观察到 3 次（机理未证）：**`getNetlistFile` 返回 null 时，图上往往有真实错误**
（引脚重合之类）；换回已验证的好布局就立刻能导出。
→ 导不出来先别怀疑客户端，先查图。

### 网表导出有缓存

`sch_ManufactureData.getNetlistFile` 改完属性立刻导会看到上一版的值。
**等 1~2 秒再导，或导两次对比。**

⚠ `getNetlistFile` 的 name 参数是幌子，它导的是「**当前激活的那块板**」——
不先 `open_page` 会拿到上一块板的网表。

### SVG/PDF 导出器有「已删文字缓存」

文字删了改了、`getDocumentSource()` 都已经干净，导出的图里**还是旧的**。
切走切回该页没用，**只有重启客户端才清**。
→ 要拿给人看的 PDF，务必重启客户端后再导。

---

## PCB（版本 3.2.186，未在 4.1.60 复验）

- `pcb_Layer.*PhysicalStacking*` 全家是**哑的**（v4.2 才有）。物理叠层只能在层管理器里点；
  要读就从 `getDocumentSource()` 解 `LAYER_PHYS`（`thickness` 单位 **mil**）
- `setTheNumberOfCopperLayers(4)` **只改层数，不动物理叠层**
- **DRC 规则配置有两套序列化**，长得像但不通用。`overwriteCurrentRuleConfiguration()`
  要传 `cfg["config"]` 且必须是**运行态形状**；用错形状**不报错**，自定义模板被静默丢掉
- `createNetClass` / `createEqualLengthNetGroup` 的 color 是 `{r,g,b,alpha}` **对象**，
  且 **alpha 标度是 0~255 不是 0~1**。传 1 会被存成 0.0039（几乎全透明）→
  **凡是进了网络类的网，飞线在 GUI 上全看不见**。
  ⚠ 这类 bug 在 API 侧完全查不出来 —— **回读 `getAllNetClasses()` 看存下来的真实 alpha**
- 建完网络类之后 `getNetRules()` 的行会**重组**（入类的网变成一行 `type:"netClass"`）
- `pcb_Document.importChanges()` 返回 true **但不增删器件，也同步不了网名**。
  器件增删和网名同步**只能在 GUI 里「从原理图更新 PCB」**
- `pcb_PrimitiveComponent.getAllPadsByPrimitiveId` **不存在**，叫 **`getAllPinsByPrimitiveId`**。
  它给的是**焊盘世界坐标**，是标定旋转约定的唯一可靠来源
- `pcb_PrimitivePad.getAll()` 的对象**不带 `designator`**，但 **`net` 是有的**
- 焊盘几何四种形状（单位 mil）：
  `["RECT",w,h,圆角]` `["OVAL",w,h]` `["ELLIPSE",w,h]`
  `["POLYGON",[x0,y0,"L",x1,y1,…]]` ⚠ POLYGON 里是**绝对坐标**，不是相对焊盘中心
- 没有「重新铺铜」接口

---

## 枚举 API 要走原型链

`Object.getOwnPropertyNames(eda.xxx)` 对这些命名空间**回空数组**，
得沿 `Object.getPrototypeOf` 往上走才看得到方法。
