# -*- coding: utf-8 -*-
"""
JieYa 自检脚本
构造一套覆盖全部规则的样例数据，跑两遍引擎，逐项校验结果。

用法:  python 自检.py
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import JieYa  # noqa: E402

SEVENZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
RAR = Path(r"C:\Program Files\WinRAR\Rar.exe")
TEST = HERE / "_自检样例"
ROOT = TEST / "输入"
CNW = 0x08000000 if os.name == "nt" else 0

# 自检使用独立的记录文件，不污染正式记录
JieYa.LEDGER_PATH = TEST / "自检记录.json"

RESULTS = []


def check(desc, ok, detail=""):
    RESULTS.append((desc, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}]  {desc:<36} {detail}")


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, creationflags=CNW)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"{cmd} -> {r.returncode}\n"
                           f"{r.stdout.decode('utf-8', 'replace')}")


def mk7z(path: Path, src_dir: Path):
    sh([str(SEVENZIP), "a", "-t7z", "-bd", "-bso0", "-bsp0", str(path), "."],
       cwd=src_dir)


# --------------------------------------------------------------------- #
# 构造样例
# --------------------------------------------------------------------- #
def build():
    if TEST.exists():
        shutil.rmtree(TEST)
    ROOT.mkdir(parents=True)
    st = TEST / "__stage"
    st.mkdir(parents=True)

    # ---- 三层递归: A.zip > B.7z > C.zip ----
    with zipfile.ZipFile(st / "C.zip", "w") as z:
        z.writestr("deep.txt", "最深层内容 deep\n")

    b = st / "b"
    b.mkdir()
    (b / "world.txt").write_text("第二层内容 world\n", encoding="utf-8")
    shutil.copy2(st / "C.zip", b / "C.zip")
    mk7z(st / "B.7z", b)
    shutil.rmtree(b)

    with zipfile.ZipFile(ROOT / "A.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("hello.txt", "第一层内容 hello\n")
        z.write(st / "B.7z", "B.7z")

    # ---- 伪装成 mp4 的 zip，内部还嵌一个 zip ----
    with zipfile.ZipFile(st / "sub.zip", "w") as z:
        z.writestr("subdeep.txt", "伪装包里的嵌套内容 subdeep\n")
    with zipfile.ZipFile(ROOT / "movie.mp4", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("movie.txt", "伪装 mp4 里的内容\n")
        z.write(st / "sub.zip", "sub.zip")

    # ---- 伪装成 jpg 的 zip ----
    with zipfile.ZipFile(ROOT / "photo.jpg", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("photo.txt", "伪装 jpg 里的内容\n")

    # ---- 真视频（不是压缩包）----
    real = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 16
    (ROOT / "real_video.mp4").write_bytes(real)

    # ---- 加密 rar（密码 123456）----
    if RAR.is_file():
        rc = st / "rc"
        rc.mkdir()
        (rc / "secret.txt").write_text("加密内容 secret\n", encoding="utf-8")
        sh([str(RAR), "a", "-ep1", "-p123456", "-inul",
            str(ROOT / "enc.rar"), "secret.txt"], cwd=rc)
        shutil.rmtree(rc)

    # ---- 同名冲突: Same.7z 与 Same.zip ----
    with zipfile.ZipFile(ROOT / "Same.zip", "w") as z:
        z.writestr("x.txt", "from Same.zip\n")
    s = st / "s"
    s.mkdir()
    (s / "y.txt").write_text("from Same.7z\n", encoding="utf-8")
    mk7z(ROOT / "Same.7z", s)
    shutil.rmtree(s)

    shutil.rmtree(st)


# --------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------- #
def run(skip_done: bool, label: str):
    print("\n" + "=" * 62)
    print(f"运行引擎（{label}）")
    print("=" * 62)
    job = JieYa.Job()
    cfg = dict(JieYa.DEFAULT_CONFIG)
    cfg["passwords"] = ["", "123456", "0000"]
    cfg["skip_done"] = skip_done
    job.running = True
    JieYa.Engine(job, cfg).run([str(ROOT)])
    job.running = False
    for e in job.logs:
        print(f"  {e['t']}  {e['m']}")
    return job


def verify(job):
    print("\n" + "=" * 62)
    print("校验结果")
    print("=" * 62)

    # 1. 逐项文件存在性
    for rel, desc in [
        ("A/hello.txt", "第一层 zip 解出"),
        ("A/B/world.txt", "zip 内嵌 7z 继续解压"),
        ("A/B/C/deep.txt", "7z 内嵌 zip 继续解压（三层递归）"),
        ("movie/movie.txt", "伪装 mp4 被识别并解压"),
        ("movie/sub/subdeep.txt", "伪装包内嵌套 zip 继续解压"),
        ("photo/photo.txt", "伪装 jpg 被识别并解压"),
        ("real_video.mp4", "真视频文件保持原样"),
        ("enc/secret.txt", "加密 rar 用字典密码解开"),
    ]:
        p = ROOT / rel
        check(desc, p.exists(), rel)

    # 2. 同名冲突自动重命名
    d1, d2 = ROOT / "Same", ROOT / "Same (1)"
    ok = d1.is_dir() and d2.is_dir()
    check("同名冲突自动生成两个目录", ok, f"{d1.name} / {d2.name}")
    if ok:
        f1 = list(d1.glob("*.txt"))
        f2 = list(d2.glob("*.txt"))
        ok2 = len(f1) == 1 and len(f2) == 1 and \
            f1[0].read_text(encoding="utf-8") != f2[0].read_text(encoding="utf-8")
        check("两个目录内容各自独立、无覆盖",
              ok2, f"{f1[0].name} vs {f2[0].name}" if ok2 else "内容异常")

    # 3. 真视频内容未被破坏
    rv = ROOT / "real_video.mp4"
    data = rv.read_bytes() if rv.exists() else b""
    check("真视频内容完整未损坏", data.startswith(b"\x00\x00\x00\x18ftypmp42"),
          f"{len(data)} 字节")

    # 4. 伪装文件名已还原，无残留 .zip
    leftover = [p.name for p in ROOT.iterdir()
                if p.is_file() and p.suffix.lower() == ".zip"
                and p.stem in ("movie", "photo", "real_video")]
    check("伪装文件名已还原、无残留 .zip", not leftover, str(leftover or "无"))

    # 5. 无残留空目录
    empty = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
             if p.is_dir() and not any(p.iterdir())]
    check("无残留空目录", not empty, ", ".join(empty) if empty else "无")

    print("-" * 62)
    print("统计:", json.dumps(job.stats, ensure_ascii=False))


if __name__ == "__main__":
    print("构造样例数据 …")
    build()
    print("样例目录:", ROOT)

    job1 = run(skip_done=True, label="首次运行")
    verify(job1)

    # 第二遍：验证「跳过已解压」，不应产生任何新目录
    before = {p.name for p in ROOT.iterdir()}
    job2 = run(skip_done=True, label="重复运行（应全部跳过）")
    after = {p.name for p in ROOT.iterdir()}
    print("\n" + "=" * 62)
    print("重复运行校验")
    print("=" * 62)
    check("重复运行未产生新目录", before == after,
          ", ".join(sorted(after - before)) or "无新增")
    check("重复运行解压数为 0", job2.stats["extracted"] == 0,
          f"extracted={job2.stats['extracted']}")
    check("重复运行跳过数 > 0", job2.stats["skipped_done"] > 0,
          f"skipped_done={job2.stats['skipped_done']}")

    failed = [d for d, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 62)
    total = len(RESULTS)
    print(f"自检结果: {total - len(failed)}/{total} 项通过 —— "
          + ("全部通过 ✅" if not failed else "存在失败 ❌"))
    if failed:
        for f in failed:
            print("  ×", f)
    print("=" * 62)
    sys.exit(0 if not failed else 1)
