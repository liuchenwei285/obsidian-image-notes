#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian 图片笔记 Skill —— 确定性落盘工具（只用 Python 标准库）。

职责边界
--------
本脚本 **不做** 图片内容识别；识别由模型直接看图完成。
本脚本负责全部确定性且与安全相关的工作：

  * 配置 / vault 路径解析与校验（未配置时给出明确指引）
  * 课程分类：用户明确指定 > 模型从图片读到的课程名 > 配置关键词匹配 > 待分类
  * 文件名清洗：Windows 非法字符、保留名、超长、Obsidian 特殊字符
  * 独占创建写入（O_CREAT|O_EXCL），永不覆盖 vault 中任何已有文件
  * 附件按内容 SHA-256 去重；同名不同内容自动加后缀
  * 重复图片检测（同一张图是否已整理过，并指出对应笔记）
  * 路径穿越 / 符号链接逃逸防护
  * LaTeX 与 Markdown 校验（默认只告警，不阻断保存）
  * YAML frontmatter 生成与合并
  * 出错回滚本次新建的半成品文件
  * 输出机器可读 (JSON) + 人类可读的保存报告
  * dry-run（只报告将要创建什么，不写任何文件）

本脚本永远只 **新增** 文件：不删除、不移动、不重命名、不修改 vault 里已有的任何文件，
也不修改用户提供的原始图片。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

VERSION = "1.0.0"
TOOL_NAME = "obsidian-image-notes"

SKILL_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 常量

# Windows 文件名非法字符
ILLEGAL_WIN_CHARS = set('<>:"/\\|?*')
# 会破坏 Obsidian [[wikilink]] / 标签语义的字符
OBSIDIAN_BAD_CHARS = set('#^[]')
CONTROL_CHARS = {chr(i) for i in range(32)} | {"\x7f"}
# Windows 保留设备名（不含扩展名的主干）
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    "COM%d" % i for i in range(1, 10)
} | {
    "LPT%d" % i for i in range(1, 10)
}

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".svg", ".avif", ".heic", ".pdf",
}

# vault 内扫描时跳过的目录
SKIP_DIRS = {".obsidian", ".git", ".trash", ".claudian", ".smart-connections", "node_modules"}

# MathJax / Obsidian 中不可靠或需要额外宏包的命令
FRAGILE_MACROS = [
    r"\usepackage", r"\documentclass", r"\begin{document}", r"\input",
    r"\includegraphics", r"\bibliography", r"\cite", r"\label",
    r"\ref", r"\eqref", r"\pageref", r"\mathds", r"\RequirePackage",
    r"\newpage", r"\pagebreak", r"\caption",
]

DEFAULT_CONFIG: dict = {
    "config_version": 1,
    # vault 根目录。留空则依次尝试环境变量 OBSIDIAN_IMAGE_NOTES_VAULT / OBSIDIAN_VAULT
    "vault_path": "",
    "require_obsidian_dir": True,
    "strict_symlinks": True,
    "notes_root": "",                 # 笔记根目录（相对 vault），"" 表示 vault 根
    "unknown_course_folder": "待分类",
    "allow_new_course": True,
    "attachments": {
        "mode": "subfolder",          # course | subfolder | global
        "subdir": "attachments",      # mode=subfolder 时，课程目录下的子目录名
        "global_path": "Attachments", # mode=global 时，相对 vault 的目录
    },
    "naming": {
        "max_filename_len": 80,       # 文件名主干最大字符数
        "conflict_pattern": "{stem} ({n})",  # 重名后缀模板，从 2 开始
        "conflict_max_suffix": 999,
        "attachment_naming": "original",     # original | hash
        "max_total_path_len": 240,    # 保守避开 Windows 260 字符限制
    },
    "classify": {
        "enabled": True,
        "min_keyword_score": 2,       # 关键词命中的最低总分
        "require_unique_winner": True,# 平局时判为无法确定 -> 待分类
        "scan_chars": 6000,           # 关键词扫描的正文长度上限
    },
    "dedupe": {
        "scan_scope": "vault",        # vault | attachments | off
        "max_scan_files": 20000,
        "duplicate_image_policy": "skip",   # skip | allow
        "scan_notes_for_source": True,
    },
    "embed": {
        "mode": "auto",               # auto | none   auto=正文缺少嵌入时自动追加“## 原图”
        "style": "short",             # short | path  short=![[文件名]] path=![[相对路径]]
    },
    "frontmatter": {
        "extra_tags": ["图片笔记"],
        "tag_prefix": "course/",
    },
    "courses": [
        {
            "name": "示例课程",
            "folder": "示例课程",
            "aliases": [],
            "keywords": [],
        }
    ],
}


class SkillError(Exception):
    """带错误码与修复指引的异常。"""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


# ---------------------------------------------------------------- 通用工具

