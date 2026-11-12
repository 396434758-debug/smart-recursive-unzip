"""在临时目录验证子文件夹递归，不读取用户配置或正式账本。"""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import JieYa


def archive(path, name, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, data)


class RecursiveTests(unittest.TestCase):
    def test_subfolders_and_nested_archive(self):
        for builtin in (False, True):
            with self.subTest(builtin=builtin), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                inner = io.BytesIO()
                with zipfile.ZipFile(inner, "w") as z:
                    z.writestr("done.txt", "complete")
                outer = root / "existing" / "deeper" / "outer.zip"
                archive(outer, "folder/another/inner.zip", inner.getvalue())
                cfg = dict(JieYa.DEFAULT_CONFIG, max_depth=2)
                with patch.object(JieYa, "LEDGER_PATH", root / "ledger.json"):
                    with patch.object(JieYa, "TOOL", None if builtin else JieYa.TOOL):
                        job = JieYa.Job()
                        JieYa.Engine(job, cfg).run([root, outer.parent])
                        self.assertTrue((outer.parent / "outer/folder/another/inner/done.txt").is_file())
                        self.assertEqual(job.stats["extracted"], 2)
                        again = JieYa.Job()
                        JieYa.Engine(again, cfg).run([root])
                        self.assertEqual(again.stats["extracted"], 0)

    def test_archive_target_and_delete_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outer = root / "outer.zip"
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as z:
                z.writestr("done.txt", "complete")
            archive(outer, "folder/inner.zip", inner.getvalue())
            cfg = dict(JieYa.DEFAULT_CONFIG, skip_done=False, delete_original=True)
            job = JieYa.Job()
            JieYa.Engine(job, cfg).run([outer])
            self.assertTrue((root / "outer/folder/inner/done.txt").is_file())
            self.assertFalse(outer.exists())
            self.assertFalse((root / "outer/folder/inner.zip").exists())

    def test_depth_limit_still_limits_archive_layers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as z:
                z.writestr("done.txt", "complete")
            outer = root / "folder/outer.zip"
            archive(outer, "subfolder/inner.zip", inner.getvalue())
            cfg = dict(JieYa.DEFAULT_CONFIG, skip_done=False, max_depth=0)
            job = JieYa.Job()
            JieYa.Engine(job, cfg).run([root])
            self.assertTrue((outer.parent / "outer/subfolder/inner.zip").is_file())
            self.assertFalse((outer.parent / "outer/subfolder/inner/done.txt").exists())
            self.assertEqual(job.stats["extracted"], 1)

    def test_stop_when_real_content_extracted(self):
        def zbytes(files):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for n, d in files.items():
                    z.writestr(n, d)
            return buf.getvalue()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 包装层：只有内层包 + 广告 txt → 继续深入
            leaf = zbytes({"done.txt": "x"})
            (root / "wrap.zip").write_bytes(zbytes({"a/inner.zip": leaf, "解压密码.txt": "ad"}))
            # 真内容：4 个普通文件 + 一个自带的 zip → 视为解到底，不拆那个 zip
            (root / "game.zip").write_bytes(zbytes(
                {f"f{i}.dat": "x" for i in range(4)} | {"data/res.zip": leaf}))
            cfg = dict(JieYa.DEFAULT_CONFIG, skip_done=False)
            job = JieYa.Job()
            JieYa.Engine(job, cfg).run([root])
            self.assertTrue((root / "wrap/a/inner/done.txt").is_file())
            self.assertTrue((root / "game/data/res.zip").is_file())
            self.assertFalse((root / "game/data/res").exists())
            off = JieYa.Job()  # 0 = 不限制，照旧全部拆开
            JieYa.Engine(off, dict(cfg, stop_files=0)).run([root / "game"])
            self.assertTrue((root / "game/data/res/done.txt").is_file())


if __name__ == "__main__":
    unittest.main()
