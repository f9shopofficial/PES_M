import subprocess
import re
import os
import time

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

def adb_connect(device_ip="127.0.0.1:16416"):
    try:
        result = subprocess.check_output(["adb", "connect", device_ip], text=True)
        if "connected" in result or "already connected" in result:
            print(f"✅ Connected to {device_ip}")
            return True
        else:
            print(result.strip())
            return False
    except subprocess.CalledProcessError as e:
        print("❌ Connect failed:", e.output)
        return False
def adb_run(cmd_list, timeout=20, **kwargs):
    '''
    รัน subprocess.run([...]) พร้อมตั้ง creationflags ไม่ให้โผล่หน้าต่างใหม่
    '''
    if os.name == 'nt':
        kwargs.setdefault('creationflags', CREATE_NO_WINDOW)
    return subprocess.run(cmd_list, timeout=timeout, **kwargs)

def open_line_ranger(serial):
        res = adb_run(
            ['adb', '-s', serial, 'shell', 'monkey', '-p', 'jp.konami.pesam',
             '-c', 'android.intent.category.LAUNCHER', '1'],
            timeout=20, capture_output=True, text=True
        )
        if res.returncode != 0:
            print(f'Stage 2 launch failed on {serial}: {res.stderr}')

def delete_file_pes(serial):
    FOLDER_PATH = '/data/data/jp.konami.pesam'

    res = adb_run(
        ['adb', '-s', serial, 'shell', f'ls -1 {FOLDER_PATH}'],
        timeout=20, capture_output=True, text=True
    )

    print(res.stdout.split())

    for name in res.stdout.split():
        print(f'ลบ {name} ...')
        if name not in ('cache', 'code_cache'):
            adb_run(
                ['adb', '-s', serial, 'shell',
                    f'rm -rf {FOLDER_PATH}/{name}']
            )
    # ลบ cache/*
    adb_run(
        ['adb', '-s', serial, 'shell', f'rm -rf {FOLDER_PATH}/cache/*']
    )

def swipe_down(device_serial: str, x_start: int, y_start: int, x_end: int, y_end: int, duration_ms: int = 500):
    try:
        # Backwards-compatible simple swipe if no hold requested
        adb_run([
            'adb', '-s', device_serial, 'shell', 'input', 'swipe',
            str(x_start), str(y_start), str(x_end), str(y_end), str(duration_ms)
        ], timeout=20)
    except Exception as e:
        print(f'{device_serial}: swipe_down exception: {e}')

def loop_select_gacha_slot(serial, swip_start, swip_end, mode='main', index=0):
    gacha_slot = 1

    if mode == 'main':
        gacha_slot = 7
    elif mode == 'free':
        gacha_slot = 7
    elif mode == 'multi':
        gacha_slot = min(index, 2)
        
    if gacha_slot <= 1:
        return

    for i in range(gacha_slot - 1):
        swipe_down(serial, swip_start[0],swip_start[1], swip_end[0], swip_end[1], duration_ms=6500)
        time.sleep(4)

# 🧪 ทดลองใช้
if __name__ == "__main__":
    serial = "127.0.0.1:16416"
    
    # adb_run(
    #     ['adb', '-s', serial, 'root'],
    #     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    #     text=True, timeout=20
    # )

    # info = delete_file_pes(serial)
    # if info:
    #     print(f"📱 Current app: {info['package']} ({info['activity']})")
    # else:
    #     print("❌ ไม่พบแอปที่กำลังเปิดอยู่")

    loop_select_gacha_slot(serial, (628, 252), (180, 252))