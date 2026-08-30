# Photo Picker — AI 照片管理工具

Python + llama.cpp (OpenAI API) 本地照片管理工具。AI 建议哪些照片删除、哪些放入电脑保存，人工确认后执行。

## 1. 分阶段开发

### 第一阶段：验证可行性 ✅
- pymobiledevice3 AFC 协议读取 iPhone 照片 ✅
- llama.cpp OpenAI API 调用
- Pillow EXIF 读取 + 缩略图生成

### 第二阶段：CLI 工具
- 核心库（两个 ThreadPoolExecutor + 协程下载）
- 从 iPhone 批量读取照片（默认 100 张/批）
- Pillow 解析日期、地点、生成缩略图
- 按日期分组，LLM 批量分析
- 结果输出到 JSON 文件

### 第三阶段：Web 前端
- 基于核心库开发 Web 服务器（FastAPI）
- 简单 HTML 网页（无框架）
- 照片网格展示 + 人工确认

## 2. 核心架构

### 2.1 三阶段流水线

```
iPhone USB (pymobiledevice3 AFC, async)
    │ 原始图片 bytes
    │ 在线程内用 asyncio 协程下载
    ▼
┌─────────────────────────┐
│  ThreadPoolExecutor 1   │  缩略图 + EXIF 解析
│  (Pillow, 1 线程)       │  解析日期、地点、生成 512px 缩略图
└────────────┬────────────┘
             │ 缩略图 + 元数据
             │ 按日期分组（每组 ≤30 张）
             ▼
┌─────────────────────────┐
│  ThreadPoolExecutor 2   │  批量 LLM 调用
│  (OpenAI API, 1 线程)   │  每组一次性调用，传入缩略图
└────────────┬────────────┘
             │ 分类结果
             ▼
        results.jsonl
```

### 2.2 执行流程

```
1. iPhone 连接 → 列出 DCIM 所有文件
2. 批量下载 100 张照片（async，在线程中用 asyncio）
3. 提交到 Pillow 线程池：
   - 读取 EXIF（拍摄时间、GPS 坐标）
   - 生成 512px 缩略图（压缩为 JPEG）
4. 按拍摄日期分组
5. 每组 ≤30 张 → 提交到 LLM 线程池：
   - 缩略图转 base64
   - 调用 OpenAI API（llama.cpp 后端）
   - 解析返回的 JSON 分类结果
6. 结果追加写入 results.jsonl
7. 重复 2-6 直到处理完所有照片
```

### 2.3 线程与队列

| 组件 | 类型 | 数量 | 职责 |
|---|---|---|---|
| iPhone 下载 | asyncio 协程 | 主线程内 | 从 AFC 读取文件 bytes |
| Pillow 处理 | ThreadPoolExecutor | 1 线程 | EXIF 解析 + 缩略图生成 |
| LLM 调用 | ThreadPoolExecutor | 1 线程 | OpenAI API 批量调用 |

不使用 concurrent.futures.Queue，直接用 `executor.submit()` 提交任务，`future.result()` 获取结果。

## 3. 缩略图策略（Qwen3.6-35B-A3B 适配）

### 3.1 模型信息

**Qwen3.6-35B-A3B** — MoE 架构，35B 总参数 / ~3B 激活参数，多模态（text + image + video）。
- 视觉编码器 patch size = 16x16
- 支持分辨率：256x256 ~ 4096x4096
- 图片不强制缩放，按原始分辨率生成 token
- 每张图片 token 数 = (H/16) × (W/16)

### 3.2 分辨率选择

| 缩略图尺寸 | Patches | Token/张 | 30张总 Token | 适用场景 |
|---|---|---|---|---|
| 256px | 16×16 | 256 | 7,680 | 快速扫描，节省显存 |
| **384px** | **24×24** | **576** | **17,280** | **推荐：细节够用，显存友好** |
| 512px | 32×32 | 1,024 | 30,720 | 高质量分析 |
| 768px | 48×48 | 2,304 | 69,120 | 精细辨认（OCR等） |

### 3.3 推荐方案

**默认 384px**，理由：
- 每张照片 576 token，30 张合计 ~17K token
- 足够 LLM 识别：人物、风景、截图、模糊、重复
- 缩略图 JPEG 质量 85，文件约 20-40KB
- 3B 激活参数处理 17K token 输入很轻松

```python
THUMBNAIL_SIZE = 384  # 长边像素
THUMBNAIL_QUALITY = 85  # JPEG 压缩质量
```

### 3.3 EXIF 提取

从原图 EXIF 提取：
- `DateTimeOriginal` → 拍摄时间 → 分组依据
- `GPSInfo` → 纬度/经度 → 地点信息
- `ImageWidth` / `ImageLength` → 图片尺寸
- `Make` / `Model` → 相机型号

使用 Pillow `Image.getexif()` 提取，无需额外库。

## 4. 数据格式

### 4.1 断点续传：progress.json

```json
{
  "last_imported_file": "IMG_0999.HEIC",
  "total_imported": 10000,
  "total_analyzed": 8500
}
```

### 4.2 LLM 结果：results.jsonl

