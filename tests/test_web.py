"""Web (FastAPI) 接口测试（无需 iPhone / LLM / HTTP 客户端）。

运行方式（项目根目录）：conda run -n photo python tests\test_web.py
直接调用 FastAPI 端点函数，注入 FakePicker 代替真实 iPhone 流水线。
覆盖：目录列表 / 分类（返回带缩略图的 items） / 缩略图批量 base64 /
      全图下载转 JPEG / 确认（Dummy，仅日志）。
"""
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_picker.web as web
from photo_picker.core.llm_client import TokenUsage
from photo_picker.core.models import PhotoItem, PhotoResult
from PIL import Image


def make_jpeg(color: tuple[int, int, int], size=(320, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeImporter:
    def __init__(self):
        self.photo_files = {
            "IMG_0001.HEIC": "/DCIM/100APPLE/IMG_0001.HEIC",
            "IMG_0002.HEIC": "/DCIM/100APPLE/IMG_0002.HEIC",
        }
        self.removed = []

    async def list_dcim_dirs(self):
        return ["100APPLE", "100CLOUD"]

    async def list_photos(self, top_dir):
        return [SimpleNamespace(filename=f, folder=top_dir, remote_path=p)
                for f, p in self.photo_files.items() if top_dir in p]

    async def download_photo(self, photo):
        return make_jpeg((120, 60, 240), size=(800, 600))

    async def remove_file(self, remote_path):
        self.removed.append(remote_path)


class FakePicker:
    def __init__(self):
        self.importer = FakeImporter()
        self.usage = TokenUsage()
        self.export_calls = []

    async def connect(self):
        pass

    async def list_dirs(self):
        return ["100APPLE", "100CLOUD"]

    async def classify(self, top_dir, count=None, start_from=None, return_items=False):
        raw1, raw2 = make_jpeg((200, 40, 40)), make_jpeg((40, 120, 200))
        items = [
            PhotoItem(id="IMG_0001.HEIC", taken_date="2026-01-01", width=800,
                      height=600, location="31.1325,121.3962", thumbnail_bytes=raw1),
            PhotoItem(id="IMG_0002.HEIC", taken_date="2026-01-02", width=800,
                      height=600, location="", thumbnail_bytes=raw2),
        ]
        results = [
            PhotoResult(id="IMG_0001.HEIC", action="DELETE",
                        confidence=0.95, reason="测试删除"),
            PhotoResult(id="IMG_0002.HEIC", action="KEEP_PC",
                        confidence=0.8, reason="测试保留到PC"),
        ]
        if return_items:
            return results, items
        return results

    async def export_photos(self, top_dir, entries, dest_root, dst_sub,
                            on_progress=None):
        """镜像真实 PhotoPicker.export_photos：复制→删除，写本地 moved/recycle。"""
        self.export_calls.append((
            top_dir,
            tuple((e.filename, e.sub_path, e.keep_phone) for e in entries),
            dst_sub,
        ))
        for n, e in enumerate(entries, 1):
            photo = SimpleNamespace(filename=e.filename, folder=top_dir,
                                    remote_path=f"/DCIM/{top_dir}/{e.filename}")
            raw = await self.importer.download_photo(photo)
            sub = e.sub_path or dst_sub
            target = Path(dest_root) / sub / top_dir / e.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if not e.keep_phone:
                await self.importer.remove_file(photo.remote_path)
            if on_progress:
                on_progress(n, e.filename)
        return len(entries)


def setup_fake():
    web.session = web.AppSession()
    web.session.picker = FakePicker()
    web.session.connected = True


async def main():
    setup_fake()

    r = await web.api_dirs()
    assert r == {"dirs": ["100APPLE", "100CLOUD"]}, r
    print("api_dirs OK")

    r = await web.api_classify(web.ClassifyRequest(dir="100APPLE"))
    assert r == {"started": True, "dir": "100APPLE"}, r
    await web.session.job
    st = await web.api_status()
    assert st["running"] is False, st
    assert st["count"] == 2, st
    assert st["items"][0]["action"] == "DELETE", st
    assert st["items"][1]["action"] == "KEEP_PC", st
    assert len(web.session.thumbnails) == 2
    print("api_classify OK (background job + status)")

    u = await web.api_usage()
    assert set(u) >= {"input_cached", "input_uncached", "output"}, u
    assert "usage" in st and set(st["usage"]) >= {"input_cached", "output"}, st
    print("api_usage OK (token 消耗统计)")
    print("api_status OK (含 done/total 进度与 usage)")

    r = await web.api_thumbs(ids=["IMG_0001.HEIC", "IMG_0002.HEIC", "IMG_MISSING.HEIC"])
    assert set(r["thumbs"]) == {"IMG_0001.HEIC", "IMG_0002.HEIC"}, r
    from base64 import b64decode
    thumb = Image.open(io.BytesIO(b64decode(r["thumbs"]["IMG_0001.HEIC"])))
    assert thumb.format == "JPEG"
    print("api_thumbs OK (base64 JPEG, missing ids ignored)")

    resp = await web.api_photo("IMG_0001.HEIC", iphone_dir="100APPLE")
    full = Image.open(io.BytesIO(resp.body))
    assert full.format == "JPEG"
    print("api_photo OK (full JPEG download)")

    with tempfile.TemporaryDirectory() as tempdir:
        resp = await web.api_confirm(web.ConfirmRequest(
            iphone_dir="100APPLE",
            delete_ids=["IMG_0001.HEIC"],
            moves=[web.MoveItem(id="IMG_0002.HEIC")],
            dest=tempdir))
        # 后台任务只能使用请求中固定的目录，不能再读取可变 session.dir。
        web.session.dir = "100CLOUD"
        assert resp["started"] and resp["to_delete"] == 1 and resp["to_move"] == 1, resp
        await web.session.job
        tr = await web.api_transfer()
        assert tr["running"] is False and not tr["error"], tr
        assert tr["done"] == 2 == tr["total"], tr
        assert set(tr) == {"running", "error", "done", "total", "phase", "current"}
        assert (Path(tempdir) / "recycle").is_dir()
        assert (Path(tempdir) / "moved").is_dir()
        removed = web.session.picker.importer.removed
        assert removed == ["/DCIM/100APPLE/IMG_0001.HEIC",
                           "/DCIM/100APPLE/IMG_0002.HEIC"], removed
        assert web.session.picker.export_calls == [
            ("100APPLE", (("IMG_0001.HEIC", "", False),), "recycle"),
            ("100APPLE", (("IMG_0002.HEIC", "", False),), "moved"),
        ]
        print("api_confirm OK (copy→delete background task + transfer status)")

        # named 子文件夹 + 同时保留手机
        web.session.picker.importer.removed = []
        resp2 = await web.api_confirm(web.ConfirmRequest(
            iphone_dir="100APPLE",
            moves=[web.MoveItem(id="IMG_0002.HEIC", named="旅行",
                                keep_phone=True)],
            dest=tempdir))
        await web.session.job
        tr2 = await web.api_transfer()
        assert tr2["running"] is False and not tr2["error"], tr2
        assert (Path(tempdir) / "named" / "旅行" / "100APPLE" / "IMG_0002.HEIC").is_file()
        assert web.session.picker.importer.removed == []   # keep_phone 不删除
        r2 = await web.api_named_dirs(dest=tempdir)
        assert r2["dirs"] == ["旅行"], r2
        r3 = await web.api_named_mkdir(web.MkdirRequest(dest=tempdir, name="工作"))
        assert r3["dirs"] == ["工作", "旅行"], r3
        assert (Path(tempdir) / "named" / "工作").is_dir()
        try:
            await web.api_named_mkdir(web.MkdirRequest(dest=tempdir, name="../x"))
            raise AssertionError("should reject path-like folder name")
        except web.HTTPException as e:
            assert e.status_code == 400
        print("api_named_dirs/mkdir OK (list + create named folder + security)")

        try:
            await web.api_confirm(web.ConfirmRequest(
                iphone_dir="100APPLE", delete_ids=[], dest="   "))
            raise AssertionError("should reject empty dest")
        except web.HTTPException as e:
            assert e.status_code == 400
        print("api_confirm OK (empty dest rejected)")

        try:
            await web.api_confirm(web.ConfirmRequest(
                iphone_dir="100APPLE", delete_ids=["../IMG_0001.HEIC"],
                dest=tempdir))
            raise AssertionError("should reject path-like filename")
        except web.HTTPException as e:
            assert e.status_code == 400

        try:
            await web.api_confirm(web.ConfirmRequest(
                iphone_dir="100CLOUD", delete_ids=["IMG_0001.HEIC"],
                dest=tempdir))
            raise AssertionError("should reject a file outside the requested dir")
        except web.HTTPException as e:
            assert e.status_code == 400
        print("api_confirm OK (remote dir/file validation)")

        web.session.running = True
        try:
            await web.api_confirm(web.ConfirmRequest(
                iphone_dir="100APPLE", delete_ids=["IMG_0001.HEIC"],
                dest=tempdir))
            raise AssertionError("confirm should be blocked while classifying")
        except web.HTTPException as e:
            assert e.status_code == 409
        finally:
            web.session.running = False

        web.session.transfer_status["running"] = True
        try:
            await web.api_classify(web.ClassifyRequest(dir="100APPLE"))
            raise AssertionError("classify should be blocked while transferring")
        except web.HTTPException as e:
            assert e.status_code == 409
        finally:
            web.session.transfer_status["running"] = False
        print("classification/transfer mutual exclusion OK")

        br = await web.api_browse(path=tempdir)
        assert br["path"] == str(Path(tempdir).resolve()), br
        assert "dirs" in br and "sep" in br, br
        try:
            await web.api_browse(path=str(Path(tempfile.gettempdir()) / "no_such_dir_xyz"))
            raise AssertionError("should reject missing dir")
        except web.HTTPException as e:
            assert e.status_code == 400
        print("api_browse OK (list dirs + bad path rejected)")

        print("ALL OK")


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
