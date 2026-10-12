# my-claude-skills

个人 Claude Code 技能库。所有自建技能集中在此仓库，用 `install.sh` 软链接到
`~/.claude/skills/`；不属于本仓库的外部技能（第三方上游、公司仓库）在 `external.tsv`
登记，由同一个脚本一并安装。

```
my-claude-skills/
├── install.sh                  安装 / 同步 / 体检
├── external.tsv                外部技能登记（不复制进本仓库的那些）
├── hardware/
│   └── eda-design-practice/    ← 一个技能 = 一个含 SKILL.md 的目录
└── software/
    └── ccrelay/
```

## 为什么不能整个仓库软链过去

Claude Code 的个人技能发现是**严格一级**的，只认：

```
~/.claude/skills/<skill-name>/SKILL.md
```

**不会递归扫描子目录。** 如果执行 `ln -s <repo> ~/.claude/skills/my-skills`，Claude 只会去找
`~/.claude/skills/my-skills/SKILL.md`——找不到，于是 `hardware/`、`software/` 下的技能
**一个都不会注册**。`SKILL.md` 之外的子目录（`references/`、`lib/`、`scripts/`）一律被当作
该技能的支持文件，而不是独立技能。

所以分类目录留在仓库里，软链接指向**叶子目录**，由 `install.sh` 自动完成。

## 日常用法

```bash
./install.sh --list       # 只体检并列出，不改动任何文件
./install.sh --dry-run    # 打印将要做什么，不改动任何文件
./install.sh              # 安装 / 同步
./install.sh --force      # 同上，并接管已存在的同名软链接（如旧位置遗留的）
./install.sh --uninstall  # 移除本脚本创建过的全部链接
```

改完之后**重开一个 Claude Code 会话**才会生效（技能在启动时注册）。

脚本只删自己在 `~/.claude/.my-claude-skills.state` 里记过的链接，不会碰你手工建的其他
链接；遇到同名的**真实目录**（而非软链接）时即使 `--force` 也拒绝删除。

### 新增一个技能

```bash
mkdir -p software/my-thing && $EDITOR software/my-thing/SKILL.md
./install.sh
```

目录名即命令名（`/my-thing`），**必须全局唯一**——重名时 `install.sh` 直接报错退出。
建议带域前缀：`k230-flash`、`gl-ci-pipeline`。

两个保留名不要用：`synced`（claude.ai 同步目录）、`anthropic-skills*`。

## 写 SKILL.md 的两条硬规矩

### 1. description 是常驻开销，要当预算花

每个允许自动触发的技能，其 `name` + `description` + `when_to_use` **每一轮对话都在上下文里**，
无论本次会话用不用得上它。这是线性累加的。`install.sh --list` 的 `RESIDENT` 列就是粗估值。

真正的代价不是钱（有 prompt cache），而是**选择精度**：挂着 30 条描述、其中 25 条和当前任务
无关时，误触发和漏触发都会变多。所以——

- description 只写**什么场景该用它**，不写背景、不写步骤正文；
- 定期删废弃技能，比抠 token 更有效。

### 2. 只手动调用的技能，加 `disable-model-invocation`

```yaml
---
name: k230-flash
description: Flash firmware to Kendryte K230 board
disable-model-invocation: true
---
```

效果是**描述完全不进上下文**（零常驻开销），只有你敲 `/k230-flash` 才加载。凡是烧录、部署、
重构这类"只会手动触发"的技能一律加上。

对照表：

| frontmatter | 你能调 | 模型能调 | 描述是否常驻 |
|---|---|---|---|
| 默认 | ✅ | ✅ | **是** |
| `disable-model-invocation: true` | ✅ | ❌ | **否** |
| `user-invocable: false` | ❌ | ✅ | 是 |

## external.tsv —— 为什么有些技能不收进来

有自己上游的技能**不复制进本仓库**，否则会切断上游更新，也切断你把改动推回去的路。
它们只在 `external.tsv` 登记本机路径，`install.sh` 照样为它们建链接。

当前登记：

| 技能 | 上游 | 原因 |
|---|---|---|
| `easyeda-api` | `github.com/easyeda/easyeda-api-skill` | 第三方官方 skill，持续发版 |
| `k230` | `g.a-bug.org:canmv/tools/k230-skills` | 公司 GitLab，团队协作，改动需推回 |

换机器时 clone 路径不同，改 `external.tsv` 第 2 列即可；路径缺失只会提示跳过，
不影响其余技能安装。

## 已知出处

- `hardware/eda-design-practice` 来自 `github.com/huangzhenming/eda-design-practice`
  （22 commits，已于 2026-10-02 并入本仓库，只取文件不取历史；原仓库归档保留）。

## 什么时候改成 plugin

技能超过 ~15 个、或要分享给团队时，可以把仓库改造成 plugin：加 `.claude-plugin/plugin.json`，
技能移到 `skills/` 下，这样一条软链接就能管全部，调用变成 `/<plugin>:<skill>` 自带命名空间。
代价是分类目录没了（plugin 的 `skills/` 同样是一级扫描）、调试路径更绕。
现在技能少，`install.sh` 的方案更直接；需要迁移时成本也很低。

## 注意

**不要把 `~/.claude/` 纳入版本管理**——`settings.json` 里有明文 API token。
本仓库只管 `skills/` 的内容，不碰任何配置文件。
