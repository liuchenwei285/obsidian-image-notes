#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian 图片笔记 Skill 的真实测试用例（只用标准库 unittest）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_obsidian_image_notes.py

每个用例都在系统临时目录里新建一个一次性的假 vault，绝不碰你真实的笔记库。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "obsidian_image_notes.py"

_spec = importlib.util.spec_from_file_location("oin", str(SCRIPT))
oin = importlib.util.module_from_spec(_spec)
sys.modules["oin"] = oin
_spec.loader.exec_module(oin)


def run_cli(args):
    """真实调用 CLI，返回 (exit_code, stdout, parsed_json_or_None)。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = oin.main([str(a) for a in args])
    out = buf.getvalue()
    try:
        return code, out, json.loads(out)
    except json.JSONDecodeError:
        return code, out, None


def _make_dir_link(link: Path, target: Path) -> bool:
    """先试 symlink（需权限），再试 Windows junction（不需权限）。"""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True)
        if r.returncode == 0 and link.exists():
            return True
    return False


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def snapshot(root: Path):
    """vault 全量快照：相对路径 -> (大小, sha256)。用于证明“只新增，不改动”。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = os.path.relpath(str(p), str(root)).replace("\\", "/")
            try:
                out[rel] = (p.stat().st_size, sha(p))
            except OSError:
                out[rel] = (-1, "")
    return out