def _now() -> datetime:
    return datetime.now().astimezone()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def deep_merge(base: dict, override) -> dict:
    out = dict(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_text_lenient(path: Path) -> tuple[str, str, list[str]]:
    """读取文本文件；优先 UTF-8，失败时回退 gb18030 并给出告警。"""
    raw = Path(path).read_bytes()
    warns: list[str] = []
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc, warns
        except UnicodeDecodeError:
            pass
    try:
        text = raw.decode("gb18030")
        warns.append(
            "正文文件不是 UTF-8，已按 gb18030 解码并转存为 UTF-8：%s" % path
        )
        return text, "gb18030", warns
    except UnicodeDecodeError:
        raise SkillError(
            "BODY_NOT_UTF8",
            "无法以 UTF-8 或 gb18030 解码正文文件：%s" % path,
            "请用 UTF-8 编码重新写入正文临时文件。",
        )

# ---------------------------------------------------------------- 配置

def config_search_paths(explicit: str | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    env = os.environ.get("OBSIDIAN_IMAGE_NOTES_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(SKILL_DIR / "config.json")
    home_cfg = Path(os.path.expanduser("~")) / ".obsidian-image-notes" / "config.json"
    paths.append(home_cfg)
    return paths


def load_config(explicit: str | None = None) -> tuple[dict, Path | None, list[str]]:
    """加载配置。找不到 config.json 时抛 CONFIG_MISSING 并给出设置指引。"""
    warns: list[str] = []
    chosen: Path | None = None
    if explicit:
        cand = Path(explicit).expanduser()
        if cand.is_file():
            chosen = cand
    else:
        for p in config_search_paths(None):
            if p and p.is_file():
                chosen = p
                break
    if explicit and chosen is None:
        raise SkillError(
            "CONFIG_MISSING",
            "指定的配置文件不存在：%s" % explicit,
            "不会回退到其它配置文件，以免写错 vault。运行 init-config 生成配置：\n"
            '  python "%s" init-config --vault "D:\\你的\\vault"'
            % str(SKILL_DIR / "scripts" / "obsidian_image_notes.py"),
        )
    if chosen is None:
        searched = "\n  - ".join(str(p) for p in config_search_paths(None))
        raise SkillError(
            "CONFIG_MISSING",
            "未找到配置文件 config.json，无法确定 Obsidian vault 位置。",
            "请执行一次性初始化：\n"
            '  python "%s" init-config --vault "D:\\你的\\vault路径"\n'
            "或用环境变量指定配置：OBSIDIAN_IMAGE_NOTES_CONFIG\n"
            "已搜索的位置：\n  - %s" % (str(SKILL_DIR / "scripts" / "obsidian_image_notes.py"), searched),
        )
    try:
        text = chosen.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise SkillError(
            "CONFIG_INVALID",
            "配置文件不是 UTF-8 编码：%s" % chosen,
            "请用 UTF-8（无 BOM）保存 config.json。",
        )
    except OSError as exc:
        raise SkillError("CONFIG_INVALID", "配置文件读取失败：%s（%s）" % (chosen, exc))
    try:
        user_cfg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillError(
            "CONFIG_INVALID",
            "配置文件 JSON 语法错误：%s 第 %d 行第 %d 列：%s"
            % (chosen, exc.lineno, exc.colno, exc.msg),
            "修复 JSON 语法后重试（注意逗号、引号）。",
        )
    if not isinstance(user_cfg, dict):
        raise SkillError("CONFIG_INVALID", "配置文件顶层必须是 JSON 对象：%s" % chosen)
    cfg = deep_merge(DEFAULT_CONFIG, user_cfg)
    cfg["_config_path"] = str(chosen)
    _validate_config(cfg, chosen, warns)
    return cfg, chosen, warns


def _validate_config(cfg: dict, path: Path, warns: list[str]) -> None:
    courses = cfg.get("courses")
    if not isinstance(courses, list):
        raise SkillError("CONFIG_INVALID", "config.courses 必须是数组（%s）" % path)
    seen_folders: set[str] = set()
    seen_names: set[str] = set()
    cleaned: list[dict] = []
    for i, c in enumerate(courses):
        if not isinstance(c, dict):
            raise SkillError("CONFIG_INVALID", "config.courses[%d] 必须是对象（%s）" % (i, path))
        name = str(c.get("name") or "").strip()
        folder = str(c.get("folder") or name).strip()
        if not name:
            raise SkillError("CONFIG_INVALID", "config.courses[%d].name 不能为空（%s）" % (i, path))
        if not folder:
            folder = name
        if name.lower() in seen_names:
            warns.append("配置里课程名重复：%s（已保留第一个）" % name)
            continue
        if folder.lower() in seen_folders:
            warns.append("配置里课程文件夹重复：%s（课程 %s）" % (folder, name))
        seen_names.add(name.lower())
        seen_folders.add(folder.lower())
        cleaned.append({
            "name": name,
            "folder": folder,
            "aliases": [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()],
            "keywords": [str(k).strip() for k in (c.get("keywords") or []) if str(k).strip()],
        })
    cfg["courses"] = cleaned
    att = cfg.get("attachments") or {}
    if att.get("mode") not in ("course", "subfolder", "global"):
        warns.append("attachments.mode 非法（%r），已回退为 subfolder" % att.get("mode"))
        att["mode"] = "subfolder"
    if att.get("mode") == "subfolder" and not str(att.get("subdir") or "").strip():
        att["mode"] = "course"
        warns.append("attachments.subdir 为空，已回退为 course 模式")
    nam = cfg.get("naming") or {}
    if nam.get("attachment_naming") not in ("original", "hash"):
        warns.append("naming.attachment_naming 非法，已回退为 original")
        nam["attachment_naming"] = "original"


# ---------------------------------------------------------------- vault 与路径安全

def resolve_vault(cfg: dict, cli_vault: str | None, allow_non_vault: bool = False) -> Path:
    raw = (
        cli_vault
        or os.environ.get("OBSIDIAN_IMAGE_NOTES_VAULT")
        or os.environ.get("OBSIDIAN_VAULT")
        or cfg.get("vault_path")
        or ""
    )
    raw = str(raw).strip().strip('"').strip("'")
    if not raw or raw.upper() in {"CHANGE_ME", "TODO", "<VAULT_PATH>", "NULL", "NONE"}:
        raise SkillError(
            "VAULT_UNCONFIGURED",
            "尚未配置 Obsidian vault 路径，拒绝写入任何位置。",
            "三选一：\n"
            '  1) 编辑配置文件里的 vault_path：%s\n'
            '  2) 运行 init-config：python "%s" init-config --vault "D:\\你的\\vault"\n'
            "  3) 设置环境变量 OBSIDIAN_IMAGE_NOTES_VAULT（或临时用 --vault 参数）"
            % (cfg.get("_config_path") or "(未找到 config.json)",
               str(SKILL_DIR / "scripts" / "obsidian_image_notes.py")),
        )
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise SkillError(
            "VAULT_MISSING",
            "vault 路径不存在：%s" % p,
            "请检查 config.json 里的 vault_path 是否正确（注意盘符与空格）。",
        )
    real = Path(os.path.realpath(str(p)))
    if not real.is_dir():
        raise SkillError("VAULT_MISSING", "vault 路径不是目录：%s" % real)
    need_obs = bool(cfg.get("require_obsidian_dir", True)) and not allow_non_vault
    if need_obs and not (real / ".obsidian").is_dir():
        raise SkillError(
            "NOT_A_VAULT",
            "目录 %s 下没有 .obsidian 文件夹，为避免写错地方已拒绝写入。" % real,
            "确认这是 Obsidian 知识库根目录；仅用于测试时可加 --allow-non-vault。",
        )
    return real


def is_lexically_within(base: Path, target: Path) -> bool:
    """纯字面判断（不解析符号链接）：用于区分穿越与链接逃逸。"""
    b = os.path.normcase(os.path.abspath(str(base)))
    t = os.path.normcase(os.path.abspath(str(target)))
    if b == t:
        return True
    return t.startswith(b.rstrip("\\/") + os.sep)


def is_within(base: Path, target: Path) -> bool:
    """target（含 base 自身）是否位于 base 之内。两者都应已 realpath。"""
    try:
        b = os.path.normcase(os.path.realpath(str(base)))
        t = os.path.normcase(os.path.realpath(str(target)))
    except OSError:
        return False
    if b == t:
        return True
    try:
        common = os.path.commonpath([b, t])
    except ValueError:
        return False
    return common == b


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _validate_component(comp: str, what: str) -> str:
    c = str(comp).replace("\\", "/").strip()
    if not c:
        raise SkillError("PATH_INVALID", "%s 含空路径段" % what)
    if c in (".", ".."):
        raise SkillError(
            "PATH_ESCAPE",
            "%s 含 %r，拒绝相对路径穿越" % (what, c),
            "课程名 / 子目录请使用不含 .. 的相对路径。",
        )
    if c.startswith("/") or _DRIVE_RE.match(c):
        raise SkillError("PATH_ESCAPE", "%s 含绝对路径片段：%r" % (what, c))
    if any(ch in ILLEGAL_WIN_CHARS or ch in CONTROL_CHARS for ch in c):
        raise SkillError("PATH_INVALID", "%s 含非法字符：%r" % (what, c))
    return c


def safe_join(vault_real: Path, rel_posix: str, cfg: dict, what: str = "路径") -> Path:
    """把相对路径安全地拼到 vault 内，防穿越与符号链接逃逸。"""
    rel = str(rel_posix or "").replace("\\", "/").strip("/")
    comps = [_validate_component(x, what) for x in rel.split("/")] if rel else []
    lexical = vault_real.joinpath(*comps) if comps else vault_real
    # 1) 字面检查：规范化后必须仍在 vault 内（防 .. 穿越）
    norm = Path(os.path.normpath(str(lexical)))
    if not is_lexically_within(vault_real, norm):
        raise SkillError("PATH_ESCAPE", "%s 解析后超出 vault 范围：%s" % (what, rel))
    # 2) 链接检查：realpath（解析符号链接 / junction）后仍必须在 vault 内
    real = Path(os.path.realpath(str(norm)))
    if not is_within(vault_real, real):
        raise SkillError(
            "SYMLINK_ESCAPE",
            "%s 经过符号链接解析后指向 vault 之外：%s -> %s" % (what, norm, real),
            "vault 内不要使用指向外部的符号链接 / 目录联接。",
        )
    if cfg.get("strict_symlinks", True):
        cur = vault_real
        for comp in comps:
            cur = cur / comp
            if not cur.exists():
                break
            if os.path.islink(str(cur)):
                raise SkillError(
                    "SYMLINK_ESCAPE",
                    "%s 经过符号链接目录：%s（strict_symlinks=true 时禁止写入）" % (what, cur),
                    "如确需写入符号链接目录，请把 config.json 的 strict_symlinks 设为 false。",
                )
    return real


def sanitize_component(name: str, cfg: dict, what: str = "文件名") -> tuple[str, str | None]:
    """清洗单个文件/目录名。返回 (清洗后名称, 若被改动则为原名)。"""
    original = str(name)
    s = unicodedata.normalize("NFC", original).strip()
    s = s.replace("\x00", "")
    out = []
    for ch in s:
        if ch in ILLEGAL_WIN_CHARS or ch in CONTROL_CHARS or ch in OBSIDIAN_BAD_CHARS:
            out.append("_")
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"_{2,}", "_", s)
    s = s.strip(" .")          # Windows 不允许结尾的点与空格
    s = s.lstrip("\u200b\ufeff")
    if not s:
        s = "未命名"
    if s.split(".")[0].upper() in RESERVED_NAMES:
        s = "_" + s
    max_len = int((cfg.get("naming") or {}).get("max_filename_len", 80))
    if len(s) > max_len:
        s = _clamp_with_ext(s, max_len)
    changed = original if s != original else None
    return s, changed


def _clamp_with_ext(name: str, max_len: int) -> str:
    if len(name) <= max_len:
        return name
    root, ext = os.path.splitext(name)
    ext = ext if len(ext) <= 12 else ""
    keep = max(1, max_len - len(ext))
    return (root[:keep].rstrip(" ._") or "n") + ext


def sanitize_rel_path(rel, cfg: dict, what: str = "相对路径") -> tuple[str, str | None]:
    """清洗一个 vault 内的相对路径（可多级）。

    保留 / 作为分层符，但拒绝 .. 、绝对路径与非法字符。
    """
    text = str(rel or "").replace("\\", "/").strip()
    if not text.strip("/"):
        return "", None
    out: list[str] = []
    changed = False
    for comp in text.strip("/").split("/"):
        if comp.strip() in ("", ".", ".."):
            raise SkillError(
                "PATH_ESCAPE",
                "%s 含非法路径段 %r，拒绝相对路径穿越" % (what, comp),
                "请使用不含 .. 的相对路径，例如 课程笔记/高数。",
            )
        if _DRIVE_RE.match(comp):
            raise SkillError("PATH_ESCAPE", "%s 不能是绝对路径：%r" % (what, text))
        clean, ch = sanitize_component(comp, cfg, what)
        out.append(clean)
        if ch:
            changed = True
    joined = "/".join(out)
    return joined, (text if changed or text != joined else None)


def clamp_for_total_path(dirpath: Path, stem: str, ext: str, cfg: dict) -> tuple[str, bool]:
    """确保 dirpath/stem+ext 的绝对路径长度不超限。"""
    nam = cfg.get("naming") or {}
    max_total = int(nam.get("max_total_path_len", 240))
    max_len = int(nam.get("max_filename_len", 80))
    stem = _clamp_with_ext(stem, max_len) if len(stem) > max_len else stem
    base_len = len(str(dirpath)) + 1 + len(ext) + 8  # 8 预留给 " (999)" 之类后缀
    avail = max_total - base_len
    if avail < 4:
        raise SkillError(
            "PATH_TOO_LONG",
            "目标目录太深，无法生成安全的文件名：%s" % dirpath,
            "请缩短 notes_root / 课程文件夹名，或调大 naming.max_total_path_len（需系统已启用长路径）。",
        )
    if len(stem) > avail:
        return stem[:avail].rstrip(" ._") or "n", True
    return stem, False

# ---------------------------------------------------------------- frontmatter

class Raw(str):
    """包装后的字符串在 YAML 中原样输出（不加引号）。"""


def yaml_quote(value: str) -> str:
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


TAG_BAD_CHARS = set('#^[]|{}<>:"*?\\,;!`')


def sanitize_tag(tag: str) -> str:
    """Obsidian 标签只能用字母/数字/下划线/连字符/斜杠（嵌套标签）。"""
    t = unicodedata.normalize("NFKC", str(tag)).strip().lstrip("#").strip()
    t = re.sub(r"\s+", "-", t)
    t = "".join(ch for ch in t if ch not in TAG_BAD_CHARS and ch not in CONTROL_CHARS)
    t = re.sub(r"-{2,}", "-", t)
    t = t.strip("-/._")
    return t


FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def build_frontmatter(fields: list[tuple[str, object]], raw_extra: list[str] | None = None) -> str:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, (list, tuple)):
            items = [v for v in value if str(v).strip() != ""]
            if not items:
                continue
            lines.append("%s:" % key)
            for it in items:
                lines.append("  - %s" % yaml_quote(it))
        elif isinstance(value, Raw):
            lines.append("%s: %s" % (key, str(value)))
        elif isinstance(value, bool):
            lines.append("%s: %s" % (key, "true" if value else "false"))
        elif value is None:
            continue
        else:
            lines.append("%s: %s" % (key, yaml_quote(value)))
    for raw_line in raw_extra or []:
        lines.append(raw_line.rstrip())
    lines.append("---")
    return "\n".join(lines) + "\n"


UNCERTAIN_PATTERNS = [
    "[?不确定", "〔图片中不清晰", "[!warning] 待核对", "[!question] 待核对",
    "待核对", "[?uncertain", "看不清", "无法辨认",
]


def detect_uncertain(body: str, declared: list[str]) -> bool:
    if declared:
        return True
    low = body
    return any(p in low for p in UNCERTAIN_PATTERNS)


# ---------------------------------------------------------------- Markdown / LaTeX 校验

_BLANK = re.compile(r"[^\n]")


def _blank_out(s: str) -> str:
    """把内容替换成等长空白（保留换行），便于跳过不参与校验的区域。"""
    return _BLANK.sub(" ", s)


def _strip_for_math(seg: str) -> str:
    r"""移除行内代码与转义 \$，避免误判。"""
    seg = re.sub(r"`[^`\n]*`", lambda m: _blank_out(m.group(0)), seg)
    seg = seg.replace("\\$", "  ")
    return seg


def _check_math_body(inner: str, offset_line: int, issues: list[dict]) -> None:
    depth = 0
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                issues.append(_issue("warning", "math_brace_unbalanced", offset_line,
                                     "数学公式中 } 多于 {（括号未配对）"))
                depth = 0
        i += 1
    if depth != 0:
        issues.append(_issue("warning", "math_brace_unbalanced", offset_line,
                             "数学公式中有 %d 个 { 未闭合" % depth))
    begins = re.findall(r"\\begin\{([^}]*)\}", inner)
    ends = re.findall(r"\\end\{([^}]*)\}", inner)
    if sorted(begins) != sorted(ends):
        missing = [b for b in begins if b not in ends]
        extra = [e for e in ends if e not in begins]
        detail = ""
        if missing:
            detail += "未闭合环境：%s" % ", ".join(sorted(set(missing)))
        if extra:
            detail += ("；" if detail else "") + "多余的 \\end：%s" % ", ".join(sorted(set(extra)))
        issues.append(_issue("error", "math_env_unclosed", offset_line,
                             "公式环境未正确闭合。" + detail))
    nl = len(re.findall(r"\\left\b", inner))
    nr = len(re.findall(r"\\right\b", inner))
    if nl != nr:
        issues.append(_issue("warning", "left_right_mismatch", offset_line,
                             "\\left 与 \\right 数量不匹配（%d vs %d）" % (nl, nr)))
    for macro in FRAGILE_MACROS:
        if macro in inner:
            issues.append(_issue("warning", "fragile_macro", offset_line,
                                 "公式使用了 Obsidian/MathJax 中不可靠的命令 %s，建议改用基础命令" % macro))
    for m in re.finditer(r"(?<!\\)%", inner):
        issues.append(_issue("warning", "percent_in_math", offset_line,
                             "公式中出现未转义的 %（LaTeX 中是注释符），应写成 \\%"))
        break


def _issue(level: str, code: str, line: int, message: str) -> dict:
    return {"level": level, "code": code, "line": line, "message": message}


def _looks_like_math(inner: str) -> bool:
    t = inner.strip()
    if not t:
        return False
    if re.search(r"[\\^_{}]", t):
        return True
    if " " not in t and len(t) <= 24:
        return True
    return False


def validate_markdown(text: str) -> list[dict]:
    """确定性的 Markdown / LaTeX 体检。只报告，不修改内容。"""
    issues: list[dict] = []
    lines = text.split("\n")

    # --- 1) 代码围栏配对，并收集“非代码”区段
    segments: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_start = 1
    in_fence = False
    fence_ch = ""
    fence_len = 0
    fence_line = 0
    for idx, ln in enumerate(lines, 1):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", ln)
        if m and not in_fence:
            if buf:
                segments.append((buf_start, "\n".join(buf)))
                buf = []
            in_fence = True
            fence_ch = m.group(1)[0]
            fence_len = len(m.group(1))
            fence_line = idx
            continue
        if in_fence:
            m2 = re.match(r"^ {0,3}(`{3,}|~{3,})\s*$", ln)
            if m2 and m2.group(1)[0] == fence_ch and len(m2.group(1)) >= fence_len:
                in_fence = False
                buf_start = idx + 1
            continue
        if not buf:
            buf_start = idx
        buf.append(ln)
    if buf:
        segments.append((buf_start, "\n".join(buf)))
    if in_fence:
        issues.append(_issue("warning", "unclosed_code_fence", fence_line,
                             "代码块围栏（``` 或 ~~~）未闭合，其后内容在 Obsidian 中会被当作代码"))

    # --- 2) 标题需要空格：#标题 在 Obsidian 里会被当成标签
    for idx, ln in enumerate(lines, 1):
        m = re.match(r"^(#{1,6})([^ #\n\t].*)$", ln)
        if m:
            issues.append(_issue("warning", "heading_needs_space", idx,
                                 "标题 %s 后缺少空格，Obsidian 会把它当作标签：%s"
                                 % (m.group(1), ln.strip()[:40])))

    # --- 3) 表格列数一致性
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("|") and i + 1 < len(lines) \
                and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]) and "-" in lines[i + 1]:
            header_cells = _table_cells(ln)
            j = i + 1
            sep_cells = _table_cells(lines[j])
            if len(sep_cells) != len(header_cells):
                issues.append(_issue("warning", "table_column_mismatch", j + 1,
                                     "表格分隔行列数(%d)与表头(%d)不一致" % (len(sep_cells), len(header_cells))))
            j += 1
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                cells = _table_cells(lines[j])
                if len(cells) != len(header_cells):
                    issues.append(_issue("warning", "table_column_mismatch", j + 1,
                                         "表格该行列数(%d)与表头(%d)不一致" % (len(cells), len(header_cells))))
                j += 1
            i = j
            continue
        i += 1

    # --- 4) 数学公式
    for seg_start, seg in segments:
        work = _strip_for_math(seg)
        # 4a) 独立公式 $$ ... $$
        positions = [m.start() for m in re.finditer(r"\$\$", work)]
        if len(positions) % 2 != 0:
            ln = seg_start + work[:positions[-1]].count("\n")
            issues.append(_issue("error", "display_math_unbalanced", ln,
                                 "$$ 数量为奇数，独立公式未闭合（Obsidian 中不会渲染）"))
            positions = positions[:-1]
        spans: list[tuple[int, int]] = []
        for k in range(0, len(positions) - 1, 2):
            a, b = positions[k], positions[k + 1]
            inner = work[a + 2:b]
            spans.append((a, b + 2))
            ln = seg_start + work[:a].count("\n")
            stripped = inner.strip()
            if stripped:
                # $$ 是否独占一行（或整段在同一行内闭合）
                before = work[:a].split("\n")[-1].strip()
                after = work[b + 2:].split("\n")[0].strip()
                same_line = "\n" not in inner
                if (before or after) and not same_line:
                    issues.append(_issue("warning", "display_math_inline_text", ln,
                                         "$$ 公式与其前后文字混在同一行，Obsidian 可能不渲染；建议让 $$ 独占一行"))
                _check_math_body(inner, ln, issues)
        # 4b) 移除独立公式后检查行内 $
        masked = list(work)
        for a, b in spans:
            for p in range(a, min(b, len(masked))):
                masked[p] = " "
        masked_s = "".join(masked)
        for rel_ln, ln_text in enumerate(masked_s.split("\n")):
            dollars = [m.start() for m in re.finditer(r"\$", ln_text)]
            if len(dollars) % 2 != 0:
                issues.append(_issue("warning", "inline_math_unbalanced", seg_start + rel_ln,
                                     "该行 $ 数量为奇数，行内公式可能未闭合（若表示美元请写成 \\$）"))
                continue
            for k in range(0, len(dollars) - 1, 2):
                inner = ln_text[dollars[k] + 1:dollars[k + 1]]
                if not _looks_like_math(inner):
                    issues.append(_issue("info", "dollar_maybe_currency", seg_start + rel_ln,
                                         "成对的 $ 之间不像公式：%r；若为货币符号请转义为 \\$" % inner[:30]))
                else:
                    _check_math_body(inner, seg_start + rel_ln, issues)
    return issues


