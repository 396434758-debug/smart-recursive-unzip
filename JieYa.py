# -*- coding: utf-8 -*-
"""
JieYa - 递归解压工具
=====================================================================
功能：
  1. 解压压缩包；如果解压出来的内容里还有压缩包，继续自动解压，直到没有压缩包为止。
  2. 遇到 .mp4 / .jpg 文件，一律改名为 .zip 后尝试解压（用于识别伪装成视频/图片的压缩包）；
     解压失败则自动改名还原，文件绝不会丢失。
  3. 输出文件夹名或文件名重复时，自动重命名为 "名字 (1)"、"名字 (2)" ... 绝不覆盖已有文件。
  4. 支持加密压缩包：自动尝试常用密码字典 + 界面手动指定密码。
  5. 全程日志实时输出到网页界面，可随时停止。

依赖：本机已安装的 7-Zip (7z.exe) 或 WinRAR (UnRAR.exe)。
      找不到时，退化为 Python 内置 zipfile（仅支持 zip）。
=====================================================================
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 打包成 exe 后：配置/账本放 exe 旁边，界面等资源从 PyInstaller 解包目录读取
APP_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
WEB_DIR = RES_DIR / "web"
CONFIG_PATH = APP_DIR / "config.json"


def open_folder_foreground(target: str) -> None:
    """Open only a directory (or a file's parent) and verify Explorer activation."""
    item = Path(target).resolve(strict=True)
    folder = item if item.is_dir() else item.parent
    helper = RES_DIR / "open_folder.ps1"
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-File", str(helper), "-Folder", str(folder)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=0x08000000, timeout=12,
    )
    if completed.returncode != 0:
        raise OSError("未能确认文件夹已切到前台，请检查资源管理器或重试。")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PROC_KW = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
           "stderr": subprocess.STDOUT, "creationflags": CREATE_NO_WINDOW}

# --------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------- #

# 被识别为「压缩包」的扩展名
ARCHIVE_EXTS = {
    ".zip", ".zipx", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz",
    ".tbz2", ".xz", ".txz", ".lzma", ".lz4", ".zst", ".z", ".cab", ".iso",
    ".arj", ".lzh", ".ace", ".wim", ".cpio", ".001",
}

# 多卷压缩包的「后续分卷」——只处理第一卷，其余跳过，避免重复解压
RE_PART_N = re.compile(r"\.part(\d+)\.rar$", re.I)
RE_RNN = re.compile(r"\.r\d{2,3}$", re.I)
RE_ZNN = re.compile(r"\.z\d{2,3}$", re.I)
RE_NNN = re.compile(r"\.(\d{3,4})$")

DEFAULT_PASSWORDS = [
    "", "123456", "12345678", "1234", "12345", "123", "0000", "000000",
    "111111", "666666", "888888", "123123", "123321", "654321", "456789",
    "password", "admin", "root", "abc123", "qwerty", "a123456", "admin123",
    "5201314", "woaini1314", "112233", "102030", "1qaz2wsx", "zxcvbnm",
]

DEFAULT_CONFIG = {
    "passwords": DEFAULT_PASSWORDS,
    "disguise_exts": [".mp4", ".jpg"],
    "max_depth": 25,
    "stop_files": 3,                # 解出的普通文件超过这个数，就当作最终内容不再深入；0 = 不限制
    "delete_original": False,       # 解压成功后删除原压缩包
    "restore_disguise": True,       # 伪装包解压成功后把 .zip 改回 .mp4/.jpg
    "fast_precheck": False,         # 快速预检：非 ZIP 特征的文件直接跳过（不做改名尝试）
    "skip_done": True,              # 跳过历史上已成功解压过的同一文件，避免重复解压
    "last_target": "",
    "port": 8765,
}

LEDGER_PATH = APP_DIR / "已解压记录.json"
# 包装层里常见的广告/说明小文件，不算“普通文件”
JUNK_EXTS = {".txt", ".url", ".lnk", ".htm", ".html", ".nfo", ".ini", ".website"}
DROPPED: list[str] = []  # 启动参数里拖进来的路径，界面首次加载时取走
LEDGER_MAX = 20000


# --------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------- #

COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lzma")


def split_name(name: str):
    """把文件名拆成 (主干, 扩展名)，正确处理 .tar.gz 这类复合扩展名。"""
    low = name.lower()
    for c in COMPOUND_EXTS:
        if low.endswith(c):
            return name[: -len(c)], name[-len(c):]
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    return name[:i], name[i:]


