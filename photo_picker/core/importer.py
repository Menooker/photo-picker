from dataclasses import dataclass

from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService


@dataclass
class PhotoFile:
    filename: str
    folder: str  # e.g. "100APPLE"
    remote_path: str  # e.g. "/DCIM/100APPLE/IMG_0001.HEIC"


class IPhoneImporter:
    def __init__(self):
        self._lockdown = None
        self._afc = None

    async def connect(self) -> dict:
        devices = await list_devices()
        if not devices:
            raise RuntimeError("No iPhone connected")
        device = devices[0]
        self._lockdown = await create_using_usbmux(device.serial)
        self._afc = AfcService(self._lockdown)
        info = {
            "serial": device.serial,
            "name": await self._lockdown.get_value("DeviceName"),
            "model": await self._lockdown.get_value("ProductType"),
            "ios_version": await self._lockdown.get_value("ProductVersion"),
        }
        return info

    async def list_dcim_dirs(self) -> list[str]:
        dcim_folders = await self._afc.listdir("/DCIM")
        dirs = []
        for folder in dcim_folders:
            if folder.startswith(".") or not folder[0].isdigit():
                continue
            dirs.append(folder)
        return sorted(dirs)

    async def list_photos(self, top_dir: str) -> list[PhotoFile]:
        try:
            files = await self._afc.listdir(f"/DCIM/{top_dir}")
        except Exception:
            return []
        photos = []
        for f in files:
            if f.upper().endswith((".HEIC", ".JPG", ".JPEG", ".PNG")):
                photos.append(PhotoFile(
                    filename=f,
                    folder=top_dir,
                    remote_path=f"/DCIM/{top_dir}/{f}",
                ))
        return sorted(photos, key=lambda p: p.filename)

    async def download_photo(self, photo: PhotoFile) -> bytes:
        return await self._afc.get_file_contents(photo.remote_path)

    async def remove_file(self, remote_path: str) -> None:
        """删除手机上的单个文件。删除失败时抛 RuntimeError。"""
        try:
            # rm_single 成功时返回 None，失败时由 AFC 抛出异常；force 只属于 rm。
            await self._afc.rm_single(remote_path)
        except Exception as exc:
            raise RuntimeError(f"无法删除手机文件: {remote_path}") from exc