def _table_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    parts = re.split(r"(?<!\\)\|", s)
    return [p.strip() for p in parts]


def summarize_issues(issues: list[dict]) -> dict:
    out = {"errors": 0, "warnings": 0, "infos": 0, "items": []}
    for it in issues:
        if it["level"] == "error":
            out["errors"] += 1
        elif it["level"] == "warning":
            out["warnings"] += 1
        else:
            out["infos"] += 1
        out["items"].append(it)
    return out

# ---------------------------------------------------------------- 课程分类

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip().casefold()


def _count_keyword(haystack: str, needle: str) -> int:
    n = _norm(needle)
    if not n:
        return 0
    h = _norm(haystack)
    if re.fullmatch(r"[a-z0-9 _\-+.]+", n):
        # ASCII 关键词按词边界匹配，避免 int 命中 print
        return len(re.findall(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", h))
    return h.count(n)


def new_course_name_ok(raw: str) -> bool:
    """新课程名要能安全地当作文件夹名；否则宁可放进待分类。"""
    s = str(raw or "").strip()
    if len(s) < 2 or len(s) > 60:
        return False
    if "/" in s or "\\" in s or ".." in s:
        return False
    if any(ch in ILLEGAL_WIN_CHARS or ch in CONTROL_CHARS for ch in s):
        return False
    if s.strip(" .") != s:
        return False
    return True


def match_course_by_name(cfg: dict, text: str) -> dict | None:
    """按课程名 / 别名精确或包含匹配。"""
    t = _norm(text)
    if not t:
        return None
    for c in cfg["courses"]:
        names = [c["name"]] + list(c["aliases"])
        for nm in names:
            if _norm(nm) == t:
                return c
    for c in cfg["courses"]:
        for nm in [c["name"]] + list(c["aliases"]):
            nn = _norm(nm)
            if len(nn) >= 2 and (nn in t or t in nn):
                return c
    return None


def classify_course(cfg: dict, explicit: str | None, hint: str | None,
                    title: str, body: str) -> dict:
    """返回分类结果。优先级：用户明确指定 > 模型从图片读到的课程名 > 配置关键词 > 待分类。"""
    unknown = str(cfg.get("unknown_course_folder") or "待分类").strip() or "待分类"
    res = {
        "course": None, "folder": unknown, "match": "none",
        "is_new_course": False, "needs_review": True,
        "scores": [], "reason": "",
    }
    if explicit and str(explicit).strip():
        c = match_course_by_name(cfg, explicit)
        if c:
            res.update(course=c["name"], folder=c["folder"], match="explicit",
                       needs_review=False, reason="用户明确指定，命中配置课程「%s」" % c["name"])
            return res
        if cfg.get("allow_new_course", True) and new_course_name_ok(explicit):
            res.update(course=str(explicit).strip(), folder=str(explicit).strip(),
                       match="explicit-new", is_new_course=True, needs_review=True,
                       reason="用户明确指定「%s」，但配置里没有该课程，已按新课程处理" % explicit)
            return res
        if not new_course_name_ok(explicit):
            res.update(match="explicit-invalid",
                       reason="用户指定的课程名「%s」不能安全地作为文件夹名（含路径分隔符/非法字符/过长），已放入待分类" % explicit)
            return res
        res.update(match="explicit-ignored",
                   reason="用户指定「%s」但配置中不存在，且 allow_new_course=false，已放入待分类" % explicit)
        return res

    if hint and str(hint).strip() and len(str(hint).strip()) >= 2:
        c = match_course_by_name(cfg, hint)
        if c:
            res.update(course=c["name"], folder=c["folder"], match="hint",
                       needs_review=False, reason="图片中识别到课程名「%s」，命中配置" % c["name"])
            return res
        if _norm(hint) != _norm(unknown) and cfg.get("allow_new_course", True) and new_course_name_ok(hint):
            res.update(course=str(hint).strip(), folder=str(hint).strip(),
                       match="hint-new", is_new_course=True, needs_review=True,
                       reason="图片中识别到课程名「%s」，配置里没有，已按新课程处理（请确认）" % hint)
            return res

    if cfg.get("classify", {}).get("enabled", True):
        limit = int(cfg.get("classify", {}).get("scan_chars", 6000))
        hay = (title or "") + "\n" + (body or "")[:limit]
        scores = []
        for c in cfg["courses"]:
            score = 0
            hits = []
            for a in [c["name"]] + list(c["aliases"]):
                n = _count_keyword(hay, a)
                if n:
                    score += 3 * n
                    hits.append("%s x%d" % (a, n))
            for k in c["keywords"]:
                n = _count_keyword(hay, k)
                if n:
                    score += n
                    hits.append("%s x%d" % (k, n))
            if score > 0:
                scores.append({"course": c["name"], "score": score, "hits": hits[:8]})
        scores.sort(key=lambda d: (-d["score"], d["course"]))
        res["scores"] = scores[:5]
        min_score = int(cfg.get("classify", {}).get("min_keyword_score", 2))
        if scores and scores[0]["score"] >= min_score:
            ambiguous = (len(scores) > 1 and scores[1]["score"] == scores[0]["score"]
                         and cfg.get("classify", {}).get("require_unique_winner", True))
            if ambiguous:
                res.update(match="ambiguous", needs_review=True,
                           reason="关键词打分并列（%s），无法确定课程，已放入待分类"
                                  % ", ".join("%s=%d" % (s["course"], s["score"]) for s in scores[:3]))
                return res
            c = match_course_by_name(cfg, scores[0]["course"])
            if c:
                res.update(course=c["name"], folder=c["folder"], match="keyword",
                           needs_review=False,
                           reason="关键词命中：%s（得分 %d）"
                                  % (", ".join(scores[0]["hits"][:4]), scores[0]["score"]))
                return res
        if scores:
            res["reason"] = "关键词得分不足或不确定（最高 %s=%d，阈值 %d），放入待分类" % (
                scores[0]["course"], scores[0]["score"], min_score)
        else:
            res["reason"] = "未匹配到任何课程关键词，放入待分类"
    return res


# ---------------------------------------------------------------- 嵌入链接

def rewrite_embeds(body: str, placements: list[dict], cfg: dict, vault: Path,
                   note_dir_rel: str) -> tuple[str, list[str]]:
    """把正文里指向原图的嵌入链接改写为实际写入 vault 的文件名，并补齐缺失的嵌入。"""
    notes: list[str] = []
    emb = cfg.get("embed") or {}
    for p in placements:
        final_name = p["final_name"]
        final_stem = os.path.splitext(final_name)[0]
        link_target = final_name
        if emb.get("style") == "path":
            link_target = (p["rel_path"] or final_name)
        candidates: set[str] = set()
        for v in (p.get("source_name"), p.get("source_stem"), p.get("source"), p.get("source_abs")):
            if not v:
                continue
            v = str(v)
            candidates.add(v)
            candidates.add(v.replace("\\", "/"))
            candidates.add(Path(v).name)
            candidates.add(Path(v).stem)
        candidates.discard("")
        candidates.discard(final_name)
        candidates.discard(final_stem)
        for cand in sorted(candidates, key=len, reverse=True):
            rx = re.compile(r"!\[\[ ?" + re.escape(cand) + r"( ?\|[^\]]*)?\]\]")
            body, n = rx.subn(lambda m: "![[%s%s]]" % (link_target, m.group(1) or ""), body)
            if n:
                notes.append("嵌入链接 %s -> %s（%d 处）" % (cand, link_target, n))
            for variant in {cand, cand.replace("\\", "/"), cand.replace("/", "\\")}:
                rx2 = re.compile(r"!\[[^\]\n]*\]\(\s*<?" + re.escape(variant) +
                                 r">?(?:\s+\"[^\"]*\")?\s*\)")
                body, n2 = rx2.subn("![[%s]]" % link_target, body)
                if n2:
                    notes.append("Markdown 图片引用 -> ![[%s]]（%d 处）" % (link_target, n2))
                    break
        # 兼容写法兜底：任意路径下的同名图片
        base = re.escape(p["source_name"])
        rx3 = re.compile(r"!\[[^\]\n]*\]\(\s*<?[^)\n]*?[/\\]" + base + r">?(?:\s+\"[^\"]*\")?\s*\)")
        body, n3 = rx3.subn("![[%s]]" % link_target, body)
        if n3:
            notes.append("Markdown 图片引用（按文件名匹配）-> ![[%s]]（%d 处）" % (link_target, n3))
    if emb.get("mode", "auto") == "auto":
        missing = [p for p in placements
                   if p["final_name"] not in body and os.path.splitext(p["final_name"])[0] not in body]
        if missing:
            block = "\n\n## 原图\n\n" + "\n".join("![[%s]]" % p["final_name"] for p in missing) + "\n"
            body = body.rstrip("\n") + block
            notes.append("正文未嵌入 %d 张原图，已自动追加「## 原图」小节" % len(missing))
    return body, notes


# ---------------------------------------------------------------- vault 扫描（去重）

def iter_vault_files(vault: Path, exts: set[str], max_files: int,
                     roots: list[Path] | None = None) -> tuple[list[Path], bool]:
    out: list[Path] = []
    truncated = False
    bases = roots or [vault]
    for base in bases:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".git")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in exts:
                    continue
                out.append(Path(dirpath) / fn)
                if len(out) >= max_files:
                    return out, True
    return out, truncated


def find_same_content(vault: Path, cfg: dict, sha: str, size: int,
                      search_dirs: list[Path]) -> dict | None:
    """在 vault 中查找内容完全相同的附件（先比大小再比哈希）。"""
    scope = (cfg.get("dedupe") or {}).get("scan_scope", "vault")
    if scope == "off":
        return None
    max_files = int((cfg.get("dedupe") or {}).get("max_scan_files", 20000))
    if scope == "attachments":
        roots = [d for d in search_dirs if d.is_dir()]
    else:
        roots = [vault]
    files, truncated = iter_vault_files(vault, IMAGE_EXTS, max_files, roots)
    for f in files:
        try:
            if f.stat().st_size != size:
                continue
        except OSError:
            continue
        try:
            if sha256_file(f) == sha:
                return {"path": f, "name": f.name,
                        "rel": os.path.relpath(str(f), str(vault)).replace("\\", "/"),
                        "truncated": truncated}
        except OSError:
            continue
    if truncated:
        return {"path": None, "name": None, "rel": None, "truncated": True}
    return None


def find_notes_by_hash(vault: Path, cfg: dict, sha: str, notes_root_rel: str) -> list[str]:
    """扫描笔记 frontmatter 里的 source_sha256，找出这张图是否已整理过。"""
    if not (cfg.get("dedupe") or {}).get("scan_notes_for_source", True):
        return []
    base = safe_join(vault, notes_root_rel, cfg, "notes_root") if notes_root_rel else vault
    hits: list[str] = []
    max_files = int((cfg.get("dedupe") or {}).get("max_scan_files", 20000))
    files, _ = iter_vault_files(vault, {".md"}, max_files, [base])
    for f in files:
        try:
            with open(f, "rb") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        if sha.encode() not in head:
            continue
        try:
            text = head.decode("utf-8", "replace")
        except Exception:
            continue
        fm, _ = split_frontmatter(text)
        if fm and re.search(r"\b" + re.escape(sha) + r"\b", fm):
            hits.append(os.path.relpath(str(f), str(vault)).replace("\\", "/"))
    return sorted(hits)

# ---------------------------------------------------------------- 写入器（独占创建 + 回滚）

class Writer:
    """只新增文件；任何写入都用 O_CREAT|O_EXCL，绝不覆盖已有文件。"""

    def __init__(self, vault: Path, cfg: dict, dry_run: bool = False):
        self.vault = vault
        self.cfg = cfg
        self.dry_run = dry_run
        self.created_files: list[Path] = []
        self.created_dirs: list[Path] = []

    # -- 目录
    def ensure_dir(self, path: Path) -> None:
        if self.dry_run:
            if not path.is_dir():
                self.created_dirs.append(path)
            return
        missing: list[Path] = []
        cur = path
        while not cur.is_dir():
            missing.append(cur)
            if cur == cur.parent:
                break
            cur = cur.parent
        path.mkdir(parents=True, exist_ok=True)
        for d in reversed(missing):
            if d not in self.created_dirs:
                self.created_dirs.append(d)

    # -- 候选文件名（重名自动加后缀，规则固定可预测）
    def pick_name(self, dirpath: Path, stem: str, ext: str) -> tuple[Path, int | None]:
        nam = self.cfg.get("naming") or {}
        pattern = str(nam.get("conflict_pattern") or "{stem} ({n})")
        max_suffix = int(nam.get("conflict_max_suffix", 999))
        reserved = {os.path.normcase(str(x)) for x in self.created_files}

        def free(path: Path) -> bool:
            return not path.exists() and os.path.normcase(str(path)) not in reserved

        first = dirpath / (stem + ext)
        if free(first):
            return first, None
        n = 2
        while n <= max_suffix:
            try:
                cand_name = pattern.format(stem=stem, n=n)
            except (KeyError, IndexError, ValueError):
                cand_name = "%s (%d)" % (stem, n)
            cand = dirpath / (cand_name + ext)
            if free(cand):
                return cand, n
            n += 1
        raise SkillError(
            "NAME_EXHAUSTED",
            "在 %s 下找不到可用的文件名（已尝试到 %s (%d)%s）" % (dirpath, stem, max_suffix, ext),
            "请调大 naming.conflict_max_suffix 或换一个标题。",
        )

    # -- 独占创建文本文件
    def write_text_exclusive(self, path: Path, text: str) -> None:
        if self.dry_run:
            self.created_files.append(path)
            return
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(path), flags, 0o666)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(str(path))
            except OSError:
                pass
            raise
        self.created_files.append(path)

    # -- 独占创建 + 复制二进制，复制后校验哈希
    def copy_exclusive(self, src: Path, dst: Path, expect_sha: str, expect_size: int) -> None:
        if self.dry_run:
            self.created_files.append(dst)
            return
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(dst), flags, 0o666)
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                try:
                    os.fsync(out.fileno())
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(str(dst))
            except OSError:
                pass
            raise
        got_size = dst.stat().st_size
        got_sha = sha256_file(dst)
        if got_size != expect_size or got_sha != expect_sha:
            try:
                os.unlink(str(dst))
            except OSError:
                pass
            raise SkillError(
                "COPY_VERIFY_FAILED",
                "附件复制后校验失败（内容不一致），已删除半成品：%s" % dst,
                "原图可能正在被其他程序占用，请稍后重试。",
            )
        self.created_files.append(dst)

    def rollback(self) -> list[str]:
        if self.dry_run:
            # dry-run 没有真正创建任何文件，绝不能删除同名已存在文件
            self.created_files = []
            self.created_dirs = []
            return []
        rolled: list[str] = []
        for p in reversed(self.created_files):
            try:
                if p.is_file() or p.is_symlink():
                    os.unlink(str(p))
                    rolled.append(str(p))
            except OSError:
                pass
        self.created_files = []
        for d in reversed(self.created_dirs):
            try:
                os.rmdir(str(d))   # 只在目录为空时成功，绝不误删已有内容
            except OSError:
                pass
        self.created_dirs = []
        return rolled


