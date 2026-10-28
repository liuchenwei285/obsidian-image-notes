---
name: obsidian-image-notes
description: Transcribe note images (handwritten notes, textbook or slide screenshots, blackboard photos, homework, formula derivations) into Obsidian-compatible Markdown with LaTeX math, classify them by course, copy the original image into the vault as an attachment, and save a brand-new note that never overwrites, moves, or deletes any existing vault file. 当用户发来一张或多张笔记图片并希望「整理/识别/归档到 Obsidian、笔记库、vault」，或说「把这张图变成笔记」「存进我的 Obsidian」「按课程分类保存」时使用；也用于 dry-run 预演、检查某张图片是否已整理过、配置 vault 路径与课程分类规则。Do not use for editing or reorganizing existing notes, generating new images, or organizing files outside an Obsidian vault.
---

# Obsidian 图片笔记

把用户在当前对话里发来的笔记图片，转成 Obsidian 可用的 Markdown 笔记，按课程分类后**安全新增**到 vault。

## 职责分工（重要）

- **你（模型）负责识别与整理**：直接看图，转写文字、公式、表格、代码、列表，判断课程归属，写出不确定标记。不要调用 OCR 依赖，不要靠猜。
- **脚本负责确定性与安全**：`scripts/obsidian_image_notes.py` 处理 vault 校验、课程分类兜底、文件名清洗、重名后缀、附件哈希去重、路径穿越防护、独占创建写入、失败回滚、报告生成。
- 绝不自己用 shell 往 vault 里写文件（`Set-Content` / `>` / `Copy-Item` 都不行）。所有落盘只能通过该脚本，因为只有它保证「独占创建、永不覆盖」。

## 一次性准备

第一次使用（或换机器、换 vault）时先自检：

```
python "<本Skill目录>\scripts\obsidian_image_notes.py" doctor
```

`doctor` 会报告配置文件、vault 路径、`.obsidian` 是否存在、课程列表、写入权限。若报 `CONFIG_MISSING` 或 `VAULT_UNCONFIGURED`，按错误里的提示执行：

```
python "<本Skill目录>\scripts\obsidian_image_notes.py" init-config --vault "D:\你的vault" --detect-courses
```

配置字段含义见 `references/config.md`。vault 路径也可以由环境变量 `OBSIDIAN_IMAGE_NOTES_VAULT` 提供（优先级高于配置文件，低于 `--vault`）。**未配置时脚本会拒绝写入并给出指引，此时不要退而在别处创建笔记。**

## 收到图片后的工作流

1. **看图转写**。按 `references/transcription.md` 的规范产出 Markdown：忠实还原、保留原有层级、公式用 LaTeX、看不清的地方打明确标记。多张图片先判断是「同一篇笔记的多页」还是「不同主题」：同一篇合并成一篇笔记（保持页序，多张图都作为附件），不同主题分成多篇。
2. **判断课程**。优先级：用户明确说明的课程 > 你在图片里看到的课程名/章节标题 > 配置文件关键词匹配。把前两者分别通过 `--course` 和 `--course-hint` 传给脚本；不要自己拼文件夹路径。判断不了就不传，脚本会放进「待分类」——**这是正确行为，不要猜**。
3. **把正文写到 vault 外的临时文件**（UTF-8，例如会话工作目录下的 `work/note-1.md`）。正文**不要**自己写 frontmatter，脚本会生成并合并；写了也会被安全合并。
4. **调用脚本落盘**（默认就是自动保存，不需要征求用户同意）。单篇：

```
python "<本Skill目录>\scripts\obsidian_image_notes.py" save ^
  --title "if语句与条件判断" ^
  --course "C语言" ^
  --body-file "<临时正文.md 的绝对路径>" ^
  --image "<原图绝对路径>" ^
  --tag "控制结构" ^
  --uncertain "第3行的手写公式无法辨认" ^
  --json
```

   多篇（推荐用于一次发多张图）：把笔记数组写进一个 job JSON，每篇含 `title` / `course` 或 `course_hint` / `body_file` / `images` / `tags` / `uncertain`，然后：

```
python "<本Skill目录>\scripts\obsidian_image_notes.py" save --job "<job.json>" --json
```

   Windows 下换行请用反引号或写成一行；`--json` 必须加，你要解析它的输出。
5. **读脚本返回的 JSON 并向用户汇报**。`ok` 为 `false` 时不要声称保存成功；把 `error.code`、`error.message`、`error.hint` 如实转达。`status` 为 `skipped_duplicate` 表示这张图以前整理过，要把 `duplicate_of` 里的笔记路径告诉用户。
6. **不要动原图**：不移动、不重命名、不删除用户提供的图片；脚本只读取它并复制一份到附件目录。

### 常用参数

| 目的 | 参数 |
| :-- | :-- |
| 只预演不写入（用户说「先看看会存哪」「dry-run」「别真写」） | `--dry-run` |
| 用户明确要求重复整理同一张图 | `--allow-duplicate` |
| 公式/代码块校验有 error 时拒绝写入 | `--strict` |
| 失败时保留已写成功的部分（默认整批回滚） | `--keep-on-error` |
| 保存完整 JSON 报告备查 | `--report-file "<路径>"` |
| 落盘前先查图片是否整理过 | `check-images "<图片>" --json` |
| 只体检 Markdown / LaTeX，不写入 | `validate "<文件>" --json` |

## 保存报告（每次都必须输出）

脚本返回的 `human_report` 已经是可直接转述的中文报告。向用户汇报时至少覆盖这四点，用简洁列表，不要贴整段 JSON：

1. **保存路径**：vault 内相对路径（多篇就逐条列出最终文件名）。
2. **分类结果**：归到哪个课程、依据是什么（用户指定 / 图片中的课程名 / 关键词匹配 / 无法确定→待分类）。
3. **是否发生重名处理**：笔记或附件是否被改名（`标题 (2).md`、`图 (2).png`）、原图文件名是否因非法字符被清洗。没有就明确说「无」。
4. **是否有未能识别的部分**：`has_uncertain` 为 true 时列出打了标记的位置；同时转达校验告警（公式未闭合等）与需要用户确认的事项（新课程文件夹、待分类笔记）。

## 硬性安全规则（不可违反）

- 只**新增**文件。永不覆盖、修改、删除、移动、重命名 vault 里任何已有文件，也不修改 `.obsidian/` 配置、插件或 git 状态。
- 所有写入只能落在 vault 内。脚本报 `PATH_ESCAPE` / `SYMLINK_ESCAPE` / `NOT_A_VAULT` / `VAULT_UNCONFIGURED` 时，修好配置再重试，**不要**绕过脚本直接写文件，也不要改用户没让你改的配置。
- 课程无法确定 → 放进「待分类」并在报告里提示，不要猜。
- 图片内容看不清 → 打标记（`==[?不确定: ...]==` 或 `> [!warning] 待核对`），不要补全、不要改写原意、不要顺手「修正」你认为错的内容。
- 临时文件（正文、job、报告）写在 vault **外面**，不要污染笔记库。
- 不把笔记内容或图片发给任何外部服务。

## 参考

- `references/transcription.md`：转写与 LaTeX / Obsidian 渲染规范、不确定标记写法、笔记结构模板。
- `references/config.md`：配置文件全部字段、课程规则写法、错误码与排查。
- `tests/test_obsidian_image_notes.py`：59 个真实测试用例，`python tests/test_obsidian_image_notes.py` 可直接运行。