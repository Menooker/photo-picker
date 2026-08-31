"""FastAPI Web 服务器：通过浏览器驱动照片分类 + 删除 / 搬移到 PC。

「删除」= 把手机原片复制到本地磁盘 <输出目录>/recycle 后删掉手机上的原片；
「转移 PC」= 复制到 <输出目录>/moved 后删掉手机上的原片。
本地两个目录下会复刻手机相册路径（如 moved/100APPLE/IMG_0100.JPG），
每个文件按「先复制、再删除」的顺序逐个处理。
"""
import asyncio
import base64
import io
import os
import traceback
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from .core.picker import PhotoPicker

FRONTEND_DIR = Path(__file__).parent / "frontend"
FULL_MAX_SIZE = (2400, 2400)  # 放大查看时最大边
FULL_QUALITY = 90

app = FastAPI(title="Photo Picker")


class AppSession:
    """保存一次分类任务在服务器端复用的数据（含缩略图）。"""

    def __init__(self, max_thinking_tokens: int = 10000):
        self._lock = asyncio.Lock()
        self.picker: PhotoPicker | None = None
        self.connected = False
        self.dir = ""              # 本次任务的 DCIM 子目录
        self.items: list[dict] = []   # 元数据，顺序与照片一致
        self.thumbnails: dict[str, bytes] = {}  # filename -> JPEG bytes
        self.max_thinking_tokens = max_thinking_tokens
        # 后台分类任务状态
        self.job: asyncio.Task | None = None
        self.running = False
        self.error = ""
        self.progress: dict = {"done": 0, "total": 0}
        # token 消耗（由 core 回调更新）
        self.usage_snapshot: dict = {
            "input_cached": 0, "input_uncached": 0, "output": 0, "input_total": 0,
        }
        # 删除 / 转移后台任务的运行状态（不保存传输目标）
        self.transfer_status: dict = {
            "running": False, "error": "", "done": 0, "total": 0,
            "phase": "", "current": "",
        }

    async def ensure_connected(self):
        if not self.picker:
            self.picker = PhotoPicker(max_thinking_tokens=self.max_thinking_tokens)
            # 注册 core 回调：LLM 批次进度（并顺带刷新 token 消耗快照）
            self.picker.on_progress = self._on_progress
        if not self.connected:
            await self.picker.connect()
            self.connected = True

    def _on_progress(self, done: int, total: int) -> None:
        self.progress = {"done": done, "total": total}
        self.usage_snapshot.update(self.picker.usage.as_dict())


DEFAULT_MAX_THINKING_TOKENS = 10000
session = AppSession(DEFAULT_MAX_THINKING_TOKENS)


def _http_500(detail: str) -> HTTPException:
    # 完整堆栈打印到服务器终端，便于调试
    traceback.print_exc()
    return HTTPException(status_code=500, detail=detail)


class ClassifyRequest(BaseModel):
    dir: str
    start_from: str | None = None
    count: int | None = None


class ConfirmRequest(BaseModel):
    iphone_dir: str
    delete_ids: list[str] = Field(default_factory=list)
    move_ids: list[str] = Field(default_factory=list)
    dest: str  # 本地输出根目录，会生成 moved / recycle 两个子文件夹


@dataclass(frozen=True)
class TransferPlan:
    """确认时固定的传输参数，后台任务不得再读取 session.dir。"""

    iphone_dir: str
    delete_ids: tuple[str, ...]
    move_ids: tuple[str, ...]
    dest: str


def _is_plain_component(value: str) -> bool:
    """只允许一个目录项，禁止把客户端值当成相对路径拼接。"""
    return bool(value) and value not in (".", "..") and not any(
        char in value for char in ("/", "\\", "\x00")
    )


async def _remote_photos_by_name(iphone_dir: str) -> dict[str, object]:
    """验证 DCIM 子目录并返回其中当前可见的照片。"""
    if not _is_plain_component(iphone_dir):
        raise HTTPException(status_code=400, detail="iPhone 照片目录无效")

    dirs = await session.picker.importer.list_dcim_dirs()
    if iphone_dir not in dirs:
        raise HTTPException(
            status_code=400, detail=f"iPhone 照片目录不存在: {iphone_dir}"
        )
    return {
        photo.filename: photo
        for photo in await session.picker.importer.list_photos(iphone_dir)
    }