# ---------------------------------------------------------------- 附件处理

def place_attachments(note: dict, attach_dir: Path, vault: Path, cfg: dict,
                      writer: Writer, report_warnings: list[str]) -> list[dict]:
    placements: list[dict] = []
    naming = (cfg.get("naming") or {}).get("attachment_naming", "original")
    search_dirs = [attach_dir]
    gmode = (cfg.get("attachments") or {}).get("mode")
    if gmode != "global":
        gp = (cfg.get("attachments") or {}).get("global_path") or "Attachments"
        try:
            search_dirs.append(safe_join(vault, gp, cfg, "attachments.global_path"))
        except SkillError:
            pass
    for raw in note.get("images") or []:
        src = Path(os.path.expanduser(str(raw)))
        if not src.is_absolute():
            src = Path.cwd() / src
        if not src.exists():
            raise SkillError("SOURCE_MISSING", "原始图片不存在：%s" % src)
        if not src.is_file():
            raise SkillError("SOURCE_INVALID", "原始图片不是文件：%s" % src)
        src_real = Path(os.path.realpath(str(src)))
        if is_within(vault, src_real):
            report_warnings.append("图片本身已在 vault 内（%s），仍会按附件规则复制一份" % src_real.name)
        ext = os.path.splitext(src.name)[1].lower()
        if ext not in IMAGE_EXTS:
            raise SkillError(
                "SOURCE_UNSUPPORTED",
                "不支持的附件类型：%s（%s）" % (src.name, ext or "无扩展名"),
                "支持的类型：%s" % ", ".join(sorted(IMAGE_EXTS)),
            )
        size = src_real.stat().st_size
        if size == 0:
            raise SkillError("SOURCE_INVALID", "原始图片是 0 字节文件：%s" % src_real)
        sha = sha256_file(src_real)

        entry: dict = {
            "source": str(src),
            "source_abs": str(src_real),
            "source_name": src.name,
            "source_stem": os.path.splitext(src.name)[0],
            "source_sha256": sha,
            "size": size,
        }

        # 1) 内容相同的附件已存在 -> 复用，不新建
        found = find_same_content(vault, cfg, sha, size, search_dirs)
        if found and found.get("path"):
            entry.update(status="reused", final_name=found["name"],
                         path=found["path"], rel_path=found["rel"],
                         name_conflict=False, reused_from=found["rel"])
            if found.get("truncated"):
                report_warnings.append("附件扫描达到上限，去重结果可能不完整")
            placements.append(entry)
            continue
        if found and found.get("truncated"):
            report_warnings.append("附件数量超过扫描上限，已跳过内容去重，改为新建附件")

        # 2) 生成附件名
        base_stem, changed_from = sanitize_component(os.path.splitext(src.name)[0], cfg, "附件名")
        if naming == "hash":
            base_stem = "%s-%s" % (_clamp_with_ext(base_stem, 60), sha[:8])
        entry["sanitized_from"] = changed_from
        # 3) 同名但内容不同 -> 自动加后缀；同名且内容相同已在上面复用
        target, suffix = _copy_with_fallback(writer, attach_dir, base_stem, ext, src_real, sha, size)
        rel = os.path.relpath(str(target), str(vault)).replace("\\", "/")
        entry.update(status="created", final_name=target.name, path=target,
                     rel_path=rel, name_conflict=suffix is not None,
                     conflict_suffix=suffix)
        if changed_from:
            entry["renamed_from"] = changed_from
        placements.append(entry)
    return placements