def unique_path(p: Path, stats=None) -> Path:
    """若路径已存在，自动追加 (1)、(2) ... 直到不冲突。"""
    if not p.exists():
        return p
    base, ext = split_name(p.name)
    i = 1
    while True:
        cand = p.with_name(f"{base} ({i}){ext}")
        if not cand.exists():
            if stats is not None:
                stats["renamed"] += 1
            return cand
        i += 1


def strip_volume_suffix(stem: str) -> str:
    """game.part1 -> game；  game.r00 -> game；  game.001 -> game"""
    s = re.sub(r"\.part\d+$", "", stem, flags=re.I)
    s = re.sub(r"\.(r|z)\d{2,3}$", "", s, flags=re.I)
    s = re.sub(r"\.\d{3,4}$", "", s)
    return s or stem


def is_zip_magic(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(4)
        return head[:2] == b"PK"
    except OSError:
        return False


# 按文件头识别压缩包（扩展名不对/没有扩展名时也能认出来）
ARCHIVE_MAGICS = (
    b"PK\x03\x04", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b",
    b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd", b"MSCF", b"\x60\xea",
)
# 这些格式内部是 zip，但它们本身是正常文件，不能当压缩包拆开
ZIP_CONTAINER_EXTS = {
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".jar",
    ".apk", ".aab", ".xpi", ".crx", ".vsix", ".nupkg", ".whl", ".ipa",
    ".appx", ".msix", ".3mf", ".kmz", ".pak", ".pk3", ".pk4",
}


# 程序/安装包里常内嵌压缩数据，不按“拼接包”拆
EMBED_SKIP_EXTS = {".exe", ".dll", ".msi", ".sys", ".scr", ".ocx", ".bin"}
EMBED_SIGS = (b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")
EMBED_SCAN = 4 * 1024 * 1024


def has_archive_magic(p: Path) -> bool:
    """按内容判断是不是压缩包：文件头、或图片/视频后面拼接的 zip/rar/7z（图种）。"""
    ext = p.suffix.lower()
    if ext in ZIP_CONTAINER_EXTS:
        return False
    try:
        with open(p, "rb") as f:
            head = f.read(EMBED_SCAN)
            if head.startswith(ARCHIVE_MAGICS):
                return True
            if ext in EMBED_SKIP_EXTS:
                return False
            # zip 的目录结尾标记在文件最后 64KB 内，拼接在任何大文件后面都能查到
            size = f.seek(0, 2)
            f.seek(max(0, size - 65558))
            if b"PK\x05\x06" in f.read():
                return True
    except OSError:
        return False
    # shortcut: rar/7z 拼接包只在前 4MB 里找，前面的图片/视频超过 4MB 时认不出，遇到再加大
    return any(s in head for s in EMBED_SIGS)


def is_multipart_continuation(name: str) -> bool:
    m = RE_PART_N.search(name)
    if m:
        return int(m.group(1)) > 1
    if RE_RNN.search(name) or RE_ZNN.search(name):
        return True
    m = RE_NNN.search(name)
    if m:
        return int(m.group(1)) > 1
    return False


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# --------------------------------------------------------------------- #
# 已解压记录（放在程序目录，不污染用户文件夹）
# --------------------------------------------------------------------- #

def ledger_key(p: Path):
    """用 绝对路径 + 大小 + 修改时间 作为指纹。"""
    try:
        st = p.stat()
        return f"{os.path.normcase(os.path.abspath(p))}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return None


def load_ledger():
    try:
        if LEDGER_PATH.is_file():
            data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
    except Exception:  # noqa: BLE001
        pass
    return set()


def save_ledger(keys):
    try:
        items = list(keys)[-LEDGER_MAX:]
        LEDGER_PATH.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------- #
# 解压内核定位
# --------------------------------------------------------------------- #

def find_tool():
    """返回 (可执行文件路径, 类型) —— 类型为 '7z' 或 'unrar'；找不到返回 (None, None)。"""
    cands = []
    for exe in ("7z.exe", "7za.exe"):
        w = shutil.which(exe)
        if w:
            cands.append(Path(w))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(env)
        if not root:
            continue
        cands += [Path(root) / "7-Zip" / "7z.exe",
                  Path(root) / "WinRAR" / "UnRAR.exe"]
    cands += [Path(r"C:\Program Files\7-Zip\7z.exe"),
              Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
              Path(r"C:\Program Files\WinRAR\UnRAR.exe")]
    for c in cands:
        try:
            if c.is_file():
                kind = "unrar" if c.name.lower().startswith("unrar") else "7z"
                return c, kind
        except OSError:
            continue
    return None, None


TOOL, TOOL_KIND = find_tool()


# --------------------------------------------------------------------- #
# 解压执行
# --------------------------------------------------------------------- #

def run_extract(tool: Path, kind: str, archive: Path, dest: Path, password: str):
    """执行一次解压，返回 (是否成功, 输出文本, 返回码)。"""
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "7z":
        cmd = [str(tool), "x", "-y", "-bso0", "-bse1",
               f"-o{dest}", f"-p{password}", "--", str(archive)]
    else:  # unrar
        cmd = [str(tool), "x", "-y", f"-p{password}", str(archive), str(dest) + os.sep]
    try:
        r = subprocess.run(cmd, timeout=3600, **PROC_KW)
        out = (r.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return False, "解压超时（超过 1 小时）", -1
    except Exception as e:  # noqa: BLE001
        return False, f"调用解压程序失败: {e}", -1

    rc = r.returncode
    low = out.lower()
    not_archive = ("is not archive" in low or "can not open the file as" in low
                   or "cannot open the file as" in low or "is not rar archive" in low
                   or "not supported archive" in low or "can not open as" in low)
    if kind == "unrar":
        ok = rc in (0, 1) or rc == 10
        wrong_pwd = rc == 11
    else:
        ok = rc in (0, 1)
        wrong_pwd = (not ok) and ("wrong password" in low or "password" in low
                                  or "加密" in out)
    if ok:
        # 二次确认：目标目录里确实产出了东西
        if any(dest.iterdir()):
            return True, out, rc
        return False, "压缩包为空或内容全部缺失，未产生任何文件", rc
    if wrong_pwd:
        return False, "PASSWORD", rc
    if not_archive:
        return False, "NOT_ARCHIVE", rc
    return False, out.strip() or f"解压失败，返回码 {rc}", rc


def extract_archive(job, archive: Path, out_dir: Path, passwords, stats):
    """带密码字典的解压。

    参数 out_dir 是期望的输出目录；函数内部会自动做重名规避。
    返回真正创建成功的输出目录 Path；失败返回 None。
    """
    out_dir = unique_path(out_dir, stats)
    job.log("info", f"  → 解压到  {out_dir.name}/")

    def ret(result, value=None):
        job.last_result = result
        return value

    def cleanup():
        try:
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
        except OSError:
            pass

    # 内置 zipfile 兜底（没有 7z / unrar 时）
    if TOOL is None:
        if archive.suffix.lower() not in (".zip", ".zipx"):
            job.log("info", "  未找到 7-Zip / WinRAR，且该文件不是 zip，保持原样")
            stats["not_archive"] += 1
            return ret("not_archive")
        try:
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                if names and any(i.flag_bits & 0x1 for i in z.infolist()):
                    for p in passwords:
                        try:
                            z.read(names[0], pwd=p.encode("utf-8", "ignore"))
                            break
                        except Exception:  # noqa: BLE001
                            continue
                    else:
                        job.log("warn", "  密码字典全部失败，跳过")
                        stats["encrypted_skipped"] += 1
                        cleanup()
                        return ret("encrypted")
                z.extractall(out_dir)
            stats["extracted"] += 1
            job.log("ok", "  ✓ 解压成功（内置 zipfile）")
            return ret("ok", out_dir)
        except Exception as e:  # noqa: BLE001
            job.log("err", f"  ✗ zipfile 解压失败: {e}")
            stats["failed"] += 1
            cleanup()
            return ret("failed")

    tried = []
    for pwd in passwords:
        if job.stop_flag:
            cleanup()
            return ret("stopped")
        ok, out, rc = run_extract(TOOL, TOOL_KIND, archive, out_dir, pwd)
        if ok:
            label = "（无密码）" if pwd == "" else f"（密码: {pwd}）"
            job.log("ok", f"  ✓ 解压成功{label}")
            if pwd != "":
                job.used_password = pwd
            stats["extracted"] += 1
            return ret("ok", out_dir)
        if out == "PASSWORD":
            tried.append(pwd)
            cleanup()
            continue
        if out == "NOT_ARCHIVE":
            job.log("info", "  · 不是压缩包（普通文件），保持原样")
            stats["not_archive"] += 1
            cleanup()
            return ret("not_archive")
        # 非密码原因的真实失败
        job.log("err", f"  ✗ 解压失败: {out.splitlines()[0][:200] if out else '未知错误'}")
        stats["failed"] += 1
        cleanup()
        return ret("failed")

    job.log("warn", f"  ! 已尝试 {len(tried)} 个密码仍未成功，跳过该压缩包")
    stats["encrypted_skipped"] += 1
    cleanup()
    return ret("encrypted")


# --------------------------------------------------------------------- #
# 任务对象
# --------------------------------------------------------------------- #

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.seq = 0
        self.running = False
        self.stop_flag = False
        self.used_password = None
        self.started_at = None
        self.ended_at = None
        self.current = ""
        self.queue_len = 0
        self.depth = 0
        self.stats = {
            "scanned": 0, "extracted": 0, "failed": 0, "encrypted_skipped": 0,
            "disguised": 0, "disguised_ok": 0, "renamed": 0, "files": 0,
            "dirs": 0, "not_archive": 0, "skipped_done": 0,
        }
        self.last_result = None

    def log(self, level: str, msg: str):
        with self.lock:
            self.seq += 1
            self.logs.append({"i": self.seq, "lv": level, "m": msg,
                              "t": time.strftime("%H:%M:%S")})
            if len(self.logs) > 40000:
                del self.logs[:10000]

    def snapshot(self, since: int):
        with self.lock:
            logs = [e for e in self.logs if e["i"] > since]
            return {
                "seq": self.seq,
                "logs": logs[-800:],
                "running": self.running,
                "current": self.current,
                "queue": self.queue_len,
                "depth": self.depth,
                "stats": dict(self.stats),
                "elapsed": round((self.ended_at or time.time()) - self.started_at, 1)
                if self.started_at else 0,
                "used_password": self.used_password,
            }


JOB = Job()


# --------------------------------------------------------------------- #
# 引擎主流程
# --------------------------------------------------------------------- #

class Engine:
    def __init__(self, job: Job, cfg: dict):
        self.job = job
        self.cfg = cfg
        self.stats = job.stats
        self.disguise_exts = [e.strip().lower() for e in cfg["disguise_exts"] if e.strip()]
        self.max_depth = int(cfg.get("max_depth", 25))
        self.stop_files = int(cfg.get("stop_files", 3) or 0)
        self.passwords = [p for p in cfg["passwords"]]
        if "" not in self.passwords:
            self.passwords.insert(0, "")
        self.processed = set()
        self.skip_done = bool(cfg.get("skip_done", True))
        self.ledger = load_ledger() if self.skip_done else set()
        self.ledger_dirty = False

    def _key(self, p: Path):
        return ledger_key(p) if self.skip_done else None

    def _mark_done(self, p: Path):
        k = self._key(p)
        if k:
            self.ledger.add(k)
            self.ledger_dirty = True

    def _already_done(self, p: Path) -> bool:
        k = self._key(p)
        return bool(k) and k in self.ledger

    # ---------------- 扫描 ----------------
    def scan(self, d: Path):
        found = []
        subdirs = []
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            self.job.log("warn", f"无法读取目录 {d}: {e}")
            return found, subdirs
        for ent in entries:
            if self.job.stop_flag:
                break
            if ent.is_dir(follow_symlinks=False):
                # 不进入目录联接，避免循环和扫描到目标目录之外。
                if not getattr(os.path, "isjunction", lambda _p: False)(ent.path):
                    subdirs.append(Path(ent.path))
                continue
            if not ent.is_file(follow_symlinks=False):
                continue
            name = ent.name
            ext = Path(name).suffix.lower()
            key = os.path.normcase(os.path.abspath(ent.path))
            if key in self.processed:
                continue
            if is_multipart_continuation(name):
                continue
            # 复合扩展名，如 .tar.gz / .7z.001
            low = name.lower()
            is_arc = ext in ARCHIVE_EXTS or any(
                low.endswith(c) for c in COMPOUND_EXTS)
            if not is_arc and ext not in self.disguise_exts:
                if not has_archive_magic(Path(ent.path)):
                    continue
            try:
                if ent.stat().st_size < 4:
                    continue
            except OSError:
                continue
            fp = Path(ent.path)
            if self._already_done(fp):
                self.stats["skipped_done"] += 1
                self.job.log("info", f"  · 跳过 {name}（上次已成功解压过）")
                continue
            found.append(fp)
        found.sort(key=lambda p: p.name.lower())
        subdirs.sort(key=lambda p: p.name.lower())
        return found, subdirs

    # ---------------- 主循环 ----------------
    def run(self, targets):
        job = self.job
        work = deque()
        scanned_dirs = set()
        for t in targets:
            p = Path(t).expanduser()
            try:
                p = p.resolve()
            except OSError:
                pass
            if p.is_dir():
                work.append(("dir", p, 0))
            elif p.is_file():
                work.append(("file", p, 0))
            else:
                job.log("err", f"路径不存在或无权限: {t}")

        if not work:
            job.log("err", "没有可处理的目标")
            return

        job.log("info", "=" * 62)
        job.log("info", f"解压内核: {TOOL if TOOL else '（未找到 7-Zip/WinRAR，降级为 zipfile）'}")
        job.log("info", f"伪装扩展名: {', '.join(self.disguise_exts) or '（无）'}")
        job.log("info", f"最大递归深度: {self.max_depth}")
        if self.stop_files:
            job.log("info", f"解出超过 {self.stop_files} 个普通文件即视为解到底")
        job.log("info", f"密码字典: {len(self.passwords)} 条")
        job.log("info", "=" * 62)

        while work and not job.stop_flag:
            kind, p, depth = work.popleft()
            if kind == "dir":
                if depth > self.max_depth:
                    job.log("warn", f"已达最大递归深度 {self.max_depth}，跳过: {p}")
                    continue
                key = os.path.normcase(os.path.realpath(p))
                if key in scanned_dirs:
                    continue
                scanned_dirs.add(key)
                job.depth = depth
                job.current = f"扫描 {p}"
                files, subdirs = self.scan(p)
                # 普通文件夹不增加解压层数；只有解开一层压缩包才 +1。
                for subdir in subdirs:
                    work.append(("dir", subdir, depth))
                if files:
                    job.log("info", f"[深度 {depth}] 扫描 {p}  →  发现 {len(files)} 个待处理文件")
                self.stats["scanned"] += 1
                for f in files:
                    if job.stop_flag:
                        break
                    key = os.path.normcase(os.path.abspath(f))
                    if key in self.processed:
                        continue
                    self.processed.add(key)
                    new_dirs = self.handle_file(f, p, depth)
                    for nd in new_dirs:
                        if self._is_wrapper(nd):
                            work.append(("dir", nd, depth + 1))
            else:
                key = os.path.normcase(os.path.abspath(p))
                if key not in self.processed:
                    self.processed.add(key)
                    new_dirs = self.handle_file(p, p.parent, depth)
                    for nd in new_dirs:
                        if self._is_wrapper(nd):
                            work.append(("dir", nd, depth + 1))
            job.queue_len = len(work)

        if job.stop_flag:
            job.log("warn", "已被用户停止。")
        else:
            job.log("ok", "=" * 62)
            job.log("ok", "全部完成：已没有可继续解压的压缩包。")
        if self.ledger_dirty:
            save_ledger(self.ledger)
            job.log("info", f"已记录本次成功解压的 {len(self.ledger)} 项（下次运行会自动跳过，避免重复解压）")

    # ---------------- 单个文件处理 ----------------
    def handle_file(self, f: Path, parent: Path, depth: int):
        """返回需要继续扫描的新目录列表。"""
        job = self.job
        new_dirs = []
        ext = f.suffix.lower()
        low = f.name.lower()
        is_arc = ext in ARCHIVE_EXTS or any(low.endswith(c) for c in COMPOUND_EXTS)
        by_magic = False
        if not is_arc and ext not in self.disguise_exts and has_archive_magic(f):
            is_arc = by_magic = True
            job.log("info", f"[深度 {depth}] {f.name}  →  文件头识别为压缩包")

        if is_arc:
            job.current = f.name
            job.log("info", f"[深度 {depth}] 压缩包: {f.name}")
            stem = strip_volume_suffix(split_name(f.name)[0])
            out_dir = extract_archive(job, f, parent / stem, self.passwords, self.stats)
            if out_dir is not None:
                self._mark_done(f)
                new_dirs.append(out_dir)
                self.stats["dirs"] += 1
                self._count_files(out_dir)
                if self.cfg.get("delete_original") and not by_magic:
                    try:
                        f.unlink()
                        job.log("info", "  · 已删除原压缩包")
                    except OSError as e:
                        job.log("warn", f"  · 删除原压缩包失败: {e}")
            elif job.last_result == "not_archive":
                # 已确认不是压缩包，记入记录，重复运行时不再白试一次
                self._mark_done(f)
            return new_dirs

        if ext in self.disguise_exts:
            job.current = f.name
            self.stats["disguised"] += 1
            if self.cfg.get("fast_precheck") and not is_zip_magic(f):
                job.log("info", f"[深度 {depth}] {f.name}  →  非 ZIP 特征，跳过（快速预检）")
                return []
            job.log("info", f"[深度 {depth}] 疑似伪装包: {f.name}  →  改名为 .zip 并尝试解压")
            zip_path = unique_path(f.with_suffix(".zip"), self.stats)
            try:
                os.replace(f, zip_path)
            except OSError as e:
                job.log("err", f"  ✗ 改名失败: {e}")
                return []
            stem = strip_volume_suffix(split_name(zip_path.name)[0])
            out_dir = extract_archive(job, zip_path, zip_path.parent / stem,
                                      self.passwords, self.stats)
            ok = out_dir is not None
            # 收尾：成功且未开启「删除原文件」时，把 .zip 改回 .mp4/.jpg
            keep_as_zip = ok and not self.cfg.get("restore_disguise", True)
            try:
                if not zip_path.exists():
                    pass
                elif self.cfg.get("delete_original") and ok:
                    zip_path.unlink()
                    job.log("info", "  · 已删除原文件")
                elif not keep_as_zip:
                    target = unique_path(f, self.stats)
                    os.replace(zip_path, target)
                    job.log("info", f"  · 已还原文件名为 {target.name}"
                                    + ("" if ok else "（解压失败，文件未受影响）"))
            except OSError as e:
                job.log("warn", f"  · 收尾改名失败: {e}")
            if ok:
                self.stats["disguised_ok"] += 1
                self._mark_done(f if f.exists() else zip_path)
                new_dirs.append(out_dir)
                self.stats["dirs"] += 1
                self._count_files(out_dir)
            elif job.last_result == "not_archive":
                self._mark_done(f)
            return new_dirs

        if depth == 0:
            job.log("warn", f"{f.name}  →  不是可解压文件，跳过")
        return new_dirs

    # ---------------- 辅助 ----------------
    def _is_wrapper(self, d: Path) -> bool:
        """解出的目录是不是“又一层包装”：普通文件不多就继续深入，多了就当作解到底。"""
        if not self.stop_files:
            return True
        others = 0
        for root, _dirs, names in os.walk(d):
            for name in names:
                low = name.lower()
                if (Path(low).suffix in ARCHIVE_EXTS or low.endswith(COMPOUND_EXTS)
                        or is_multipart_continuation(name)
                        or Path(low).suffix in JUNK_EXTS):
                    continue
                others += 1
                if others > self.stop_files:
                    self.job.log("info", f"  · {d.name}/ 里已有多个普通文件，视为最终内容，不再深入")
                    return False
        return True

    def _count_files(self, d: Path):
        cnt = 0
        try:
            for _root, _dirs, files in os.walk(d):
                cnt += len(files)
        except OSError:
            pass
        self.stats["files"] += cnt


# --------------------------------------------------------------------- #
# 原生对话框（ctypes，无需 tkinter）
# --------------------------------------------------------------------- #

def _picker_user32():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    return user32


def _activate_native_dialog(hwnd):
    """Activate only this dialog; never make it permanently topmost."""
    import ctypes
    from ctypes import wintypes
    user32 = _picker_user32()
    current = ctypes.windll.kernel32.GetCurrentThreadId()
    ignored = wintypes.DWORD()
    foreground = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(ignored))
    attached = foreground != 0 and foreground != current and user32.AttachThreadInput(current, foreground, True)
    try:
        user32.ShowWindow(hwnd, 5)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current, foreground, False)


def pick_folder(title="请选择要解压的文件夹") -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    owner = _picker_user32().GetForegroundWindow()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    try:
        ole32.CoInitialize(None)
    except Exception:  # noqa: BLE001
        pass

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL

    buf = ctypes.create_unicode_buffer(260)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM)
    @callback_type
    def initialized(hwnd, message, _param, _data):
        if message == 1:  # BFFM_INITIALIZED
            _activate_native_dialog(hwnd)
        return 0

    bi = BROWSEINFO()
    bi.hwndOwner = owner
    bi.lpfn = ctypes.cast(initialized, ctypes.c_void_p)
    bi.pszDisplayName = ctypes.cast(buf, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = 0x0001 | 0x0010 | 0x0040  # RETURNONLYFSDIRS | EDITBOX | NEWDIALOGSTYLE
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None
    path_buf = ctypes.create_unicode_buffer(2048)
    shell32.SHGetPathFromIDListW(pidl, path_buf)
    try:
        ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    except Exception:  # noqa: BLE001
        pass
    return path_buf.value or None


def pick_file(title="请选择压缩包") -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = _picker_user32()
    owner = user32.GetForegroundWindow()
    comdlg32 = ctypes.windll.comdlg32

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    file_buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAMEW()
    ofn.hwndOwner = owner
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = "所有文件\0*.*\0压缩包\0*.zip;*.rar;*.7z;*.tar;*.gz\0\0"
    ofn.lpstrFile = ctypes.cast(file_buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = title
    hook_type = ctypes.WINFUNCTYPE(ctypes.c_size_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    @hook_type
    def initialized(hwnd, message, _wparam, _lparam):
        if message == 0x0110:  # WM_INITDIALOG: Explorer hook is a child dialog.
            _activate_native_dialog(user32.GetParent(hwnd) or hwnd)
        return 0

    ofn.lpfnHook = ctypes.cast(initialized, ctypes.c_void_p)
    ofn.Flags = 0x00000008 | 0x00000800 | 0x00001000 | 0x00080000 | 0x00000020  # NOCHANGEDIR|PATHMUSTEXIST|FILEMUSTEXIST|EXPLORER|ENABLEHOOK
    if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return file_buf.value or None
    return None


# --------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------- #

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


CFG = load_config()
WORKER = {"thread": None}


# --------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "JieYa/1.0"

    def log_message(self, *args):  # 静默
        pass

    # ---------- 输出助手 ----------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        if path in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            if not f.is_file():
                self._send(500, "缺少 web/index.html".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/logs":
            since = int((q.get("since") or ["0"])[0] or 0)
            snap = JOB.snapshot(since)
            snap["config"] = CFG
            self._json(snap)
            return

        if path == "/api/env":
            dropped = DROPPED[:]
            DROPPED.clear()  # 只自动开始一次，刷新页面不重复
            self._json({
                "tool": str(TOOL) if TOOL else None,
                "tool_kind": TOOL_KIND,
                "python": sys.version.split()[0],
                "config": CFG,
                "default_passwords": DEFAULT_PASSWORDS,
                "dropped": dropped,
            })
            return

        if path == "/api/open":
            target = (q.get("path") or [""])[0]
            if target and os.path.exists(target) and os.name == "nt":
                try:
                    open_folder_foreground(target)
                    self._json({"ok": True})
                    return
                except (OSError, subprocess.SubprocessError) as e:
                    self._json({"ok": False, "error": str(e)})
                    return
            self._json({"ok": False, "error": "路径不存在"})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self._read_body()

        if path == "/api/pick-folder":
            p = pick_folder()
            self._json({"path": p})
            return

        if path == "/api/pick-file":
            p = pick_file()
            self._json({"path": p})
            return

        if path == "/api/save-config":
            for k in DEFAULT_CONFIG:
                if k in body:
                    CFG[k] = body[k]
            save_config(CFG)
            self._json({"ok": True, "config": CFG})
            return

        if path == "/api/start":
            if JOB.running:
                self._json({"ok": False, "error": "已有任务正在运行"})
                return
            targets = [str(t) for t in (body.get("targets") or []) if str(t).strip()]
            targets = [t for t in targets if os.path.exists(t)]
            if not targets:
                self._json({"ok": False, "error": "请先选择存在的文件夹或压缩包"})
                return
            opts = body.get("options") or {}
            for k in ("disguise_exts", "max_depth", "stop_files", "delete_original",
                      "restore_disguise", "fast_precheck", "skip_done"):
                if k in opts:
                    CFG[k] = opts[k]
            pwd_text = opts.get("passwords_text")
            if isinstance(pwd_text, str):
                lines = [ln for ln in (p.strip() for p in pwd_text.splitlines())]
                CFG["passwords"] = lines if lines else [""]
            CFG["last_target"] = targets[0]
            save_config(CFG)

            # 重置任务
            with JOB.lock:
                JOB.logs.clear()
                JOB.seq = 0
            JOB.stats = {k: 0 for k in JOB.stats}
            JOB.stop_flag = False
            JOB.running = True
            JOB.started_at = time.time()
            JOB.ended_at = None
            JOB.used_password = None
            JOB.log("info", f"开始处理 {len(targets)} 个目标")

            def worker():
                try:
                    eng = Engine(JOB, CFG)
                    eng.run(targets)
                except Exception as e:  # noqa: BLE001
                    import traceback
                    JOB.log("err", "发生未预期的错误: " + str(e))
                    JOB.log("err", traceback.format_exc()[-1500:])
                finally:
                    JOB.running = False
                    JOB.ended_at = time.time()
                    JOB.queue_len = 0

            th = threading.Thread(target=worker, daemon=True)
            WORKER["thread"] = th
            th.start()
            self._json({"ok": True})
            return

        if path == "/api/stop":
            JOB.stop_flag = True
            JOB.log("warn", "收到停止指令，正在收尾…")
            self._json({"ok": True})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def free_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 40)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_app_window(url: str):
    """优先用 Edge/Chrome 的 --app 模式打开，看起来就是一个独立软件窗口。"""
    if os.name != "nt":
        webbrowser.open(url)
        return
    cands = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for exe in cands:
        try:
            if exe.is_file():
                subprocess.Popen(
                    [str(exe), f"--app={url}", "--window-size=1180,800",
                     "--user-data-dir=" + str(APP_DIR / ".browser_profile")],
                    creationflags=CREATE_NO_WINDOW)
                return
        except OSError:
            continue
    webbrowser.open(url)


def run_cli(paths, use_ledger=True):
    """命令行模式： python JieYa.py --cli "D:\\某个目录" """
    cfg = dict(CFG)
    if not use_ledger:
        cfg["skip_done"] = False
    job = JOB
    job.running = True
    job.started_at = time.time()
    job.stop_flag = False

    def printer():
        seen = 0
        while job.running or seen < job.seq:
            snap = job.snapshot(seen)
            for e in snap["logs"]:
                print(e["m"], flush=True)
            seen = snap["seq"]
            if not job.running and seen >= job.seq:
                break
            time.sleep(0.2)

    th = threading.Thread(target=printer, daemon=True)
    th.start()
    try:
        Engine(job, cfg).run(paths)
    finally:
        job.running = False
        job.ended_at = time.time()
    th.join(timeout=3)
    s = job.stats
    print("-" * 62)
    print(f"完成：解压 {s['extracted']} 个，失败 {s['failed']} 个，"
          f"加密未解开 {s['encrypted_skipped']} 个，重名改名 {s['renamed']} 次")
    return 0 if s["failed"] == 0 else 1


def main():
    # GBK 控制台/管道下 ✓ 等字符会抛错打断日志线程；无控制台的窗口版 stdout 为 None
    if sys.stdout:
        sys.stdout.reconfigure(errors="replace")
    args = [a for a in sys.argv[1:]]
    if args and args[0] in ("--cli", "-c"):
        targets = [a for a in args[1:] if not a.startswith("--")]
        if not targets:
            print("用法: python JieYa.py --cli \"要处理的文件夹或压缩包\" [更多路径...]")
            return 2
        bad = [t for t in targets if not os.path.exists(t)]
        if bad:
            print("路径不存在: " + ", ".join(bad))
            return 2
        return run_cli(targets, use_ledger="--force" not in args)

    # 文件/文件夹拖到 exe 图标上：交给界面，打开后自动加入目标并开始
    DROPPED[:] = [os.path.abspath(a) for a in args if os.path.exists(a)]

    port = free_port(int(CFG.get("port", 8765) or 8765))
    CFG["port"] = port
    url = f"http://127.0.0.1:{port}/"

    print("=" * 62)
    print("  智能递归解压工具")
    print("=" * 62)
    print(f"  界面地址 : {url}")
    print(f"  解压内核 : {TOOL if TOOL else '未找到 7-Zip / WinRAR（降级 zipfile）'}")
    print(f"  工作目录 : {APP_DIR}")
    print("  关闭本窗口即退出程序。")
    print("=" * 62)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    no_open = "--no-open" in args or os.environ.get("JIEYA_NO_BROWSER")
    if not no_open:
        try:
            import webview
        except ImportError:
            webview = None
        if webview:
            # 独立窗口（WebView2）：关掉窗口 = 退出程序
            webview.create_window("智能递归解压工具", url, width=1180, height=860,
                                  min_size=(820, 600))
            webview.start()
            httpd.shutdown()
            return 0
        open_app_window(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
