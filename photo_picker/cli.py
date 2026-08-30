"""命令行界面（CLI）包装。

核心实现（PhotoPicker、流水线、常量）已移到 core.picker。
这里只做统一再导出，保持调用方（__main__ 命令、外部脚本）接口不变。
"""
from .core.picker import (
    LLM_GROUP_MAX,
    LLM_QUEUE_MAX,
    LLM_WORKERS,
    PhotoPicker,
    _process_photo,
)

__all__ = [
    "LLM_GROUP_MAX",
    "LLM_QUEUE_MAX",
    "LLM_WORKERS",
    "PhotoPicker",
    "_process_photo",
]