def _copy_with_fallback(writer: Writer, dirpath: Path, stem: str, ext: str,
                        src: Path, sha: str, size: int) -> tuple[Path, int | None]:
    """独占创建复制；若发生竞态（名字被别人抢先占用）则自动换下一个后缀。"""
    max_suffix = int((writer.cfg.get("naming") or {}).get("conflict_max_suffix", 999))
    pattern = str((writer.cfg.get("naming") or {}).get("conflict_pattern") or "{stem} ({n})")
    target, suffix = writer.pick_name(dirpath, stem, ext)
    n = suffix if suffix is not None else 1
    while True:
        try:
            writer.copy_exclusive(src, target, sha, size)
            return target, suffix
        except FileExistsError:
            n += 1
            if n > max_suffix + 1:
                raise SkillError("NAME_EXHAUSTED", "在 %s 下无法为附件找到可用文件名" % dirpath)
            name = pattern.format(stem=stem, n=n) if n >= 2 else stem + ext
            target = dirpath / (name + ext)
            suffix = n if n >= 2 else None


def _write_note_with_fallback(writer: Writer, dirpath: Path, stem: str, ext: str,
                              text: str) -> tuple[Path, int | None]:
    max_suffix = int((writer.cfg.get("naming") or {}).get("conflict_max_suffix", 999))
    pattern = str((writer.cfg.get("naming") or {}).get("conflict_pattern") or "{stem} ({n})")
    target, suffix = writer.pick_name(dirpath, stem, ext)
    n = suffix if suffix is not None else 1
    while True:
        try:
            writer.write_text_exclusive(target, text)
            return target, suffix
        except FileExistsError:
            n += 1
            if n > max_suffix + 1:
                raise SkillError("NAME_EXHAUSTED", "在 %s 下无法为笔记找到可用文件名" % dirpath)
            name = pattern.format(stem=stem, n=n) if n >= 2 else stem + ext
            target = dirpath / (name + ext)
            suffix = n if n >= 2 else None


def resolve_dirs(cfg: dict, vault: Path, folder: str) -> tuple[str, Path, str, Path]:
    """返回 (笔记目录相对路径, 绝对路径, 附件目录相对路径, 绝对路径)。"""
    notes_root, _ = sanitize_rel_path(cfg.get("notes_root"), cfg, "notes_root")
    folder_clean, _ = sanitize_rel_path(folder, cfg, "课程文件夹")
    if not folder_clean:
        raise SkillError(
            "CONFIG_INVALID",
            "课程文件夹名为空，无法确定保存位置",
            "请检查 config.json 的 unknown_course_folder / courses[].folder。",
        )
    parts = [p for p in [notes_root, folder_clean] if p]
    note_rel = "/".join(parts)
    note_dir = safe_join(vault, note_rel, cfg, "课程目录") if note_rel else vault
    att = cfg.get("attachments") or {}
    mode = att.get("mode", "subfolder")
    if mode == "course":
        att_rel = note_rel
    elif mode == "global":
        att_rel, _ = sanitize_rel_path(att.get("global_path") or "Attachments", cfg,
                                      "attachments.global_path")
    else:
        subdir, _ = sanitize_rel_path(att.get("subdir") or "attachments", cfg,
                                     "attachments.subdir")
        att_rel = (note_rel + "/" + subdir) if (note_rel and subdir) else (note_rel or subdir)
    att_dir = safe_join(vault, att_rel, cfg, "附件目录") if att_rel else vault
    return note_rel, note_dir, att_rel, att_dir


