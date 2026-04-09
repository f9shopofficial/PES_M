# 🚨 Quick Troubleshooting Checklist

## โปรแกรมเด้งออกแล้ว - ทำงี้ก่อน!

### ⏱️ ทีแรก (5 นาที):

- [ ] **ดูเลย:** `logs/pesbot.log` (มี ERROR ไหม?)
- [ ] **ค่าย grep:** `grep "ERROR\|CRASH\|Exception" logs/pesbot.log`
- [ ] **ดู exit code:** ค้นหา `exit code` ใน log
- [ ] **ตรวจ RAM:** Task Manager → Performance
  - [ ] ถ้า 90%+ → OOM (exit code: -9)
  - [ ] ถ้า 50-70% → OK

### 🔧 ทีสอง (ถ้าไม่หาสาเหตุ):

- [ ] **Run terminal mode:**

  ```bash
  python pesbot.py 2>&1 | tee debug_run.log
  # เด้งแล้ว ดู final error
  ```

- [ ] **Check devices:**

  ```bash
  adb devices
  # Online ไหม? Lost connection?
  ```

- [ ] **Restart ADB:**
  ```bash
  adb kill-server
  adb start-server
  adb devices
  ```

### 🎯 Exit Code Guide (สำคัญ!):

```
0       → ปกติ ✅
1       → Error ใน code (ดู log)
-9      → Killed by system (OOM!) ⚠️ RAM เต็ม
-15     → SIGTERM (ตัวเอง terminate)
-11     → Segfault (ADB crash?)
```

### 📊 ถ้า Exit Code = -9:

**สาเหตุ:** RAM เต็ม (Memory Out)

```
ทำความสว่าง:
1. ปิด Emulator อื่น
2. ปิด Program อื่น ๆ
3. Restart bot
```

### 📊 ถ้า Exit Code = 1:

**สาเหตุ:** Exception ใน code

```
1. ตรวจ log หา "Exception in ..."
2. ดู traceback
3. ตรวจ device หลุด (adb devices)
```

### 💬 ถ้าไม่มี Exit Code:

**สาเหตุ:** Crash อื่นๆ (GUI, threading, etc.)

```
1. ลอง Terminal mode (ได้ error message ไหม?)
2. ตรวจ Tesseract ทำงาน (ค้าง?)
3. Restart Python interpreter
```

---

## 🎓 Common Issues

| Problem            | Symptom              | Fix                          |
| ------------------ | -------------------- | ---------------------------- |
| **OOM**            | exit -9              | Reduce devices, close others |
| **ADB Lost**       | stuck waiting        | `adb kill-server`            |
| **Queue Full**     | exit 1, "Queue full" | Restart, check UI            |
| **Tesseract Hang** | stuck OCR, timeout   | Reduce image size            |

---

## 📝 What's New?

✅ Log system (pesbot.log)
✅ Exit code tracking  
✅ Queue timeout  
✅ Screenshot cleanup  
✅ Process monitor  
✅ Crash notification

---

**Next:** Open `DEBUG_GUIDE.md` for full documentation
