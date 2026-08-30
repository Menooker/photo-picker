"""photo_picker/core/thumbnail.py 测试（不需要断言到某张具体照片的确切值）。

图片路径通过环境变量 PICKER_TEST_IMAGE 提供，默认取：
    C:\\Users\\myjis\\Desktop\\IMG_3630.HEIC

运行方式：conda run -n photo python tests\\test_thumbnail.py
断言均为“正常值范围”而非该图片的精确匹配：
1. 尺寸、文件大小与实际数据一致
2. 拍摄时间是合法 ISO 日期且落在合理年代区间
3. GPS 地点若非空则落在合法经纬度范围
4. 缩略图是合法 JPEG，最长边不超过 THUMBNAIL_SIZE
"""

import io
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from photo_picker.core import thumbnail as T

TEST_IMAGE = os.environ.get("PICKER_TEST_IMAGE",
                            r"C:\Users\myjis\Desktop\IMG_3630.HEIC")


def load_raw() -> bytes:
    if not os.path.isfile(TEST_IMAGE):
        print(f"缺少测试图片：{TEST_IMAGE}（可用 PICKER_TEST_IMAGE 指定）",
              file=sys.stderr)
        sys.exit(1)
    with open(TEST_IMAGE, "rb") as f:
        return f.read()


def test_metadata(raw: bytes):
    meta = T.parse_metadata(raw, "IMG_TEST.HEIC")

    assert meta.filename == "IMG_TEST.HEIC"
    assert meta.width > 0 and meta.height > 0
    assert meta.width < 20000 and meta.height < 20000  # 合法照片尺寸范围
    assert meta.file_size == len(raw)

    # 拍摄日期：合法 ISO 日期，且年份在合理区间（2000 ~ 今年）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.taken_date), meta.taken_date
    taken = datetime.strptime(meta.taken_date, "%Y-%m-%d").date()
    assert date(2000, 1, 1) <= taken <= date.today()

    # taken_at 可被解析的完整时间戳
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", meta.taken_at)

    # 地点（若有 GPS）：合法经纬度范围；本测试图片带有 GPS，期望坐标非零
    if meta.latitude or meta.longitude:
        assert -90.0 <= meta.latitude <= 90.0
        assert -180.0 <= meta.longitude <= 180.0
    print("test_metadata OK")


def test_thumbnail(raw: bytes):
    thumb = T.make_thumbnail(raw)
    assert thumb.startswith(b"\xff\xd8")  # JPEG 魔数
    assert 0 < len(thumb) < len(raw)

    img = Image.open(io.BytesIO(thumb))
    im_w, im_h = img.size
    assert im_w > 0 and im_h > 0
    assert max(im_w, im_h) <= max(T.THUMBNAIL_SIZE), f"{im_w}x{im_h}"
    assert img.format == "JPEG"
    print(f"test_thumbnail OK ({im_w}x{im_h}, {len(thumb)} bytes)")


def main():
    raw = load_raw()
    test_metadata(raw)
    test_thumbnail(raw)
    print("ALL OK")


if __name__ == "__main__":
    sys.exit(main())