def build_note_text(note: dict, cls: dict, cfg: dict, placements: list[dict],
                    title: str) -> tuple[str, list[str], bool]:
    """生成最终笔记正文（frontmatter + 正文），返回 (文本, 说明列表, has_uncertain)。"""
    infos: list[str] = []
    body = note["_body"]
    existing_fm, body_wo_fm = split_frontmatter(body)
    if existing_fm is not None:
        infos.append("正文自带 frontmatter，已合并（脚本生成的必填字段优先）")
    raw_extra: list[str] = []
    if existing_fm:
        known = {"title", "course", "date", "source", "source_sha256", "tags",
                 "has_uncertain", "classified_by", "tool", "tool_version"}
        fm_lines = existing_fm.split("\n")
        i = 0
        while i < len(fm_lines):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", fm_lines[i])
            if m and m.group(1) not in known:
                block = [fm_lines[i]]
                i += 1
                while i < len(fm_lines) and fm_lines[i][:1] in (" ", "\t") and fm_lines[i].strip():
                    block.append(fm_lines[i])
                    i += 1
                raw_extra.append("\n".join(block))
                continue
            i += 1

    uncertain_items = [str(u).strip() for u in (note.get("uncertain") or []) if str(u).strip()]
    has_uncertain = detect_uncertain(body_wo_fm, uncertain_items)

    # 把未写进正文的不确定项，补成 Obsidian callout
    todo = [u for u in uncertain_items if u not in body_wo_fm]
    if todo:
        block = "\n\n> [!warning] 待核对\n" + "".join("> - %s\n" % t for t in todo)
        body_wo_fm = body_wo_fm.rstrip("\n") + block
        infos.append("已把 %d 条不确定内容写入「待核对」callout" % len(todo))

    # 缺少一级标题时补上（Obsidian 中更清晰）
    if (cfg.get("frontmatter") or {}).get("add_h1_title", True):
        if not re.search(r"(?m)^#\s+\S", body_wo_fm):
            body_wo_fm = "# %s\n\n%s" % (title, body_wo_fm.lstrip("\n"))
            infos.append("正文没有一级标题，已补上「# %s」" % title)

    fm_cfg = cfg.get("frontmatter") or {}
    tags: list[str] = []
    for t in list(note.get("tags") or []) + list(fm_cfg.get("extra_tags") or []):
        st = sanitize_tag(t)
        if st and st not in tags:
            tags.append(st)
    prefix = str(fm_cfg.get("tag_prefix") or "course/")
    if cls.get("folder"):
        ct = sanitize_tag(prefix + cls["folder"])
        if ct and ct not in tags:
            tags.insert(0, ct)

    fields: list[tuple[str, object]] = [
        ("title", title),
        ("course", cls.get("course") or cls.get("folder") or ""),
        ("date", Raw(_now().date().isoformat())),
        ("source", [p["source_name"] for p in placements] or list(note.get("source_files") or [])),
        ("source_sha256", [p["source_sha256"] for p in placements]),
        ("tags", tags),
        ("has_uncertain", bool(has_uncertain)),
        ("classified_by", cls.get("match") or "none"),
        ("tool", "%s %s" % (TOOL_NAME, VERSION)),
    ]
    text = build_frontmatter(fields, raw_extra) + "\n" + body_wo_fm.strip("\n") + "\n"
    return text, infos, has_uncertain


def process_note(note: dict, cfg: dict, vault: Path, writer: Writer,
                 warnings: list[str], allow_duplicate: bool) -> dict:
    entry: dict = {
        "title": None, "status": "pending", "course": None, "course_match": None,
        "course_is_new": False, "needs_review": False, "reason": "",
        "note_path": None, "note_abs_path": None, "conflict_suffix": None,
        "renamed_from": None, "attachments": [], "duplicate_of": [],
        "validation": {"errors": 0, "warnings": 0, "infos": 0, "items": []},
        "has_uncertain": False, "infos": [],
    }

    # --- 正文
    if note.get("body_file"):
        bf = Path(os.path.expanduser(str(note["body_file"])))
        if not bf.is_absolute():
            bf = Path.cwd() / bf
        if not bf.is_file():
            raise SkillError("BODY_MISSING", "正文文件不存在：%s" % bf)
        body, enc, enc_warns = read_text_lenient(bf)
        warnings.extend(enc_warns)
        entry["body_encoding"] = enc
    elif note.get("body_markdown") is not None:
        body = str(note["body_markdown"])
    else:
        raise SkillError("BODY_MISSING", "笔记缺少 body_file 或 body_markdown")
    if not body.strip():
        raise SkillError("BODY_EMPTY", "正文为空，拒绝创建空笔记")
    note["_body"] = body.replace("\r\n", "\n").replace("\r", "\n")

    # --- 标题
    raw_title = str(note.get("title") or "").strip()
    if not raw_title:
        raw_title = "未命名笔记-%s" % _now().strftime("%Y%m%d-%H%M%S")
        entry["infos"].append("未提供标题，已使用「%s」" % raw_title)
    title, title_changed = sanitize_component(raw_title, cfg, "笔记标题")
    if title_changed:
        entry["renamed_from"] = title_changed
        entry["infos"].append("标题含非法/特殊字符，已清洗：%r -> %r" % (title_changed, title))

    # --- 课程分类
    cls = classify_course(cfg, note.get("course"), note.get("course_hint"), title, note["_body"])
    entry.update(course=cls.get("course") or cls.get("folder"), course_match=cls["match"],
                 course_is_new=cls["is_new_course"], needs_review=cls["needs_review"],
                 reason=cls["reason"], course_scores=cls.get("scores") or [],
                 course_folder=cls["folder"])

    notes_root = str(cfg.get("notes_root") or "").strip().strip("/\\")
    note_rel, note_dir, att_rel, att_dir = resolve_dirs(cfg, vault, cls["folder"])
    entry["note_dir"] = note_rel or "(vault 根目录)"
    entry["attachment_dir"] = att_rel or "(vault 根目录)"

    # --- 原图哈希 & 重复图片检测（在写入任何文件之前）
    image_paths = [Path(os.path.expanduser(str(x))) for x in (note.get("images") or [])]
    hashes: list[str] = []
    for ip in image_paths:
        if not ip.is_absolute():
            ip = Path.cwd() / ip
        if not ip.is_file():
            raise SkillError("SOURCE_MISSING", "原始图片不存在：%s" % ip)
        hashes.append(sha256_file(Path(os.path.realpath(str(ip)))))
    dup_notes: list[str] = []
    for h in hashes:
        for p in find_notes_by_hash(vault, cfg, h, notes_root):
            if p not in dup_notes:
                dup_notes.append(p)
    entry["duplicate_of"] = dup_notes
    policy = "allow" if allow_duplicate else (cfg.get("dedupe") or {}).get("duplicate_image_policy", "skip")
    if hashes and dup_notes and policy == "skip":
        entry["status"] = "skipped_duplicate"
        entry["title"] = title
        entry["infos"].append("该图片已整理过，默认不重复生成（可用 --allow-duplicate 强制新建）")
        return entry

    # --- 目录
    writer.ensure_dir(note_dir)
    if image_paths:
        writer.ensure_dir(att_dir)

    # --- 附件
    placements = place_attachments(note, att_dir, vault, cfg, writer, warnings)
    entry["attachments"] = [
        {k: (str(v) if isinstance(v, Path) else v) for k, v in p.items() if k != "path"}
        for p in placements
    ]

    # --- 嵌入链接改写
    body_final, embed_notes = rewrite_embeds(note["_body"], placements, cfg, vault, note_rel)
    entry["infos"].extend(embed_notes)
    note["_body"] = body_final

    # --- 校验
    issues = validate_markdown(note["_body"])
    entry["validation"] = summarize_issues(issues)

    # --- frontmatter + 落盘
    stem, clamped = clamp_for_total_path(note_dir, title, ".md", cfg)
    if clamped:
        entry["infos"].append("文件名过长，已截断为「%s」" % stem)
    text, fm_infos, has_uncertain = build_note_text(note, cls, cfg, placements, stem)
    entry["infos"].extend(fm_infos)
    entry["has_uncertain"] = has_uncertain
    target, suffix = _write_note_with_fallback(writer, note_dir, stem, ".md", text)
    entry["conflict_suffix"] = suffix
    if suffix:
        entry["infos"].append("同名笔记已存在，已改用「%s」" % target.name)
    entry["title"] = stem
    entry["note_path"] = os.path.relpath(str(target), str(vault)).replace("\\", "/")
    entry["note_abs_path"] = str(target)
    entry["status"] = "dry_run" if writer.dry_run else "saved"

    # --- 嵌入链接是否都能在 vault 中找到（防止链接指向不存在的文件）
    for m in re.finditer(r"!\[\[([^\]\|]+)", text):
        tgt = m.group(1).strip()
        if tgt.endswith(tuple(IMAGE_EXTS)):
            if not (att_dir / tgt).exists() and not writer.dry_run:
                cand = list(vault.rglob(tgt))
                if not cand:
                    entry["validation"]["items"].append(_issue(
                        "warning", "embed_target_missing", 0,
                        "嵌入的图片在 vault 中找不到：%s" % tgt))
                    entry["validation"]["warnings"] += 1
    return entry

# ---------------------------------------------------------------- 报告

MATCH_LABEL = {
    "explicit": "用户明确指定",
    "explicit-invalid": "用户指定的课程名不可用作文件夹名",
    "explicit-new": "用户明确指定（新课程）",
    "explicit-ignored": "用户指定但配置中无此课程",
    "hint": "图片中识别到课程名",
    "hint-new": "图片中识别到课程名（新课程）",
    "keyword": "配置关键词匹配",
    "ambiguous": "关键词打分并列，无法确定",
    "none": "无法确定",
}


