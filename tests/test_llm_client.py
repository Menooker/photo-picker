"""LLMClient 单元测试：mock OpenAI API 与图片 bytes，验证最终 prompt 格式。

运行方式：conda run -n photo python tests\test_llm_client.py
覆盖：
1. 最终 messages 结构：system + user，user.content 按 元数据→图片 交错排列
2. {{DATE}} 替换、JSON Schema 附加、base64 图片、index/id/date/location 传参
3. markdown 围栏剥离 + 校验通过；校验失败自动重试（追加 assistant + 错误信息）
4. 重试耗尽返回空列表
"""

import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from photo_picker.core import llm_client as L
from photo_picker.core.models import PhotoItem

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_TXT = (PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")
USER_TEMPLATE = (PROMPTS_DIR / "user_template.txt").read_text(encoding="utf-8")


def fake_completion(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None)


def make_client(*contents, model="test-model"):
    client = L.LLMClient(base_url="http://mock/v1", model=model)
    client.client = MagicMock()
    client.client.chat.completions.create.side_effect = [
        fake_completion(c) for c in contents
    ]
    return client


def make_photo(i, date="2023-02-27", location="31.1325,121.3962", thumb=b"\x01\x02\x03"):
    return PhotoItem(id=f"IMG_{i:04d}", taken_date=date, location=location,
                     thumbnail_bytes=thumb)


def valid_results(ids, action="DELETE"):
    results = [{"id": pid, "action": action, "confidence": 0.9, "reason": "模糊"}
               for pid in ids]
    return json.dumps({"results": results}, ensure_ascii=False)


def test_prompt_format():
    photos = [
        make_photo(1, location="31.1325,121.3962", thumb=b"\x01\x02\x03"),
        make_photo(2, date="2023-02-28", location="", thumb=b"\x04\x05\x06"),
    ]
    client = make_client(valid_results([p.id for p in photos], action="KEEP_PHONE"))
    assert client.max_thinking_tokens == 10000  # 默认预算
    results = client.classify_batch(photos)
    assert len(results) == 2
    assert results[0].id == "IMG_0001"
    assert results[0].action.value == "KEEP_PHONE"

    call = client.client.chat.completions.create.call_args
    assert call.kwargs["model"] == "test-model"
    messages = call.kwargs["messages"]
    assert call.kwargs["extra_body"] == {
        "thinking_budget_tokens": client.max_thinking_tokens,
        "reasoning_budget": client.max_thinking_tokens,
    }

    # system 消息 = system.txt 原文
    assert messages[0] == {"role": "system", "content": SYSTEM_TXT}

    # user 消息
    user = messages[1]
    assert user["role"] == "user"
    parts = user["content"]
    assert parts[0]["type"] == "text"

    # {{DATE}} 用第一张照片日期替换，schema 附加在模板后
    assert parts[0]["text"].startswith(USER_TEMPLATE.replace("{{DATE}}", "2023-02-27"))
    assert "## JSON Schema" in parts[0]["text"]

    # 每张照片一对：meta 文本 + 图片 base64，顺序一致
    expected = []
    for i, p in enumerate(photos):
        meta = {"index": i, "id": p.id, "date": p.taken_date, "location": p.location}
        expected.append({"type": "text",
                         "text": "==========\n" + json.dumps(meta, ensure_ascii=False)})
        b64 = base64.b64encode(p.thumbnail_bytes).decode()
        expected.append({"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    assert parts[1:] == expected
    print("test_prompt_format OK")


def test_retry_then_ok():
    photos = [make_photo(1), make_photo(2)]
    ids = [p.id for p in photos]
    # 第一次：围栏包裹但非法 JSON；第二次：合法（含围栏，应剥离）
    first = "```json\n{invalid json\n```"
    second = f"```json\n{valid_results(ids)}\n```"
    client = make_client(first, second)
    results = client.classify_batch(photos)
    assert [r.id for r in results] == ids

    create = client.client.chat.completions.create
    assert create.call_count == 2
    second_messages = create.call_args_list[1].kwargs["messages"]
    assert len(second_messages) == 4  # 追加 assistant + 错误提示
    assert second_messages[2]["role"] == "assistant"
    assert second_messages[2]["content"] == first
    assert second_messages[3]["role"] == "system"
    assert "验证失败" in second_messages[3]["content"]
    print("test_retry_then_ok OK")


def test_validation_failure_all_exhausted():
    photos = [make_photo(1), make_photo(2)]
    client = make_client("not json", "still not json", "still not json 2")
    results = client.classify_batch(photos)  # MAX_RETRIES=2 -> 共 3 次尝试
    assert results == []
    assert client.client.chat.completions.create.call_count == 1 + L.MAX_RETRIES
    print("test_validation_failure_all_exhausted OK")


def test_missing_photo_triggers_retry():
    # 返回结果缺一张 -> 校验失败，第二次补齐
    photos = [make_photo(1), make_photo(2), make_photo(3)]
    ids = [p.id for p in photos]
    incomplete = json.dumps({"results": [
        {"id": ids[0], "action": "DELETE", "confidence": 0.9, "reason": "a"},
    ]}, ensure_ascii=False)
    client = make_client(incomplete, valid_results(ids))
    results = client.classify_batch(photos)
    assert [r.id for r in results] == ids
    print("test_missing_photo_triggers_retry OK")


def main():
    test_prompt_format()
    test_retry_then_ok()
    test_validation_failure_all_exhausted()
    test_missing_photo_triggers_retry()
    print("ALL OK")


if __name__ == "__main__":
    sys.exit(main())