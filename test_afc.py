import asyncio
import sys

print("Step 1: imports...", flush=True)
from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
print("Step 2: base imports OK", flush=True)

print("Step 3: importing AfcService...", flush=True)
from pymobiledevice3.services.afc import AfcService
print("Step 4: AfcService imported", flush=True)

async def main():
    print("Step 5: listing devices...", flush=True)
    devices = await list_devices()
    print(f"Step 6: found {len(devices)} devices", flush=True)

    print("Step 7: creating lockdown...", flush=True)
    lockdown = await create_using_usbmux(devices[0].serial)
    print("Step 8: lockdown created", flush=True)

    print("Step 9: creating AFC...", flush=True)
    afc = AfcService(lockdown)
    print("Step 10: AFC created", flush=True)

    print("Step 11: listing / ...", flush=True)
    dirs = await afc.listdir("/")
    print(f"Step 12: root dirs = {dirs}", flush=True)

    print("Step 13: listing DCIM ...", flush=True)
    dcim = await afc.listdir("/DCIM")
    print(f"Step 14: DCIM dirs = {dcim}", flush=True)

    if dcim:
        first_folder = dcim[0]
        print(f"Step 15: listing /DCIM/{first_folder} ...", flush=True)
        photos = await afc.listdir(f"/DCIM/{first_folder}")
        print(f"Step 16: photos = {photos[:10]}", flush=True)

        # 测试下载第一张照片
        if photos:
            first_photo = photos[0]
            print(f"Step 17: reading /DCIM/{first_folder}/{first_photo} ...", flush=True)
            data = await afc.get_file_contents(f"/DCIM/{first_folder}/{first_photo}")
            print(f"Step 18: downloaded {len(data)} bytes", flush=True)

asyncio.run(main())