def human_report(rep: dict) -> str:
    L: list[str] = []
    dry = rep.get("dry_run")
    head = "DRY-RUN 预演（未写入任何文件）" if dry else "已保存到 Obsidian"
    if not rep.get("ok"):
        head = "保存失败"
    L.append("%s%s" % ("[!] " if not rep.get("ok") else ("[dry-run] " if dry else "[ok] "), head))
    L.append("vault：%s" % rep.get("vault"))
    if rep.get("config_path"):
        L.append("配置：%s" % rep["config_path"])
    s = rep.get("summary") or {}
    L.append("统计：新建笔记 %d，跳过重复 %d，附件新建 %d，复用 %d，校验错误 %d，告警 %d"
             % (s.get("saved", 0), s.get("skipped", 0), s.get("attachments_created", 0),
                s.get("attachments_reused", 0), s.get("validation_errors", 0),
                s.get("validation_warnings", 0)))
    for i, n in enumerate(rep.get("notes") or [], 1):
        L.append("")
        L.append("%d) %s  [%s]" % (i, n.get("title") or "(无标题)", n.get("status")))
        L.append("   分类结果：%s（依据：%s）" % (n.get("course"), MATCH_LABEL.get(n.get("course_match"), n.get("course_match"))))
        if n.get("reason"):
            L.append("   判定说明：%s" % n["reason"])
        verb = "将写入" if dry else "笔记路径"
        if n.get("note_path"):
            L.append("   %s：%s" % (verb, n["note_path"]))
        if n.get("note_abs_path"):
            L.append("   绝对路径：%s" % n["note_abs_path"])
        bits = []
        if n.get("renamed_from"):
            bits.append("标题 %r 含非法/特殊字符，已清洗为 %r" % (n["renamed_from"], n.get("title")))
        if n.get("conflict_suffix"):
            bits.append("同名笔记已存在，新笔记改用「%s」"
                        % os.path.basename(n.get("note_path") or ""))
        for a in (n.get("attachments") or []):
            if a.get("name_conflict"):
                bits.append("附件同名但内容不同，已改用「%s」" % a.get("final_name"))
            elif a.get("renamed_from"):
                bits.append("附件名 %r 已清洗为 %r" % (a["renamed_from"], a.get("final_name")))
        L.append("   重名处理：%s" % ("；".join(bits) if bits else "无"))
        atts = n.get("attachments") or []
        if not atts:
            L.append("   附件：无")
        if atts:
            L.append("   附件（%d）：" % len(atts))
            for a in atts:
                flag = {"created": "新建", "reused": "复用", "dry_run": "将新建"}.get(a.get("status"), a.get("status"))
                extra = ""
                if a.get("name_conflict"):
                    extra = "，同名不同内容已加后缀(%s)" % a.get("conflict_suffix")
                if a.get("renamed_from"):
                    extra += "，文件名已清洗自 %r" % a["renamed_from"]
                L.append("     - [%s] %s%s" % (flag, a.get("rel_path") or a.get("final_name"), extra))
        if n.get("duplicate_of"):
            L.append("   重复图片：这张图已整理过，对应笔记：%s" % ", ".join(n["duplicate_of"]))
        v = n.get("validation") or {}
        if v.get("errors") or v.get("warnings"):
            L.append("   校验：%d 错误 / %d 告警" % (v.get("errors", 0), v.get("warnings", 0)))
            for it in (v.get("items") or []):
                if it["level"] == "info":
                    continue
                L.append("     - [%s L%s] %s" % (it["level"], it.get("line"), it["message"]))
        L.append("   未能识别/待核对：%s" % ("有" if n.get("has_uncertain") else "无"))
        if n.get("infos"):
            L.append("   处理说明：%s" % "；".join(n["infos"]))
    news = [n for n in (rep.get("notes") or []) if n.get("course_is_new")]
    review = [n for n in (rep.get("notes") or []) if n.get("needs_review")]
    L.append("")
    if news:
        L.append("需要您确认：")
        for n in news:
            L.append("  - 本次自动创建了新课程文件夹「%s」，建议加入 config.json 的 courses（含别名/关键词），或把笔记移到已有课程" % n.get("course"))
    if review and not news:
        L.append("需要您确认：")
    for n in review:
        if n.get("course_is_new"):
            continue
        L.append("  - 笔记「%s」课程无法确定，已放入「%s」，请手动归类" % (n.get("title"), n.get("course_folder")))
    for w in rep.get("warnings") or []:
        L.append("  - 告警：%s" % w)
    if rep.get("rolled_back"):
        L.append("  - 已回滚本次新建的半成品文件：%s" % ", ".join(rep["rolled_back"]))
    if rep.get("error"):
        L.append("  - 错误[%s]：%s" % (rep["error"].get("code"), rep["error"].get("message")))
        if rep["error"].get("hint"):
            L.append("  - 处理建议：%s" % rep["error"]["hint"])
    return "\n".join(L)


def summarize(rep: dict) -> dict:
    notes = rep.get("notes") or []
    out = {
        "saved": sum(1 for n in notes if n["status"] == "saved"),
        "dry_run": sum(1 for n in notes if n["status"] == "dry_run"),
        "skipped": sum(1 for n in notes if n["status"] == "skipped_duplicate"),
        "failed": sum(1 for n in notes if n["status"] == "failed"),
        "attachments_created": sum(1 for n in notes for a in (n.get("attachments") or []) if a.get("status") == "created"),
        "attachments_reused": sum(1 for n in notes for a in (n.get("attachments") or []) if a.get("status") == "reused"),
        "validation_errors": sum((n.get("validation") or {}).get("errors", 0) for n in notes),
        "validation_warnings": sum((n.get("validation") or {}).get("warnings", 0) for n in notes),
        "needs_review": sum(1 for n in notes if n.get("needs_review")),
        "new_courses": sorted({n.get("course") for n in notes if n.get("course_is_new") and n.get("course")}),
        "uncertain_notes": sum(1 for n in notes if n.get("has_uncertain")),
    }
    return out


# ---------------------------------------------------------------- 保存流程

def load_job(args) -> list[dict]:
    if args.job:
        jp = Path(os.path.expanduser(args.job))
        if not jp.is_absolute():
            jp = Path.cwd() / jp
        if not jp.is_file():
            raise SkillError("JOB_MISSING", "job 文件不存在：%s" % jp)
        try:
            data = json.loads(jp.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise SkillError("JOB_INVALID", "job 文件 JSON 语法错误（第 %d 行）：%s" % (exc.lineno, exc.msg))
        notes = data.get("notes") if isinstance(data, dict) else data
        if not isinstance(notes, list) or not notes:
            raise SkillError("JOB_INVALID", "job 文件必须包含非空的 notes 数组")
        return [dict(n) for n in notes]
    note = {
        "title": args.title,
        "course": args.course,
        "course_hint": args.course_hint,
        "body_file": args.body_file,
        "body_markdown": args.body_markdown,
        "images": list(args.image or []),
        "tags": list(args.tag or []),
        "uncertain": list(args.uncertain or []),
        "source_files": list(args.source_file or []),
    }
    if not note["title"] and not note["body_file"] and note["body_markdown"] is None:
        raise SkillError("ARGS_INVALID", "请用 --job，或用 --title/--body-file 提供单篇笔记")
    return [note]


def run_save(args) -> dict:
    warnings: list[str] = []
    cfg, cfg_path, cfg_warns = load_config(args.config)
    warnings.extend(cfg_warns)
    vault = resolve_vault(cfg, args.vault, args.allow_non_vault)
    notes_in = load_job(args)
    writer = Writer(vault, cfg, dry_run=bool(args.dry_run))
    rep: dict = {
        "ok": True, "tool": TOOL_NAME, "version": VERSION,
        "dry_run": bool(args.dry_run), "vault": str(vault),
        "config_path": str(cfg_path) if cfg_path else None,
        "generated_at": _now().isoformat(timespec="seconds"),
        "notes": [], "created_files": [], "rolled_back": [],
        "warnings": warnings, "error": None,
    }
    keep_on_error = bool(args.keep_on_error)
    failure: SkillError | None = None
    for raw in notes_in:
        try:
            entry = process_note(raw, cfg, vault, writer, warnings, bool(args.allow_duplicate))
        except SkillError as exc:
            failure = exc
            rep["notes"].append({"title": raw.get("title"), "status": "failed",
                                 "error": exc.to_dict()})
            break
        except OSError as exc:
            failure = SkillError("WRITE_FAILED", "写入失败：%s" % exc,
                                 "请检查磁盘空间、文件是否被 Obsidian/同步程序占用。")
            rep["notes"].append({"title": raw.get("title"), "status": "failed",
                                 "error": failure.to_dict()})
            break
        if args.strict and (entry.get("validation") or {}).get("errors"):
            failure = SkillError("VALIDATION_STRICT",
                                 "笔记「%s」存在 %d 个校验错误（--strict 模式下拒绝写入）"
                                 % (entry.get("title"), entry["validation"]["errors"]),
                                 "修正公式/代码块后重试，或去掉 --strict 仅记录告警。")
            rep["notes"].append(entry)
            break
        rep["notes"].append(entry)
    if failure is not None:
        rep["ok"] = False
        rep["error"] = failure.to_dict()
        if not keep_on_error:
            rep["rolled_back"] = writer.rollback()
        if keep_on_error:
            rep["created_files"] = [str(p) for p in writer.created_files]
    else:
        rep["created_files"] = [str(p) for p in writer.created_files]
    rep["summary"] = summarize(rep)
    rep["human_report"] = human_report(rep)
    if args.report_file:
        rp = Path(os.path.expanduser(args.report_file))
        if not rp.is_absolute():
            rp = Path.cwd() / rp
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8", newline="\n")
    return rep


# ---------------------------------------------------------------- 其它子命令

def run_doctor(args) -> dict:
    out: dict = {"ok": True, "tool": TOOL_NAME, "version": VERSION, "checks": []}

    def add(name, ok, detail, hint=None):
        out["checks"].append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})
        if not ok:
            out["ok"] = False

    add("python", sys.version_info >= (3, 9), "Python %s" % sys.version.split()[0],
        None if sys.version_info >= (3, 9) else "需要 Python 3.9 或更新版本")
    try:
        cfg, cfg_path, warns = load_config(args.config)
        add("config", True, "已加载 %s" % cfg_path, None)
        for w in warns:
            add("config-warning", True, w, None)
        add("courses", bool(cfg["courses"]),
            "已配置 %d 门课程：%s" % (len(cfg["courses"]), ", ".join(c["name"] for c in cfg["courses"])),
            None if cfg["courses"] else "config.json 的 courses 为空，所有内容都会进入待分类")
        try:
            vault = resolve_vault(cfg, args.vault, args.allow_non_vault)
            add("vault", True, str(vault), None)
            nr = str(cfg.get("notes_root") or "").strip().strip("/\\")
            nd = safe_join(vault, nr, cfg, "notes_root") if nr else vault
            add("notes_root", True, "%s（存在=%s）" % (nd, nd.is_dir()), None)
            writable = os.access(str(vault), os.W_OK)
            add("writable", writable, "vault 可写=%s" % writable,
                None if writable else "Codex 需要获得该目录的写入权限")
            add("obsidian_git", not (vault / ".git").is_dir() or True,
                "vault 是 git 仓库=%s（本 Skill 只新增文件，不会执行任何 git 操作）" % (vault / ".git").is_dir(), None)
        except SkillError as exc:
            add("vault", False, "%s: %s" % (exc.code, exc.message), exc.hint)
    except SkillError as exc:
        add("config", False, "%s: %s" % (exc.code, exc.message), exc.hint)
    lines = ["doctor 结果：%s" % ("通过" if out["ok"] else "存在问题")]
    for c in out["checks"]:
        lines.append("  [%s] %s：%s" % ("ok" if c["ok"] else "FAIL", c["name"], c["detail"]))
        if c.get("hint") and not c["ok"]:
            lines.append("        -> %s" % c["hint"])
    out["human_report"] = "\n".join(lines)
    return out


