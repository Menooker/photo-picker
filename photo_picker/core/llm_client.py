import json
import base64
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from pydantic import ValidationError

from .models import PhotoItem, PhotoResult, ClassificationResponse

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
MAX_RETRIES = 2


@dataclass
class TokenUsage:
    """OpenAI 接口 token 消耗累计（挂在 PhotoPicker 上的成员）。"""

    input_cached: int = 0
    input_uncached: int = 0
    output: int = 0

    def add(self, input_cached: int, input_uncached: int, output: int) -> None:
        self.input_cached += input_cached
        self.input_uncached += input_uncached
        self.output += output

    def as_dict(self) -> dict:
        return {
            "input_cached": self.input_cached,
            "input_uncached": self.input_uncached,
            "output": self.output,
            "input_total": self.input_cached + self.input_uncached,
        }


class ResponseValidationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class LLMClient:
    def __init__(self, base_url: str = "http://localhost:8080/v1",
                 model: str = "default",
                 max_thinking_tokens: int = 10000,
                 usage: TokenUsage | None = None):
        """max_thinking_tokens: llama.cpp 推理模型的 thinking 预算。

        通过 OpenAI API 请求体的 extra_body（非标准字段）传给后端：
          chat_template_kwargs.reasoning_budget
        -1 = 不限；0 = 关闭思考；>0 = thinking token 上限。

        usage: 累计消耗对象（由 PhotoPicker 持有），每次调用后累加。
        """
        self.client = OpenAI(base_url=base_url, api_key="not-needed")
        self.model = model
        self.max_thinking_tokens = max_thinking_tokens
        self.usage = usage or TokenUsage()
        self._system_prompt = (PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")
        self._user_template = (PROMPTS_DIR / "user_template.txt").read_text(encoding="utf-8")
        self._schema_str = json.dumps(ClassificationResponse.model_json_schema(), ensure_ascii=False, indent=2)

    def classify_batch(self, photos: list[PhotoItem]) -> list[PhotoResult]:
        """Classify a batch of photos with retry on validation failure."""
        date = photos[0].taken_date or "unknown"

        # 构建交错的 metadata + image 列表
        content_parts = []

        for i, p in enumerate(photos):
            thumb_b64 = base64.b64encode(p.thumbnail_bytes).decode()
            meta = {
                "index": i,
                "id": p.id,
                "date": p.taken_date,
                "location": p.location,
            }

            # content_parts: metadata text + separator + image
            content_parts.append({"type": "text", "text": "==========\n" + json.dumps(meta, ensure_ascii=False)})
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{thumb_b64}"}})

        # 构建 user message
        user_text = self._user_template.replace("{{DATE}}", date)
        user_text += f"\n\n## JSON Schema（必须严格遵守）\n```json\n{self._schema_str}\n```"

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                *content_parts
            ]}
        ]

        photo_ids = [p.id for p in photos]

        for attempt in range(1 + MAX_RETRIES):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                extra_body={
                    # llama.cpp 支持的非标准字段：限制 thinking token 预算
                    "thinking_budget_tokens": self.max_thinking_tokens,
                    "reasoning_budget": self.max_thinking_tokens,
                },
            )

            content = response.choices[0].message.content
            self._record_usage(response)
            try:
                validated = self._parse_and_validate(content, photo_ids)
                return validated.results
            except ResponseValidationError as e:
                error_msg = f"上一次回复验证失败：{e.message}\n请严格按 JSON Schema 重新输出。"

                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "system", "content": error_msg})

        # 所有重试失败，返回空
        print(f"    Warning: validation failed after {1 + MAX_RETRIES} attempts")
        return []

    def _record_usage(self, response) -> None:
        """每次 LLM 调用后把 response.usage 累加进 self.usage。"""
        usage = response.usage
        if usage is None:
            return
        prompt_tokens = max(0, usage.prompt_tokens or 0)
        output_tokens = max(0, usage.completion_tokens or 0)
        cached = 0
        details = usage.prompt_tokens_details
        if details is not None:
            cached = max(0, details.cached_tokens or 0)
        uncached = max(0, prompt_tokens - cached)
        self.usage.add(cached, uncached, output_tokens)

    def _parse_and_validate(self, content: str, expected_ids: list[str]) -> ClassificationResponse:
        """Parse and validate LLM response. Returns ClassificationResponse or raises ResponseValidationError."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ResponseValidationError(f"JSON 解析失败: {e}")

        try:
            if isinstance(data, dict) and "results" in data:
                validated = ClassificationResponse(**data)
            elif isinstance(data, list):
                validated = ClassificationResponse(results=data)
            else:
                raise ResponseValidationError("返回格式必须是 {\"results\": [...]} 或 [...]")
        except ValidationError as e:
            raise ResponseValidationError(str(e))

        # 检查是否每张照片都有结果
        result_ids = {r.id for r in validated.results}
        missing = sorted(set(expected_ids) - result_ids)
        if missing:
            missing_str = ", ".join(missing)
            raise ResponseValidationError(f"缺少以下照片的结果: {missing_str}。你需要返回所有照片（包括缺失的和已经返回的）结果")

        return validated
