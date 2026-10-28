# Obsidian 图片笔记 Skill

把笔记图片（手写、课本截图、板书、PPT、习题、公式推导）转成 Obsidian 可用的 Markdown 笔记，按课程分类后**只新增、不覆盖**地写进你的知识库，并输出一份保存报告。

- 识别由模型直接看图完成；文件名、分类、重名、去重、路径安全、回滚、报告由 `scripts/obsidian_image_notes.py` 确定性处理。
- 只用 Python 标准库（3.9+），无需 pip 安装任何东西，也**不引入 OCR 依赖**。
- 平台：Windows 优先（路径、UTF-8、非法字符、保留名、260 字符限制都已处理），macOS/Linux 同样可用。

## 安装

Codex 从以下目录发现 Skill（任选其一，推荐第一个）：

- `%USERPROFILE%\.codex\skills\obsidian-image-notes\`
- `%USERPROFILE%\.agents\skills\obsidian-image-notes\`

要求：目录里直接就是 `SKILL.md`（不要多套一层文件夹）。只需 Python 3.9+，无需 pip 安装任何依赖。

### 方式一：git clone（推荐，后续可直接 git pull 更新）

```powershell
git clone https://github.com/liuchenwei285/obsidian-image-notes.git "$env:USERPROFILE\.codex\skills\obsidian-image-notes"
```

### 方式二：下载 ZIP 手动安装

1. 在仓库页点 **Code → Download ZIP** 并解压；
2. 把解压出来的 `obsidian-image-notes` 整个文件夹移动到上面任一目录；
3. 确认该目录下直接就是 `SKILL.md`、`scripts\`、`references\`、`tests\`。

### 方式三：在 Codex 会话里让我装

把仓库地址发给 Codex，说「把这个 Skill 装到本机」：会自动 clone 到正确位置并跑一遍 `doctor` 自检。

### 安装后初始化（一次性，必做）

```powershell
$s = "$env:USERPROFILE\.codex\skills\obsidian-image-notes\scripts\obsidian_image_notes.py"

# 1) 生成你自己的配置：填 vault 路径，并把 vault 里已有的一级目录识别为课程
python $s init-config --vault "D:\Notes\MyVault" --detect-courses

# 2) 自检：配置、vault、.obsidian、课程列表、写权限
python $s doctor
```

`init-config` 会在 Skill 目录里生成 `config.json`；该文件已被 `.gitignore` 排除，不会把你的 vault 路径带进仓库。
也可以不改文件，直接用环境变量指定 vault：

```powershell
[Environment]::SetEnvironmentVariable("OBSIDIAN_IMAGE_NOTES_VAULT", "D:\Notes\MyVault", "User")
```

装完**重启一次 Codex**，然后附图说「整理进我的 Obsidian」即可触发。

### 更新到新版本

```powershell
cd "$env:USERPROFILE\.codex\skills\obsidian-image-notes"
git pull
python tests\test_obsidian_image_notes.py    # 可选：更新后跑一遍测试
```

### 目录结构

```
obsidian-image-notes/
├── SKILL.md                     Skill 指令（工作流 + 安全规则 + 报告格式）
├── README.md                    本文件
├── config.example.json          配置模板（config.json 由 init-config 生成，不入库）
├── agents/openai.yaml           Codex UI 显示名与默认提示词
├── scripts/obsidian_image_notes.py   落盘脚本（唯一会写 vault 的东西）
├── references/transcription.md  转写与 LaTeX/Obsidian 渲染规范
├── references/config.md         配置字段、匹配算法、错误码、排查
├── docs/TEST-REPORT.md          测试与验证报告
└── tests/test_obsidian_image_notes.py  59 个真实测试用例
```

## 配置（两处，都可选）

**1) vault 路径** —— 打开 `config.json` 确认 `vault_path` 指向你的知识库根目录（里面有 `.obsidian` 的那个目录）：

```json
"vault_path": "D:\\Notes\\MyVault"
```

也可以不改文件，用环境变量（优先级更高）：

```powershell
[Environment]::SetEnvironmentVariable("OBSIDIAN_IMAGE_NOTES_VAULT", "D:\Notes\MyVault", "User")
```

或者重新生成配置（会顺带把 vault 里已有的一级目录当作课程列表）：

```powershell
python "$env:USERPROFILE\.codex\skills\obsidian-image-notes\scripts\obsidian_image_notes.py" init-config --vault "D:\Notes\MyVault" --detect-courses --force
```

**2) 课程列表** —— 编辑 `config.json` 的 `courses`，每门课给 `name` / `folder` / `aliases` / `keywords`：

```json
{ "name": "高等数学", "folder": "高数",
  "aliases": ["高数", "微积分"],
  "keywords": ["极限", "导数", "积分", "级数"] }
