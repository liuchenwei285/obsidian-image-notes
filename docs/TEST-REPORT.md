# Obsidian 图片笔记 Skill — 测试与验证报告

- 日期：2026-10-28（Asia/Shanghai）
- 环境：Windows，Python 3.14.4（仅标准库），Codex desktop
- Skill 安装位置：`%USERPROFILE%\.codex\skills\obsidian-image-notes`
- 测试命令：`python "%USERPROFILE%\.codex\skills\obsidian-image-notes\tests\test_obsidian_image_notes.py"`
- 结果：**59 个用例全部通过，0 跳过，0 失败**（约 1.6 秒）。所有用例都在系统临时目录里新建一次性假 vault，不触碰真实笔记库。

## 需求覆盖矩阵

| 你的要求 | 对应用例 | 结果 |
| :-- | :-- | :-- |
| 正常保存（含 frontmatter、附件、嵌入链接） | test_02, test_49, test_57 | ok |
| 课程分类：明确指定 / 图片课程名 / 关键词 / 未命中 | test_02, test_03, test_04, test_05 | ok |
| 未命中进「待分类」并提示 | test_05, test_08 | ok |
| 新课程自动建文件夹并在报告说明 | test_06, test_07, test_08 | ok |
| 笔记重名加后缀、绝不覆盖 | test_09, test_10, test_42 | ok |
| 附件重名：内容相同复用 / 内容不同加后缀 | test_15, test_16, test_17, test_18 | ok |
| 重复图片检测 + 指出原笔记 + 默认不重复生成 | test_19, test_20, test_21 | ok |
| 非法文件名 / Windows 保留名 / 超长名 / Obsidian 特殊字符 | test_11, test_12, test_13, test_14 | ok |
| 路径穿越（`..`、绝对路径、配置里的坏目录） | test_22, test_23, test_24, test_25 | ok |
| 符号链接 / junction 逃逸 | test_26 | ok |
| dry-run 不写任何东西 | test_38, test_39 | ok |
| vault 未配置 / 不存在 / 不是 vault | test_29, test_30, test_31, test_32 | ok |
| 环境变量配置 vault | test_33 | ok |
| 配置缺失 / JSON 语法错误给明确指引 | test_34, test_35 | ok |
| 失败回滚、不留半成品与空目录 | test_40, test_41, test_43 | ok |
| 只新增：已有文件一个字节都不变 | test_28（全库快照比对） | ok |
| 原图只读、不移动不修改 | test_27 | ok |
| LaTeX / Markdown 校验（括号、环境、$$、表格、围栏、标题空格） | test_44, test_45, test_46 | ok |
| 嵌入链接改写到最终文件名 + 自动补「## 原图」 | test_47, test_48 | ok |
| 不确定标记 → frontmatter 标记 + callout | test_50, test_51 | ok |
| 编码（UTF-8 / CRLF / gb18030） | test_55, test_56 | ok |
| 报告四要素（路径/分类/重名/未识别） | test_02, test_59 | ok |
| init-config / doctor / check-images / validate | test_01, test_21, test_37, test_46 | ok |

## 关键安全用例说明

- **test_10**：预先放一篇手写老笔记 `C语言/指针.md`，再用同名标题保存 → 新笔记落到 `指针 (2).md`，老笔记 SHA-256 前后完全一致。
- **test_28**：在已有笔记、附件、`.obsidian/app.json` 的 vault 上连续做两次保存（其中一次故意同名），对全库做「路径→大小+SHA256」快照比对：所有旧文件零变化，只有新增项。
- **test_42（竞态）**：模拟「脚本刚选好文件名、写入前被别的进程抢先创建同名文件」→ 自动改用 `(2)` 后缀，且抢先写入的内容一字未动。
- **test_26**：在 vault 里建一个指向外部的 junction（Windows 无需管理员权限）→ 报 `SYMLINK_ESCAPE` 拒绝写入；即使关掉 `strict_symlinks`，realpath 二级检查仍然拦住。
- **test_40**：批量任务里第二篇正文文件缺失 → 第一篇已创建的笔记与附件、以及空目录全部回滚，vault 快照回到空。

## 端到端验证（真实图片 + 真实 vault）

用你 vault 里的真实图片 `D:\Notes\MyVault\C语言\C-数据类型-内存占用.png`（各数据类型内存占用条形图）走完整流程：

1. 模型看图转写成 Markdown（表格 + 图示文字描述 + 不确定标记），正文写入 vault 外临时文件。
2. 保存到一个模拟你库结构的临时 vault：关键词打分 26 分命中「C语言」，笔记落在 `C语言/各数据类型的内存占用.md`，原图复制到 `C语言/attachments/`，frontmatter 含 `source_sha256`、`has_uncertain: true`、`classified_by: keyword`，正文自动补 `# 标题`、`## 原图` 嵌入与 `> [!warning] 待核对` callout。
3. `check-images` 对该图：`已整理过：是，对应笔记：C语言/各数据类型的内存占用.md`。
4. 再次发送同一张图：`skipped_duplicate`，报告给出原笔记路径，vault 零新增。
5. 对**真实 vault** `D:\Notes\MyVault` 运行 `doctor`（全部通过）与 `--dry-run`（只预演，未写入；并识别出该图内容已存在于你的库中，将复用 `C语言/C-数据类型-内存占用.png` 而不重复复制）。

## 安装校验

- frontmatter：`name: obsidian-image-notes`，description 含中英文触发场景与排除项；文件夹名与 name 一致；description 为安全的 YAML 纯标量（无 `: `、无 ` #`）。
- 交付文件齐全：SKILL.md、README.md、config.json、config.example.json、agents/openai.yaml、scripts/obsidian_image_notes.py、references/transcription.md、references/config.md、tests/test_obsidian_image_notes.py。
- 所有文本文件 UTF-8 无 BOM、LF 换行；两个 JSON 均通过解析；脚本与测试均通过编译（无语法告警）。

## 已知限制

- 识别质量取决于图片清晰度与模型能力；不引入 OCR，模糊内容只打标记不猜。
- 关键词分类是确定性打分，语义理解有限；拿不准就进「待分类」。
- 附件去重要遍历 vault 图片（先比大小再比哈希），图片极多时稍慢；超过 `dedupe.max_scan_files` 自动跳过去重改为新建。
- dry-run 的文件名只代表「此刻可用」，真正保存时若被并发占用会自动换后缀。
- 不执行任何 git 操作；vault 同步仍由你自己的流程负责。