import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

THUMBNAIL_SIZE = (640, 480)
THUMBNAIL_QUALITY = 85


@dataclass
class PhotoMetadata:
    filename: str
    width: int = 0
    height: int = 0
    taken_at: str = ""  # ISO format
    taken_date: str = ""  # YYYY-MM-DD
    latitude: float = 0.0
    longitude: float = 0.0
    file_size: int = 0


def _get_exif_datetime(img: Image.Image) -> str:
    exif = img.getexif()
    for tag_id in (36867, 36868, 306):
        val = exif.get(tag_id)
        if val:
            return val
    return ""


def _get_gps_info(img: Image.Image) -> tuple[float, float]:
    exif = img.getexif()
    gps_ifd = exif.get_ifd(0x8825)
    if not gps_ifd:
        return 0.0, 0.0

    def _to_degrees(values):
        d, m, s = [float(v) for v in values]
        return d + m / 60 + s / 3600

    lat = gps_ifd.get(2)
    lat_ref = gps_ifd.get(1)
    lon = gps_ifd.get(4)
    lon_ref = gps_ifd.get(3)

    if not all([lat, lon]):
        return 0.0, 0.0

    latitude = _to_degrees(lat)
    if lat_ref == "S":
        latitude = -latitude
    longitude = _to_degrees(lon)
    if lon_ref == "W":
        longitude = -longitude

    return round(latitude, 6), round(longitude, 6)


def parse_metadata(raw_data: bytes, filename: str) -> PhotoMetadata:
    img = Image.open(io.BytesIO(raw_data))

    dt_str = _get_exif_datetime(img)
    taken_at = ""
    taken_date = ""
    if dt_str:
        try:
            dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
            taken_at = dt.isoformat()
            taken_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    lat, lon = _get_gps_info(img)

    return PhotoMetadata(
        filename=filename,
        width=img.width,
        height=img.height,
        taken_at=taken_at,
        taken_date=taken_date,
        latitude=lat,
        longitude=lon,
        file_size=len(raw_data),
    )


def make_thumbnail(raw_data: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw_data))
    thumb = img.copy()
    thumb.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)
    if thumb.mode in ("RGBA", "P", "LA"):
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, "JPEG", quality=THUMBNAIL_QUALITY)
    return buf.getvalue()