COURSES = [
    {"name": "C语言", "folder": "C语言", "aliases": ["C", "C Language"],
     "keywords": ["printf", "scanf", "指针", "结构体", "malloc", "数据类型", "sizeof"]},
    {"name": "高等数学", "folder": "高数", "aliases": ["高数", "微积分"],
     "keywords": ["极限", "导数", "积分", "级数", "洛必达", "微分"]},
    {"name": "数据结构", "folder": "数据结构", "aliases": ["DS"],
     "keywords": ["二叉树", "哈希表", "排序算法", "时间复杂度", "队列", "堆"]},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oin-test-"))
        self.vault = self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.imgs = self.tmp / "imgs"
        self.imgs.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.cfg_path = self.tmp / "config.json"
        self._saved_env = {}
        for k in ("OBSIDIAN_IMAGE_NOTES_VAULT", "OBSIDIAN_VAULT",
                  "OBSIDIAN_IMAGE_NOTES_CONFIG"):
            if k in os.environ:
                self._saved_env[k] = os.environ.pop(k)
        self.write_config()
        self.script = SCRIPT

    def tearDown(self):
        for k, v in getattr(self, "_saved_env", {}).items():
            os.environ[k] = v

        def onerr(func, path, exc):
            try:
                os.chmod(path, 0o666)
                func(path)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True, onerror=onerr)

    # ---- 配置
    def write_config(self, **over):
        cfg = {
            "vault_path": str(self.vault),
            "notes_root": over.pop("notes_root", ""),
            "unknown_course_folder": "待分类",
            "allow_new_course": over.pop("allow_new_course", True),
            "courses": over.pop("courses", json.loads(json.dumps(COURSES))),
        }
        cfg.update(over)
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                 encoding="utf-8", newline="\n")
        return cfg

    def cli(self, *args, **kw):
        base = ["--config", self.cfg_path] if kw.get("with_config", True) else []
        return run_cli(list(args[:1]) + base + list(args[1:]))

    def save(self, title, body, course=None, hint=None, images=(), tags=(),
             uncertain=(), extra=()):
        body_file = self.make_body(body)
        args = ["save", "--config", self.cfg_path, "--title", title,
                "--body-file", str(body_file), "--json"]
        if course:
            args += ["--course", course]
        if hint:
            args += ["--course-hint", hint]
        for im in images:
            args += ["--image", str(im)]
        for t in tags:
            args += ["--tag", t]
        for u in uncertain:
            args += ["--uncertain", u]
        args += list(extra)
        return run_cli(args)

    def make_body(self, text):
        p = self.work / ("body-%d.md" % (len(list(self.work.glob("body-*.md"))) + 1))
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def make_image(self, name, payload=b"PNGDATA"):
        p = self.imgs / name
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        p.write_bytes(payload)
        return p

    def vault_file(self, *parts):
        return self.vault.joinpath(*parts)

    def read_note(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def frontmatter(self, rel):
        fm, body = oin.split_frontmatter(self.read_note(rel))
        self.assertIsNotNone(fm, "笔记缺少 frontmatter：%s" % rel)
        return fm, body


class TestSetupAndNormalSave(Base):
    def test_01_doctor_reports_ok(self):
        code, out, rep = run_cli(["doctor", "--config", str(self.cfg_path), "--json"])
        self.assertEqual(code, 0, out)
        self.assertTrue(rep["ok"], out)
        names = {c["name"]: c["ok"] for c in rep["checks"]}
        for key in ("python", "config", "vault", "courses", "writable"):
            self.assertIn(key, names)
            self.assertTrue(names[key], "%s 检查未通过：%s" % (key, names))

    def test_02_normal_save_explicit_course(self):
        img = self.make_image("IMG_0001.png", b"img-bytes-A")
        code, out, rep = self.save(
            "if语句与条件判断",
            "## 语法\n\n```c\nif (a > b) { printf(\"%d\", a); }\n```\n\n行内公式 $a > b$ 与独立公式：\n\n$$\n\\begin{aligned}\nx &= 1 \\\\\ny &= 2\n\\end{aligned}\n$$\n",
            course="C语言", images=[img], tags=["控制结构"])
        self.assertEqual(code, 0, out)
        self.assertTrue(rep["ok"], out)
        n = rep["notes"][0]
        self.assertEqual(n["status"], "saved")
        self.assertEqual(n["note_path"], "C语言/if语句与条件判断.md")
        self.assertEqual(n["course_match"], "explicit")
        self.assertFalse(n["needs_review"])
        self.assertTrue(self.vault_file("C语言", "if语句与条件判断.md").is_file())
        # 附件已复制
        att = self.vault_file("C语言", "attachments", "IMG_0001.png")
        self.assertTrue(att.is_file(), out)
        self.assertEqual(sha(att), sha(img))
        self.assertEqual(n["attachments"][0]["status"], "created")
        # frontmatter 字段齐全
        fm, body = self.frontmatter("C语言/if语句与条件判断.md")
        for key in ("title:", "course:", "date:", "source:", "source_sha256:",
                    "tags:", "has_uncertain:", "classified_by:"):
            self.assertIn(key, fm)
        self.assertTrue(
            any(x.replace("\\", "/").endswith("C语言/if语句与条件判断.md")
                for x in rep["created_files"]), rep["created_files"])
        # 嵌入指向真实文件名
        self.assertIn("![[IMG_0001.png]]", body)
        # 报告里包含“保存路径/分类结果/重名/待核对”四要素
        hr = rep["human_report"]
        for token in ("笔记路径", "分类结果", "重名", "未能识别"):
            self.assertIn(token, hr)
        # 原图未被修改
        self.assertTrue(img.is_file())

    def test_03_hint_course_from_image(self):
        code, out, rep = self.save("第3章 极限与连续", "极限的定义……", hint="高等数学")
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["course_match"], "hint")
        self.assertEqual(n["note_path"], "高数/第3章 极限与连续.md")

    def test_04_keyword_classification(self):
        body = "## 指针\n\n指针是存放地址的变量。\n\n```c\nint *p;\nprintf(\"%p\", p);\n```\n\nmalloc 分配内存。\n"
        code, out, rep = self.save("指针基础", body)
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["course_match"], "keyword", out)
        self.assertEqual(n["course"], "C语言")
        self.assertTrue(n["note_path"].startswith("C语言/"))

    def test_05_unknown_course_goes_to_inbox(self):
        code, out, rep = self.save("今天的杂记", "随便写点什么，没有任何课程线索。")
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["course_match"], "none")
        self.assertEqual(n["note_path"], "待分类/今天的杂记.md")
        self.assertTrue(n["needs_review"])
        self.assertIn("待分类", rep["human_report"])
        self.assertTrue(self.vault_file("待分类", "今天的杂记.md").is_file())

    def test_06_new_course_folder_created_and_flagged(self):
        code, out, rep = self.save("绪论", "概率论的基本概念。", course="概率论与数理统计")
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertTrue(n["course_is_new"])
        self.assertEqual(n["course_match"], "explicit-new")
        self.assertTrue(self.vault_file("概率论与数理统计", "绪论.md").is_file())
        self.assertIn("概率论与数理统计", rep["summary"]["new_courses"])
        self.assertIn("建议加入 config.json", rep["human_report"])

    def test_07_illegal_course_name_falls_back_to_inbox(self):
        code, out, rep = self.save("标题", "内容", course="../../evil/course")
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["course_match"], "explicit-invalid")
        self.assertTrue(n["note_path"].startswith("待分类/"), n["note_path"])
        self.assertFalse((self.outside / "evil").exists())

    def test_08_allow_new_course_false(self):
        self.write_config(allow_new_course=False)
        code, out, rep = self.save("绪论", "内容", course="新课程X")
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertTrue(n["note_path"].startswith("待分类/"), out)
        self.assertFalse((self.vault / "新课程X").exists())