每行一条 JSON，追加写入：

```jsonl
{"id": 1, "filename": "IMG_0001.HEIC", "date": "2026-07-03", "location": "杭州", "action": "DELETE", "confidence": 0.9, "reason": "模糊废片"}
{"id": 2, "filename": "IMG_0002.HEIC", "date": "2026-07-03", "location": "杭州", "action": "KEEP_PC", "confidence": 0.85, "reason": "风景照，清晰，与IMG_0003角度不同"}
```

### 4.3 LLM 输入格式

每组照片以 JSON + 缩略图 base64 输入：

```json
{
  "date": "2026-07-03",
  "photos": [
    {
      "id": 1,
      "filename": "IMG_0706.HEIC",
      "size": "4032x3024",
      "location": "杭州西湖",
      "thumbnail_base64": "/9j/4AAQSkZJRg..."
    }
  ]
}
```

## 5. 分类标准

### DELETE（建议删除）
- 模糊/严重抖动
- 截图（聊天记录、验证码、临时信息、购物页面）
- 误触/口袋照/纯黑纯白
- 完全重复的照片
- 屏幕翻拍（摩尔纹）

### KEEP_PHONE（留在手机 — 有独特故事性或组内精选）
- 有独特故事性的照片（关键瞬间、有纪念意义）
- 重复组中精选出的一张（表情、光线、构图最好），同组其他设为 DELETE/KEEP_PC

### KEEP_PC（放入电脑保存 — 有类似内容但不完全一样）
核心逻辑：**同一天拍了多张相似但不同的照片，说明是刻意拍摄的多个角度/姿势/瞬间，应该全部保留**。

具体场景：
- 同一天的类似风景（不同时间点/光线/构图）
- 同一镜头故事的不同角度
- 同一合照的不同姿势
- 同一人物的不同表情/动作
- 连拍中每张都有独特价值

### UNDECIDED（不确定）
- 价值不明确
- 无法判断是否为刻意多拍

## 6. LLM 调用

### 6.1 API 格式

使用 OpenAI API 兼容格式，对接 llama.cpp server：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="default",
    messages=[...],
    temperature=0.3,
    max_tokens=2048
)
```

### 6.2 Prompt 文件

Prompt 放在独立文件方便审核：

```
photo-picker/
├── prompts/
│   ├── system.txt          # 系统提示词
│   └── user_template.txt   # 用户消息模板（含占位符）
```

## 7. CLI 命令

```bash
# 从 iPhone 批量导入并分析
python -m photo_picker analyze --count 100

# 从指定文件开始
python -m photo_picker analyze --start IMG_0500.HEIC --count 100

# 查看结果
python -m photo_picker results --action DELETE
python -m photo_picker results --action KEEP_PC

# 查看进度
python -m photo_picker progress
```

## 8. Web 前端（第三阶段）

简单 HTML 页面，无框架，fetch API 调用后端。

### API 端点（FastAPI）

| Method | Path | 说明 |
|---|---|---|
| POST | /api/analyze | 触发分析（指定数量） |
| GET | /api/results | 查询结果（支持筛选、分页） |
| GET | /api/thumbnail/{filename} | 缩略图 |
| GET | /api/original/{filename} | 原图 |
| POST | /api/decide | 人工确认 |
| GET | /api/progress | 进度查询 |

## 9. 项目结构

```
photo-picker/
├── photo_picker/
│   ├── __init__.py
│   ├── __main__.py          # CLI 入口
│   ├── core/
│   │   ├── __init__.py
│   │   ├── importer.py      # iPhone 照片读取（pymobiledevice3 AFC, async）
│   │   ├── thumbnail.py     # Pillow 缩略图 + EXIF 解析（ThreadPoolExecutor）
│   │   └── llm_client.py    # OpenAI API 客户端（ThreadPoolExecutor）
│   ├── cli.py               # 命令行界面
│   ├── server.py            # Web 服务器（FastAPI，第三阶段）
│   └── executor.py          # 删除/移动操作（第三阶段）
├── prompts/
│   ├── system.txt           # LLM 系统提示词
│   └── user_template.txt    # 用户消息模板
├── static/                  # Web 前端（第三阶段）
│   ├── index.html
│   └── app.js
├── photos/                  # 导入的照片原图
├── thumbnails/              # 缩略图缓存
├── progress.json            # 断点续传
├── results.jsonl            # LLM 分类结果
├── requirements.txt
└── DESIGN.md
```

## 10. 依赖

```
pymobiledevice3   # iPhone 照片读取
Pillow            # 缩略图生成 + EXIF 提取
openai            # llama.cpp OpenAI API 兼容
fastapi           # Web 服务器（第三阶段）
uvicorn           # ASGI 服务器（第三阶段）
```

## 11. 环境变量

```env
PICKER_LLM_BASE_URL=http://localhost:8080/v1
PICKER_LLM_MODEL=default
PICKER_BATCH_SIZE=100
PICKER_LLM_GROUP_SIZE=30
PICKER_THUMBNAIL_SIZE=384
PICKER_THUMBNAIL_QUALITY=85
```
