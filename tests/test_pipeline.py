"""PhotoPicker 分类流水线测试（无需 iPhone / LLM 服务）。

运行方式（项目根目录）：conda run -n photo python tests\test_pipeline.py
覆盖：
1. 串行下载 + Pillow 主循环处理 + LLM 后台批分类
2. LLM 批按 photos 数组顺序（连续切片）提交
3. LLM 队列背压：同时在途 batch 数不超过 LLM_QUEUE_MAX / LLM_GROUP_MAX
4. PhotoPicker 上下文管理器正常关闭 LLM 线程池
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

# 保证从任意工作目录都能 import photo_picker 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_picker.cli as cli
import photo_picker.core.picker as core_picker


class FakeImporter:
    def __init__(self, count=65):
        self.count = count

    async def connect(self):
        return {"name": "test", "model": "X", "ios_version": "18"}

    async def list_photos(self, top_dir):
        return [SimpleNamespace(filename=f"IMG_{i:04d}.HEIC", folder=top_dir,
                                remote_path=f"/DCIM/{top_dir}/IMG_{i:04d}.HEIC")
                for i in range(self.count)]

    async def download_photo(self, photo):
        await asyncio.sleep(0)  # yield 一次即可，避免 Windows 定时器粗粒度干扰并发
        return b"\xff\xd8" + photo.filename.encode()


class FakeLLM:
    """记录每次 classify_batch 的入参顺序与并发数。"""

    def __init__(self, delay=0.05):
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0

    def classify_batch(self, photos):
        with self.lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        time.sleep(self.delay)
        self.calls.append([p.id for p in photos])
        with self.lock:
            self.concurrent -= 1
        return [SimpleNamespace(id=p.id, action=SimpleNamespace(value="DELETE"),
                                reason="test", confidence=0.5) for p in photos]


class FakePicker(cli.PhotoPicker):
    def __init__(self, fake_llm, llm_workers=4, count=65):
        self.importer = FakeImporter(count)
        self.llm_client = fake_llm
        self.on_progress = None
        self._llm_pool = ThreadPoolExecutor(
            max_workers=llm_workers, thread_name_prefix="llm")
        self._closed = False


def expected_order(count):
    return [f"IMG_{i:04d}.HEIC" for i in range(count)]


def picker_for(fake_llm, **kw):
    picker = FakePicker(fake_llm, **kw)
    core_picker._process_photo = (lambda raw, fn:
                                  SimpleNamespace(id=fn, taken_date="2026-01-01"))
    return picker


async def _run_classify(count, delay=0.05, llm_workers=4):
    fake_llm = FakeLLM(delay=delay)
    picker = picker_for(fake_llm, llm_workers=llm_workers, count=count)
    with picker:
        results = await picker.classify("100APPLE")
    return fake_llm, picker, results


async def test_batch_sizes_and_order():
    count = 65  # 期望批次 [30, 30, 5]
    fake_llm, picker, results = await _run_classify(count)
    expected = expected_order(count)
    slices = [expected[0:30], expected[30:60], expected[60:65]]
    assert len(results) == count
    # fake_llm.calls 按完成顺序记录，批次内容才是提交内容；用集合做无序匹配
    assert sorted(len(c) for c in fake_llm.calls) == [5, 30, 30]
    assert {tuple(c) for c in fake_llm.calls} == {tuple(s) for s in slices}
    # results 保持提交顺序（= photos 数组顺序），严格验证
    assert [r.id for r in results] == expected
    assert picker._closed
    print("test_batch_sizes_and_order OK")


async def test_backpressure():
    count = 200  # 7 个 batch
    fake_llm, picker, results = await _run_classify(count, delay=0.2)
    expected_batches = (count + cli.LLM_GROUP_MAX - 1) // cli.LLM_GROUP_MAX
    limit = cli.LLM_QUEUE_MAX // cli.LLM_GROUP_MAX
    assert len(fake_llm.calls) == expected_batches
    assert fake_llm.max_concurrent <= limit, "LLM 队列超过上限，背压失效"
    assert fake_llm.max_concurrent > 1, "测试未形成并发，无法验证背压"
    assert [r.id for r in results] == expected_order(count)
    print(f"test_backpressure OK (max concurrent {fake_llm.max_concurrent} <= {limit})")


async def test_tail_batch_submitted():
    count = 30  # 恰好一个整批，无尾批
    fake_llm, _, results = await _run_classify(count)
    assert [len(c) for c in fake_llm.calls] == [30]
    assert [r.id for r in results] == expected_order(count)
    print("test_tail_batch_submitted OK")


class ExportImporter:
    def __init__(self, top_dir):
        self.files = ["IMG_0001.HEIC", "IMG_0002.JPG"]
        self.bytes_by = {f: b"jpeg-bytes-" + f.encode() for f in self.files}
        self.removed = []

    async def download_photo(self, photo):
        await asyncio.sleep(0)
        return self.bytes_by[photo.filename]

    async def remove_file(self, remote_path):
        await asyncio.sleep(0)
        self.removed.append(remote_path)


class FakeAfc:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def rm_single(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return None


async def test_afc_remove_contract():
    """rm_single 只接收路径，成功返回 None，失败通过异常报告。"""
    importer = core_picker.IPhoneImporter()
    afc = FakeAfc()
    importer._afc = afc
    path = "/DCIM/100APPLE/IMG_0001.HEIC"
    await importer.remove_file(path)
    assert afc.calls == [((path,), {})]

    failure = OSError("AFC failed")
    importer._afc = FakeAfc(error=failure)
    try:
        await importer.remove_file(path)
        raise AssertionError("remove_file should propagate AFC failure")
    except RuntimeError as exc:
        assert path in str(exc)
        assert exc.__cause__ is failure
    print("test_afc_remove_contract OK")


async def test_export_copy_then_delete():
    """export_photos：先复制到本地、再删除手机原片；目录复刻相册路径。"""
    imp = ExportImporter("100APPLE")
    p = SimpleNamespace(importer=imp)
    progress = []
    with tempfile.TemporaryDirectory() as d:
        n1 = await core_picker.PhotoPicker.export_photos(
            p, "100APPLE", ["IMG_0001.HEIC", "IMG_0002.JPG"], d, "recycle",
            on_progress=lambda a, b: progress.append((a, b)))
        assert n1 == 2
        assert (Path(d) / "recycle" / "100APPLE" / "IMG_0001.HEIC").is_file()
        assert (Path(d) / "recycle" / "100APPLE" / "IMG_0002.JPG").is_file()

        n2 = await core_picker.PhotoPicker.export_photos(
            p, "100APPLE", ["IMG_0002.JPG"], d, "moved")
        assert n2 == 1
        assert (Path(d) / "moved" / "100APPLE" / "IMG_0002.JPG").is_file()

        # 复制成功后才删除原片：删除时本地副本必须已在磁盘
        assert (Path(d) / "recycle" / "100APPLE" / "IMG_0002.JPG").is_file()
        assert imp.removed == [
            "/DCIM/100APPLE/IMG_0001.HEIC",
            "/DCIM/100APPLE/IMG_0002.JPG",   # recycle 副本先落盘
            "/DCIM/100APPLE/IMG_0002.JPG",   # moved 副本先落盘
        ]
        assert progress == [(1, "IMG_0001.HEIC"), (2, "IMG_0002.JPG")]
    print("test_export_copy_then_delete OK")


async def test_export_never_overwrites_backup():
    """同内容复用；不同内容从 (1) 起寻找未占用或内容相同的名称。"""
    imp = ExportImporter("100APPLE")
    p = SimpleNamespace(importer=imp)
    with tempfile.TemporaryDirectory() as d:
        target_dir = Path(d) / "recycle" / "100APPLE"
        target_dir.mkdir(parents=True)

        same = target_dir / "IMG_0001.HEIC"
        same.write_bytes(imp.bytes_by["IMG_0001.HEIC"])
        await core_picker.PhotoPicker.export_photos(
            p, "100APPLE", ["IMG_0001.HEIC"], d, "recycle")
        assert same.read_bytes() == imp.bytes_by["IMG_0001.HEIC"]
        assert not (target_dir / "IMG_0001 (1).HEIC").exists()

        base = target_dir / "IMG_0002.JPG"
        first = target_dir / "IMG_0002 (1).JPG"
        base.write_bytes(b"older-photo")
        first.write_bytes(b"another-photo")
        await core_picker.PhotoPicker.export_photos(
            p, "100APPLE", ["IMG_0002.JPG"], d, "recycle")
        assert base.read_bytes() == b"older-photo"
        assert first.read_bytes() == b"another-photo"
        assert (target_dir / "IMG_0002 (2).JPG").read_bytes() == imp.bytes_by[
            "IMG_0002.JPG"
        ]

        # AFC 删除失败后的重试会找到刚写好的 (2)，不会再制造 (3)。
        await core_picker.PhotoPicker.export_photos(
            p, "100APPLE", ["IMG_0002.JPG"], d, "recycle")
        assert not (target_dir / "IMG_0002 (3).JPG").exists()
        assert imp.removed == [
            "/DCIM/100APPLE/IMG_0001.HEIC",
            "/DCIM/100APPLE/IMG_0002.JPG",
            "/DCIM/100APPLE/IMG_0002.JPG",
        ]
    print("test_export_never_overwrites_backup OK")


async def main():
    await test_batch_sizes_and_order()
    await test_backpressure()
    await test_tail_batch_submitted()
    await test_afc_remove_contract()
    await test_export_copy_then_delete()
    await test_export_never_overwrites_backup()
    print("ALL OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
