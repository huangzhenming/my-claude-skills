# 备份与版本

## B1. 破坏性改动之前，让人建一个版本节点，而不是 AI 导 `.epro2`

**场景**：任何批量写、重建、重布局之前，需要一个能退回去的点。

**机理**：两者根本不是一类东西。

| | 版本节点 | 导出 `.epro2` |
|---|---|---|
| 恢复方式 | 工具自己的回滚点，**原地退回** | 离线文件，要**重新导入成新工程** |
| 活的工程 | 回得去 | **回不去**（导入的是另一个工程，得重新指向） |
| 完整性 | 云端快照**保留已删除的文档** | **丢掉已删除的文档** |
| 比对 | 可按记录逐条 diff（见 B2） | 只能整份比 |
| 依赖 | 云端账号 | `sys_FileSystem` **路径授权** |

最后一行是关键：**导出所依赖的路径授权会随客户端重启丢失**
（`saveFileToFileSystem` 回 `{'error':'路径不允许'}`，`getEdaPath()` 回空串），
得绕 base64 回传才能写盘。**一个自己都可能失效的手段，不该当作安全网。**

**代价**：用 `.epro2` 当唯一备份，事故发生时「有备份」和「能恢复」是两回事 ——
要新建工程导入、再把 PCB/原理图的引用关系接回去，成本远高于点一下回滚。
（daikin-rcu 2026-09-28：一轮里跑了 4 次 `--apply`，页面被逐次改坏；
手上的 `.epro2` 全程没用上，真正让人敢继续往下试的是用户建的 v0.1 节点。）

**出处**：canmv_serdes 2026-09-25（打通版本 API，发现快照比 `.epro2` 更完整）；
daikin-rcu 2026-09-28（用户提出改用版本节点做备份）。

**失效条件**：

- 需要**离线产物**时仍然导 `.epro2` —— 存档、发给别人、进 git、做跨版本的
  文本 diff。两者是互补的，不是替代。
- ⚠ **AI 建不了版本节点。** `eda.*` 没有版本管理命名空间；
  从客户端 JS 包里挖出来的云端接口目前只验证过**读**（见 B2）。
  → **这是一个交接点：AI 必须开口请人去建**，不能默默跳过备份。
  见 `human-ai-handoff.md`。
- ⚠ 版本节点记录的是**已保存状态**，而 `sys_FileManager.getProjectFile()`
  拿的是**实时未保存状态**。→ **建节点前先存盘。**
  （这条是保守建议：「节点是否自动存盘」未逐项实测，先存一次没有代价。）

---

## B2. 怎么把两个版本真的比出来

**场景**：想知道「人这一轮到底改了什么」，或者验证回滚是否干净。

`eda.*` 里**没有**版本管理接口。版本是云端功能，接口是从客户端加载的 JS 包里
搜出来的 —— 沙箱能访问 `window`/`document`，把 `script[src]` 全 fetch 下来 grep
`/api/...(version|history|snapshot)` 就能挖到。**这招对任何「文档没写的云端功能」都能用。**

**立创 Pro 的「版本」其实是分支**（main / v0.1 / v1.0，parent 指向 main）：

```
GET /api/v4/projects/{pu}/branches?page=1&pageSize=50
    ⚠ 默认 pageSize=1，不传参数只回一条 —— 会以为只有一个版本
GET /api/v4/projects/{pu}/branches/{branch_uuid}/histories/{history_uuid}
    → 该 history 及父链，每条带 dataStrUrl(增量) / snapshotUrl(完整快照) / key / iv
GET https://modules.lceda.cn/projects/histories/{uuid}   → 密文
```

**解密：AES-128-GCM**。key 和 iv 都是那两个 32 位十六进制串 **hex 解码成 16 字节**
（不是当 ASCII 用），密文末 16 字节是 tag，明文是 **gzip**，解开即 `.epru` 行式文本。

```
增量的 iv = history 记录的 `iv` 字段（等于 history uuid）
快照的 iv = **`snapshot` 字段那个 uuid**，不是 history uuid   ← 这里最容易卡
```

⚠ **比较时必须过滤三类噪声**，否则会冒出上千条假差异：

1. **云端快照保留着早已删除的文档**（带 `DELETE_DOC` 记录），而 `.epro2` 导出会丢掉
   —— canmv_serdes 有 10 个旧页，不剔掉就是 **512 条**假的「只在旧版本有」
2. **`ELE_PLACEHOLDER` / `FONT` / `EDIT_HEAD`** 是序列化格式的内部产物，
   两种来源天然不一致（实测 **981 条**全是 placeholder）
3. 字段级忽略 `ticket / updateTime / version / editVersion / user / client`

过滤干净之后 canmv_serdes 的 v1.0 vs main 是 **58142 vs 58142 条、只差 3 条**，
且全是 `zIndex` / `strikeout` / `attrs` 这类内部记账，`value` 一字未动。
→ **这个信噪比才是「能用来验收」的标准**；没过滤前的几千条差异毫无意义。

**代价**：不过滤就是几千条假差异，得出「改了很多」的错误结论。

**出处**：canmv_serdes 2026-09-25。参考实现 `scripts/version_diff.py`
（`--branches` / `--delta <版本名>` / `--diff <版本名>`）。

**失效条件**：接口是从压缩 JS 里挖的，**没有兼容性承诺**，客户端升级可能变。
拿不到就退回 `.epro2` 整份比。