class TestNamingAndConflicts(Base):
    def test_09_note_name_conflict_gets_suffix(self):
        for i, expect in enumerate(["C语言/指针.md", "C语言/指针 (2).md", "C语言/指针 (3).md"]):
            img = self.make_image("conflict-%d.png" % i, ("CONTENT-%d" % i).encode())
            code, out, rep = self.save("指针", "第 %d 次内容" % i, course="C语言", images=[img])
            self.assertEqual(code, 0, out)
            self.assertEqual(rep["notes"][0]["note_path"], expect, out)
            self.assertTrue(self.vault_file(*expect.split("/")).is_file())
        self.assertEqual(rep["notes"][0]["conflict_suffix"], 3)
        # 三份内容各自独立
        self.assertIn("第 0 次内容", self.read_note("C语言/指针.md"))
        self.assertIn("第 2 次内容", self.read_note("C语言/指针 (3).md"))

    def test_10_never_overwrites_existing_note(self):
        d = self.vault / "C语言"
        d.mkdir(parents=True)
        sentinel = d / "指针.md"
        sentinel.write_text("---\ntitle: 老笔记\n---\n\n这是我手写的老笔记，绝不能被覆盖。\n",
                            encoding="utf-8", newline="\n")
        before = sha(sentinel)
        code, out, rep = self.save("指针", "新内容", course="C语言")
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["notes"][0]["note_path"], "C语言/指针 (2).md")
        self.assertEqual(sha(sentinel), before, "已有笔记被改动了！")
        self.assertIn("老笔记", sentinel.read_text(encoding="utf-8"))

    def test_11_illegal_filename_sanitized(self):
        code, out, rep = self.save('if/else: 判断<>|?"*分支', "内容", course="C语言")
        self.assertEqual(code, 0, out)
        p = rep["notes"][0]["note_path"]
        name = os.path.basename(p)
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, name)
        self.assertTrue(self.vault_file(*p.split("/")).is_file(), p)
        self.assertTrue(rep["notes"][0]["renamed_from"], "应报告原始标题")

    def test_12_windows_reserved_name(self):
        code, out, rep = self.save("CON", "内容", course="C语言")
        self.assertEqual(code, 0, out)
        name = os.path.basename(rep["notes"][0]["note_path"])
        self.assertEqual(name, "_CON.md")
        code, out, rep = self.save("nul.txt", "内容", course="C语言")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.basename(rep["notes"][0]["note_path"]).startswith("_"))

    def test_13_obsidian_special_chars_in_title(self):
        code, out, rep = self.save("笔记#标签^块[链接]|别名", "内容", course="C语言")
        self.assertEqual(code, 0, out)
        name = os.path.basename(rep["notes"][0]["note_path"])
        for ch in "#^[]|":
            self.assertNotIn(ch, name)

    def test_14_long_title_clamped(self):
        long_title = "很长的标题" * 40 + "END"
        code, out, rep = self.save(long_title, "内容", course="C语言")
        self.assertEqual(code, 0, out)
        p = self.vault / rep["notes"][0]["note_path"]
        self.assertTrue(p.is_file())
        self.assertLessEqual(len(p.name), 90)
        self.assertLessEqual(len(str(p)), 240)

    def test_15_attachment_same_content_reused(self):
        img1 = self.make_image("shot.png", b"SAME-CONTENT")
        img2 = self.make_image("shot-renamed-copy.png", b"SAME-CONTENT")
        code, out, rep = self.save("笔记一", "内容一", course="C语言", images=[img1])
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["notes"][0]["attachments"][0]["status"], "created")
        # 默认：同一张图（内容相同）会被重复检测拦下
        code, out, rep = self.save("笔记二", "内容二", course="C语言", images=[img2])
        self.assertEqual(rep["notes"][0]["status"], "skipped_duplicate", out)
        # 明确要求重复生成时：附件必须复用，不能再存一份
        code, out, rep = self.save("笔记二", "内容二", course="C语言", images=[img2],
                                   extra=["--allow-duplicate"])
        self.assertEqual(code, 0, out)
        att = rep["notes"][0]["attachments"][0]
        self.assertEqual(att["status"], "reused", out)
        self.assertEqual(att["final_name"], "shot.png")
        files = sorted(p.name for p in (self.vault / "C语言" / "attachments").iterdir())
        self.assertEqual(files, ["shot.png"], "内容相同却新建了附件：%s" % files)
        self.assertIn("复用", rep["human_report"])
        # 第二篇笔记的嵌入指向被复用的那个文件名
        self.assertIn("![[shot.png]]", self.read_note("C语言/笔记二.md"))

    def test_16_attachment_same_name_different_content(self):
        dir1 = self.tmp / "day1"
        dir2 = self.tmp / "day2"
        dir1.mkdir()
        dir2.mkdir()
        img1 = dir1 / "slide.png"
        img1.write_bytes(b"VERSION-1")
        img2b = dir2 / "slide.png"
        img2b.write_bytes(b"VERSION-2-TOTALLY-DIFFERENT")
        code, out, rep = self.save("第一张", "内容", course="C语言", images=[img1])
        self.assertEqual(code, 0, out)
        code, out, rep = self.save("第二张", "内容", course="C语言", images=[img2b])
        self.assertEqual(code, 0, out)
        att = rep["notes"][0]["attachments"][0]
        self.assertEqual(att["status"], "created")
        self.assertEqual(att["final_name"], "slide (2).png", out)
        self.assertTrue(att["name_conflict"])
        d = self.vault / "C语言" / "attachments"
        self.assertEqual(sha(d / "slide.png"), sha(img1), "第一张附件被覆盖了！")
        self.assertEqual(sha(d / "slide (2).png"), sha(img2b))
        self.assertIn("![[slide (2).png]]", self.read_note("C语言/第二张.md"))

    def test_17_attachment_hash_naming_mode(self):
        self.write_config(naming={"attachment_naming": "hash", "max_filename_len": 80})
        img = self.make_image("照片[一号]#1.PNG", b"HASHMODE")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        att = rep["notes"][0]["attachments"][0]
        # 非法字符被清洗 + 哈希后缀，同名不同内容天然不会碰撞
        self.assertRegex(att["final_name"],
                         r"^照片_一号_1-[0-9a-f]{8}\.png$", att["final_name"])
        self.assertEqual(att["renamed_from"], "照片[一号]#1")
        self.assertTrue((self.vault / "C语言" / "attachments" / att["final_name"]).is_file())

    def test_18_attachments_global_mode(self):
        self.write_config(attachments={"mode": "global", "global_path": "Attachments"})
        img = self.make_image("g.png", b"G")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["notes"][0]["attachments"][0]["rel_path"], "Attachments/g.png")
        self.assertTrue((self.vault / "Attachments" / "g.png").is_file())


