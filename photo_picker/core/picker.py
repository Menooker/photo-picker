import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .importer import IPhoneImporter, PhotoFile
from .thumbnail import parse_metadata, make_thumbnail
from .llm_client import LLMClient, TokenUsage
from .models import PhotoItem, PhotoResult

LLM_GROUP_MAX = 25
LLM_QUEUE_MAX = 75  # LLM 队列中待处理照片数上限（= 3 个 batch）
LLM_WORKERS = 1
FILE_COMPARE_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ExportEntry:
    """单个待导出文件：本地目标子路径与是否删除手机原片。

    sub_path 为空时由 export_photos 的 dst_sub 决定（如 recycle / moved）；
    keep_phone=True 表示「同时保留到手机和 PC」：复制到 PC 但删除手机原片。
    """

    filename: str
    sub_path: str = ""        # 相对 dest_root 的子路径（如 "recycle"、"named/旅行"）
    keep_phone: bool = False  # 只复制不到删除：保留手机原片 + 保存 PC 副本


def _file_matches(path: Path, expected: bytes) -> bool:
    """逐字节确认现有普通文件是否与 expected 完全一致。"""
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != len(expected)
        ):
            return False
        offset = 0
        with path.open("rb") as src:
            while chunk := src.read(FILE_COMPARE_CHUNK_SIZE):
                end = offset + len(chunk)
                if chunk != expected[offset:end]:
                    return False
                offset = end
        return offset == len(expected)
    except FileNotFoundError:
        # 文件可能在排他创建失败后被外部移除；按不匹配处理即可。
        return False


def _numbered_target(target: Path, n: int) -> Path:
    return target.with_name(f"{target.stem} ({n}){target.suffix}")


def _write_backup(target: Path, raw: bytes) -> Path:
    """不覆盖地写入备份；相同内容复用，不同内容依次尝试 (N)。"""
    n = 0
    while True:
        candidate = target if n == 0 else _numbered_target(target, n)
        try:
            # 排他创建避免 exists() 与 write() 之间的覆盖竞态。
            with candidate.open("xb") as dst:
                written = dst.write(raw)
                if written != len(raw):
                    raise OSError(
                        f"备份写入不完整: {candidate} ({written}/{len(raw)} bytes)"
                    )
        except FileExistsError:
            if _file_matches(candidate, raw):
                return candidate
            n += 1
            continue

        return candidate


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
                 llm_model: str = "default",
                 max_thinking_tokens: int = 10000):
        self.importer = IPhoneImporter()
        self.usage = TokenUsage()
        self.llm_client = LLMClient(base_url=llm_url, model=llm_model,
                                    max_thinking_tokens=max_thinking_tokens,
                                    usage=self.usage)
        # 可选回调，由上层（如 Web 服务器）注入：LLM 批次进度 (done, total)
        self.on_progress: callable | None = None
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
        print("DCIM directories:")
        for d in dirs:
            print(f"  {d}")
        return dirs

    async def classify(self, top_dir: str, count: int = None,
                       start_from: str = None, return_items: bool = False):
        """分类指定目录的照片。

        return_items=True 时返回 (results, items)；
        items 携带 thumbnail_bytes，供服务器复用（不再内部使用后丢弃）。
        """
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

        # LLM 批次进度：总 batch 数 = ceil(total / LLM_GROUP_MAX)
        total_batches = 0
        if total > 0:
            total_batches = (total + LLM_GROUP_MAX - 1) // LLM_GROUP_MAX
        completed = 0

        def emit_progress() -> None:
            if self.on_progress:
                self.on_progress(completed, total_batches)

        def on_batch_done(_f) -> None:
            nonlocal completed
            completed += 1
            emit_progress()

        # 带宽受限：主循环串行下载，Pillow 直接在主循环同步处理（无 Pillow 线程池）。
        # LLM 池保留：批量分类在后台线程执行。
        # 用信号量限制同时在途的 LLM batch 数；队列过满则阻塞主循环（背压）。
        llm_futures: list[asyncio.Future] = []
        batch_buffer: list[PhotoItem] = []
        items: list[PhotoItem] = []
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
            fut.add_done_callback(on_batch_done)
            return fut

        emit_progress()

        for i, photo in enumerate(photos):
            raw = await self.importer.download_photo(photo)
            item = _process_photo(raw, photo.filename)
            items.append(item)
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
        print("Download complete. Classifying...")
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

        return (results, items) if return_items else results

    async def export_photos(self, top_dir: str, entries: list[ExportEntry],
                            dest_root: str, dst_sub: str = "",
                            on_progress=None) -> int:
        """把手机照片导出到 <dest_root>/<sub_path>/<top_dir>/ 下并发回本机磁盘。

        每个 ExportEntry 指定本地目标子路径（sub_path 为空则归入 dst_sub）与
        是否删除手机原片（keep_phone=True 时只复制、不删除，用于「同时保留到
        手机和 PC」）。本地目录会复刻手机相册路径（如 recycle/100APPLE/IMG_0100.JPG）。
        返回成功处理的文件数；中途失败会向上抛异常（已完成的保留）。
        """
        n = 0
        for entry in entries:
            photo = PhotoFile(filename=entry.filename, folder=top_dir,
                              remote_path=f"/DCIM/{top_dir}/{entry.filename}")
            raw = await self.importer.download_photo(photo)
            sub_path = entry.sub_path or dst_sub
            target_dir = Path(dest_root) / sub_path / top_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            _write_backup(target_dir / entry.filename, raw)
            if not entry.keep_phone:
                await self.importer.remove_file(photo.remote_path)
            n += 1
            if on_progress:
                on_progress(n, entry.filename)
        return n