```

`folder` 写你 vault 里**已有的目录名**，笔记就会并进去，不会另起一套。字段全表见 `references/config.md`。

**自检**：

```powershell
python "$env:USERPROFILE\.codex\skills\obsidian-image-notes\scripts\obsidian_image_notes.py" doctor
```

## 怎么触发

在 Codex 对话里**附上图片**，然后正常说话即可，例如：

- 「把这张笔记整理进我的 Obsidian」
- 「这是高数的板书，识别一下存起来」
- 「这几张是同一节 C 语言课的笔记，合并成一篇保存」
- 「先 dry-run 看看会存到哪，别真写」

自动触发不稳定时，直接点名：「用 obsidian-image-notes 处理这张图」。

Codex 需要对你的 vault 目录有写入权限；第一次写入时按提示授权即可。

## 怎么看报告

每次保存后 Codex 会汇报四件事：

1. **保存路径**（vault 内相对路径，多篇逐条列出最终文件名）
2. **分类结果**与依据（用户指定 / 图片中的课程名 / 关键词匹配 / 无法确定→待分类）
3. **是否发生重名处理**（`标题 (2).md`、`图 (2).png`、非法字符清洗；没有就写「无」）
4. **是否有未能识别的部分**（`has_uncertain`、公式校验告警、需要你确认的新课程/待分类）

同样的信息在脚本输出里：人类可读文本是默认输出，`--json` 得到结构化结果，`--report-file` 可存档。

## dry-run 与常用命令

```powershell
$s = "$env:USERPROFILE\.codex\skills\obsidian-image-notes\scripts\obsidian_image_notes.py"

# 只预演，不写任何文件
python $s save --title "测试" --body-file "$env:TEMP\n.md" --course "C语言" --dry-run

# 这张图以前整理过吗？整理进哪篇笔记了？
python $s check-images "D:\pics\IMG_0001.png"

# 只体检 Markdown / LaTeX（公式括号、环境闭合、$$ 配对、表格列数、代码围栏…）
python $s validate "$env:TEMP\n.md"

# 跑测试（在临时目录里造假 vault，绝不碰你的真实笔记库）
python "$env:USERPROFILE\.codex\skills\obsidian-image-notes\tests\test_obsidian_image_notes.py"
```

## 安全承诺

- 写入一律使用**独占创建**（`O_CREAT|O_EXCL`），从机制上不可能覆盖已有文件；重名只会加后缀。
- 只新增文件：不删除、不移动、不重命名、不改写 vault 里任何已有内容，也不动 `.obsidian/` 与 git。
- 你提供的原图只被读取和复制，绝不移动或修改。
- 所有写入路径都经过「字面规范化 + 符号链接/junction 解析」双重校验，必须落在 vault 内。
- 任何一步失败 → 回滚本次新建的文件与空目录，vault 不留半成品。
- vault 路径没配置好时直接拒绝写入并给出指引，不会退而在别的地方建笔记。

## 已知限制

- 识别质量取决于图片清晰度与模型能力；脚本不做 OCR，也不会替你猜模糊内容（只打标记）。
- 课程关键词匹配是确定性打分，不追求语义理解；拿不准就进「待分类」，需要你补 `keywords` 或明确说课程名。
- 附件内容去重需要遍历 vault 里的图片（先比大小再比哈希）。图片极多时会稍慢；`dedupe.max_scan_files` 到上限后会自动跳过去重并改为新建。
- dry-run 报告的文件名只是「此刻可用」，真正保存时若被别的进程抢先占用，会自动改用下一个后缀。
- 不会自动执行 git 提交/推送；vault 的同步仍由你自己的流程（如 Obsidian Git）负责。