class TestDuplicates(Base):
    def test_19_duplicate_image_skipped_and_located(self):
        img = self.make_image("page1.png", b"DUPLICATE-PAYLOAD")
        code, out, rep = self.save("第一次整理", "内容 A", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        first = rep["notes"][0]["note_path"]
        before = snapshot(self.vault)
        code, out, rep = self.save("第二次整理", "内容 B", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["status"], "skipped_duplicate", out)
        self.assertEqual(n["duplicate_of"], [first])
        self.assertIn("已整理过", rep["human_report"])
        self.assertIn(first, rep["human_report"])
        self.assertEqual(snapshot(self.vault), before, "重复图片却新建了文件")
        self.assertFalse((self.vault / "C语言" / "第二次整理.md").exists())

    def test_20_duplicate_image_allowed_on_request(self):
        img = self.make_image("page1.png", b"DUPLICATE-PAYLOAD-2")
        self.save("第一次", "内容 A", course="C语言", images=[img])
        code, out, rep = self.save("第二次", "内容 B", course="C语言", images=[img],
                                   extra=["--allow-duplicate"])
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertEqual(n["status"], "saved", out)
        self.assertEqual(n["duplicate_of"], ["C语言/第一次.md"])
        self.assertEqual(n["note_path"], "C语言/第二次.md")
        self.assertEqual(n["attachments"][0]["status"], "reused")

    def test_21_check_images_command(self):
        img = self.make_image("chk.png", b"CHECK")
        self.save("整理过的笔记", "内容", course="C语言", images=[img])
        code, out, rep = run_cli(["check-images", "--config", str(self.cfg_path),
                                  "--json", str(img)])
        self.assertEqual(code, 0, out)
        item = rep["images"][0]
        self.assertTrue(item["already_processed"])
        self.assertEqual(item["existing_notes"], ["C语言/整理过的笔记.md"])
        self.assertEqual(item["existing_attachment"], "C语言/attachments/chk.png")
        other = self.make_image("new.png", b"NEVER-SEEN")
        code, out, rep = run_cli(["check-images", "--config", str(self.cfg_path),
                                  "--json", str(other)])
        self.assertFalse(rep["images"][0]["already_processed"])

class TestPathSafety(Base):
    def test_22_notes_root_traversal_blocked(self):
        self.write_config(notes_root="../outside")
        code, out, rep = self.save("逃逸笔记", "内容", course="C语言")
        self.assertEqual(code, 1)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["error"]["code"], "PATH_ESCAPE", out)
        self.assertEqual(list(self.outside.iterdir()), [], "有文件写到了 vault 外面！")

    def test_23_attachment_dir_traversal_blocked(self):
        self.write_config(attachments={"mode": "global", "global_path": "../../outside/att"})
        img = self.make_image("x.png", b"X")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "PATH_ESCAPE", out)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_24_absolute_path_rejected(self):
        self.write_config(attachments={"mode": "global",
                                       "global_path": str(self.outside).replace("\\", "/")})
        img = self.make_image("y.png", b"Y")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 1)
        self.assertIn(rep["error"]["code"], ("PATH_ESCAPE", "PATH_INVALID"), out)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_25_dotdot_inside_course_folder(self):
        self.write_config(courses=[{"name": "坏课程", "folder": "a/../../outside",
                                    "aliases": [], "keywords": []}])
        code, out, rep = self.save("笔记", "内容", course="坏课程")
        self.assertEqual(code, 1, out)
        self.assertIn(rep["error"]["code"], ("PATH_ESCAPE", "PATH_INVALID"), out)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_26_symlink_escape_blocked(self):
        link = self.vault / "link-out"
        if not _make_dir_link(link, self.outside):
            self.skipTest("当前系统/权限既不支持 symlink 也不支持 junction")
        self.write_config(notes_root="link-out")
        code, out, rep = self.save("笔记", "内容", course="C语言")
        self.assertEqual(code, 1, out)
        self.assertEqual(rep["error"]["code"], "SYMLINK_ESCAPE", out)
        self.assertEqual(list(self.outside.iterdir()), [])
        # 关掉 strict_symlinks 后仍必须被 realpath 检查拦住（链接指向 vault 外）
        self.write_config(notes_root="link-out", strict_symlinks=False)
        code, out, rep = self.save("笔记", "内容", course="C语言")
        self.assertEqual(code, 1, out)
        self.assertEqual(rep["error"]["code"], "SYMLINK_ESCAPE", out)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_27_source_image_outside_vault_is_only_read(self):
        img = self.make_image("orig.png", b"ORIGINAL-BYTES")
        before_sha, before_mtime = sha(img), img.stat().st_mtime
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        self.assertTrue(img.is_file(), "原图被移动或删除了！")
        self.assertEqual(sha(img), before_sha)
        self.assertEqual(img.stat().st_mtime, before_mtime)

    def test_28_existing_vault_files_never_changed(self):
        # 预先造一个“已有内容”的 vault
        (self.vault / "C语言").mkdir(parents=True)
        (self.vault / "C语言" / "老笔记.md").write_text("老内容", encoding="utf-8")
        (self.vault / "C语言" / "attachments").mkdir()
        old_att = self.vault / "C语言" / "attachments" / "old.png"
        old_att.write_bytes(b"OLD-ATTACHMENT")
        (self.vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        before = snapshot(self.vault)
        img = self.make_image("new.png", b"NEW")
        self.save("新笔记", "新内容", course="C语言", images=[img])
        self.save("老笔记", "试图覆盖", course="C语言")   # 与已有笔记同名
        after = snapshot(self.vault)
        for rel, meta in before.items():
            self.assertIn(rel, after, "已有文件被删除：%s" % rel)
            self.assertEqual(after[rel], meta, "已有文件被修改：%s" % rel)
        added = set(after) - set(before)
        self.assertTrue(added, "没有新增文件？")
        self.assertIn("C语言/新笔记.md", added)
        self.assertIn("C语言/老笔记 (2).md", added)


class TestVaultConfig(Base):
    def test_29_vault_unconfigured(self):
        self.write_config()
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["vault_path"] = ""
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        code, out, rep = self.save("笔记", "内容")
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "VAULT_UNCONFIGURED", out)
        self.assertIn("init-config", rep["error"]["hint"])
        self.assertIn("OBSIDIAN_IMAGE_NOTES_VAULT", rep["error"]["hint"])

    def test_30_placeholder_vault_treated_as_unconfigured(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["vault_path"] = "CHANGE_ME"
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        code, out, rep = self.save("笔记", "内容")
        self.assertEqual(rep["error"]["code"], "VAULT_UNCONFIGURED", out)

    def test_31_vault_missing(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["vault_path"] = str(self.tmp / "no-such-vault")
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        code, out, rep = self.save("笔记", "内容")
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "VAULT_MISSING", out)

    def test_32_directory_without_obsidian_rejected(self):
        plain = self.tmp / "plain-dir"
        plain.mkdir()
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["vault_path"] = str(plain)
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        code, out, rep = self.save("笔记", "内容")
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "NOT_A_VAULT", out)
        self.assertEqual(list(plain.iterdir()), [], "在不是 vault 的目录里写了东西")
        # 显式 --allow-non-vault 才允许（测试用途）
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path),
                                  "--allow-non-vault", "--title", "笔记",
                                  "--body-markdown", "内容", "--json"])
        self.assertEqual(code, 0, out)
        self.assertTrue((plain / "待分类" / "笔记.md").is_file())

    def test_33_env_var_vault(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["vault_path"] = ""
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        old = os.environ.get("OBSIDIAN_IMAGE_NOTES_VAULT")
        os.environ["OBSIDIAN_IMAGE_NOTES_VAULT"] = str(self.vault)
        try:
            code, out, rep = self.save("环境变量笔记", "内容", course="C语言")
            self.assertEqual(code, 0, out)
            self.assertTrue((self.vault / "C语言" / "环境变量笔记.md").is_file())
        finally:
            if old is None:
                os.environ.pop("OBSIDIAN_IMAGE_NOTES_VAULT", None)
            else:
                os.environ["OBSIDIAN_IMAGE_NOTES_VAULT"] = old

    def test_34_missing_config_file_gives_guidance(self):
        code, out, rep = run_cli(["save", "--config", str(self.tmp / "nope.json"),
                                  "--title", "x", "--body-markdown", "y", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "CONFIG_MISSING", out)
        self.assertIn("init-config", rep["error"]["hint"])

    def test_35_broken_json_config(self):
        self.cfg_path.write_text("{ this is not json", encoding="utf-8")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path),
                                  "--title", "x", "--body-markdown", "y", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "CONFIG_INVALID", out)
        self.assertIn("行", rep["error"]["message"])

    def test_36_notes_root_subfolder(self):
        self.write_config(notes_root="课程笔记")
        img = self.make_image("n.png", b"N")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["notes"][0]["note_path"], "课程笔记/C语言/笔记.md")
        self.assertEqual(rep["notes"][0]["attachments"][0]["rel_path"],
                         "课程笔记/C语言/attachments/n.png")

    def test_37_init_config(self):
        dest = self.tmp / "gen" / "config.json"
        code, out, rep = run_cli(["init-config", "--vault", str(self.vault),
                                  "--dest", str(dest), "--detect-courses", "--json"])
        self.assertEqual(code, 0, out)
        self.assertTrue(dest.is_file())
        data = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(os.path.realpath(data["vault_path"]), os.path.realpath(str(self.vault)))
        self.assertIn("naming", data)
        # 已存在则拒绝覆盖，除非 --force
        code, out, rep = run_cli(["init-config", "--vault", str(self.vault),
                                  "--dest", str(dest), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "CONFIG_EXISTS", out)
        code, out, rep = run_cli(["init-config", "--vault", str(self.vault),
                                  "--dest", str(dest), "--force", "--json"])
        self.assertEqual(code, 0, out)


class TestDryRunRollbackRace(Base):
    def test_38_dry_run_writes_nothing(self):
        before = snapshot(self.vault)
        img = self.make_image("d.png", b"DRY")
        code, out, rep = self.save("预演笔记", "内容", course="C语言",
                                   images=[img], extra=["--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["notes"][0]["status"], "dry_run")
        self.assertEqual(rep["notes"][0]["note_path"], "C语言/预演笔记.md")
        self.assertEqual(snapshot(self.vault), before, "dry-run 竟然改动了 vault")
        self.assertEqual([p for p in self.vault.rglob("*") if p.is_dir() and p.name == "C语言"], [])
        self.assertIn("DRY-RUN", rep["human_report"])
        self.assertIn("将写入", rep["human_report"])

    def test_39_dry_run_batch_reserves_distinct_names(self):
        job = {"notes": [
            {"title": "同名", "course": "C语言", "body_markdown": "A"},
            {"title": "同名", "course": "C语言", "body_markdown": "B"},
        ]}
        jp = self.work / "job.json"
        jp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path),
                                  "--job", str(jp), "--dry-run", "--json"])
        self.assertEqual(code, 0, out)
        paths = [n["note_path"] for n in rep["notes"]]
        self.assertEqual(paths, ["C语言/同名.md", "C语言/同名 (2).md"], out)
        self.assertEqual(snapshot(self.vault), {})

    def test_40_rollback_on_failure(self):
        img = self.make_image("r.png", b"ROLLBACK")
        job = {"notes": [
            {"title": "会成功", "course": "C语言", "body_markdown": "正文",
             "images": [str(img)]},
            {"title": "会失败", "course": "C语言",
             "body_file": str(self.work / "不存在的正文.md")},
        ]}
        jp = self.work / "job.json"
        jp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path),
                                  "--job", str(jp), "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["error"]["code"], "BODY_MISSING", out)
        self.assertEqual(snapshot(self.vault), {}, "回滚后 vault 里仍有残留文件")
        self.assertFalse((self.vault / "C语言").exists(), "回滚后留下了空目录")
        self.assertTrue(rep["rolled_back"], "报告里没有列出被回滚的文件")
        self.assertTrue(img.is_file())

    def test_41_keep_on_error(self):
        job = {"notes": [
            {"title": "会成功", "course": "C语言", "body_markdown": "正文"},
            {"title": "会失败", "course": "C语言", "body_file": str(self.work / "无.md")},
        ]}
        jp = self.work / "job.json"
        jp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path), "--job", str(jp),
                                  "--keep-on-error", "--json"])
        self.assertEqual(code, 1)
        self.assertTrue((self.vault / "C语言" / "会成功.md").is_file())

    def test_42_race_falls_back_without_overwriting(self):
        # 模拟竞态：pick_name 选中的名字在真正写入前被别人抢先创建
        d = self.vault / "C语言"
        d.mkdir(parents=True)
        raced = d / "竞态笔记.md"
        orig_pick = oin.Writer.pick_name
        state = {"done": False}

        def racing_pick(self, dirpath, stem, ext):
            path, suffix = orig_pick(self, dirpath, stem, ext)
            if not state["done"] and suffix is None:
                raced.write_text("别人抢先写入的内容", encoding="utf-8")
                state["done"] = True
                return path, suffix
            return path, suffix

        oin.Writer.pick_name = racing_pick
        try:
            code, out, rep = self.save("竞态笔记", "我的内容", course="C语言")
        finally:
            oin.Writer.pick_name = orig_pick
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["notes"][0]["note_path"], "C语言/竞态笔记 (2).md", out)
        self.assertEqual(raced.read_text(encoding="utf-8"), "别人抢先写入的内容",
                         "竞态时覆盖了别人的文件！")
        self.assertIn("我的内容", self.read_note("C语言/竞态笔记 (2).md"))

    def test_43_strict_mode_blocks_validation_errors(self):
        bad = "公式未闭合：$$\n\\begin{cases} x=1 \n\n正文\n"
        code, out, rep = self.save("坏公式", bad, course="高等数学", extra=["--strict"])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "VALIDATION_STRICT", out)
        self.assertEqual(snapshot(self.vault), {}, "--strict 失败后仍写入了文件")
        # 不加 --strict 时照常保存，但报告里给出告警
        code, out, rep = self.save("坏公式", bad, course="高等数学")
        self.assertEqual(code, 0, out)
        self.assertGreater(rep["notes"][0]["validation"]["errors"], 0)
        codes = {i["code"] for i in rep["notes"][0]["validation"]["items"]}
        self.assertTrue(codes & {"math_env_unclosed", "display_math_unbalanced"}, codes)


