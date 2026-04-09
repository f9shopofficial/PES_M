# 🔧 PES BOT Debug Guide

## ✅ Improvements Made

### 1. 📋 Global Exception Hook

- ✅ เพิ่ม `sys.excepthook` ที่จับ uncaught exception ทั้งหมด
- ✅ แล้ว log ลง `pesbot.log` เพื่อไม่หาย

### 2. 📊 Comprehensive Logging System

- ✅ Setup logging ที่เขียน log ทั้งไป file และ console
- ✅ ใช้ `logging` module แทน print()
- ✅ Log file: `logs/pesbot.log`

### 3. 🛡️ Queue Safety with Timeout

- ✅ เพิ่ม `safe_queue_put()` function ที่มี timeout
- ✅ ป้องกัน deadlock เมื่อ queue เต็ม
- ✅ Update farm_mode.py ให้ใช้ safe queue

### 4. 💾 Process Health Monitoring

- ✅ Check process exitcode เมื่อ process ตาย
- ✅ Log sาเหตุทำไม process ตาย (-9 = OOM, 1 = error, etc.)
- ✅ UI notification เมื่อ process crash

### 5. 🧹 Resource Cleanup

- ✅ Periodic cleanup old screenshots
- ✅ `safe_cleanup_screenshots()` helper
- ✅ Keep only 5 recent screenshots

### 6. 🔍 Enhanced Error Logging

- ✅ Update `log_exception_to_json` decorator
- ✅ Add logger.error() calls
- ✅ Full traceback ใน log file

### 7. 📍 Farm Mode Protection

- ✅ Add logging ใน farm_mode main loop
- ✅ Add traceback printing ใน catch block
- ✅ Use safe_ui_queue_put ทั้ง 20 instances

---

## 🔍 วิธีดู Log

### 1. Real-time log ชอบใช้ terminal:

```bash
tail -f logs/pesbot.log
```

### 2. ดูเฉพาะ ERROR:

```bash
grep ERROR logs/pesbot.log
```

### 3. ดูเฉพาะ CRASH:

```bash
grep CRASH logs/pesbot.log
```

### 4. ดูเฉพาะ device ใดอย่างหนึ่ง:

```bash
grep "192.168.1.1" logs/pesbot.log
```

---

## 🚨 Crash Diagnosis

### ถ้าเด้งออกเงียบ ๆ:

1. ✅ ตรวจ `logs/pesbot.log` ก่อน
2. ✅ หา keyword: `ERROR`, `CRASH`, `Exception`
3. ✅ ดู traceback ที่ด้านล่าง

### Exit Code หมายถึง:

- `0` = ปกติ
- `1` = Generic error
- `-9` = Killed by OOM (RAM เต็ม!) ⚠️
- `-15` = SIGTERM (ถูก terminate)
- `-11` = SIGSEGV (Segmentation fault)

**ปัจจุบัน Exit Code จะแสดงใน UI:**

```
❌ CRASHED (exit code: -9)
```

---

## 💾 File Locations

| File                   | สำหรับอะไร                     |
| ---------------------- | ------------------------------ |
| `logs/pesbot.log`      | Main log file                  |
| `logs/error_log.json`  | Old error logging (still keep) |
| `screens/screen_*.jpg` | Auto delete old files          |

---

## 🎯 Recent Fixes ที่ช่วยได้

### ❌ ปัญหา: Queue deadlock

**✅ Fix:** Add 5s timeout, `safe_queue_put()`

### ❌ ปัญหา: Process crash ไม่รู้สาเหตุ

**✅ Fix:** Log exit code + monitor ใน poll_queues

### ❌ ปัญหา: RAM เต็ม

**✅ Fix:** Periodic cleanup old screenshots

### ❌ ปัญหา: Exception ใน child process หายไป

**✅ Fix:** Add global exception hook + logger

### ❌ ปัญหา: ไม่รู้ว่า farm_mode crashed ตรงไหน

**✅ Fix:** Add logging + traceback ใน farm_mode

---

## 🚀 Tips ถ้ายังเด้งออก

### 1. เช็ค Memory:

```powershell
# PowerShell
Get-Process | Where-Object { $_.ProcessName -like "*python*" } | Select-Object ProcessName, @{Name="MemoryMB";Expression={[math]::Round($_.WorkingSet/1MB)}}
```

### 2. Run with debug output (terminal):

```bash
python pesbot.py 2>&1 | tee debug_run.log
```

_(Keep everything in file + screen)_

### 3. ตรวจ devices หลุด:

```bash
adb devices
```

### 4. ตรวจ Tesseract:

- Tesseract processor ค้าง → use timeout
- OOM → reduce image size

---

## 📝 Log Format

```
2026-02-28 10:23:45,123 - pesbot - ERROR - [MainProcess:1234] - Exception in detect: ...
                          ^time               ^level    ^process info
                                                                           ^message
```

---

## ⚠️ Known Issues ที่ยังอาจเกิด

### 1. ADB Disconnect

- Solution: Restart ADB, reconnect device

### 2. Tesseract Timeout

- Solution: Already wrapped, fallback อยู่

### 3. Screenshot Permission Denied

- Solution: Check adb permissions

### 4. Queue.Full after timeout

- Solution: UI notification ส่ไป, data dropped (ยอมรับได้)

---

## 📞 Debugging Workflow

```
🔴 โปรแกรมเด้ง
    ↓
🔍 ตรวจ logs/pesbot.log
    ↓
☑️ ถ้ามี ERROR/CRASH
    → Fix ตามที่บอก
    → Re-run
    ↓
☐ ถ้าไม่มี log (crash ตั้งแต่เริ่มต้น)
    → Run terminal แล้วดู output
    → Check pesbot.log เกิด error ไหน
    ↓
✅ Fixed!
```

---

## 🎓 Technical Details

### Process Monitoring

`poll_queues()` run ทุก 500ms และ check:

- ✅ Process alive?
- ✅ Queue messages?
- ✅ Old screenshots to clean?

### Screenshot Cleanup

- Keep 5 latest
- Delete old ones every 30s
- Free disk space + memory

### Safe Queue

```python
safe_queue_put(queue_obj, data, timeout=5.0, device_serial='serial')
# จะ drop data ถ้า queue full (พิมพ์ warning แทน crash)
```

---

## 🔗 Related

- `pesbot.py` - Main bot
- `utils/farm_mode.py` - Farm mode logic (now with safe queue)
- `utils/utils_helper.py` - Helper functions
- `logs/pesbot.log` - Log file (บันทึกทั้งหมด)

---

**Last Updated:** 2026-02-28
**Status:** ✅ Enhanced debugging system v1.0
