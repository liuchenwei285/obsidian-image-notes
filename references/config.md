# 配置说明（config.json）

配置文件是 UTF-8（无 BOM）的 JSON。改完立即生效，不需要重启 Codex。

## 查找顺序与优先级

**vault 路径**（高 → 低）：

1. 命令行 `--vault`
2. 环境变量 `OBSIDIAN_IMAGE_NOTES_VAULT`
3. 环境变量 `OBSIDIAN_VAULT`
4. `config.json` 的 `vault_path`

**配置文件**（高 → 低）：

1. 命令行 `--config`（指定了就必须存在，**不会**回退到其它配置，以免写错 vault）
2. 环境变量 `OBSIDIAN_IMAGE_NOTES_CONFIG`
3. `<本Skill目录>/config.json`
4. `%USERPROFILE%\.obsidian-image-notes\config.json`

都找不到时报 `CONFIG_MISSING`，并给出 `init-config` 命令。模板见 `config.example.json`。

## 字段一览

| 字段 | 默认 | 说明 |
| :-- | :-- | :-- |
| `vault_path` | `""` | Obsidian 知识库**根目录**（里面有 `.obsidian` 文件夹）。留空则走环境变量 |
| `require_obsidian_dir` | `true` | 目录下没有 `.obsidian` 就拒绝写入，防止写错地方。测试时可用 `--allow-non-vault` 临时绕过 |
| `strict_symlinks` | `true` | 禁止穿过符号链接/junction 目录写入。即使设为 `false`，链接解析后指向 vault 外仍会被拒绝 |
| `notes_root` | `""` | 笔记根目录（相对 vault）。`""` = vault 根目录；例如 `"课程笔记"` 则课程文件夹建在它下面。可写多级 `课程/理科` |
| `unknown_course_folder` | `"待分类"` | 无法确定课程时使用的文件夹 |
| `allow_new_course` | `true` | 允许为配置里没有的课程自动建文件夹（会在报告里提示你确认）。设 `false` 则一律进「待分类」 |
| `attachments.mode` | `"subfolder"` | `course`=附件与笔记同目录；`subfolder`=课程目录下的子目录；`global`=vault 内统一目录 |
| `attachments.subdir` | `"attachments"` | `mode=subfolder` 时的子目录名 |
| `attachments.global_path` | `"Attachments"` | `mode=global` 时相对 vault 的目录 |
| `naming.max_filename_len` | `80` | 文件名主干最大字符数，超出截断 |
| `naming.conflict_pattern` | `"{stem} ({n})"` | 重名后缀模板，`n` 从 2 开始递增 |
| `naming.conflict_max_suffix` | `999` | 最多尝试到 `标题 (999).md`，仍冲突则报错（绝不覆盖） |
| `naming.attachment_naming` | `"original"` | `original`=沿用原图名（同名不同内容才加后缀）；`hash`=名字里带 8 位内容哈希，天然不冲突 |
| `naming.max_total_path_len` | `240` | 绝对路径长度上限，避免撞上 Windows 260 字符限制 |
| `classify.enabled` | `true` | 关闭后不再做关键词匹配（只认 `--course` / `--course-hint`） |
| `classify.min_keyword_score` | `2` | 关键词打分的最低门槛，低于门槛判为无法确定 |
| `classify.require_unique_winner` | `true` | 两门课程打分并列时判为无法确定（进待分类） |
| `classify.scan_chars` | `6000` | 关键词扫描的正文长度上限 |
| `dedupe.scan_scope` | `"vault"` | `vault`=全库比对附件内容；`attachments`=只比附件目录；`off`=关闭去重 |
| `dedupe.max_scan_files` | `20000` | 扫描文件数上限，超出则跳过去重并改为新建（宁可多存一份，绝不误判复用） |
| `dedupe.duplicate_image_policy` | `"skip"` | `skip`=同一张图已整理过就不再新建（报告里给出原笔记路径）；`allow`=照常新建 |
| `dedupe.scan_notes_for_source` | `true` | 扫描笔记 frontmatter 的 `source_sha256` 来判断「这张图整理过没有」 |
| `embed.mode` | `"auto"` | `auto`=正文没嵌入原图时自动追加 `## 原图` 小节；`none`=不追加 |
| `embed.style` | `"short"` | `short`=`![[文件名]]`；`path`=`![[课程/attachments/文件名]]`（同名文件多时用 path 更稳） |
| `frontmatter.extra_tags` | `["图片笔记"]` | 每篇笔记都加的标签 |
| `frontmatter.tag_prefix` | `"course/"` | 课程标签前缀，生成如 `course/C语言`。设为 `""` 则不加课程标签 |
| `frontmatter.add_h1_title` | `true` | 正文没有一级标题时自动补 `# 标题` |
| `courses[]` | — | 课程规则，见下 |

## 课程规则写法

```json
{
  "name": "高等数学",
  "folder": "高数",
  "aliases": ["高数", "微积分", "Calculus"],
  "keywords": ["极限", "导数", "积分", "级数", "洛必达"]
}
```

- `name`：课程显示名（写进 frontmatter 的 `course`）。
- `folder`：vault 内的目标文件夹（相对 `notes_root`），**可以和你已有的目录同名以复用旧目录**；支持多级 `理学/高数`；不能含 `..`、绝对路径或 Windows 非法字符。
- `aliases`：别名，和 `name` 同等权重，用于匹配用户说的课程名或图片里出现的课程名。
- `keywords`：章节/术语关键词，用于在用户没说课程、图片里也没有课程名时兜底匹配。

