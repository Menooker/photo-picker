import asyncio
from dataclasses import dataclass

from pymobiledevice3.exceptions import (
    AfcException,
    AfcFileNotFoundError,
    ConnectionTerminatedError,
    LockdownError,
    MuxException,
    NoDeviceConnectedError,
)
from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

# iPhone 断开后，重连重试的等待间隔（秒）
RETRY_INTERVAL_SECONDS = 5.0


class DeviceMismatchError(RuntimeError):
    """重连到的 iPhone 与首次记录的 serial 不一致，拒绝继续操作。"""


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否属于「iPhone 断开 / 连接异常」，从而触发重连重试。

    AfcFileNotFoundError 是数据错误（文件/目录不存在），不是断连，不重试。
    """
    if isinstance(exc, AfcFileNotFoundError):
        return False
    return isinstance(exc, (
        AfcException,                 # AFC 通讯层错误（不含 FileNotFound）
        MuxException,                 # usbmux / 设备未连接
        LockdownError,                # 锁屏服务错误
        ConnectionTerminatedError,    # 通道被关闭
        NoDeviceConnectedError,       # 没有已连接的 iPhone
        ConnectionError,              # 含 Reset/Aborted
        BrokenPipeError,
        EOFError,
        TimeoutError,
    ))


@dataclass
class PhotoFile:
    filename: str
    folder: str  # e.g. "100APPLE"
    remote_path: str  # e.g. "/DCIM/100APPLE/IMG_0001.HEIC"


class IPhoneImporter:
    def __init__(self):
        self._lockdown = None
        self._afc = None
        # 首次连接确定的 iPhone serial：重连时换成别的机器直接拒绝
        self.serial = None
        self._info = None  # 最近一次成功连接的设备信息缓存（避免 connect 重复建连）

    async def _establish(self) -> dict:
        """确保已建立 lockdown + AFC 连接，返回设备信息。

        serial 是唯一标识：重连时读到与首连不同的 serial，直接拒绝
        （export 会删除手机原片，绝不能连错设备）。连接已存在时直接
        返回缓存信息（幂等，避免 connect 重复建连）。
        """
        if self._afc is not None and self._info is not None:
            return self._info
        devices = await list_devices()
        if not devices:
            raise NoDeviceConnectedError("No iPhone connected")
        device = devices[0]
        if self.serial is not None and device.serial != self.serial:
            raise DeviceMismatchError(
                f"iPhone serial 不一致：预期「{self.serial}」，实际「{device.serial}」；"
                "为避免在错误设备上操作，请只连接同一台 iPhone。")
        if self.serial is None:
            self.serial = device.serial
        self._lockdown = await create_using_usbmux(device.serial)
        self._afc = AfcService(self._lockdown)
        info = {
            "serial": device.serial,
            "name": await self._lockdown.get_value("DeviceName"),
            "model": await self._lockdown.get_value("ProductType"),
            "ios_version": await self._lockdown.get_value("ProductVersion"),
        }
        self._info = info
        return info

    async def _teardown(self) -> None:
        """关闭并清空连接，为下一次重连做准备。"""
        afc = self._afc
        lockdown = self._lockdown
        self._afc = None
        self._lockdown = None
        self._info = None
        if afc is not None:
            try:
                await afc.close()
            except Exception:
                pass
        if lockdown is not None:
            close = getattr(lockdown, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass

    async def _with_reconnect(self, fn) -> object:
        """执行 iPhone 操作；连接异常时提示并按 RETRY_INTERVAL_SECONDS 重连重试。

        fn 为返回 awaitable 的零参可调用对象（如 lambda: self._afc.get_file_contents(p)）。
        仅对「断连」类异常重试；数据类错误（如文件不存在）直接向上抛。
        """
        while True:
            try:
                if self._afc is None:
                    await self._establish()
                return await fn()
            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                print(f"[photo-picker] iPhone 连接异常：{exc}；"
                      f"等待 {RETRY_INTERVAL_SECONDS} 秒后重连重试…")
                await self._teardown()
                await asyncio.sleep(RETRY_INTERVAL_SECONDS)

    async def connect(self) -> dict:
        """建立与 iPhone 的连接；断连/未连接时按 RETRY_INTERVAL_SECONDS 反复重试，
        返回设备信息。重连若发现 serial 不一致则抛 DeviceMismatchError。"""
        return await self._with_reconnect(self._establish)

    async def list_dcim_dirs(self) -> list[str]:
        dcim_folders = await self._with_reconnect(
            lambda: self._afc.listdir("/DCIM"))
        dirs = []
        for folder in dcim_folders:
            if folder.startswith(".") or not folder[0].isdigit():
                continue
            dirs.append(folder)
        return sorted(dirs)

    async def list_photos(self, top_dir: str) -> list[PhotoFile]:
        try:
            files = await self._with_reconnect(
                lambda: self._afc.listdir(f"/DCIM/{top_dir}"))
        except AfcFileNotFoundError:
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
        return await self._with_reconnect(
            lambda: self._afc.get_file_contents(photo.remote_path))

    async def remove_file(self, remote_path: str) -> None:
        """删除手机上的单个文件。删除失败时抛 RuntimeError。"""
        try:
            await self._with_reconnect(
                lambda: self._afc.rm_single(remote_path))
        except Exception as exc:
            raise RuntimeError(f"无法删除手机文件: {remote_path}") from exc