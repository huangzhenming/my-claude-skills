#!/usr/bin/env bash
#
# 把本仓库的技能（以及 external.tsv 登记的外部技能）软链接到 ~/.claude/skills/。
#
# 背景：Claude Code 的个人技能发现是**严格一级**的 —— 只认
#     ~/.claude/skills/<skill-name>/SKILL.md
# 不会递归扫描子目录。所以不能把整个仓库软链过去（那样 hardware/、software/ 下的
# 技能一个都不会被注册），必须为每个技能目录单独建链接。本脚本就是干这个的。
#
# 用法：
#   ./install.sh              安装 / 同步
#   ./install.sh --dry-run    只打印将要做什么，不改动任何文件
#   ./install.sh --force      同上，但接管已存在的同名链接（如旧位置留下的）
#   ./install.sh --uninstall  移除本脚本创建过的全部链接
#   ./install.sh --list       只体检并列出技能，不改动
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEST="$CLAUDE_HOME/skills"
# 记录本脚本创建过哪些链接，用于精确清理。
# 放在 skills/ 之外，免得被当成技能目录扫描；属本机状态，不入库。
STATE="$CLAUDE_HOME/.my-claude-skills.state"
EXTERNAL="$REPO/external.tsv"

MODE=install
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)   MODE=dryrun ;;
    --uninstall) MODE=uninstall ;;
    --list)      MODE=list ;;
    --force)     FORCE=1 ;;
    *) echo "未知参数: $arg" >&2; sed -n '/^# 用法/,/^#$/p' "$0" >&2; exit 2 ;;
  esac
done

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
[[ -t 1 ]] || { c_red=; c_grn=; c_ylw=; c_dim=; c_off=; }

warnings=0
note()  { printf '  %s\n' "$*"; }
warn()  { printf '%s! %s%s\n' "$c_ylw" "$*" "$c_off" >&2; warnings=$((warnings+1)); }
fail()  { printf '%sx %s%s\n' "$c_red" "$*" "$c_off" >&2; exit 1; }

# ---- 从 SKILL.md 的 YAML frontmatter 里取一个顶层字段 ----------------------
# 只扫第一个 --- 到第二个 --- 之间，支持 `key: value` 与 `key: >-` / `key: |` 折叠块。
frontmatter_field() {
  local file=$1 key=$2
  awk -v key="$key" '
    NR==1 && $0!="---" { exit }                 # 首行不是 --- 则无 frontmatter
    NR==1 { infm=1; next }
    infm && $0=="---" { exit }
    !infm { next }
    {
      if (collecting) {
        if ($0 ~ /^[A-Za-z_][A-Za-z0-9_-]*:/) { collecting=0 }   # 遇到下一个顶层键
        else { sub(/^[ \t]+/, ""); buf = buf " " $0; next }
      }
      if (index($0, key ":") == 1) {
        val = substr($0, length(key)+2)
        sub(/^[ \t]+/, "", val); sub(/[ \t]+$/, "", val)
        if (val == ">-" || val == ">" || val == "|" || val == "|-") { collecting=1; buf=""; next }
        gsub(/^["'"'"']|["'"'"']$/, "", val)
        buf = val; exit
      }
    }
    END { sub(/^[ \t]+/, "", buf); print buf }
  ' "$file"
}

# 粗估常驻 token：非 ASCII 字符按 1 token，ASCII 按 4 字符 1 token。
# 只用于相对比较，不是精确值。
est_tokens() {
  LC_ALL=C.UTF-8 awk '{
    n=length($0)
    for (i=1;i<=n;i++) { ch=substr($0,i,1); if (ch ~ /^[\x00-\x7F]$/) a++; else c++ }
  } END { printf "%d", int(a/4) + c }' <<<"$1"
}

# ---- 收集技能：name <TAB> path <TAB> origin ---------------------------------
collect() {
  # 仓库内：任意深度的 SKILL.md，其父目录即一个技能
  find "$REPO" -name SKILL.md -not -path '*/.git/*' -print0 2>/dev/null |
    while IFS= read -r -d '' f; do
      d=$(dirname "$f")
      printf '%s\t%s\t%s\n' "$(basename "$d")" "$d" "repo:${d#$REPO/}"
    done

  # 外部登记
  [[ -f $EXTERNAL ]] || return 0
  while IFS=$'\t' read -r name path _upstream _note; do
    [[ -z ${name:-} || $name == \#* ]] && continue
    path="${path/#\~/$HOME}"
    path="$(eval printf '%s' "\"$path\"")"   # 展开 $VAR
    printf '%s\t%s\t%s\n' "$name" "$path" "external"
  done < "$EXTERNAL"
}

# ---- 体检 + 打印清单 --------------------------------------------------------
declare -A seen_name=()
declare -a plan=()

echo
printf '%s%-26s %-9s %-8s %-5s %s%s\n' "$c_dim" "SKILL" "ORIGIN" "RESIDENT" "AUTO" "PATH" "$c_off"
printf '%s%s%s\n' "$c_dim" "$(printf '%.0s-' {1..92})" "$c_off"

while IFS=$'\t' read -r name dir origin; do
  [[ -z $name ]] && continue

  if [[ ! -d $dir ]]; then
    warn "外部技能 '$name' 路径不存在，已跳过：$dir"
    if [[ -f $EXTERNAL ]]; then
      up=$(awk -F'\t' -v n="$name" '$1==n{print $3}' "$EXTERNAL")
      [[ -n $up ]] && note "${c_dim}clone 自：$up$c_off"
    fi
    continue
  fi
  [[ -f $dir/SKILL.md ]] || { warn "'$name' 缺少 SKILL.md，已跳过：$dir"; continue; }

  # 重名 = 致命：两个技能抢同一个 /name
  if [[ -n ${seen_name[$name]:-} ]]; then
    fail "技能重名 '$name'：
      ${seen_name[$name]}
      $dir
    技能名即 /命令名，必须全局唯一。请重命名其中一个目录（或 external.tsv 的第 1 列）。"
  fi
  seen_name[$name]=$dir

  fm_name=$(frontmatter_field "$dir/SKILL.md" name)
  desc=$(frontmatter_field "$dir/SKILL.md" description)
  when=$(frontmatter_field "$dir/SKILL.md" when_to_use)
  dmi=$(frontmatter_field "$dir/SKILL.md" disable-model-invocation)

  # frontmatter 的 name 会覆盖目录名；不一致会让 /命令名 和目录名对不上
  [[ -n $fm_name && $fm_name != "$name" ]] &&
    warn "'$name' 的 frontmatter name 是 '$fm_name'，与目录名不一致 —— 实际命令是 /$fm_name"
  [[ -z $desc ]] &&
    warn "'$name' 没有 description —— Claude 将无法判断何时该用它（只能手动 /$name 调用）"
  # 保留名，不会被加载
  [[ $name == synced || $name == anthropic-skills* ]] &&
    warn "'$name' 是 Claude Code 保留名，不会被加载，请改名"

  auto=yes; tok=$(est_tokens "$desc $when")
  case "${dmi,,}" in true|yes|on|1) auto=no; tok=0 ;; esac

  color=""
  [[ $tok -gt 120 ]] && color="$c_ylw"
  printf '%-26s %-9s %s%-8s%s %-5s %s%s%s\n' \
    "$name" "${origin%%:*}" "$color" "$tok" "$c_off" "$auto" "$c_dim" "$dir" "$c_off"

  plan+=("$name"$'\t'"$dir")
