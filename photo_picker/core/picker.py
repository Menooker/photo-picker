import asyncio
from concurrent.futures import ThreadPoolExecutor

from .importer import IPhoneImporter
from .thumbnail import parse_metadata, make_thumbnail
from .llm_client import LLMClient
from .models import PhotoItem, PhotoResult

LLM_GROUP_MAX = 30
LLM_QUEUE_MAX = 90  # LLM 队列中待处理照片数上限（= 3 个 batch）
LLM_WORKERS = 1


def _process_photo(raw: bytes, filename: str) -> PhotoItem:
    """单个照片的 Pillow 处理：EXIF 解析 + 缩略图生成。"""
    meta = parse_metadata(raw, filename)
    thumb = make_thumbnail(raw)
    return PhotoItem(
        id=filename,
        taken_date=meta.taken_date,
        width=meta.width,
        height=meta.height,
        location=f"{meta.latitude:.4f},{meta.longitude:.4f}" if meta.latitude else "",
        thumbnail_bytes=thumb,
    )


class PhotoPicker:
    def __init__(self, llm_url: str = "http://localhost:8080/v1",
                 llm_model: str = "default"):
        self.importer = IPhoneImporter()
        self.llm_client = LLMClient(base_url=llm_url, model=llm_model)
        self._llm_pool = ThreadPoolExecutor(
            max_workers=LLM_WORKERS, thread_name_prefix="llm"
        )
        self._closed = False

    def close(self) -> None:
        """关闭 LLM 线程池。线程池是 PhotoPicker 的成员，由它负责清理。"""
        if self._closed:
            return
        self._closed = True
        self._llm_pool.shutdown(wait=True)

    def __enter__(self) -> "PhotoPicker":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    async def connect(self) -> dict:
        info = await self.importer.connect()
        print(f"Connected: {info['name']} ({info['model']}, iOS {info['ios_version']})")
        return info

    async def list_dirs(self) -> list[str]:
        dirs = await self.importer.list_dcim_dirs()
        print(f"DCIM directories:")
        for d in dirs:
            print(f"  {d}")
        return dirs

    async def classify(self, top_dir: str, count: int = None,
                       start_from: str = None):
        photos = await self.importer.list_photos(top_dir)
        if not photos:
            print(f"No photos in /DCIM/{top_dir}")
            return

        # 应用 start_from
        if start_from:
            start_idx = None
            for i, p in enumerate(photos):
                if p.filename == start_from:
                    start_idx = i
                    break
            if start_idx is not None:
                photos = photos[start_idx:]
                print(f"Starting from: {start_from}")
            else:
                print(f"Warning: '{start_from}' not found, processing all")

        # 仅处理前 N 张
        if count is not None:
            photos = photos[:count]
            print(f"Limit to first {count} photos")

        total = len(photos)
        print(f"Processing {total} photos from {top_dir}...")

        # 带宽受限：主循环串行下载，Pillow 直接在主循环同步处理（无 Pillow 线程池）。
        # LLM 池保留：批量分类在后台线程执行。
        # 用信号量限制同时在途的 LLM batch 数；队列过满则阻塞主循环（背压）。
        llm_futures: list[asyncio.Future] = []
        batch_buffer: list[PhotoItem] = []
        loop = asyncio.get_running_loop()
        llm_slots = asyncio.Semaphore(LLM_QUEUE_MAX // LLM_GROUP_MAX)

        def submit_batch(batch: list[PhotoItem]) -> asyncio.Future:
            return loop.run_in_executor(
                self._llm_pool, self.llm_client.classify_batch, batch
            )

        async def acquire_and_submit(batch: list[PhotoItem]) -> asyncio.Future:
            await llm_slots.acquire()
            fut = submit_batch(batch)
            fut.add_done_callback(
                lambda _f: loop.call_soon_threadsafe(llm_slots.release)
            )
            return fut

        for i, photo in enumerate(photos):
            raw = await self.importer.download_photo(photo)
            item = _process_photo(raw, photo.filename)
            batch_buffer.append(item)

            if len(batch_buffer) == LLM_GROUP_MAX:
                llm_futures.append(await acquire_and_submit(batch_buffer))
                batch_buffer = []
            if (i + 1) % 10 == 0:
                print(f"  Downloaded {i + 1}/{total}")

        # 收尾不足一个 batch 的剩余照片
        if batch_buffer:
            llm_futures.append(await acquire_and_submit(batch_buffer))

        # 收集 LLM 结果（任务已在后台完成，这里只取回结果）
        print(f"Download complete. Classifying...")
        results: list[PhotoResult] = []
        for fut in llm_futures:
            results.extend(await fut)

        # 显示结果
        delete = [r for r in results if r.action.value == "DELETE"]
        keep_phone = [r for r in results if r.action.value == "KEEP_PHONE"]
        keep = [r for r in results if r.action.value == "KEEP_PC"]
        undecided = [r for r in results if r.action.value == "UNDECIDED"]

        print(f"\nResults: {len(results)} classified")
        print(f"  DELETE:     {len(delete)}")
        print(f"  KEEP_PHONE: {len(keep_phone)}")
        print(f"  KEEP_PC:    {len(keep)}")
        print(f"  UNDECIDED:  {len(undecided)}")

        for label, group in (("DELETE", delete), ("KEEP_PHONE", keep_phone),
                             ("KEEP_PC", keep)):
            if not group:
                continue
            print(f"\n{label} candidates:")
            for r in group:
                print(f"  {r.id} - {r.reason} ({r.confidence:.0%})")

        return results