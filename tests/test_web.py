"""Web (FastAPI) 接口测试（无需 iPhone / LLM / HTTP 客户端）。

运行方式（项目根目录）：conda run -n photo python tests\test_web.py
直接调用 FastAPI 端点函数，注入 FakePicker 代替真实 iPhone 流水线。
覆盖：目录列表 / 分类（返回带缩略图的 items） / 缩略图批量 base64 /
      全图下载转 JPEG / 确认（Dummy，仅日志）。
"""
import io
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_picker.web as web
from photo_picker.core.models import PhotoItem, PhotoResult
from PIL import Image


def make_jpeg(color: tuple[int, int, int], size=(320, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeImporter:
    def __init__(self):
        self.photo_files = {
            "IMG_0001.HEIC": f"/DCIM/100APPLE/IMG_0001.HEIC",
            "IMG_0002.HEIC": f"/DCIM/100APPLE/IMG_0002.HEIC",
        }

    async def list_photos(self, top_dir):
        return [SimpleNamespace(filename=f, folder=top_dir, remote_path=p)
                for f, p in self.photo_files.items() if top_dir in p]

    async def download_photo(self, photo):
        return make_jpeg((120, 60, 240), size=(800, 600))


class FakePicker:
    def __init__(self):
        self.importer = FakeImporter()

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


def setup_fake():
    web.session.picker = FakePicker()
    web.session.connected = True


async def main():
    setup_fake()

    r = await web.api_dirs()
    assert r == {"dirs": ["100APPLE", "100CLOUD"]}, r
    print("api_dirs OK")

    r = await web.api_classify(web.ClassifyRequest(dir="100APPLE"))
    assert r["count"] == 2, r
    assert r["items"][0]["action"] == "DELETE", r
    assert r["items"][1]["action"] == "KEEP_PC", r
    assert len(web.session.thumbnails) == 2
    print("api_classify OK (returns items with thumbnails)")

    r = await web.api_thumbs(ids=["IMG_0001.HEIC", "IMG_0002.HEIC", "IMG_MISSING.HEIC"])
    assert set(r["thumbs"]) == {"IMG_0001.HEIC", "IMG_0002.HEIC"}, r
    from base64 import b64decode
    thumb = Image.open(io.BytesIO(b64decode(r["thumbs"]["IMG_0001.HEIC"])))
    assert thumb.format == "JPEG"
    print("api_thumbs OK (base64 JPEG, missing ids ignored)")

    resp = await web.api_photo("IMG_0001.HEIC")
    full = Image.open(io.BytesIO(resp.body))
    assert full.format == "JPEG"
    print("api_photo OK (full JPEG download)")

    resp = await web.api_confirm(web.ConfirmRequest(
        delete_ids=["IMG_0001.HEIC"], move_ids=["IMG_0002.HEIC"]))
    assert resp == {"ok": True, "to_delete": 1, "to_move": 1}, resp
    print("api_confirm OK (dummy log only)")

    print("ALL OK")


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))