"""FastAPI Web 服务器：通过浏览器驱动照片分类流水线。

注意：删除 / 搬到 PC 的操作目前都是 Dummy 实现 —— 服务器只在终端打印日志，
不会真正删除或搬运任何照片。
"""
import asyncio
import base64
import io
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from .core.picker import PhotoPicker

FRONTEND_DIR = Path(__file__).parent / "frontend"
FULL_MAX_SIZE = (2400, 2400)  # 放大查看时最大边
FULL_QUALITY = 90

app = FastAPI(title="Photo Picker")


class AppSession:
    """保存一次分类任务在服务器端复用的数据（含缩略图）。"""

    def __init__(self, max_thinking_tokens: int = 10000):
        self._lock = threading.Lock()
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
    delete_ids: list[str] = []
    move_ids: list[str] = []


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
    with session._lock:
        try:
            await session.ensure_connected()
        except Exception as e:
            raise _http_500(str(e))
        if session.running:
            raise HTTPException(status_code=409, detail="已有任务进行中")
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
async def api_photo(photo_id: str):
    """放大查看：从手机重新下载原图并转 JPEG 返回。"""
    try:
        await session.ensure_connected()
        files = await session.picker.importer.list_photos(session.dir)
        photo = next((f for f in files if f.filename == photo_id), None)
        if photo is None:
            raise HTTPException(status_code=404, detail=f"{photo_id} not found")
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
    """确认环节：Dummy 实现，仅打印日志，不做真实操作。"""
    for pid in req.delete_ids:
        print(f"[DUMMY DELETE] {pid}")
    for pid in req.move_ids:
        print(f"[DUMMY MOVE PC] {pid}")
    print(f"[DUMMY] 共 {len(req.delete_ids)} 张待删除, "
          f"{len(req.move_ids)} 张待移至 PC")
    return {"ok": True, "to_delete": len(req.delete_ids), "to_move": len(req.move_ids)}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def run(host: str = "127.0.0.1", port: int = 8000,
        llm_url: str = "http://localhost:8080/v1", llm_model: str = "default",
        max_thinking_tokens: int = 10000) -> None:
    import uvicorn

    session.max_thinking_tokens = max_thinking_tokens
    if session.picker is not None:
        session.picker.llm_client.max_thinking_tokens = max_thinking_tokens

    uvicorn.run(app, host=host, port=port, log_level="info")