def detect_courses(vault: Path, cfg: dict) -> list[dict]:
    unknown = str(cfg.get("unknown_course_folder") or "待分类")
    out: list[dict] = []
    for name in sorted(os.listdir(vault)):
        p = vault / name
        if not p.is_dir() or name.startswith(".") or name in SKIP_DIRS or name == unknown:
            continue
        out.append({"name": name, "folder": name, "aliases": [name], "keywords": []})
    return out


def run_init_config(args) -> dict:
    dest = Path(os.path.expanduser(args.dest)) if args.dest else (SKILL_DIR / "config.json")
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    if dest.exists() and not args.force:
        raise SkillError("CONFIG_EXISTS", "配置文件已存在：%s" % dest,
                         "加 --force 覆盖，或直接编辑该文件。")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    vault_path = args.vault or os.environ.get("OBSIDIAN_IMAGE_NOTES_VAULT") or ""
    if vault_path:
        vp = Path(os.path.expanduser(vault_path))
        if not vp.is_dir():
            raise SkillError("VAULT_MISSING", "vault 路径不存在：%s" % vp)
        cfg["vault_path"] = str(Path(os.path.realpath(str(vp))))
        if not (cfg["vault_path"] and (Path(cfg["vault_path"]) / ".obsidian").is_dir()):
            cfg["_warning"] = "该目录下没有 .obsidian，请确认是否为 Obsidian 知识库根目录"
        if args.detect_courses:
            found = detect_courses(Path(cfg["vault_path"]), cfg)
            if found:
                cfg["courses"] = found
    else:
        cfg["vault_path"] = ""
        cfg["_warning"] = "请填写 vault_path，或设置环境变量 OBSIDIAN_IMAGE_NOTES_VAULT"
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return {"ok": True, "config_path": str(dest), "vault_path": cfg.get("vault_path"),
            "courses": [c["name"] for c in cfg.get("courses", [])],
            "human_report": "已生成配置：%s\nvault：%s\n课程：%s"
                            % (dest, cfg.get("vault_path") or "(未填写)",
                               ", ".join(c["name"] for c in cfg.get("courses", [])))}


def run_check_images(args) -> dict:
    cfg, cfg_path, _ = load_config(args.config)
    vault = resolve_vault(cfg, args.vault, args.allow_non_vault)
    notes_root = str(cfg.get("notes_root") or "").strip().strip("/\\")
    items = []
    for raw in args.images:
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            items.append({"source": str(p), "error": "文件不存在"})
            continue
        real = Path(os.path.realpath(str(p)))
        sha = sha256_file(real)
        size = real.stat().st_size
        found = find_same_content(vault, cfg, sha, size, [vault])
        notes = find_notes_by_hash(vault, cfg, sha, notes_root)
        items.append({
            "source": str(p), "sha256": sha, "size": size,
            "existing_attachment": (found or {}).get("rel"),
            "existing_notes": notes,
            "already_processed": bool(notes),
        })
    rep = {"ok": True, "vault": str(vault), "images": items,
           "already_processed": [i["source"] for i in items if i.get("already_processed")]}
    lines = ["图片检查（vault：%s）" % vault]
    for i in items:
        if i.get("error"):
            lines.append("  - %s：%s" % (i["source"], i["error"]))
            continue
        lines.append("  - %s" % i["source"])
        lines.append("      sha256=%s size=%d" % (i["sha256"][:16] + "...", i["size"]))
        lines.append("      已整理过：%s%s" % ("是" if i["already_processed"] else "否",
                     ("，对应笔记：" + ", ".join(i["existing_notes"])) if i["existing_notes"] else ""))
        if i.get("existing_attachment"):
            lines.append("      vault 中已有相同内容的附件：%s" % i["existing_attachment"])
    rep["human_report"] = "\n".join(lines)
    return rep


def run_validate(args) -> dict:
    p = Path(os.path.expanduser(args.file))
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        raise SkillError("BODY_MISSING", "文件不存在：%s" % p)
    text, enc, warns = read_text_lenient(p)
    issues = summarize_issues(validate_markdown(text))
    rep = {"ok": issues["errors"] == 0, "file": str(p), "encoding": enc,
           "warnings_input": warns, "validation": issues}
    lines = ["校验 %s（%d 错误 / %d 告警 / %d 提示）"
             % (p.name, issues["errors"], issues["warnings"], issues["infos"])]
    for it in issues["items"]:
        lines.append("  [%s L%s] %s: %s" % (it["level"], it["line"], it["code"], it["message"]))
    if not issues["items"]:
        lines.append("  未发现问题")
    rep["human_report"] = "\n".join(lines)
    return rep

# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="obsidian_image_notes.py",
        description="Obsidian 图片笔记 Skill 的确定性落盘工具（永不覆盖已有文件）",
    )
    ap.add_argument("--version", action="version", version="%s %s" % (TOOL_NAME, VERSION))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", help="配置文件路径（默认 <skill>/config.json）")
        p.add_argument("--vault", help="临时覆盖 vault 路径")
        p.add_argument("--allow-non-vault", action="store_true",
                       help="允许目录中没有 .obsidian（仅测试用）")
        p.add_argument("--json", action="store_true", help="只输出 JSON（供模型解析）")

    p = sub.add_parser("save", help="把整理好的笔记安全写入 vault")
    common(p)
    p.add_argument("--job", help="批量任务 JSON 文件（推荐用于多张图片）")
    p.add_argument("--title", help="单篇模式：笔记标题")
    p.add_argument("--course", help="单篇模式：用户明确指定的课程")
    p.add_argument("--course-hint", dest="course_hint", help="单篇模式：从图片中读到的课程名")
    p.add_argument("--body-file", dest="body_file", help="单篇模式：UTF-8 Markdown 正文文件")
    p.add_argument("--body-markdown", dest="body_markdown", help="单篇模式：直接给正文文本")
    p.add_argument("--image", action="append", help="单篇模式：原图路径（可重复）")
    p.add_argument("--tag", action="append", help="单篇模式：标签（可重复）")
    p.add_argument("--uncertain", action="append", help="单篇模式：不确定内容说明（可重复）")
    p.add_argument("--source-file", dest="source_file", action="append",
                   help="单篇模式：frontmatter 里 source 字段用的原始文件名")
    p.add_argument("--dry-run", action="store_true", help="只预演，不写入任何文件")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="即使该图片已整理过也强制新建笔记")
    p.add_argument("--strict", action="store_true", help="校验有 error 时拒绝写入")
    p.add_argument("--keep-on-error", action="store_true",
                   help="失败时保留已写入的文件（默认回滚）")
    p.add_argument("--report-file", dest="report_file", help="把完整 JSON 报告写到该文件")

    p = sub.add_parser("doctor", help="检查配置、vault、权限是否正常")
    common(p)

    p = sub.add_parser("init-config", help="生成 config.json")
    p.add_argument("--vault", help="Obsidian vault 路径")
    p.add_argument("--dest", help="配置文件输出路径（默认 <skill>/config.json）")
    p.add_argument("--detect-courses", action="store_true",
                   help="用 vault 里已有的一级目录作为课程列表")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check-images", help="检查图片是否已整理过（不写入）")
    common(p)
    p.add_argument("images", nargs="+", help="图片路径")

    p = sub.add_parser("validate", help="只校验 Markdown / LaTeX")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    return ap


def emit(rep: dict, as_json: bool) -> int:
    text = rep.get("human_report") or ""
    if as_json:
        sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return 0 if rep.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "save":
            return emit(run_save(args), args.json)
        if args.cmd == "doctor":
            return emit(run_doctor(args), args.json)
        if args.cmd == "init-config":
            return emit(run_init_config(args), args.json)
        if args.cmd == "check-images":
            return emit(run_check_images(args), args.json)
        if args.cmd == "validate":
            return emit(run_validate(args), args.json)
    except SkillError as exc:
        rep = {"ok": False, "tool": TOOL_NAME, "version": VERSION, "error": exc.to_dict()}
        rep["human_report"] = "[!] 失败[%s]：%s%s" % (
            exc.code, exc.message, ("\n    处理建议：%s" % exc.hint) if exc.hint else "")
        return emit(rep, getattr(args, "json", False)) or 1
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130
    except Exception as exc:  # 兜底：不留下 traceback 噪音，但要报告清楚
        import traceback
        rep = {"ok": False, "tool": TOOL_NAME, "version": VERSION,
               "error": {"code": "UNEXPECTED", "message": "%s: %s" % (type(exc).__name__, exc)}}
        rep["human_report"] = "[!] 未预期的错误：%s: %s" % (type(exc).__name__, exc)
        sys.stderr.write(traceback.format_exc() + "\n")
        return emit(rep, getattr(args, "json", False)) or 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())