def _item_meta(item, result=None) -> dict:
    return {
        "id": item.id,
        "taken_date": item.taken_date,
        "width": item.width,
        "height": item.height,
        "location": item.location,
        "action": result.action.value if result else "UNDECIDED",
        "confidence": round(result.confidence, 4) if result else 0.0,
        "reason": result.reason if result else "",
    }


@app.get("/api/dirs")
async def api_dirs():
    try:
        await session.ensure_connected()
        dirs = await session.picker.list_dirs()
        session.dirs = dirs
        return {"dirs": dirs}
    except Exception as e:
        raise _http_500(str(e))


@app.post("/api/classify")
async def api_classify(req: ClassifyRequest):
    async with session._lock:
        if session.running:
            raise HTTPException(status_code=409, detail="已有分类任务进行中")
        if session.transfer_status["running"]:
            raise HTTPException(status_code=409, detail="删除/转移任务进行中")
        try:
            await session.ensure_connected()
        except Exception as e:
            raise _http_500(str(e))
        session.running = True
        session.error = ""
        session.progress = {"done": 0, "total": 0}
        session.items = []
        session.thumbnails = {}
        session.job = asyncio.create_task(_run_classify(req))
    return {"started": True, "dir": req.dir}


async def _run_classify(req: ClassifyRequest):
    """后台执行分类流水线，结束后把 items/缩略图写入 session。"""
    try:
        out = await session.picker.classify(
            req.dir, count=req.count, start_from=req.start_from,
            return_items=True,
        )
        results, items = (([], []) if out is None else out)
        session.dir = req.dir
        result_by_id = {r.id: r for r in results}
        session.items = [_item_meta(item, result_by_id.get(item.id)) for item in items]
        session.thumbnails = {item.id: item.thumbnail_bytes for item in items}
    except Exception:
        traceback.print_exc()
        session.error = traceback.format_exc()
    finally:
        session.usage_snapshot.update(session.picker.usage.as_dict())
        session.running = False


@app.get("/api/status")
async def api_status():
    """分类任务状态：是否运行、LLM 批次进度、token 消耗、结果 items。"""
    return {
        "running": session.running,
        "done": session.progress["done"],
        "total": session.progress["total"],
        "error": session.error,
        "dir": session.dir,
        "count": len(session.items),
        "items": session.items,
        "usage": session.usage_snapshot,
    }


@app.get("/api/usage")
async def api_usage():
    """OpenAI API token 总消耗（每次 LLM 调用后由 core 回调更新）。"""
    return session.usage_snapshot


@app.get("/api/thumbs")
async def api_thumbs(ids: list[str] = Query(default=[])):
    """一次性返回多张缩略图（base64），供分页渲染。"""
    thumbs = {}
    for pid in ids:
        raw = session.thumbnails.get(pid)
        if raw:
            thumbs[pid] = base64.b64encode(raw).decode("ascii")
    return {"thumbs": thumbs}


@app.get("/api/photo/{photo_id}")
async def api_photo(photo_id: str, iphone_dir: str = Query(...)):
    """放大查看：从手机重新下载原图并转 JPEG 返回。"""
    try:
        await session.ensure_connected()
        if not _is_plain_component(photo_id):
            raise HTTPException(status_code=400, detail="照片文件名无效")
        photos = await _remote_photos_by_name(iphone_dir.strip())
        photo = photos.get(photo_id)
        if photo is None:
            raise HTTPException(
                status_code=404, detail=f"{iphone_dir}/{photo_id} not found"
            )
        raw = await session.picker.importer.download_photo(photo)
    except HTTPException:
        raise
    except Exception as e:
        raise _http_500(str(e))

    img = Image.open(io.BytesIO(raw))
    img.thumbnail(FULL_MAX_SIZE, Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=FULL_QUALITY)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@app.post("/api/confirm")