class TestContentFidelity(Base):
    def test_44_latex_and_markdown_warnings(self):
        body = (
            "#标题没有空格\n\n"
            "行内 $a+b 没闭合\n\n"
            "$$\n\\frac{1}{2\n$$\n\n"
            "$$ \\begin{aligned} x&=1 \\end{align} $$\n\n"
            "```c\nint main() {\n\n"
            "| a | b |\n| - |\n| 1 | 2 | 3 |\n\n"
            "价格 $5 和 $10 不是公式\n\n"
            "百分比 $50\\%$ 与 $x$ 正常\n"
        )
        code, out, rep = self.save("校验样例", body, course="C语言")
        self.assertEqual(code, 0, out)
        items = rep["notes"][0]["validation"]["items"]
        codes = {i["code"] for i in items}
        for expect in ("heading_needs_space", "inline_math_unbalanced",
                       "math_brace_unbalanced", "math_env_unclosed",
                       "unclosed_code_fence", "table_column_mismatch"):
            self.assertIn(expect, codes, "缺少告警 %s，实际：%s" % (expect, codes))
        self.assertTrue(all(i["line"] >= 1 for i in items))

    def test_45_clean_note_has_no_warnings(self):
        body = (
            "## 重要极限\n\n"
            "$$\n\\lim_{x \\to 0} \\frac{\\sin x}{x} = 1\n$$\n\n"
            "行内公式 $e^{i\\pi} + 1 = 0$ 也应正常。\n\n"
            "```c\nint x = 1;\n```\n\n"
            "| 名称 | 值 |\n| :-- | --: |\n| a | 1 |\n"
        )
        code, out, rep = self.save("干净的笔记", body, course="高等数学")
        self.assertEqual(code, 0, out)
        v = rep["notes"][0]["validation"]
        self.assertEqual(v["errors"], 0, out)
        bad = [i for i in v["items"] if i["level"] != "info"]
        self.assertEqual(bad, [], "干净笔记被误报：%s" % bad)

    def test_46_validate_command(self):
        p = self.make_body("$$\n\\begin{cases} x \n$$\n")
        code, out, rep = run_cli(["validate", str(p), "--json"])
        self.assertEqual(code, 1)
        self.assertGreater(rep["validation"]["errors"], 0)
        self.assertIn("human_report", rep)

    def test_47_embed_links_rewritten_to_final_names(self):
        img = self.make_image("板书[第1张]#1.png", b"BOARD")
        body = (
            "## 板书\n\n"
            "模型写的旧链接：![[板书[第1张]#1.png]]\n\n"
            "还有一种写法：![板书](%s)\n\n"
            "第三种写法：![板书](D:/somewhere/板书[第1张]#1.png)\n"
            % str(img).replace("\\", "/")
        )
        code, out, rep = self.save("板书整理", body, course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        final = rep["notes"][0]["attachments"][0]["final_name"]
        note = self.read_note(rep["notes"][0]["note_path"])
        self.assertIn("![[%s]]" % final, note)
        self.assertNotIn("![[板书[第1张]#1.png]]", note, "旧的嵌入链接没有被改写")
        self.assertNotIn(str(img).replace("\\", "/"), note)
        self.assertEqual(note.count("![[%s]]" % final), 3, note)
        self.assertNotIn("![板书](", note, "Markdown 图片语法没有被改写")
        self.assertTrue(any("嵌入链接" in i for i in rep["notes"][0]["infos"]), out)

    def test_48_missing_embed_auto_appended(self):
        img = self.make_image("p.png", b"P")
        code, out, rep = self.save("没有嵌入的笔记", "只有文字，没有图片链接。",
                                   course="C语言", images=[img])
        self.assertEqual(code, 0, out)
        note = self.read_note(rep["notes"][0]["note_path"])
        self.assertIn("## 原图", note)
        self.assertIn("![[p.png]]", note)

    def test_49_frontmatter_fields_and_merge(self):
        img = self.make_image("src.png", b"S")
        body = (
            "---\n"
            "author: 我\n"
            "status: draft\n"
            "title: 模型写的旧标题\n"
            "---\n\n"
            "## 正文\n\n内容\n"
        )
        code, out, rep = self.save("最终标题", body, course="C语言",
                                   images=[img], tags=["指针", "C 语言"])
        self.assertEqual(code, 0, out)
        fm, _ = self.frontmatter(rep["notes"][0]["note_path"])
        self.assertIn('title: "最终标题"', fm)
        self.assertIn('course: "C语言"', fm)
        self.assertRegex(fm, r"(?m)^date: \d{4}-\d{2}-\d{2}$")
        self.assertIn("- \"src.png\"", fm)
        self.assertIn("has_uncertain: false", fm)
        self.assertIn("author: 我", fm, "正文自带的额外字段被丢掉了")
        self.assertIn("status: draft", fm)
        self.assertIn("- \"course/C语言\"", fm)
        self.assertIn("- \"图片笔记\"", fm)
        # source_sha256 必须等于原图哈希（重复检测依赖它）
        self.assertIn(sha(img), fm)

    def test_50_uncertain_marker_sets_flag_and_callout(self):
        body = "第一行清楚。\n\n==[?不确定: 这里字迹模糊]==\n\n后面还清楚。\n"
        code, out, rep = self.save("模糊笔记", body, course="C语言",
                                   uncertain=["第 3 行的公式无法辨认"])
        self.assertEqual(code, 0, out)
        n = rep["notes"][0]
        self.assertTrue(n["has_uncertain"])
        note = self.read_note(n["note_path"])
        self.assertIn("has_uncertain: true", note)
        self.assertIn("[!warning] 待核对", note)
        self.assertIn("第 3 行的公式无法辨认", note)
        self.assertIn("==[?不确定: 这里字迹模糊]==", note, "原文的不确定标记被改动了")
        self.assertIn("有", rep["human_report"])

    def test_51_uncertain_detected_without_declaration(self):
        body = "正文。\n\n〔图片中不清晰，需核对〕\n"
        code, out, rep = self.save("自动检测", body, course="C语言")
        self.assertTrue(rep["notes"][0]["has_uncertain"], out)

    def test_52_empty_body_rejected(self):
        code, out, rep = self.save("空笔记", "   \n\n  ", course="C语言")
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "BODY_EMPTY", out)
        self.assertEqual(snapshot(self.vault), {})

    def test_53_missing_image_rejected(self):
        code, out, rep = self.save("笔记", "内容", course="C语言",
                                   images=[self.imgs / "不存在.png"])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "SOURCE_MISSING", out)
        self.assertEqual(snapshot(self.vault), {})

    def test_54_unsupported_attachment_type(self):
        bad = self.imgs / "note.exe"
        bad.write_bytes(b"MZ")
        code, out, rep = self.save("笔记", "内容", course="C语言", images=[bad])
        self.assertEqual(code, 1)
        self.assertEqual(rep["error"]["code"], "SOURCE_UNSUPPORTED", out)

    def test_55_crlf_body_normalized(self):
        p = self.work / "crlf.md"
        p.write_bytes("第一行\r\n第二行\r\n".encode("utf-8"))
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path), "--title", "换行",
                                  "--body-file", str(p), "--course", "C语言", "--json"])
        self.assertEqual(code, 0, out)
        raw = (self.vault / rep["notes"][0]["note_path"]).read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertIn("第二行".encode("utf-8"), raw)

    def test_56_gb18030_body_converted_with_warning(self):
        p = self.work / "gbk.md"
        p.write_bytes("中文内容测试".encode("gb18030"))
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path), "--title", "编码",
                                  "--body-file", str(p), "--course", "C语言", "--json"])
        self.assertEqual(code, 0, out)
        raw = (self.vault / rep["notes"][0]["note_path"]).read_bytes()
        self.assertIn("中文内容测试".encode("utf-8"), raw)
        self.assertTrue(any("gb18030" in w for w in rep["warnings"]), out)

    def test_57_batch_job_multiple_images_and_notes(self):
        i1 = self.make_image("p1.png", b"ONE")
        i2 = self.make_image("p2.png", b"TWO")
        job = {"notes": [
            {"title": "第一页", "course": "C语言", "body_markdown": "## 一\n内容一",
             "images": [str(i1)], "tags": ["第一章"]},
            {"title": "第二页", "course_hint": "高数", "body_markdown": "## 二\n极限内容",
             "images": [str(i2)]},
        ]}
        jp = self.work / "job.json"
        jp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path),
                                  "--job", str(jp), "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual([n["status"] for n in rep["notes"]], ["saved", "saved"])
        self.assertEqual(rep["notes"][0]["note_path"], "C语言/第一页.md")
        self.assertEqual(rep["notes"][1]["note_path"], "高数/第二页.md")
        self.assertEqual(rep["summary"]["saved"], 2)
        self.assertEqual(rep["summary"]["attachments_created"], 2)
        self.assertEqual(len(rep["created_files"]), 4)

    def test_58_report_file_written(self):
        rf = self.work / "report.json"
        code, out, rep = self.save("带报告", "内容", course="C语言",
                                   extra=["--report-file", str(rf)])
        self.assertEqual(code, 0, out)
        self.assertTrue(rf.is_file())
        data = json.loads(rf.read_text(encoding="utf-8"))
        self.assertEqual(data["notes"][0]["title"], "带报告")

    def test_59_human_readable_output_without_json(self):
        body_file = self.make_body("内容")
        code, out, rep = run_cli(["save", "--config", str(self.cfg_path), "--title", "人类可读",
                                  "--body-file", str(body_file), "--course", "C语言"])
        self.assertEqual(code, 0, out)
        self.assertIsNone(rep, "非 --json 模式不应输出 JSON")
        self.assertIn("[ok] 已保存到 Obsidian", out)
        self.assertIn("1) 人类可读", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)