done < <(collect | sort -t$'\t' -k1,1)

printf '%s%s%s\n' "$c_dim" "$(printf '%.0s-' {1..92})" "$c_off"
printf '共 %d 个技能。%sRESIDENT = description 的粗估常驻开销，每轮对话都占着上下文；\n' "${#plan[@]}" "$c_dim"
printf '   AUTO=no 表示设了 disable-model-invocation，零常驻开销、只能手动 /调用。%s\n\n' "$c_off"

[[ $MODE == list ]] && exit 0

# ---- 清理上次装的、这次不再需要的链接 --------------------------------------
# 只动 state 文件里记过的名字，绝不碰你手工建的其他链接。
declare -A want=()
if [[ $MODE != uninstall && ${#plan[@]} -gt 0 ]]; then
  for e in "${plan[@]}"; do want[${e%%$'\t'*}]=1; done
fi

if [[ -f $STATE ]]; then
  while read -r old; do
    [[ -z $old ]] && continue
    [[ -n ${want[$old]:-} ]] && continue
    link="$DEST/$old"
    [[ -L $link ]] || continue
    if [[ $MODE == dryrun ]]; then
      printf '%s- 将移除%s %s\n' "$c_red" "$c_off" "$link"
    else
      rm -f "$link"; printf '%s- 已移除%s %s\n' "$c_red" "$c_off" "$old"
    fi
  done < "$STATE"
fi

if [[ $MODE == uninstall ]]; then
  rm -f "$STATE"
  echo "已卸载。"
  exit 0
fi

# ---- 建链接 -----------------------------------------------------------------
[[ $MODE == dryrun ]] || mkdir -p "$DEST"
for e in ${plan[@]+"${plan[@]}"}; do
  name=${e%%$'\t'*}; dir=${e#*$'\t'}
  link="$DEST/$name"

  # 已存在同名、但不是本脚本管理的链接 —— 默认不覆盖，交给你判断
  if [[ -e $link || -L $link ]]; then
    cur=$(readlink -f "$link" 2>/dev/null || true)
    if [[ $cur == "$(readlink -f "$dir")" ]]; then
      printf '%s= 已是最新%s %s\n' "$c_dim" "$c_off" "$name"
      [[ $MODE == dryrun ]] || echo "$name" >> "$STATE.new"
      continue
    fi
    if ! grep -qxF "$name" "$STATE" 2>/dev/null; then
      if [[ $FORCE -eq 1 ]]; then
        if [[ -L $link ]]; then
          warn "接管 $name：${cur:-<失效>}  ->  $dir"
        else
          # 真实目录/文件，不是链接 —— 绝不删，它可能是你唯一的副本
          warn "$DEST/$name 是真实目录而非软链接，--force 也不会删除它。请先自行处理。"
          continue
        fi
      else
        warn "$DEST/$name 已存在且非本脚本创建，未覆盖。现指向：${cur:-<失效>}
      确认要切换到本仓库版本，就加 --force 重跑。"
        continue
      fi
    fi
  fi

  if [[ $MODE == dryrun ]]; then
    printf '%s+ 将链接%s %-26s -> %s\n' "$c_grn" "$c_off" "$name" "$dir"
  else
    ln -sfn "$dir" "$link"
    printf '%s+ 已链接%s %-26s -> %s\n' "$c_grn" "$c_off" "$name" "$dir"
    echo "$name" >> "$STATE.new"
  fi
done

if [[ $MODE == dryrun ]]; then
  echo
  printf '%s这是 --dry-run，没有改动任何文件。去掉该参数即执行安装。%s\n' "$c_ylw" "$c_off"
else
  if [[ -f $STATE.new ]]; then mv "$STATE.new" "$STATE"; else : > "$STATE"; fi
  echo
  echo "完成。重开一个 Claude Code 会话后，用 / 菜单确认技能已出现。"
fi

if [[ $warnings -gt 0 ]]; then
  printf '%s有 %d 条告警，见上。%s\n' "$c_ylw" "$warnings" "$c_off"
fi
exit 0