async def api_confirm(req: ConfirmRequest):
    """确认执行：先把照片复制到本地（recycle / moved），再删手机原片。

    返回后立即启动后台任务（POST 不阻塞），用 GET /api/transfer 轮询进度。
    """
    async with session._lock:
        if session.transfer_status["running"]:
            raise HTTPException(status_code=409, detail="已有删除/转移任务进行中")
        if session.running:
            raise HTTPException(status_code=409, detail="分类任务进行中")

        dest = req.dest.strip().strip('"')
        if not dest:
            raise HTTPException(status_code=400, detail="请先选择输出目录")
        dest = str(Path(os.path.expanduser(dest)).resolve())

        iphone_dir = req.iphone_dir.strip()
        delete_ids = tuple(req.delete_ids)
        move_ids = tuple(req.move_ids)
        if len(delete_ids) != len(set(delete_ids)):
            raise HTTPException(status_code=400, detail="删除列表包含重复文件")
        if len(move_ids) != len(set(move_ids)):
            raise HTTPException(status_code=400, detail="转移列表包含重复文件")
        overlap = set(delete_ids) & set(move_ids)
        if overlap:
            raise HTTPException(
                status_code=400,
                detail=f"同一文件不能同时删除和转移: {sorted(overlap)[0]}",
            )
        invalid = next(
            (name for name in (*delete_ids, *move_ids)
             if not _is_plain_component(name)),
            None,
        )
        if invalid is not None:
            raise HTTPException(status_code=400, detail=f"照片文件名无效: {invalid}")

        try:
            await session.ensure_connected()
            remote_photos = await _remote_photos_by_name(iphone_dir)
        except HTTPException:
            raise
        except Exception as e:
            raise _http_500(str(e))
        missing = (set(delete_ids) | set(move_ids)) - set(remote_photos)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"照片不在 iPhone 目录 {iphone_dir} 中: {sorted(missing)[0]}",
            )

        for sub in ("moved", "recycle"):
            try:
                os.makedirs(os.path.join(dest, sub), exist_ok=True)
            except OSError as e:
                raise HTTPException(
                    status_code=400, detail=f"无法在 {dest} 下创建 {sub} 目录: {e}")
        plan = TransferPlan(
            iphone_dir=iphone_dir,
            delete_ids=delete_ids,
            move_ids=move_ids,
            dest=dest,
        )
        session.transfer_status = {
            "running": True, "error": "", "done": 0,
            "total": len(delete_ids) + len(move_ids),
            "phase": "", "current": "",
        }
        session.job = asyncio.create_task(_run_transfer(plan))
    return {"started": True, "to_delete": len(delete_ids),
            "to_move": len(move_ids), "dest": dest,
            "iphone_dir": iphone_dir}


async def _run_transfer(plan: TransferPlan):
    """后台执行：先处理「删除」（recycle），再处理「转移到 PC」（moved）。"""
    done = 0

    def on_progress(_n: int, filename: str) -> None:
        nonlocal done
        done += 1
        session.transfer_status["done"] = done
        session.transfer_status["current"] = filename

    try:
        if plan.delete_ids:
            session.transfer_status["phase"] = "recycle"
            await session.picker.export_photos(
                plan.iphone_dir, plan.delete_ids, plan.dest, "recycle",
                on_progress=on_progress)
        if plan.move_ids:
            session.transfer_status["phase"] = "moved"
            await session.picker.export_photos(
                plan.iphone_dir, plan.move_ids, plan.dest, "moved",
                on_progress=on_progress)
    except Exception:
        traceback.print_exc()
        session.transfer_status["error"] = traceback.format_exc()
    finally:
        session.transfer_status["phase"] = ""
        session.transfer_status["current"] = ""
        session.transfer_status["running"] = False


@app.get("/api/transfer")
async def api_transfer():
    """删除 / 转移后台任务状态（供轮询）。"""
    return session.transfer_status


@app.get("/api/browse")
async def api_browse(path: str = ""):
    """列出本地目录的子文件夹，供前端「浏览…」选择输出路径。path 为空用主目录。"""
    try:
        base = Path(os.path.expanduser(path or "~")).resolve()
        if not base.is_dir():
            raise HTTPException(status_code=400, detail=f"不是有效目录: {base}")
        dirs = sorted(
            (p.name for p in base.iterdir()
             if p.is_dir() and not p.name.startswith(".")),
            key=str.lower,
        )
    except HTTPException:
        raise
    except OSError as e:
        raise _http_500(str(e))
    parent = str(base.parent) if base.parent != base else ""
    return {"path": str(base), "name": base.name or str(base),
            "parent": parent, "dirs": dirs, "sep": os.sep}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def run(host: str = "127.0.0.1", port: int = 8000,
        llm_url: str = "http://localhost:8080/v1", llm_model: str = "default",
        max_thinking_tokens: int = 10000) -> None:
    import uvicorn

    session.max_thinking_tokens = max_thinking_tokens
    if session.picker is not None:
        session.picker.llm_client.max_thinking_tokens = max_thinking_tokens

    uvicorn.run(app, host=host, port=port, log_level="info")