### 匹配算法（确定性）

1. `--course`（用户明确说的）→ 按 `name`/`aliases` 精确或包含匹配；命中即用（`classified_by: explicit`）。没命中则视为新课程（`explicit-new`），报告里提示你确认；课程名含路径分隔符/非法字符时判为无效并进「待分类」（`explicit-invalid`）。
2. `--course-hint`（你在图片里看到的课程名）→ 同上（`hint` / `hint-new`）。
3. 关键词打分：在「标题 + 正文前 `scan_chars` 字」里统计，`name`/`aliases` 每命中一次 **3 分**，`keywords` 每命中一次 **1 分**（ASCII 关键词按词边界匹配，避免 `int` 命中 `print`）。最高分需 ≥ `min_keyword_score` 且不与第二名并列，否则判为无法确定（`keyword` / `ambiguous` / `none`）。
4. 都不成立 → `unknown_course_folder`，`needs_review: true`，报告里提示你手动归类。

## 文件命名与重名（规则固定、可预测）

**清洗**：`< > : " / \ | ? *`、控制字符、以及会破坏 wikilink 的 `# ^ [ ]` 一律替换为 `_`；连续 `_` 合并；首尾空格与点去掉；Windows 保留名（`CON PRN AUX NUL COM1-9 LPT1-9`）前加 `_`；超过 `max_filename_len` 截断（保留扩展名）；整体路径超过 `max_total_path_len` 再截断。

**重名**：笔记 `标题.md` → `标题 (2).md` → `标题 (3).md` …；附件同理 `图.png` → `图 (2).png`。写入一律用独占创建（`O_CREAT|O_EXCL`），即使有并发也**不可能覆盖**已有文件；名字被抢先占用就自动换下一个后缀。

**附件去重**：先按文件大小筛选，再比 SHA-256。内容相同 → 复用已有附件（不新建、不覆盖），笔记里的嵌入指向那个已有文件；内容不同 → 加后缀新建。

**重复图片**：同一张图（内容哈希相同）以前整理过 → 默认跳过新建，报告里给出 `duplicate_of`（原笔记路径）。要重新整理需显式 `--allow-duplicate`。

## 错误码

| code | 含义 | 处理 |
| :-- | :-- | :-- |
| `CONFIG_MISSING` / `CONFIG_INVALID` / `CONFIG_EXISTS` | 配置缺失、JSON 语法错误、已存在 | 按 `hint` 修配置或加 `--force` |
| `VAULT_UNCONFIGURED` | 没配 vault 路径 | 填 `vault_path` 或设环境变量 |
| `VAULT_MISSING` | 路径不存在 | 检查盘符、空格、拼写 |
| `NOT_A_VAULT` | 目录下没有 `.obsidian` | 换成真正的知识库根目录 |
| `PATH_ESCAPE` / `PATH_INVALID` | 路径含 `..`、绝对路径或非法字符 | 修配置里的 `notes_root` / `folder` / 附件目录 |
| `SYMLINK_ESCAPE` | 符号链接/junction 指向 vault 外 | 移除该链接，或（不建议）关掉 `strict_symlinks` |
| `BODY_MISSING` / `BODY_EMPTY` / `BODY_NOT_UTF8` | 正文缺失、为空、编码不对 | 用 UTF-8 重写正文临时文件 |
| `SOURCE_MISSING` / `SOURCE_INVALID` / `SOURCE_UNSUPPORTED` | 原图不存在、0 字节、类型不支持 | 检查图片路径与扩展名 |
| `NAME_EXHAUSTED` | 后缀试到上限仍冲突 | 换标题或调大 `conflict_max_suffix` |
| `PATH_TOO_LONG` | 目录太深，无法生成安全文件名 | 缩短课程名/`notes_root` |
| `COPY_VERIFY_FAILED` | 附件复制后哈希校验不通过（已删除半成品） | 原图可能被占用，稍后重试 |
| `VALIDATION_STRICT` | `--strict` 下公式/Markdown 有 error | 修正后重试，或去掉 `--strict` |
| `WRITE_FAILED` | 系统层写入失败（权限、磁盘满、文件被占用） | 检查 Codex 是否有该目录写权限；关闭正在同步的程序 |

失败时默认**回滚**本次新建的所有文件与空目录（`rolled_back` 字段列出），vault 不会留下半成品笔记。加 `--keep-on-error` 可保留已成功的部分。

## 常见排查

- **`doctor` 全绿但保存失败**：多半是 Codex 没拿到 vault 目录的写权限，或 vault 正被 Obsidian/同步程序（git、OneDrive）占用。
- **笔记跑到「待分类」**：给这门课在 `courses` 里补 `aliases` 和 `keywords`，或直接 `--course "课程名"`。
- **图片在 Obsidian 里显示不出来**：确认 `embed.style`；vault 内有同名图片时改用 `"path"` 更稳。
- **想让附件就放在笔记旁边**：`attachments.mode` 改成 `"course"`。
- **vault 是 git 仓库**：本 Skill 只新增文件、从不执行 git 命令，提交/推送由你自己的同步流程决定。