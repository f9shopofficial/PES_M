# Imports
import subprocess
import traceback
import threading
import time
import json
import sys
import os
import cv2
import re
import numpy as np
import shutil
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
from ppadb.client import Client as AdbClient
from PIL import Image
from customtkinter import CTkImage
from PIL.ImageTk import PhotoImage
from typing import Any, Dict, List, Literal, Optional, Tuple
from multiprocessing import Manager, Process, Queue, freeze_support
import pytesseract
import queue
import functools
import datetime
import tempfile
import stat
import difflib
import logging
from utils.farm_mode import farm_mode
from utils.utils_helper import loop_action_before_confirm, count_checkmarks_in_image

# เก็บเวลาเริ่มแต่ละ stage
stage_start_times: Dict[str, float] = {}
# เก็บเวลา timeout ของแต่ละ device (คำนวณครั้งเดียวตอน start)
stage_timeout_at: Dict[str, float] = {}

# --------------------------------
# 1) Constant สำหรับปิด console window
# --------------------------------
if os.name == 'nt':  # Windows
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0

# --------------------------------
# 2) Coordinate Scaling Constants
# --------------------------------
SCALE_X = 1.333333333333333
SCALE_Y = 1.333333333333333

def scale_crop_area(crop_area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Scale crop area coordinates by the device scale factors"""
    if not crop_area or crop_area == (0, 0, 0, 0):
        return crop_area
    x1, y1, x2, y2 = crop_area
    return (
        int(x1 * SCALE_X),
        int(y1 * SCALE_Y),
        int(x2 * SCALE_X),
        int(y2 * SCALE_Y)
    )

# Configuration
def resource_path(relative_path: str, readonly: bool = False) -> str:
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        # 1) external path for editable files
        if not readonly:
            ext = os.path.join(exe_dir, 'bin', relative_path)
            if os.path.exists(ext):
                return ext
        # 2) bundle path (readonly or fallback)
        base = sys._MEIPASS
        cand = os.path.join(base, relative_path)
        if readonly or os.path.exists(cand):
            return cand
        # 3) fallback back to external even if readonly==True but bundle missing
        return ext
    else:
        project_dir = os.path.dirname(__file__)
        bin_path = os.path.join(project_dir, 'bin', relative_path)
        if os.path.exists(bin_path):
            return bin_path
        return os.path.join(project_dir, relative_path)

# กำหนด paths ของไฟล์ user-editable
MAIN_CONFIG_FILE = resource_path('main_config.json', readonly=False)
DEVICES_CONFIG_FILE = resource_path('device_config.json', readonly=False)
RANGERS_FOLDER = resource_path('carector_ref',  readonly=False)
STAGE_IMG_BASE = resource_path('stage_img', readonly=False)
LOG_FILE = resource_path('logs/error_log.json', readonly=False)  # สมมุติว่าเราอยากเก็บไว้ในโฟลเดอร์ชื่อ logs/

# ================================
# Setup Logging
# ================================
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
LOG_FILENAME = os.path.join(os.path.dirname(LOG_FILE), 'pesbot.log')

# 🗑️ Delete old log file to start fresh
if os.path.exists(LOG_FILENAME):
    try:
        os.remove(LOG_FILENAME)
        print(f"[STARTUP] Deleted old log file: {LOG_FILENAME}")
    except Exception as e:
        print(f"[STARTUP] Warning: Could not delete old log file: {e}")

logging.basicConfig(
    level=logging.INFO,  # Changed from DEBUG to INFO to reduce console noise 🔇
    format='%(asctime)s - %(name)s - %(levelname)s - [%(processName)s:%(process)d] - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILENAME),
        logging.StreamHandler()
    ]
)

# ⭐ Suppress pytesseract DEBUG logs (very verbose - don't show in console)
logging.getLogger('pytesseract').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("=== PES BOT STARTED ===")
logger.info(f"Log file: {LOG_FILENAME}")

# ================================
# Global Exception Hook
# ================================
def global_exception_hook(exctype, value, tb):
    """Catch all uncaught exceptions and log them"""
    logger.error("UNCAUGHT EXCEPTION:", exc_info=(exctype, value, tb))
    traceback.print_exception(exctype, value, tb)

sys.excepthook = global_exception_hook

# กำหนด paths ของไฟล์ read-only (ฝังใน bundle)
STEPS_FILE = resource_path('workflow_steps.json', readonly=True)
SCREENS_FOLDER = resource_path('screens', readonly=True)

# ตั้งค่า path ของ tesseract (สำหรับ Windows) - ปรับ path ตามที่ติดตั้ง
TESSERACT_EXE_PATH = resource_path('Tesseract-OCR/tesseract.exe', readonly=True)
pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE_PATH

# paths inside emulator
FOLDER_PATH = '/data/data/jp.konami.pesam'
MAIN_FILE_PATH = '/data/data/jp.konami.pesam/files/SaveData/AUTH/online_user_id_data.dat'

_config_lock = threading.Lock()
_config_cache = {}
_cache_timestamps = {}

connected_ports = []
devices = []
matcher = None
on_stage_manager= None

# Dictionaries to manage per-device control and UI
device_procs: Dict[str, Process] = {}
device_queues: Dict[str, Queue] = {}
stage_labels = {}
sub_stage_labels = {}
confidence_labels = {}
img_screen_labels = {}
img_template_labels = {}
file_size_labels = {}
device_labels = {}

# Runtime-only state for reroll/farm mode (NOT persisted to config JSON)
reroll_state: Dict[str, Dict[str, Optional[str]]] = {}  # {serial: {current_file, current_folder}}

main_configs = {}
devices_configs = {}
workflow = []

device_start_queue = queue.Queue()
device_reset_queue = queue.Queue()
on_stage_queue = queue.Queue()

def log_exception_to_json(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # ✅ สร้าง error_info ที่มี defensive handling
            error_info = {
                'error_message': str(e),
                'function': func.__name__,
                'traceback': traceback.format_exc(),
                'error_type': type(e).__name__,
                'timestamp': datetime.datetime.now().isoformat()
            }

            # ✅ Log to logging system
            logger.error(f"Exception in {func.__name__}: {e}", exc_info=True)

            # ✅ ตรวจสอบและสร้างโฟลเดอร์
            log_dir = os.path.dirname(LOG_FILE)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            # ✅ ปรับปรุงการเขียนไฟล์ JSON
            try:
                # อ่านข้อมูลเดิม (ถ้ามี)
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
                    with open(LOG_FILE, 'r', encoding='utf-8') as f:
                        try:
                            data = json.load(f)
                            if not isinstance(data, list):
                                data = []
                        except (json.JSONDecodeError, ValueError):
                            data = []
                else:
                    data = []
                
                # เพิ่มข้อมูลใหม่
                data.append(error_info)
                
                # เขียนข้อมูลทั้งหมดลงไฟล์
                with open(LOG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.flush()  # บังคับให้เขียนลง storage
                    
                #print(f"✅ Error logged successfully to {LOG_FILE}")
                
            except Exception as log_error:
                # ถ้า log ไม่ได้ ให้เขียนลงไฟล์ backup
                backup_file = f"{LOG_FILE}.backup"
                try:
                    with open(backup_file, 'a', encoding='utf-8') as f:
                        f.write(f"\n--- ERROR LOG {datetime.datetime.now().isoformat()} ---\n")
                        f.write(json.dumps(error_info, indent=2, ensure_ascii=False))
                        f.write("\n" + "="*50 + "\n")
                    logger.warning(f"[WARNING] Logged to backup file: {backup_file}")
                except Exception as backup_error:
                    logger.critical(f"Failed to log error: {log_error}, Backup also failed: {backup_error}")
                    print('[FAIL] Critical: Cannot log error')

            raise  # ส่งต่อ exception ต่อไป
    return wrapper

# --------------------------------
# 2) Helper สำหรับรัน ADB commands
# --------------------------------
@log_exception_to_json
def adb_run(cmd_list, timeout=20, **kwargs):
    '''
    รัน subprocess.run([...]) พร้อมตั้ง creationflags ไม่ให้โผล่หน้าต่างใหม่
    ✅ Auto-detect screencap commands and increase timeout to 30s (vs 20s default)
    '''
    if os.name == 'nt':
        kwargs.setdefault('creationflags', CREATE_NO_WINDOW)
    
    # Increase timeout for screencap commands (which can be slow on some devices)
    if 'screencap' in str(cmd_list):
        timeout = max(timeout, 60)  # Use 60s minimum for screencap - increased due to timeouts
    
    return subprocess.run(cmd_list, timeout=timeout, **kwargs)

def adb_check_output(cmd_list, timeout=20, **kwargs):
    '''
    รัน subprocess.check_output([...]) พร้อมตั้ง flags
    ✅ Auto-detect screencap commands and increase timeout to 60s (vs 20s default)
    '''
    if os.name == 'nt':
        kwargs.setdefault('creationflags', CREATE_NO_WINDOW)
    
    # Increase timeout for screencap commands (which can be slow on some devices)
    if 'screencap' in str(cmd_list):
        timeout = max(timeout, 60)  # Use 60s minimum for screencap - increased due to timeouts
    
    try:
        return subprocess.check_output(cmd_list, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as e:
        logger.error(f'ADB check_output timeout ({timeout}s): {" ".join(cmd_list)}')
        print(f'ADB check_output timeout ({timeout}s): {" ".join(cmd_list)}')
        return b''  # คืน empty bytes ให้โค้ดไม่ crash

# ================================
# Safe Queue Helper with Timeout
# ================================
def safe_queue_put(q: Queue, data: Any, timeout: float = 5.0, device_serial: str = ''):
    """
    Put data into queue with timeout to prevent deadlock
    
    Args:
        q: Queue object
        data: Data to put
        timeout: Timeout in seconds (default 5.0)
        device_serial: Device serial for logging
    """
    try:
        q.put(data, timeout=timeout)
    except queue.Full:
        logger.warning(f"Queue full for {device_serial}, data dropped: {data}")
    except Exception as e:
        logger.error(f"Error putting data in queue for {device_serial}: {e}", exc_info=True)

# ================================
# Resource Cleanup Helpers
# ================================
def safe_cleanup_image(image_array):
    """Safely cleanup numpy image array to free memory"""
    try:
        if image_array is not None:
            del image_array
            return True
    except Exception as e:
        logger.debug(f"Error cleaning up image: {e}")
    return False

def safe_cleanup_screenshots(folder_path: str, keep_count: int = 3):
    """
    Delete old screenshots to prevent disk/memory bloat
    
    Args:
        folder_path: Path to screenshot folder
        keep_count: Number of recent screenshots to keep
    """
    try:
        if not os.path.exists(folder_path):
            return
        
        files = []
        for f in os.listdir(folder_path):
            fpath = os.path.join(folder_path, f)
            if os.path.isfile(fpath):
                files.append((fpath, os.path.getmtime(fpath)))
        
        # Sort by modification time and delete old ones
        if len(files) > keep_count:
            files.sort(key=lambda x: x[1], reverse=True)
            for fpath, _ in files[keep_count:]:
                try:
                    os.remove(fpath)
                    logger.debug(f"Cleaned up screenshot: {fpath}")
                except Exception as e:
                    logger.warning(f"Failed to clean screenshot {fpath}: {e}")
    except Exception as e:
        logger.error(f"Error during screenshot cleanup: {e}")

def is_valid_png(filepath: str) -> bool:
    """Check if a PNG file is valid and not corrupted"""
    try:
        if not os.path.exists(filepath):
            logger.debug(f"PNG file does not exist: {filepath}")
            return False
        
        # Check file size (PNG files should be at least 67 bytes for a minimal PNG)
        file_size = os.path.getsize(filepath)
        if file_size < 67:
            logger.warning(f"PNG file too small ({file_size} bytes): {filepath}")
            return False
        
        # Try to read the PNG signature
        with open(filepath, 'rb') as f:
            header = f.read(8)
            # PNG signature: 137 80 78 71 13 10 26 10
            if header != b'\x89PNG\r\n\x1a\n':
                logger.warning(f"Invalid PNG signature: {filepath}")
                return False
        
        # Try to decode with cv2 to catch libpng errors
        try:
            img = cv2.imread(filepath, cv2.IMREAD_UNCHANGED)
            if img is None:
                logger.warning(f"cv2.imread returned None for: {filepath}")
                return False
        except Exception as cv_err:
            logger.warning(f"cv2.imread error for {filepath}: {cv_err}")
            return False
        
        return True
    except Exception as e:
        logger.warning(f"PNG validation error for {filepath}: {e}")
        return False

def safe_extract_text_with_retry(serial, ui_queue, max_retries=3, **kwargs):
    """Safely extract text with automatic screenshot retry on failure"""
    for attempt in range(max_retries):
        screen_path = capture_screen(serial)
        if screen_path is None:
            logger.warning(f'{serial}: capture_screen returned None (attempt {attempt+1}/{max_retries})')
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
            return {'best_text': '', 'original': '', 'error': 'Failed to capture screen'}
        
        try:
            result = extract_text_tesseract(
                serial=serial,
                ui_queue=ui_queue,
                image_path=screen_path,
                **kwargs
            )
            
            if 'error' not in result or result.get('error') != 'Failed to load screenshot':
                return result
            
            logger.warning(f'{serial}: OCR failed due to corrupted screenshot (attempt {attempt+1}/{max_retries})')
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
        except Exception as e:
            logger.error(f'{serial}: Error in safe_extract_text_with_retry: {e}')
            if attempt < max_retries - 1:
                time.sleep(0.5)
                continue
    
    return {'best_text': '', 'original': '', 'error': 'Failed after max retries'}

    
@log_exception_to_json
def overlay_on_bg(img_path: str, position: tuple[int, int]) -> np.ndarray:
    # โหลด bg
    bg_path = os.path.join(STAGE_IMG_BASE, 'stage_initial', 'gacha_background.png')
    bg = cv2.imread(bg_path, cv2.IMREAD_COLOR)
    if bg is None:
        raise FileNotFoundError(f'Background not found: {bg_path}')

    # โหลด fg พร้อม alpha
    fg = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
    if fg is None:
        raise FileNotFoundError(f'Foreground not found: {img_path}')
    
    if fg.shape[2] != 4:
        raise ValueError('Image does not have an alpha channel')

    # แยก BGR กับ alpha
    b, g, r, a = cv2.split(fg)
    alpha = a.astype(float) / 255.0
    fg_rgb = cv2.merge([b, g, r]).astype(float)

    # ขนาด fg
    h, w = fg.shape[:2]
    x_center, y2 = position

    # คำนวณมุมบนซ้าย (x1, y1) จาก bottom-center
    x1 = int(x_center - w / 2)
    y1 = y2 - h

    # ตรวจ boundary & clamp
    y1_clamped, y2_clamped = max(0, y1), min(bg.shape[0], y2)
    x1_clamped, x2_clamped = max(0, x1), min(bg.shape[1], x1 + w)

    # ตัด fg ให้พอดีกับ ROI
    crop_y0 = y1_clamped - y1
    crop_x0 = x1_clamped - x1
    fh = y2_clamped - y1_clamped
    fw = x2_clamped - x1_clamped

    if fh <= 0 or fw <= 0:
        raise ValueError('Position is completely outside the background')

    fg_crop = fg_rgb[crop_y0:crop_y0 + fh, crop_x0:crop_x0 + fw]
    alpha_crop = alpha[crop_y0:crop_y0 + fh, crop_x0:crop_x0 + fw][..., None]

    # Composite
    bg_roi = bg[y1_clamped:y2_clamped, x1_clamped:x2_clamped].astype(float)
    comp = fg_crop * alpha_crop + bg_roi * (1 - alpha_crop)
    bg[y1_clamped:y2_clamped, x1_clamped:x2_clamped] = comp.astype(np.uint8)

    # ดึงแค่ชื่อไฟล์
    # filename = os.path.basename(img_path)  # -> 'Trainee Sally.png'
    # name, ext = os.path.splitext(filename)  # -> ('Trainee Sally', '.png')
    # new_name = f'Overlay_{name.replace(" ", "_")}{ext}'  # -> 'Overlay_Trainee_Sally.png'

    # save_path = os.path.join(RANGERS_FOLDER, new_name)
    # cv2.imwrite(save_path, bg)

    return bg

# === FEATURE-BASED MATCHER ===
class FeatureMatcher:
    def __init__(
        self,
        method: str = 'ORB',
        min_matches: int = 8,
        conf_thresh: float = 0.6,
        ratio_thresh: float = 0.75,
        homography_thresh: float = 5.0,
        cross_check: bool = False
    ):
        if method.upper() not in ['SIFT', 'ORB']:
            raise ValueError(f'ไม่รองรับ method นี้: {method}')
        
        # Initialize detector and matcher
        method = method.upper()
        if method == 'SIFT':
            self.detector = cv2.SIFT_create()
            norm = cv2.NORM_L2
        else:
            self.detector = cv2.ORB_create(nfeatures=500)
            norm = cv2.NORM_HAMMING
        self.matcher = cv2.BFMatcher(norm, crossCheck=cross_check)

        # Matching thresholds
        self.min_matches = min_matches
        self.conf_thresh = conf_thresh
        self.ratio_thresh = ratio_thresh
        self.homography_thresh = homography_thresh

        # Cache: path -> (keypoints, descriptors, (w, h))
        self.templates: Dict[str, Tuple[List[cv2.KeyPoint], np.ndarray, Tuple[int,int]]] = {}

    @log_exception_to_json
    def clear_template_cache(self):
        '''ล้าง template cache เพื่อคืนหน่วยความจำ'''
        self.templates.clear()

    @log_exception_to_json
    def _load_template(self, path: str, is_overlay_image:bool = False) -> None:
        if is_overlay_image:
            img = overlay_on_bg(path, (89, 205))
        else:
            img = cv2.imread(path)
            if img is None:
                raise FileNotFoundError(f'Template not found: {path}')

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        kp, des = self.detector.detectAndCompute(gray, None)
        if des is None:
            des = np.empty((0, 0), dtype=np.float32)
        h, w = gray.shape[:2]
        self.templates[path] = (kp, des, (w, h))

    @log_exception_to_json
    def match(
        self,
        screen_bgr: Any,
        template_path: str,
        mode: str = 'single',
        conf_thresh: Optional[float] = None,
        ratio_thresh: Optional[float] = None,
        min_matches: Optional[int] = None,
        homography_thresh: Optional[float] = None,
        left_top: Optional[Tuple[int,int]] = None, 
        right_bottom: Optional[Tuple[int,int]] = None,  
        is_overlay_image:bool = False
    ) -> Dict[str, Tuple[bool, Optional[int], Optional[int], float]]:
        '''
        Match templates against screen.
        Returns dict: name -> (matched, center_x, center_y, confidence)
        '''
        # Choose thresholds
        conf_t = conf_thresh or self.conf_thresh
        ratio_t = ratio_thresh or self.ratio_thresh
        min_m = min_matches or self.min_matches
        homo_t = homography_thresh or self.homography_thresh
        
        # Prepare screen
        if screen_bgr is None:
            raise ValueError('screen_bgr is None')
        gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)

        if left_top and right_bottom:
            gray = gray[left_top[1]:right_bottom[1], left_top[0]:right_bottom[0]]
            # ต้องปรับพิกัดกลับไปยังภาพต้นฉบับ
            offset_x, offset_y = left_top[0], left_top[1]
        else:
            offset_x, offset_y = 0, 0
        
        kp2, des2 = self.detector.detectAndCompute(gray, None)
        if des2 is None or des2.shape[0] < 2:
            return {}

        results = {}
        paths = [template_path]
        if mode == 'multiple':
            if not os.path.isdir(template_path):
                raise FileNotFoundError(f'Template directory not found: {template_path}')
            paths = [os.path.join(template_path, f) for f in os.listdir(template_path)]

        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            try:
                if path not in self.templates:
                    self._load_template(path, is_overlay_image)
                kp1, des1, (w,h) = self.templates[path]
                # Validate descriptors
                if des1.shape[0] < 2:
                    results[name] = (False, None, None, 0.0)
                    continue

                # KNN match
                knn = self.matcher.knnMatch(des1, des2, k=2)
                good = [m for m,n in knn if m.distance < ratio_t * n.distance]
                if len(good) < min_m:
                    results[name] = (False, None, None, len(good)/min_m)
                    continue

                # Homography + confidence
                src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1,1,2)
                dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1,1,2)
                M, mask = cv2.findHomography(src, dst, cv2.RANSAC, homo_t)
                if M is None:
                    results[name] = (False, None, None, 0.0)
                    continue

                inliers = int(mask.sum())
                conf = inliers / len(mask)
                if conf < conf_t:
                    results[name] = (False, None, None, conf)
                    continue

                # Compute center
                pts = np.float32([[0,0],[w,0],[w,h],[0,h]]).reshape(-1,1,2)
                dst_pts = cv2.perspectiveTransform(pts, M)
                cx = int(dst_pts[:,0,0].mean()) + offset_x
                cy = int(dst_pts[:,0,1].mean()) + offset_y
                results[name] = (True, cx, cy, conf)
            except Exception as e:
                print(f'เกิดข้อผิดพลาดในการ match {name}: {e}')
                print(f'Traceback: {traceback.format_exc()}')
                results[name] = (False, None, None, 0.0)
        return results

matcher = FeatureMatcher(method='ORB', min_matches=8, conf_thresh=0.6)

class OnStageManager:
    """
    จัดการ on_stage ข้อมูลแบบ thread-safe และ process-safe
    """
    def __init__(self, shared_data=None, shared_lock=None):
        if shared_data is not None and shared_lock is not None:
            # ใช้ shared objects ที่ส่งมา
            self.manager = None  # ไม่ต้องสร้าง Manager ใหม่
            self.on_stage_data = shared_data
            self.lock = shared_lock
        else:
            # สร้าง Manager ใหม่ (ใช้ใน main process)
            self.manager = Manager()
            self.on_stage_data = self.manager.list()
            self.lock = self.manager.Lock()

    def get_shared_objects(self):
        """ส่งคืน shared objects สำหรับส่งไป child process"""
        return self.on_stage_data, self.lock
    
    def get_on_stage(self) -> List[Dict[str, str]]:
        """Get ข้อมูล on_stage ทั้งหมด"""
        
        return list(self.on_stage_data)  # Return copy
    
    def set_on_stage(self, new_on_stage: List[Dict[str, str]]):
        """Set ข้อมูล on_stage ใหม่ทั้งหมด"""
   
        self.on_stage_data[:] = new_on_stage
    
    def clear_on_stage(self):
        """ล้างข้อมูล on_stage ทั้งหมด"""
        self.on_stage_data[:] = []
    
    def add_on_stage(self, device_serial: str, filename: str):
        """เพิ่ม device และ filename เข้า on_stage"""
        new_entry = {device_serial: filename}

        # ตรวจสอบว่า device นี้มีอยู่แล้วหรือไม่
        for i, entry in enumerate(self.on_stage_data):
            if device_serial in entry:
                # Update existing entry
                self.on_stage_data[i] = new_entry
                return
        # Add new entry
        self.on_stage_data.append(new_entry)
    
    def remove_on_stage(self, device_serial: str) -> bool:
        """ลบ device ออกจาก on_stage"""
    
        for i, entry in enumerate(self.on_stage_data):
            if device_serial in entry:
                del self.on_stage_data[i]
                return True
        return False
    
    def get_device_file(self, device_serial: str) -> Optional[str]:
        """Get filename ของ device เฉพาะ"""
        for entry in self.on_stage_data:
            if device_serial in entry:
                return entry[device_serial]
        return None
    
    def update_device_file(self, device_serial: str, filename: str) -> bool:
        """Update filename ของ device เฉพาะ"""
        for i, entry in enumerate(self.on_stage_data):
            if device_serial in entry:
                self.on_stage_data[i] = {device_serial: filename}
                return True
        return False
    
    def get_all_devices(self) -> List[str]:
        """Get รายชื่อ device ทั้งหมดใน on_stage"""
        devices = []
        for entry in self.on_stage_data:
            devices.extend(entry.keys())
        return devices
    
    def get_device_count(self) -> int:
        """Get จำนวน device ใน on_stage"""
        return len(self.on_stage_data)
    
    def is_device_on_stage(self, device_serial: str) -> bool:
        """ตรวจสอบว่า device อยู่ใน on_stage หรือไม่"""
        return self.get_device_file(device_serial) is not None
    
    def is_filename_on_stage(self, filename: str) -> bool:
        """ตรวจสอบว่า filename อยู่ใน on_stage หรือไม่"""

        for entry in self.on_stage_data:
            for device_serial, file in entry.items():
                if file == filename:
                    return True
        return False
    
    def get_device_by_filename(self, filename: str) -> Optional[str]:
        """ค้นหา device serial ที่ใช้ filename นี้"""

        for entry in self.on_stage_data:
            for device_serial, file in entry.items():
                if file == filename:
                    return device_serial
        return None
    
    def get_all_filenames(self) -> List[str]:
        """Get รายชื่อ filename ทั้งหมดใน on_stage"""
        filenames = []
        for entry in self.on_stage_data:
            filenames.extend(entry.values())
        return filenames

@log_exception_to_json  
def safe_load_json_with_lock(filepath: str, default_value: Any = None, use_cache: bool = True):
    '''โหลด JSON อย่างปลอดภัยพร้อม thread safety และ caching'''
    
    # ตรวจสอบ cache ก่อน
    if use_cache and filepath in _config_cache:
        file_mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
        cache_time = _cache_timestamps.get(filepath, 0)
        
        # ถ้าไฟล์ไม่ได้ถูกแก้ไข ใช้ cache
        if file_mtime <= cache_time:
            return _config_cache[filepath]
    
    with _config_lock:  # ใช้ lock ป้องกัน race condition
        # Double-check ใน lock เผื่อมี thread อื่นโหลดไปแล้ว
        if use_cache and filepath in _config_cache:
            file_mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
            cache_time = _cache_timestamps.get(filepath, 0)
            if file_mtime <= cache_time:
                return _config_cache[filepath]
        
        if not os.path.exists(filepath):
            print(f'ไม่พบไฟล์ config: {filepath}')
            result = default_value or {}
            if use_cache:
                _config_cache[filepath] = result
                _cache_timestamps[filepath] = time.time()
            return result
        
        max_retries = 3
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    result = json.load(f)
                
                # เก็บใน cache
                if use_cache:
                    _config_cache[filepath] = result
                    _cache_timestamps[filepath] = os.path.getmtime(filepath)
                
                return result
                
            except (PermissionError, IOError) as e:
                if attempt < max_retries - 1:
                    print(f'ความพยายามที่ {attempt + 1} ล้มเหลว กำลังลองใหม่... ({e})')
                    time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                    continue
                else:
                    print(f'ไม่สามารถเข้าถึงไฟล์ {filepath} หลังจากพยายาม {max_retries} ครั้ง: {e}')
                    result = default_value or {}
                    if use_cache:
                        _config_cache[filepath] = result
                        _cache_timestamps[filepath] = time.time()
                    return result
                    
            except json.JSONDecodeError as e:
                print(f'JSON ไม่ถูกต้องใน {filepath}: {e}')
                result = default_value or {}
                if use_cache:
                    _config_cache[filepath] = result
                    _cache_timestamps[filepath] = time.time()
                return result
                
            except Exception as e:
                print(f'เกิดข้อผิดพลาดในการโหลด {filepath}: {e}')
                result = default_value or {}
                if use_cache:
                    _config_cache[filepath] = result
                    _cache_timestamps[filepath] = time.time()
                return result

# Load or initialize config and steps
@log_exception_to_json
def load_main_config():
    global main_configs
        
    # Default configuration
    default_config = {
        "gacha_slot": 1,
        "select_gacha_slot": 1,
        "free_gacha_slot": 1,
        "count_gacha":1,
        "backup_file_path": "C:/backup_bot/A-Temp Reroll",
        "stage_timeout": {
            "1": 500,
            "2": 500,
            "3": 500,
            "4": 500,
            "5": 500,
            "6": 500,
            "7": 500,
            "8": 500,
            "9": 500,
            "10": 580,
            "default": 240
        },
        "port_list": [],
        "selected_mode": "ดอง",
        "is_random": False

    }
    main_configs = safe_load_json_with_lock(MAIN_CONFIG_FILE, default_config)

    # ตั้งค่า default ถ้าไม่มี
    main_configs.setdefault('port_list', [])
    
    # Detect old port format (127.0.0.1:port) and reset to empty
    if 'port_list' in main_configs and main_configs['port_list']:
        old_format = any(isinstance(p, int) and p > 255 for p in main_configs['port_list'])
        if old_format:
            print("[INFO] Detected old port format in config. Resetting port_list to empty. Please configure new 10.0.0.X:5555 addresses using numbers 1-510.")
            main_configs['port_list'] = []

    
@log_exception_to_json
def load_devices_config():
    global devices_configs
      
    devices_configs = safe_load_json_with_lock(DEVICES_CONFIG_FILE)

@log_exception_to_json
def load_workflow_config():
    global workflow
      
    workflow = safe_load_json_with_lock(STEPS_FILE, [])

load_main_config()
load_devices_config()
load_workflow_config()

# Save config to file
@log_exception_to_json
def save_devices_config():
    try:
        global devices_configs
        with _config_lock:
            with open(DEVICES_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(devices_configs, f, indent=4, ensure_ascii=False)
        #print(f'บันทึก device config สำเร็จ: {DEVICES_CONFIG_FILE}')
        load_devices_config()
    except Exception as e:
        print(f'เกิดข้อผิดพลาดในการบันทึก device config: {e}')
        # อาจต้องแจ้งเตือน user ผ่าน UI

@log_exception_to_json
def save_main_config():
    try:
        global main_configs
        with _config_lock:
            with open(MAIN_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(main_configs, f, indent=4, ensure_ascii=False)
        #print(f'บันทึก config สำเร็จ: {MAIN_CONFIG_FILE}')
    except Exception as e:
        print(f'เกิดข้อผิดพลาดในการบันทึก config: {e}')
        # ควรมีการ handle error ที่ดีกว่า เช่น แสดง dialog box
        return False
    return True

@log_exception_to_json
def get_step_object(stage_no):
    for step in workflow:
        if step.get('stage') == stage_no:
            return step
    return None

# Update stage by number and store full step object
@log_exception_to_json
def update_stage(serial, stage_no):
    global devices_configs, stage_timeout_at

    load_devices_config()
    if not serial:
        print('เกิดข้อผิดพลาด: serial ไม่ถูกต้อง')
        return False

    # 💥 FIX: ถ้า configs[serial] ไม่ใช่ dict ก็ให้ override
    if not isinstance(devices_configs.get(serial), dict):
        devices_configs[serial] = {}

    # เซต stage number เป็นเลขธรรมชาติ
    devices_configs[serial]['stage'] = stage_no

    # บันทึกเวลาเริ่ม stage
    start_time = time.time()
    stage_start_times[serial] = start_time
    
    # คำนวณเวลา timeout ครั้งเดียว (ไม่ต้องคำนวณซ้ำในลูป)
    if main_configs.get('selected_mode') == 'ดอง':
        limit = 600  # 10 นาที สำหรับ dong mode
    elif main_configs.get('selected_mode') == 'ฟาร์ม':
        limit = 7200  # 2 ชั่วโมง สำหรับ farm mode
    else:
        timeouts = main_configs.get('stage_timeout', {})
        # Increase default timeout significantly to prevent premature restart
        limit = timeouts.get(str(stage_no), timeouts.get(stage_no, timeouts.get('default', 300)))  # 5 min default instead of 60s
    
    stage_timeout_at[serial] = start_time + limit
    logger.info(f"Stage {stage_no} started for {serial} - Timeout: {limit}s (~{limit//60}m), will finish at {datetime.datetime.fromtimestamp(stage_timeout_at[serial])}")

    try:
        save_devices_config()
        load_devices_config()
        update_status_label(serial)
        return True
    except Exception as e:
        print(f'เกิดข้อผิดพลาดในการอัปเดต stage สำหรับ {serial}: {e}')
        return False

@log_exception_to_json
def get_current_stage(serial):
    """Get the current stage from device config"""
    global devices_configs
    load_devices_config()
    
    if serial not in devices_configs:
        return 1
    
    device_config = devices_configs[serial]
    if isinstance(device_config, dict) and 'stage' in device_config:
        stage = device_config['stage']
        # stage สามารถเป็น int หรือ dict (เพื่อความเข้ากันได้)
        if isinstance(stage, int):
            return stage
        elif isinstance(stage, dict) and 'stage' in stage:
            return stage['stage']
    
    return 1


def normalize_adb_tcpip_address(value):
    """Normalize user-configured ADB TCP/IP addresses to full 10.0.0.X:5555 or 10.0.1.X:5555 form.
    
    Input format:
    - 1-255: 10.0.0.{num}:5555
    - 256-510: 10.0.1.{num-255}:5555
    - Full IP address: 10.0.0.X:5555 or 10.0.1.X:5555
    """
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None

    if ':' in token:
        parts = token.split(':')
        if len(parts) == 2 and parts[1] == '5555':
            return token
        return None

    if token.count('.') == 3:
        return f'{token}:5555'

    if token.isdigit():
        num = int(token)
        if 1 <= num <= 255:
            # 1-255 -> 10.0.0.1 to 10.0.0.255
            return f'10.0.0.{num}:5555'
        elif 256 <= num <= 510:
            # 256-510 -> 10.0.1.1 to 10.0.1.255
            octet = num - 255
            return f'10.0.1.{octet}:5555'
        elif num >= 16384:  # old MuMu port format
            # Map old ports starting from 16416 -> 34, increment by 32
            base_old = 16416
            base_num = 34
            step = 32
            if (num - base_old) % step == 0:
                mapped_num = base_num + (num - base_old) // step
                if 1 <= mapped_num <= 255:
                    return f'10.0.0.{mapped_num}:5555'

    return None


def find_adb_tcpip_ports() -> list[str]:
    '''
    หา ADB device ที่เชื่อมต่อผ่าน TCP/IP port :5555
    คืนค่าเป็น list ของ IP:port เช่น ['10.0.0.10:5555', '10.0.1.10:5555']
    '''
    try:
        result = adb_run(
            ['adb', 'devices'],
            capture_output=True,
            text=True,
            timeout=10
        )
    except FileNotFoundError:
        print('[ERROR] ไม่พบ adb กรุณาติดตั้งหรือเพิ่ม PATH ก่อน')
        return []
    except subprocess.TimeoutExpired:
        print('[ERROR] adb devices timeout')
        return []

    ports = []
    for line in result.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[1] == 'device' and cols[0].endswith(':5555'):
            ports.append(cols[0])
    return ports


# Get preconnected ports
@log_exception_to_json
def get_preconnected_ports():
    try:
        ports = find_adb_tcpip_ports()
        ports_to_connect = main_configs.get('port_list', [])
        if ports_to_connect:
            normalized_filters = [normalize_adb_tcpip_address(p) for p in ports_to_connect]
            normalized_filters = [p for p in normalized_filters if p]
            ports = [p for p in ports if p in normalized_filters]
        return ports
    except Exception as e:
        print(f'เกิดข้อผิดพลาดในการดึง preconnected ports: {e}')
        return []

# Refresh ports label
@log_exception_to_json
def refresh_connected_ports_label():
    global connected_ports
    connected_ports = get_preconnected_ports()
    if connected_ports:
        # Extract only the last two octets of each IP (e.g., 10.0.0.34:5555 -> 0_34, 10.0.1.34:5555 -> 1_34)
        short_ports = []
        for port_addr in connected_ports:
            if ':' in port_addr:
                ip_part = port_addr.split(':')[0]  # 10.0.0.34 or 10.0.1.34
                octets = ip_part.split('.')
                if len(octets) >= 4:
                    y = octets[2]  # 3rd octet (0 or 1)
                    x = octets[3]  # 4th octet (1-255)
                    short_ports.append(f'{y}_{x}')
            else:
                short_ports.append(port_addr)
        
        render_ports.configure(text='Connected: ' +
                               ' | '.join(short_ports), text_color='white')
    else:
        render_ports.configure(text='No ports connected', text_color='red')

# Connect ports
@log_exception_to_json
def auto_connect_mumu():
    '''เชื่อมต่อกับ MuMu emulator ports อัตโนมัติ'''
    try:
        status_label.configure(text='กำลังตรวจสอบ emulator ports...', text_color='white')
        ports_to_connect = main_configs.get('port_list', [])
        
        if not ports_to_connect:
            status_label.configure(text='ไม่มี ports ที่ต้องการเชื่อมต่อ', text_color='orange')
            return

        # สร้าง mapping เพื่อเก็บลำดับต้นฉบับ
        port_order_map = {}
        normalized_ports_ordered = []
        
        for idx, p in enumerate(ports_to_connect):
            normalized = normalize_adb_tcpip_address(p)
            if normalized:
                port_order_map[normalized] = idx  # เก็บลำดับต้นฉบับ
                normalized_ports_ordered.append(normalized)

        if not normalized_ports_ordered:
            status_label.configure(text='ไม่มี ports ที่ถูกต้องให้เชื่อมต่อ', text_color='orange')
            return

        status_label.configure(text='กำลังเชื่อมต่อกับ emulators...', text_color='white')
        connected_count = 0
        failed_addresses = []

        # เรียงตามลำดับต้นฉบับจาก port_list
        for address in normalized_ports_ordered:
            result = adb_run(
                ['adb', 'connect', address],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0:
                connected_count += 1
                logger.info(f"Connected to {address} (order: {port_order_map[address]})")
            else:
                failed_addresses.append(address)
                logger.warning(f"Failed to connect to {address}")

        if connected_count > 0:
            status_label.configure(
                text=f'เชื่อมต่อสำเร็จ {connected_count}/{len(normalized_ports_ordered)} devices',
                text_color='green'
            )
        else:
            status_label.configure(
                text='ไม่มี devices ที่เชื่อมต่อได้',
                text_color='orange'
            )

        if failed_addresses:
            print('Failed to connect:', ', '.join(failed_addresses))

    except Exception as e:
        print(f'เกิดข้อผิดพลาดใน auto_connect_mumu: {e}')
        status_label.configure(text='เชื่อมต่อล้มเหลว', text_color='red')
    finally:
        refresh_connected_ports_label()

@log_exception_to_json
def auto_connect_mumu_async():
    threading.Thread(target=auto_connect_mumu, daemon=True).start()

# todo
@log_exception_to_json
def process_device_start_queue():
    if device_start_queue.empty():
        return
    serial = device_start_queue.get()
    if serial in device_labels and device_labels[serial].winfo_exists():
        start_device_async(serial)
    if app and app.winfo_exists():
        selected_mode = main_configs.get('selected_mode', '')
        delayDevice = 0 if selected_mode == 'รีปกติ' else 2000  # ดอง delay น้อยกว่า ฟาร์ม
        app.after(delayDevice, process_device_start_queue)

@log_exception_to_json
def process_device_reset_queue():
    if device_reset_queue.empty():
        return
    serial = device_reset_queue.get()
    if serial in device_labels and device_labels[serial].winfo_exists():
        reset_device_async(serial)
    if app and app.winfo_exists():
        selected_mode = main_configs.get('selected_mode', '')
        delayDevice = 0 if selected_mode == 'รีปกติ' else 2000  # ดอง delay น้อยกว่า ฟาร์ม
        app.after(delayDevice, process_device_reset_queue)

@log_exception_to_json
def remove_from_on_stage_by_filename(filename, local_manager):
    """ลบ item จาก on_stage โดยใช้ชื่อไฟล์"""
    device = local_manager.get_device_by_filename(filename)
    if device:
        local_manager.remove_on_stage(device)

@log_exception_to_json
def remove_from_on_stage_by_serial(serial):
    global on_stage_manager
    """ลบ item จาก on_stage โดยใช้ serial"""
    on_stage_manager.remove_on_stage(serial)

# Connect devices and setup UI
@log_exception_to_json
def connect_devices(re_reroll_file_path):
    global main_configs, on_stage_manager

    try:    
        status_label.configure(text='กำลังเชื่อมต่อกับ devices...', text_color='white')

        # ล้างอุปกรณ์เก่า
        for widget in devices_frame.winfo_children():
            widget.destroy()

        # ล้าง global dictionaries
        device_queues.clear()
        device_procs.clear()
        stage_labels.clear()
        sub_stage_labels.clear()
        confidence_labels.clear()
        device_labels.clear()
        file_size_labels.clear()

        global devices, matcher

        client = AdbClient(host='127.0.0.1', port=5037)

        @log_exception_to_json
        def is_device_online(d):
            try:
                serial = d.get_serial_no()
                return serial.endswith(':5555') and d.get_state() == 'device'
            except Exception as e:
                print(f'ไม่สามารถตรวจสอบสถานะ device {d.get_serial_no()}: {e}')
                return False

        all_devices = client.devices()
        devices_online = [d for d in all_devices if is_device_online(d)]
        
        port_list = main_configs.get('port_list', [])
        if port_list:
            # สร้าง mapping เพื่อรักษาลำดับต้นฉบับจาก port_list
            port_order_map = {}
            normalized_filters = []
            
            for idx, p in enumerate(port_list):
                normalized = normalize_adb_tcpip_address(p)
                if normalized:
                    port_order_map[normalized] = idx
                    normalized_filters.append(normalized)
            
            # จัดเรียง devices ตามลำดับจาก port_list ต้นฉบับ
            devices_dict = {d.get_serial_no(): d for d in devices_online if d.get_serial_no() in normalized_filters}
            devices = [devices_dict[addr] for addr in normalized_filters if addr in devices_dict]
        else:
            devices = devices_online

        if not devices:
            status_label.configure(text='Error: ไม่พบ device ที่เชื่อมต่อ!', text_color='red')
            return

        # สร้าง UI เรียงตามลำดับ devices ที่จัดเรียงแล้ว
        for idx, device in enumerate(devices):
            serial = device.get_serial_no()
            logger.info(f"Setting up device {idx}: {serial}")
            create_device_ui(device, idx, serial)
            device_reset_queue.put(serial)  # เพิ่มเข้า queue

        matcher = FeatureMatcher(method='ORB', min_matches=8, conf_thresh=0.6)
        on_stage_manager = OnStageManager()

        load_devices_config()
        refresh_connected_ports_label()
        save_devices_config()
        
        status_label.configure(
            text=f'เชื่อมต่อสำเร็จ {len(devices)} devices (เรียงตามลำดับ port_list)', 
            text_color='green'
        )

        process_device_reset_queue()  # เริ่ม process คิว
    except Exception as e:
        print(f'เกิดข้อผิดพลาดใน connect_devices: {e}')
        status_label.configure(text='เชื่อมต่อ devices ล้มเหลว', text_color='red')

@log_exception_to_json
def create_device_ui(device, idx, serial):
    global devices_configs

    '''สร้าง UI สำหรับ device แต่ละตัว'''
    devices_configs.setdefault(serial, {})

    # create a queue and store it
    q = Queue()
    device_queues[serial] = q
    device_procs[serial] = None

    frame = ctk.CTkFrame(devices_frame)
    row_idx = idx // 5
    col_idx = idx % 5
    frame.grid(row=row_idx, column=col_idx, padx=5, pady=5, sticky='nsew')
    devices_frame.grid_columnconfigure(col_idx, weight=1)

    # 1) ปุ่มบนสุด (horizontally)
    header_frame = ctk.CTkFrame(
        frame, fg_color='transparent', corner_radius=0)
    header_frame.grid(row=0, column=0, sticky='ew', pady=(5, 0))

    # สร้าง Label แบบอัปเดตข้อความได้ (countdown)
    device_label = ctk.CTkLabel(header_frame, text='', anchor='center')
    device_label.pack(padx=5)
    # เก็บไว้ใน dict เพื่ออัปเดตผ่าน poll_queues later
    device_labels[serial] = device_label

    lbl_file_size = ctk.CTkLabel(header_frame, text='File Size: N/A',
                                anchor='center', justify='center')
    lbl_file_size.pack(fill='x')
    file_size_labels[serial] = lbl_file_size

    btn_frame = ctk.CTkFrame(
        frame, fg_color='transparent', corner_radius=0)
    btn_frame.grid(row=1, column=0, sticky='ew', pady=(5, 0))
    ctk.CTkButton(btn_frame, text='Start', width=60,
                command=lambda s=serial: start_device_async(s)).grid(row=0, column=0, padx=5)
    ctk.CTkButton(btn_frame, text='Stop', width=60,
                command=lambda s=serial: stop_device_async(s)).grid(row=0, column=1, padx=5)
    ctk.CTkButton(btn_frame, text='Reset', width=60,
                command=lambda s=serial: reset_device_async(s)).grid(row=0, column=2, padx=5)
    btn_frame.grid_columnconfigure((0, 1, 2), weight=1)

    status_frame = ctk.CTkFrame(
        frame, fg_color='transparent', corner_radius=0)
    status_frame.grid(row=2, column=0, sticky='ew', pady=(5, 0))

    lbl_stage = ctk.CTkLabel(
        status_frame, text='Main Stage...', anchor='center')
    lbl_stage.pack(fill='x')
    stage_labels[serial] = lbl_stage

    lbl_sub = ctk.CTkLabel(status_frame, text='Sub Stage', anchor='center')
    lbl_sub.pack(fill='x', pady=(2, 0))
    sub_stage_labels[serial] = lbl_sub

    lbl_conf = ctk.CTkLabel(
        status_frame, text='Confidence: N/A', anchor='center')
    lbl_conf.pack(fill='x', pady=(2, 0))
    confidence_labels[serial] = lbl_conf

@log_exception_to_json
def connect_devices_async(re_reroll_file_path):
    global main_configs, on_stage_manager
    selected_mode = main_configs.get('selected_mode', '')

    if selected_mode == 'ดอง' or selected_mode == 'ฟาร์ม':
        load_main_config()
        on_stage_manager.clear_on_stage()  # ล้างข้อมูล on_stage
        # Runtime state will be initialized per-device during launch_main_loop
        
        if not re_reroll_file_path:
            print('Error: กำหนด path สำหรับดองก่อน')
            status_label.configure(text='กำหนด path สำหรับดองก่อน', text_color='red')
            return

    status_label.configure(text='เริ่มทำงาน', text_color='green')
    threading.Thread(target=lambda: connect_devices(re_reroll_file_path), daemon=True).start()

# Start device thread
@log_exception_to_json
def start_device(serial):
    global on_stage_manager
    # ดึง shared objects
    shared_data, shared_lock = on_stage_manager.get_shared_objects()

    # Spawn or restart process for device
    matcher.templates.clear()

    if serial not in device_queues:
        device_queues[serial] = Queue()

    q = device_queues[serial]
    if q is None:
        return

    p = device_procs.get(serial)
    if not p or not p.is_alive():
        p = Process(target=launch_main_loop, args=(serial, q, shared_data, shared_lock), daemon=True)
        device_procs[serial] = p
        logger.info(f"Starting process for device {serial} (PID: will be {p.name})")
        p.start()
        logger.info(f"Process started for device {serial}, PID: {p.pid}")
    else:
        logger.info(f"Process already running for device {serial}, PID: {p.pid}")

    update_status_label(serial)

@log_exception_to_json
def start_device_async(serial):
    if serial not in device_queues:
        device_queues[serial] = Queue()
    device_queues[serial].put(('remaining', serial, '⏳ Starting...'))

    @log_exception_to_json
    def worker():
        start_device(serial)
        if app and app.winfo_exists():
            app.after(0, lambda: device_queues[serial].put(('remaining', serial, '')))
    threading.Thread(target=worker, daemon=True).start()


# Stop device
@log_exception_to_json
def stop_device(serial):
    global stage_timeout_at
    load_devices_config()
    #print('Stop')
    p = device_procs.get(serial)
    if p and p.is_alive():
        logger.info(f"Terminating process for device {serial} (PID: {p.pid})")
        p.terminate()
        p.join(timeout=5)
        if p.is_alive():
            logger.warning(f"Process still alive after terminate, killing device {serial}")
            p.kill()
            p.join()
        exit_code = p.exitcode
        logger.info(f"Process for device {serial} exited with code: {exit_code}")
        if exit_code != 0 and exit_code is not None:
            logger.error(f"Process for device {serial} crashed with exit code: {exit_code}")
    device_procs[serial] = None

    if serial in stage_start_times:
        del stage_start_times[serial]
    if serial in stage_timeout_at:
        del stage_timeout_at[serial]

    update_status_label(serial)
    save_devices_config()


@log_exception_to_json
def stop_device_async(serial):
    if serial not in device_queues:
        device_queues[serial] = Queue()
    device_queues[serial].put(('remaining', serial, '⏳ Stopping...'))

    @log_exception_to_json
    def worker():
        stop_device(serial)
        if app and app.winfo_exists():
            app.after(0, lambda: device_queues[serial].put(('remaining', serial, '')))
    threading.Thread(target=worker, daemon=True).start()


# --- เพิ่มฟังก์ชัน reset_device ไว้ใต้ฟังก์ชัน stop_device ---
@log_exception_to_json
def reset_device(serial, on_stage=None):
    global main_configs, stage_timeout_at
    load_devices_config()

    # 1) หยุด process เดิม
    stop_device(serial)

    # 2) ลบ entry เดิม ถ้ามี
    if serial in stage_start_times:
        del stage_start_times[serial]
    if serial in stage_timeout_at:
        del stage_timeout_at[serial]

    # 3) เคลียร์ template cache ถ้ามี
    matcher.templates.clear()

    # todo: delete
    if main_configs.get('selected_mode', '') != 'ทดสอบ':
        close_pes(serial)

    time.sleep(1)

    if main_configs.get('selected_mode', '') == 'รีปกติ':
        # 4) ลบข้อมูลเกมครั้งเดียว
        try:
            # ดึงรายชื่อโฟลเดอร์ทั้งหมด - ใช้ run-as (ไม่ต้อง root)
            res = adb_run(
                ['adb', '-s', str(serial), 'shell', 'run-as', 'jp.konami.pesam', f'ls -1 {FOLDER_PATH}'],
                timeout=20, capture_output=True, text=True
            )
            
            if res.returncode != 0:
                # Fallback: ลองใช้ su -c
                res = adb_run(
                    ['adb', '-s', str(serial), 'shell', 'su', '-c', f'ls -1 {FOLDER_PATH}'],
                    timeout=20, capture_output=True, text=True
                )

            print(f'{serial}: Found folders: {res.stdout.strip()}')

            for name in res.stdout.split():
                if name not in ('cache', 'code_cache'):
                    # ลองใช้ run-as ก่อน
                    res_delete = adb_run(
                        ['adb', '-s', str(serial), 'shell', 'run-as', 'jp.konami.pesam',
                            f'rm -rf {FOLDER_PATH}/{name}'],
                        timeout=20, capture_output=True, text=True
                    )
                    # ถ้า run-as ล้มเหลว ลองใช้ su -c
                    if res_delete.returncode != 0:
                        adb_run(
                            ['adb', '-s', str(serial), 'shell', 'su', '-c',
                                f'rm -rf {FOLDER_PATH}/{name}']
                        )
            # ลบ cache/*
            adb_run(
                ['adb', '-s', str(serial), 'shell', 'run-as', 'jp.konami.pesam', f'rm -rf {FOLDER_PATH}/cache/*'],
                timeout=20, capture_output=True, text=True
            )
            #print(f'{serial}: Game data wiped.')
        except Exception as e:
            print(f'{serial}: Failed wiping game data: {e}')
    else:
        remove_from_on_stage_by_serial(serial)

    # 4) รีเซ็ต stage ใน config
    load_devices_config()
    devices_configs.setdefault(serial, {})['stage'] = get_step_object(1) or {
        'stage': 1,
        'key': 'unknown_stage_1',
        'label': 'Stage 1'
    }
    save_devices_config()
    load_devices_config()

    # 5) อัปเดต UI แล้วเริ่มสตาร์ทใหม่
    update_stage(serial, 1)
    start_device(serial)

    stage_start_times[serial] = time.time()

_resetting_devices = set()

@log_exception_to_json
def reset_device_async(serial):
    # ถ้ากำลังรีเซ็ตอยู่แล้ว ให้ข้าม
    if serial in _resetting_devices:
        return

    # 1) แสดง loading บน UI ของ device นั้น
    if serial not in device_queues:
        device_queues[serial] = Queue()
    device_queues[serial].put(('remaining', serial, '⏳ Resetting...'))

    # ทำเครื่องหมายว่าเริ่มรีเซ็ต
    _resetting_devices.add(serial)

    # 2) ทำงานจริงใน background
    @log_exception_to_json
    def worker():
        try:
            reset_device(serial)
        finally:
            # ลบสถานะรีเซ็ต
            _resetting_devices.discard(serial)
            # เคลียร์ข้อความ loading และอัปเดต stage label
            if app and app.winfo_exists():
                app.after(0, lambda: [device_queues[serial].put(('remaining', serial, ''))])
                app.after(0, lambda: stage_labels[serial].configure(text=f'Stage 1: {get_step_object(1)["stage"]}'))

    threading.Thread(target=worker, daemon=True).start()


# Update status label including mapped workflow step
@log_exception_to_json
def update_status_label(serial):
    global devices_configs

    try:
        lbl = stage_labels.get(serial)
        if not lbl or not lbl.winfo_exists():  # Add safety check
            return
        info = devices_configs.get(serial, {})
        step_obj = info.get('stage', {})
        if isinstance(step_obj, (str, int)):
            step_obj = {'stage': step_obj, 'label': 'Unknown'}
        text = f'Stage {step_obj.get("stage", "?")}'
        p = device_procs.get(serial)
        running = p.is_alive() if p else False
        lbl.configure(
            text=('Running' if running else 'Stopped') + f' ({text})',
            text_color='green' if running else 'red'
        )
    except Exception as e:
        logger.warning(f"Error updating status label for {serial}: {e}")

@log_exception_to_json
def check_root_access(serial, timeout=20):
    """Check if device has root access via su or adb root"""
    try:
        # Try: adb root
        result = adb_run(['adb', '-s', str(serial), 'root'], timeout=timeout, capture_output=True, text=True)
        if result.returncode == 0:
            return 'adb_root'
        
        # Try: su -c
        result = adb_run(['adb', '-s', str(serial), 'shell', 'su', '-c', 'id'], timeout=timeout, capture_output=True, text=True)
        if 'uid=0' in result.stdout:
            return 'su_available'
        
        # Try: run-as (ต้องติดตั้งแอปแล้ว)
        result = adb_run(['adb', '-s', str(serial), 'shell', 'run-as', 'jp.konami.pesam', 'id'], timeout=timeout, capture_output=True, text=True)
        if result.returncode == 0:
            return 'run_as_available'
        
        return None
    except Exception as e:
        print(f'{serial}: Error checking root access: {e}')
        return None

@log_exception_to_json
def adb_root(serial, timeout=20):
    try:
        adb_run(
            ['adb', '-s', str(serial), 'root'],
            timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
    except subprocess.TimeoutExpired:
        print(f'{serial}: adb root timeout')

@log_exception_to_json
def capture_screen(device_serial: str, max_retries: int = 5):
    '''จับภาพหน้าจอจาก emulator ผ่าน adb แล้วเซฟเป็นไฟล์ พร้อม retry สำหรับภาพเสีย'''
    # สร้างชื่อไฟล์ปลอดภัย
    clean_serial = device_serial.replace(':', '_').replace('.', '_')
    filename = f'screen_{clean_serial}.jpg'

    screens_dir = resource_path('screens', readonly=False)
    os.makedirs(screens_dir, exist_ok=True)

    filepath = os.path.join(screens_dir, filename)

    for attempt in range(max_retries):
        try:
            result = adb_run(
                ['adb', '-s', device_serial, 'exec-out', 'screencap', '-p'],
                timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            if result.returncode != 0 or not result.stdout:
                logger.warning(f'{device_serial}: screencap attempt {attempt+1} failed - return code: {result.returncode}')
                if attempt < max_retries - 1:
                    time.sleep(1.5)
                continue
            
            with open(filepath, 'wb') as f:
                f.write(result.stdout)

            # Validate PNG file
            if is_valid_png(filepath):
                logger.debug(f'{device_serial}: screencap successful')
                return filepath
            else:
                logger.warning(f'{device_serial}: screencap produced corrupted PNG (attempt {attempt+1}/{max_retries})')
                if attempt < max_retries - 1:
                    time.sleep(2)  # Wait longer before retry
                    continue
                else:
                    return None
        
        except subprocess.TimeoutExpired:
            logger.warning(f'{device_serial}: screencap timeout (attempt {attempt+1}/{max_retries})')
            if attempt < max_retries - 1:
                time.sleep(1.5)
            continue
        except Exception as e:
            logger.error(f'{device_serial}: Exception in capture_screen (attempt {attempt+1}): {e}')
            if attempt < max_retries - 1:
                time.sleep(1.5)
            continue
    
    logger.error(f'{device_serial}: Failed to capture valid screen after {max_retries} attempts')
    return None

@log_exception_to_json
def capture_gacha_screen(device_serial: str, fileName: str, game_id: str, folder_temp_name):
    '''จับภาพหน้าจอจาก emulator ผ่าน adb แล้วเซฟเป็นไฟล์'''
    # สร้างชื่อไฟล์ปลอดภัย
    filename = f'{fileName}-{game_id}.jpg'

    screens_dir = resource_path(folder_temp_name, readonly=False)
    os.makedirs(screens_dir, exist_ok=True)

    filepath = os.path.join(screens_dir, filename)

    result = adb_run(
        ['adb', '-s', device_serial, 'exec-out', 'screencap', '-p'],
        timeout=20,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    if result.returncode != 0:
        raise RuntimeError(f'ADB screencap failed: {result.stderr.decode()}')

    if not result.stdout:
        raise RuntimeError(f'ADB screencap returned empty output')

    with open(filepath, 'wb') as f:
        f.write(result.stdout)

    return filepath

@log_exception_to_json
def tap_location(device_serial: str, x: int, y: int):
    try:
        x = int(x)
        y = int(y)

        byX = 1.333333333333333
        byY = 1.333333333333333

        finalX = x * byX
        finalY = y * byY

        adb_run([
            'adb', '-s', device_serial, 
            'shell', 'input', 'swipe', 
            str(finalX), str(finalY), 
            str(finalX + 1), str(finalY + 1), 
            '100'
        ], timeout=20)

    except Exception as e:
        print(f'{device_serial}: tap_location exception at ({x},{y}): {e}')


@log_exception_to_json
def swipe_down(device_serial: str, x_start: int, y_start: int, x_end: int, y_end: int, duration_ms: int = 500):
    try:
        byX = 1.333333333333333
        byY = 1.333333333333333

        final_x_start = int(x_start * byX)
        final_y_start = int(y_start * byY)
        final_x_end = int(x_end * byX)
        final_y_end = int(y_end * byY)

        # Backwards-compatible simple swipe if no hold requested
        adb_run([
            'adb', '-s', device_serial, 
            'shell', 'input', 'swipe',
            str(final_x_start), str(final_y_start), 
            str(final_x_end), str(final_y_end), 
            str(duration_ms)
        ], timeout=20)
    except Exception as e:
        print(f'{device_serial}: swipe_down exception: {e}')


@log_exception_to_json
def swipe_with_hold(
    device_serial: str, 
    x_start: int, 
    y_start: int, 
    x_end: int, 
    y_end: int, 
    duration_ms: int = 500, 
    hold_ms: int = 0,
    count: int = 1,
):
    try:
        byX = 1.333333333333333
        byY = 1.333333333333333
        
        final_x_start = int(x_start * byX)
        final_y_start = int(y_start * byY)
        final_x_end = int(x_end * byX)
        final_y_end = int(y_end * byY)

        # 1) Optional long press at the start point
        if hold_ms and hold_ms > 0:
            adb_run([
                'adb', '-s', device_serial, 'shell', 'input', 'swipe',
                str(final_x_start), str(final_y_start), str(final_x_start), str(final_y_start), str(hold_ms)
            ], timeout=20)

        # 2) Perform the drag `count` times. Insert short sleeps between repeats so
        # the emulator/app can settle and register multiple gestures.
        for i in range(max(1, int(count))):
            adb_run([
                'adb', '-s', device_serial, 'shell', 'input', 'swipe',
                str(final_x_start), str(final_y_start), str(final_x_end), str(final_y_end), str(duration_ms)
            ], timeout=20)

            if i != count - 1:
                time.sleep(0.12)
    except Exception as e:
        print(f'{device_serial}: swipe_with_hold exception: {e}')

@log_exception_to_json
def esc_key(device_serial: str = ''):
    # ✅ Defense: validate device_serial
    if not device_serial or device_serial == '':
        logger.warning("esc_key() called with empty or None device_serial - skipping")
        return
    adb_run(['adb', '-s', device_serial, 'shell',
             'input keyevent 4'], timeout=20 , capture_output=True, text=True)

@log_exception_to_json
def home_key(device_serial: str = ''):
    # ✅ Defense: validate device_serial is not None or empty
    if not device_serial or device_serial == '':
        logger.warning("home_key() called with empty or None device_serial - skipping")
        return
    adb_run(['adb', '-s', device_serial, 'shell',
             'input keyevent 3'], timeout=20 , capture_output=True, text=True)

@log_exception_to_json
def detection(
    serial,
    mode,
    template_path,
    sub_stage,
    ui_queue: Queue,
    multiple_type: Literal['some', 'all'] = 'all',
    left_top=None,
    right_bottom=None,
    conf_thresh=None,
    distance_thresh=0.75, 
    homography_thresh=5.0, 
    min_matches_thresh=None,
    is_overlay_image:bool = False
) -> tuple[bool, list[dict[str, float]], Optional[int], Optional[int]]:
    try:
        # 1) หนึ่งครั้งก็พอ: capture screen แล้วส่ง preview ให้ UI
        filename = capture_screen(serial)
        screen = cv2.imread(filename)
        if screen is None:
            # แจ้ง error ที่ UI แล้วคืนค่า False
            ui_queue.put(('error', serial, f'Failed to load screenshot: {filename}'))
            return False, [], 0, 0

        # 2) แมตช์กับ template แค่ครั้งเดียว
        results = matcher.match(
            screen_bgr=screen, 
            template_path=template_path, 
            mode=mode,
            conf_thresh=conf_thresh,
            ratio_thresh=distance_thresh,
            min_matches=min_matches_thresh,
            homography_thresh=homography_thresh,
            left_top=left_top,
            right_bottom=right_bottom,
            is_overlay_image=is_overlay_image
        )

        key_found_list: list[dict[str, float]] = []

        #print('final sub_stage', sub_stage)
        #print('final results', results)
        
        if mode == 'multiple':
            multiple_cx = 0
            multiple_cy = 0

            all_found = (any if multiple_type=='some' else all)(
                found for found,_,_,_ in results.values()
            )

            for key,(found, cx, cy, conf) in results.items():
                if found:
                    key_found_list.append({'key': key, 'conf': conf})
                    multiple_cx = cx
                    multiple_cy = cy
            
            ui_queue.put(
                ('confidence', serial,
                f'{"FOUND [OK]" if all_found else "NOT FOUND [FAIL]"} Confidence: {max((r[3] for r in results.values()), default=0):.2f}')
            )

            return all_found, key_found_list, multiple_cx, multiple_cy

        # single mode
        found, cx, cy, conf = results.get(sub_stage, (False, None, None, 0))
        if found:
            key_found_list.append({'key': sub_stage, 'conf': conf})
            ui_queue.put(('confidence', serial, f'FOUND [OK] Confidence: {conf:.2f}'))
        else:
            ui_queue.put(('confidence', serial, f'NOT FOUND [FAIL] Confidence: {conf:.2f}'))
        return found, key_found_list, cx, cy
    except Exception as e:
        print(f'{serial}: Exception in detection: {e}')
        return False, [] , 0, 0

@log_exception_to_json
def file_transfer(serial, folder_name, accumulate_date=1, date=''):
    backup_path = main_configs.get('backup_file_path', 'C:/backup_bot')
    # ✅ Normalize path to use proper backslashes on Windows
    backup_path = os.path.normpath(backup_path) if backup_path else ''
    MAIN_FILE_PATH = '/data/data/jp.konami.pesam/files/SaveData/AUTH/online_user_id_data.dat'

    tmp_remote = f'/sdcard/tmp_{date}.dat'

    dest_folder = os.path.normpath(os.path.join(
        backup_path,
        f'{accumulate_date} [50] {folder_name.replace("_", "")}'
    ))

    os.makedirs(dest_folder, exist_ok=True)

    safe_folder_part = re.sub(r'[<>:\\"/\\|?*]', '', folder_name)

    new_file_name = f'{accumulate_date}_[50]_{safe_folder_part}_{date}.dat'

    local_target = os.path.normpath(os.path.join(dest_folder, new_file_name))

    try:

        # STEP 1 copy จาก protected folder -> sdcard (ใช้ run-as ก่อน ถ้าใช้ไม่ได้ลอง su -c)
        result = adb_run([
            'adb', '-s', serial,
            'shell', 'run-as', 'jp.konami.pesam',
            f'cat {MAIN_FILE_PATH}' 
        ], timeout=20, capture_output=True, text=True)
        
        if result.returncode == 0:
            # ถ้า run-as ใช้ได้ push output ไปยัง /sdcard/
            with tempfile.NamedTemporaryFile(delete=False, suffix='.dat') as tmp:
                tmp.write(result.stdout.encode() if isinstance(result.stdout, str) else result.stdout)
                tmp_local = tmp.name
            
            adb_run(['adb', '-s', serial, 'push', tmp_local, tmp_remote], timeout=20)
            os.remove(tmp_local)
        else:
            # Fallback ไป su -c
            adb_run([
                'adb', '-s', serial,
                'shell', 'su', '-c',
                f'cp {MAIN_FILE_PATH} {tmp_remote}'
            ])

        time.sleep(0.5)

        # STEP 2 pull file
        adb_run([
            'adb', '-s', serial,
            'pull', tmp_remote, local_target
        ], check=True)

        # STEP 3 ลบ temp file
        adb_run([
            'adb', '-s', serial,
            'shell', 'rm', tmp_remote
        ])

        print("FILE SAVED:", local_target)

        return local_target

    except subprocess.CalledProcessError as e:

        stderr = getattr(e, 'stderr', '')

        print(
            f'ADB command failed from {serial} -> {local_target}: {e}; stderr: {stderr}'
        )

        return None

@log_exception_to_json
def move_file(folder_name, target_path, file_name):
    backup_path = main_configs.get('backup_file_path', 'C:/backup_bot')
    # ✅ Normalize path to use proper backslashes on Windows
    backup_path = os.path.normpath(backup_path) if backup_path else ''

    dest_folder = os.path.normpath(os.path.join(backup_path, folder_name))
    os.makedirs(dest_folder, exist_ok=True)

    new_file_name = file_name

    backup_file_path = os.path.normpath(os.path.join(backup_path, folder_name, new_file_name))
    os.makedirs(os.path.dirname(backup_file_path), exist_ok=True)

    try:
        shutil.move(target_path, backup_file_path)
        #print(f'Moved {target_path} -> {backup_file_path}')
    except Exception as e:
        logger.error(f'Failed to move {target_path} -> {backup_file_path}: {e}', exc_info=True)
        print(f'Failed to move {target_path} -> {backup_file_path}: {e}')

@log_exception_to_json
def copy_file_to_main_file_path(serial, re_reroll_folder, current_file):
    '''
    คัดลอกไฟล์ current_file ใน re_reroll_folder ไปทับ MAIN_FILE_PATH ใน emulator
    STEP 1: push file ไป /sdcard/
    STEP 2: su -c cp จาก /sdcard/ ไปยัง /data/data/ (protected folder)
    STEP 3: rm /sdcard/
    '''
    # ✅ Normalize folder path to use proper backslashes on Windows
    re_reroll_folder = os.path.normpath(re_reroll_folder) if re_reroll_folder else ''
    src_path = os.path.normpath(os.path.join(re_reroll_folder, current_file))
    MAIN_FILE_PATH = '/data/data/jp.konami.pesam/files/SaveData/AUTH/online_user_id_data.dat'
    tmp_remote = f'/sdcard/tmp_{int(time.time() * 1000)}.dat'
    
    print(f'src_path: {src_path}')
    
    # ✅ Verify source file exists
    if not os.path.isfile(src_path):
        logger.error(f"[{serial}] Source file not found: {src_path}")
        raise FileNotFoundError(f"Source file not found: {src_path}")

    try:
        with tempfile.NamedTemporaryFile(
            suffix='online_user_id_data.dat', 
            delete=False,  # เราจะลบเอง
            dir=os.path.dirname(__file__)
        ) as temp_file:
            temp_file_path = temp_file.name
            
        # Copy ไฟล์มาไว้ชื่อใหม่ในเครื่องเรา
        shutil.copyfile(src_path, temp_file_path)
        
        # STEP 1: push ไปยัง /sdcard/ (ได้เพราะ /sdcard/ writable)
        adb_run(
            ['adb', '-s', str(serial), 'push', temp_file_path, tmp_remote], 
            timeout=20,
            check=True,
            capture_output=True,
            text=True
        )
        
        time.sleep(0.5)
        
        # STEP 2: เอา privilege มา cp จาก /sdcard/ ไปยัง protected folder (ลองใช้ run-as ก่อน)
        result = adb_run([
            'adb', '-s', str(serial),
            'shell', 'run-as', 'jp.konami.pesam',
            f'cat {tmp_remote} > {MAIN_FILE_PATH}'
        ], timeout=20, capture_output=True, text=True)
        
        if result.returncode != 0:
            # Fallback ไป su -c
            adb_run([
                'adb', '-s', str(serial),
                'shell', 'su', '-c',
                f'cp {tmp_remote} {MAIN_FILE_PATH}'
            ], timeout=20, check=True)
        
        time.sleep(0.5)
        
        # STEP 3: ลบ temp file บน /sdcard/
        adb_run([
            'adb', '-s', str(serial),
            'shell', 'rm', tmp_remote
        ])
        
        print(f'[OK] Copied {src_path} to {MAIN_FILE_PATH} on {serial}')
        
    except subprocess.CalledProcessError as e:
        # Log more informative message (include stderr if available)
        stderr = getattr(e, 'stderr', '')
        print(f'[FAIL] ADB command failed copying {src_path} to {MAIN_FILE_PATH} on {serial}: {e}; stderr: {stderr}')
        
    finally:
        # ลบไฟล์ชั่วคราวด้วยการจัดการ permission
        safe_remove_file(temp_file_path)

def safe_remove_file(file_path):
    """ลบไฟล์แบบปลอดภัย พร้อม retry mechanism"""
    if not os.path.exists(file_path):
        return
        
    for attempt in range(3):
        try:
            # เปลี่ยน permission ก่อนลบ
            os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
            os.remove(file_path)
            break
        except PermissionError:
            if attempt < 2:
                time.sleep(1)  # รอ 1 วินาทีแล้วลองใหม่
            else:
                print(f"Cannot remove temp file: {file_path}")
        except Exception as e:
            print(f"Error removing temp file: {e}")
            break

@log_exception_to_json
def close_pes(serial):
    try:
        adb_run(['adb', '-s', str(serial), 'shell', 'am', 'force-stop', 'jp.konami.pesam'],
                timeout=20, capture_output=True, text=True
                )
        #print(f'{serial}: App force-stopped.')
    except Exception as e:
        print(f'{serial}: Failed to force-stop app: {e}')

@log_exception_to_json
def process_multiline_text(text, random_target='carector'):
    '''
    ประมวลผลข้อความที่มีหลายบรรทัด 
    เอาทั้งบรรทัดแรก และคำสุดท้ายจากบรรทัดสุดท้าย
    '''
    # แบ่งข้อความตามบรรทัด
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    if len(lines) >= 2:
        # หากมี 2 บรรทัดขึ้นไป
        first_line = lines[0]  # บรรทัดแรก
        last_line = lines[-1]  # บรรทัดสุดท้าย
        
        first_words = first_line.split()
        last_words = last_line.split()
        
        result_parts = []
        
        # เอาคำแรกจากบรรทัดแรก
        if first_words:
            result_parts.append(' '.join(first_words))

        # เอาคำสุดท้ายจากบรรทัดสุดท้าย
        if last_words:
            if random_target == 'gear':
                result_parts.append(' '.join(last_words))
            else:
                result_parts.append(last_words[-1])
        
        return ' '.join(result_parts)
        
    elif len(lines) == 1:
        if random_target == 'gear':
            return lines[0]
        else:
            # หากมีบรรทัดเดียว ให้เอาคำสุดท้าย
            words = lines[0].split()
            if words:
                return words[-1]
    
    return text.strip()

@log_exception_to_json
def extract_text_tesseract(
    serial, 
    ui_queue: Queue, 
    image_path, 
    crop_area: tuple[int, int, int, int], 
    extract_mode = 'normal', 
    random_target='carector' , 
    dictionary = None,
    target_file ='',
    save_roi=False,
    is_ignore_x=False,
):
    '''
    ใช้ Tesseract อ่านข้อความทั้งตัวเลขและตัวอักษร
    '''
    try:
        # Scale crop area coordinates to match device resolution
        # scaled_crop_area = scale_crop_area(crop_area)

        x1, y1, x2, y2 = crop_area

        if not is_ignore_x:
            x1 = int(x1 * 1.333333333333333)
            x2 = int(x2 * 1.333333333333333)
            y1 = int(y1 * 1.333333333333333)
            y2 = int(y2 * 1.333333333333333)

        time.sleep(0.5)  # รอให้ไฟล์ถูกเขียนเสร็จ

        # Validate PNG before reading
        if not is_valid_png(image_path):
            logger.error(f"{serial}: PNG file is corrupted or invalid: {image_path}")
            ui_queue.put(('error', serial, f'Failed to load screenshot - PNG corrupted'))
            return {
                'best_text': '',
                'original': '',
                'error': 'Failed to load screenshot'
            }

        # อ่านรูปภาพ - add try-except around cv2.imread
        try:
            screen = cv2.imread(image_path)
        except Exception as e:
            logger.error(f"{serial}: cv2.imread exception: {e}")
            ui_queue.put(('error', serial, f'Failed to load screenshot'))
            return {
                'best_text': '',
                'original': '',
                'error': 'Failed to load screenshot'
            }
        
        if screen is None:
            # แจ้ง error ที่ UI แล้วคืนค่า False
            logger.error(f"{serial}: cv2.imread returned None for {image_path}")
            ui_queue.put(('error', serial, f'Failed to load screenshot'))
            return {
                'best_text': '',
                'original': '',
                'error': 'Failed to load screenshot'
            }
        image_rgb = cv2.cvtColor(screen, cv2.COLOR_BGR2RGB)
        
        # ครอบตัดพื้นที่ตามพิกัดที่กำหนด
        roi = image_rgb[y1:y2, x1:x2]
        
        # ปรับปรุงภาพเพื่อให้ OCR อ่านได้ดีขึ้น
        if extract_mode == 'name':
            # พยายามแยกสีเหลือง (ป้ายชื่อมักเป็นสีเหลือง) เพื่อช่วย OCR
            roi_bgr = cv2.cvtColor(roi, cv2.COLOR_RGB2BGR)
            roi_hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
            
            # รวม mask: ถ้าเป็น carector ให้รวมทั้ง yellow และ brown
            if random_target == 'carector':
                lower_brown = np.array([6, 70, 30])
                upper_brown = np.array([25, 200, 150])
                mask_brown = cv2.inRange(roi_hsv, lower_brown, upper_brown)
                mask_combined = mask_brown
            else:
                lower_yellow = np.array([15, 120, 120])
                upper_yellow = np.array([45, 255, 255])
                mask_yellow = cv2.inRange(roi_hsv, lower_yellow, upper_yellow)
                mask_combined = mask_yellow

            # หากมีพิกเซลสีเหลืองเพียงพอ ให้ใช้ mask แล้ว OCR บนภาพนั้น
            use_mask = cv2.countNonZero(mask_combined) > 50
            if use_mask:
                # คอมโพสเป็นภาพขาว-ดำจาก mask เพื่อเน้นตัวอักษร
                roi_gray = mask_combined
            else:
                roi_gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)

            # เบื้องต้นลดสัญญาณรบกวนเล็กน้อย
            try:
                roi_gray = cv2.medianBlur(roi_gray, 3)
            except Exception:
                pass
        elif extract_mode == 'normal':
            roi_gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        elif extract_mode == 'number':
            roi_gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)

            # # ลด noise ก่อน
            # roi_gray = cv2.GaussianBlur(roi_gray, (3, 3), 0)
            
            # # ใช้ CLAHE (Contrast Limited Adaptive Histogram Equalization) เพื่อเพิ่มความ contrast อย่างมนุษย์สำหรับตัวเลข
            # clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(3, 3))  # เพิ่ม clipLimit จาก 2.0 เป็น 3.0
            # roi_gray = clahe.apply(roi_gray)

        # ใช้ threshold เพื่อทำให้ข้อความชัดขึ้น
        # _, roi_thresh = cv2.threshold(roi_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # ขยายขนาดภาพเพื่อให้ OCR อ่านได้ดีขึ้น (ลดจาก 3x เป็น 2x เพื่อลด CPU)
        # roi_resized = cv2.resize(roi_thresh, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
        roi_resized = roi_gray

        if extract_mode == 'name':
            config = '--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789[]'
            text = pytesseract.image_to_string(roi_resized, config=config)

            clean_text = re.sub(r'[^A-Za-z0-9\[\]\s\n]', '', text).strip()

            # print(f"extract: {text.replace('\n', ' ')} | {clean_text.replace('\n', ' ')} | {target_file}")
            # print('----------------------------------------------------------')
            
            if save_roi:
                try:
                    # แยก serial ให้สะอาด (ดึง octet ที่ 3 และ 4)
                    octets = serial.split(':')[0].split('.')
                    if len(octets) >= 4:
                        clean_serial = f"{octets[2]}_{octets[3]}"
                    else:
                        clean_serial = serial.replace(':', '_').replace('10.0.0.', '').replace('10.0.1.', '').replace('_5555', '')
                    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # ลบ microseconds เหลือ milliseconds
                    filename = f'{clean_serial}_{timestamp}_{extract_mode}_{process_multiline_text(clean_text, random_target)}.jpg'
                    roi_debug_dir = resource_path('roi_debug', readonly=False)
                    os.makedirs(roi_debug_dir, exist_ok=True)
                    filepath = os.path.join(roi_debug_dir, filename)
                    roi_bgr = cv2.cvtColor(roi_resized, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(filepath, roi_bgr)
                    logger.info(f"Saved ROI image: {filepath}")
                except Exception as e:
                    logger.warning(f"Failed to save ROI image: {e}")

            if clean_text:
                processed_text = process_multiline_text(clean_text, random_target)
                print('processed_text:', processed_text)
                formatted_text = '_'.join(processed_text.split()).lower()

                # เตรียม dictionary รายชื่อที่คาดว่าจะเจอ (underscore, lowercase)
                if dictionary:
                    dictionary = [os.path.splitext(f)[0].replace('-', '').replace(' ', '_').lower() for f in dictionary]

                    # fuzzy match
                    matches = difflib.get_close_matches(formatted_text, dictionary, n=1, cutoff=0.76 if random_target=='gear' else 0.9)
                    best_match = matches[0] if matches else formatted_text
                    formatted_text = best_match

                return {
                    'best_text': formatted_text,
                    'original': text.strip()
                }
            else:
                return {
                    'best_text': '',
                    'original': '',
                    'error': 'ไม่สามารถอ่านข้อความได้'
                }
        elif extract_mode == 'normal':
            # ลอง config หลายแบบ
            configs = [
                '--psm 8',  # Single word
                '--psm 7',  # Single text line  
                '--psm 6',  # Single uniform block
                '--psm 13'  # Raw line
            ]
            
            results = []
            
            for config in configs:
                try:
                    # อ่านข้อความ (เก็บทั้งตัวเลขและตัวอักษร)
                    text = pytesseract.image_to_string(roi_resized, config=config)
                    clean_text = re.sub(r'[^a-zA-Z0-9\s\n]', '', text).strip()
                    
                    # print(f"extract ({config}) : {text.replace('\n', ' ')} | {clean_text.replace('\n', ' ')} | {target_file}")
                    # print('----------------------------------------------------------')

                    if clean_text:
                        # ประมวลผลข้อความหลายบรรทัด
                        processed_text = process_multiline_text(clean_text)
                        
                        # ได้คะแนนความมั่นใจ
                        data = pytesseract.image_to_data(roi_resized, config=config, output_type=pytesseract.Output.DICT)
                        confidences = [int(conf) for conf in data['conf'] if int(conf) > 0]
                        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
                        
                        results.append({
                            'text': processed_text,
                            'original': text.strip(),
                            'raw_clean': clean_text,
                            'confidence': avg_confidence,
                            'config': config
                        })
                        
                except Exception as e:
                    continue
            
            if results:
                # เรียงตาม confidence
                results.sort(key=lambda x: x['confidence'], reverse=True)
                formatted_text = '_'.join(results[0]['text'].split()).lower()

                return {
                    'best_text': formatted_text,
                    'original': results[0]['original']
                }
            else:
                return {
                    'best_text': '',
                    'original': '',
                    'error': 'ไม่สามารถอ่านข้อความได้'
                }
        elif extract_mode == 'number':
            config = '--psm 8 -c tessedit_char_whitelist=0123456789ilLIoOtT'
            text = pytesseract.image_to_string(roi_resized, config=config)

            # แทนที่อักขระที่สับสนให้เป็นตัวเลข
            text = text.replace('i', '1').replace('I', '1')
            text = text.replace('l', '1').replace('L', '1')
            text = text.replace('t', '1').replace('T', '1')
            text = text.replace('o', '0').replace('O', '0')

            clean_text = re.sub(r'[^0-9\s\n]', '', text).strip()

            if save_roi:
                try:
                    # แยก serial ให้สะอาด (ดึง octet ที่ 3 และ 4)
                    octets = serial.split(':')[0].split('.')
                    if len(octets) >= 4:
                        clean_serial = f"{octets[2]}_{octets[3]}"
                    else:
                        clean_serial = serial.replace(':', '_').replace('10.0.0.', '').replace('10.0.1.', '').replace('_5555', '')
                    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]  # ลบ microseconds เหลือ milliseconds
                    filename = f'{clean_serial}_{timestamp}_{extract_mode}_{clean_text}.jpg'
                    roi_debug_dir = resource_path('roi_debug', readonly=False)
                    os.makedirs(roi_debug_dir, exist_ok=True)
                    filepath = os.path.join(roi_debug_dir, filename)
                    roi_bgr = cv2.cvtColor(roi_resized, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(filepath, roi_bgr)
                    logger.info(f"Saved ROI image: {filepath}")
                except Exception as e:
                    logger.warning(f"Failed to save ROI image: {e}")

            if clean_text:
                processed_text = process_multiline_text(clean_text, random_target)
                formatted_text = ''.join(processed_text.split())

                return {
                    'best_text': formatted_text,
                    'original': text.strip()
                }
            else:
                return {
                    'best_text': '',
                    'original': '',
                    'error': 'ไม่สามารถอ่านข้อความได้'
                }   
    except Exception as e:
        return {
            'best_text': '',
            'original': '',
            'error': f'Tesseract Error: {str(e)}, {extract_mode}, {random_target}'
        }

# Main loop for each device
@log_exception_to_json
def launch_main_loop(serial, ui_queue: Queue, shared_data, shared_lock):
    global devices_configs

    # Determine starting stage from saved config
    saved = devices_configs.get(serial, {}).get('stage')
    start_stage = saved.get('stage') if isinstance(saved, dict) else 1
    current_stage = start_stage
    workflow_count = len(workflow)
    local_manager = OnStageManager(shared_data, shared_lock)

    is_open_again = False

    @log_exception_to_json
    def pre_stage():
        nonlocal is_open_again, ui_queue
        if not is_pes_visible(serial):
            open_pes()
            is_open_again = True
            time.sleep(1)

        splash_count = 0

        while True:
            #print(is_showing_splash_screen('jp.konami.pesam', serial))
            if is_showing_splash_screen('jp.konami.pesam', serial):
                #print('ตอนนี้กำลังแสดง Splash Screen ของเกมอยู่', splash_count)
                if splash_count > 10:
                    # todo: delete
                    if main_configs.get('selected_mode', '') != 'ทดสอบ':
                        close_pes(serial)
                        time.sleep(1)
                        open_pes()
                    splash_count = 0
                else:
                    splash_count += 1
            else:
                #print('ไม่ได้อยู่ในหน้า Splash Screen')
                splash_count = 0
                break
            time.sleep(1)

    @log_exception_to_json
    def is_pes_visible(serial: str) -> bool:
        try:
            output = adb_check_output(
                ['adb', '-s', serial, 'shell', 'dumpsys', 'window', 'windows'],
                timeout=20,
                stderr=subprocess.DEVNULL,
                text=True
            )
            # regex จะ match ตั้งแต่ header 'Window{... jp.konami.pesam/...}'
            # แล้วตามด้วยเนื้อหาใด ๆ จนเจอ isOnScreen=true และ isVisible=true
            pattern = re.compile(
                r'Window\{[0-9a-f]+ u\d+ jp\.konami\.pesam/[^\}]+\}:[\s\S]*?'
                r'isOnScreen=true[\s\S]*?isVisible=true',
                re.MULTILINE
            )
            match = pattern.search(output)
            if match:
                #print('[FOUND] PES window still visible:')
                return True
            else:
                return False
        except subprocess.CalledProcessError:
            return False

    @log_exception_to_json
    def is_showing_splash_screen(package_name: str, serial: str) -> bool:
        # ประกอบคำสั่ง adb
        cmd = ['adb']
        if serial:
            cmd += ['-s', str(serial)]
        cmd += ['shell', 'dumpsys', 'window', 'windows']

        try:
            output = adb_check_output(cmd, timeout=20, stderr=subprocess.DEVNULL, text=True)
        except subprocess.CalledProcessError:
            return False

        # หา pattern เช่น 'Window{... Splash Screen jp.konami.pesam'
        splash_pattern = re.compile(
            rf'Splash Screen\s+{re.escape(package_name)}')
        for line in output.splitlines():
            if splash_pattern.search(line):
                return True

        return False

    @log_exception_to_json
    def open_pes():
        res = adb_run(
            ['adb', '-s', str(serial), 'shell', 'monkey', '-p', 'jp.konami.pesam',
             '-c', 'android.intent.category.LAUNCHER', '1'],
            timeout=20, capture_output=True, text=True
        )
        if res.returncode != 0:
            print(f'Stage 2 launch failed on {serial}: {res.stderr}')

    @log_exception_to_json
    def wait_for(
        serial: str,
        image_dir: str = '',
        image_file: str = '',
        action= lambda cx=None, cy=None: [],
        is_loop: bool = True,
        dectection_mode: Literal['single', 'multiple'] = 'single',
        multiple_type: Literal['some', 'all'] = 'all',
        limit: float = 0,
        action_limit=lambda found, conf: [],
        last_action=lambda found, conf: [],
        left_top=None,
        right_bottom=None,
        conf_thresh=None,
        distance_thresh=0.75,
        homography_thresh=5.0, 
        min_matches_thresh=None,
        is_overlay_image:bool = False,
        ui_title = '',
        detection_type: Literal['text', 'image'] = 'image',
        target_file:str = '',
        sub_target_file:str = None,
        text_action= lambda: [],
        text_crop_area = (0, 0, 0, 0),
        extract_mode = 'normal',
        pre_action = lambda: []
    ) -> list[dict[str, float]]:
        
        try:
            image_key = image_file.rsplit('.', 1)[0]
            key_list: list[dict[str, float]] = []
            limit_count = 0

            while True:
                if detection_type == 'image':
                    found, key_found_list, cx, cy = detection(
                        serial,
                        dectection_mode,
                        f'{image_dir}/{image_file}',
                        image_key,
                        ui_queue,
                        multiple_type=multiple_type,
                        left_top=left_top,
                        right_bottom=right_bottom,
                        conf_thresh=conf_thresh,
                        distance_thresh=distance_thresh,
                        homography_thresh=homography_thresh,
                        min_matches_thresh=min_matches_thresh,
                        is_overlay_image=is_overlay_image
                    )

                    key_list.extend(key_found_list)

                    time.sleep(1)

                    if limit > 0:
                        # ui_queue.put(('substage', serial, f'{ui_title} ({limit_count})'))
                        if limit_count > limit:
                            conf = 0.0
                            if key_list:
                                conf = max(key['conf'] for key in key_list)
                            action_limit(found, conf)
                            break
                        limit_count += 1

                    if found:
                        action(cx, cy)
                        break
                    elif not is_loop:
                        break
                elif detection_type == 'text':
                    screen_path = capture_screen(serial)
                    
                    if screen_path is None:
                        logger.error(f'{serial}: Failed to capture screen - skipping OCR')
                        print('❌ Failed to capture screen')
                        if not is_loop:
                            break
                        time.sleep(1)
                        continue

                    tesseract_result = extract_text_tesseract(
                        serial, 
                        ui_queue, 
                        screen_path, 
                        text_crop_area, 
                        extract_mode, 
                        target_file=target_file
                    )
                    if 'error' in tesseract_result:
                        # print(f'   ❌ {tesseract_result["error"]}')
                        print('❌')
                    
                    ranger_name = tesseract_result['original'].replace(' ', '').replace('\n', '').lower()

                    time.sleep(1)

                    #print(f'{serial} : ', ranger_name, target_file)
                    if limit > 0:
                        if target_file in ranger_name or sub_target_file != None and sub_target_file in ranger_name:
                            text_action()
                            break
                        if limit_count > limit:
                            action_limit(True, 0.0)
                            break
                        elif not is_loop:
                            break
                        limit_count += 1
                    else:
                        if target_file in ranger_name or sub_target_file != None and sub_target_file in ranger_name:
                            text_action()
                            break
                        elif not is_loop:
                            break
                    
                    pre_action()

            if detection_type == 'image':                
                if last_action:
                    time.sleep(1)
                    fileName = ''
                    if key_list:
                        sorted_keys = sorted(key_found_list, key=lambda x: x['key'])
                        fileName = '_'.join(sorted_key['key'].replace(' ', '') for sorted_key in sorted_keys)
                    last_action(found, fileName)

            return key_list
        
        except Exception as e:
            print(f'{serial}: Exception in wait_for: {e}')
            # กรณี error ให้ออกจาก loop ไม่ kill process
            return []

    def loop_confirm_wait_for(target_file, text_action, text_crop_area, sub_target_file=None):
        is_break_loop_confirm = True

        def set_break_loop_confirm():
            nonlocal is_break_loop_confirm
            is_break_loop_confirm = False
        
        wait_for(
            serial=serial,
            detection_type='text',
            target_file=target_file,
            sub_target_file=sub_target_file, 
            text_action=text_action,
            text_crop_area=text_crop_area,
            extract_mode = 'name',
        )

        while True:
            is_break_loop_confirm = True

            time.sleep(1)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file=target_file,
                sub_target_file=sub_target_file, 
                text_action=lambda:[
                    set_break_loop_confirm(),
                ],
                text_crop_area=text_crop_area, # พื้นที่คำว่า confirm
                extract_mode = 'name',
                is_loop=False
            )


            if is_break_loop_confirm:
                break

            text_action()

    def handle_move_file(current_file, current_folder, fined_gacha_name = [], date = '', mode = 'normal', accumulat = 0):
        gold_coin = 0

        def gold_detection(serial):
            nonlocal gold_coin
            from collections import Counter
            
            while True:
                coin_list = []
                
                # เช็ค 3 ครั้ง
                for i in range(3):
                    while True:
                        screen_path = capture_screen(serial)
                        
                        # ✅ Add null check for screenshot capture
                        if screen_path is None:
                            logger.error(f'{serial}-{current_file}: capture_screen failed in gold_detection (attempt {i+1})')
                            print(f"{serial}-{current_file} : Movefile - {(i)} - ❌ Screenshot capture failed")
                            time.sleep(1)
                            continue  # Retry capture
                        
                        text_crop_area = (72, 13, 122, 43) # พื้นที่คำว่า gold coin
                        extract_mode = 'number'
                        
                        tesseract_result = extract_text_tesseract(
                            serial=serial, 
                            ui_queue=ui_queue, 
                            image_path=screen_path, 
                            crop_area=text_crop_area, 
                            extract_mode=extract_mode,
                            random_target='carector' , 
                            dictionary = None,
                            target_file ='',
                            save_roi=False,
                            is_ignore_x=True
                        )

                        if 'error' in tesseract_result and tesseract_result['error'] == 'Failed to load screenshot':
                            print(f"{serial}-{current_file} : Movefile - {(i)} - Gold coin OCR result: {tesseract_result['error']}")
                            time.sleep(1)
                            continue  # ลองใหม่ถ้าโหลดภาพไม่สำเร็จ

                        break
                    print(f"{serial}-{current_file} : Movefile - {(i)} - Gold coin OCR result: {tesseract_result}")

                    if 'error' in tesseract_result:
                        print(f"{serial}-{current_file} : Movefile - {(i)} - ❌ GG")
                        coin_value = '0'
                    else:
                        coin_value = tesseract_result['original'].replace(' ', '').replace('\n', '').lower()
                    
                    coin_list.append(coin_value)
                    time.sleep(1)  # เพิ่มเวลารอระหว่างเช็ก
                
                # ตรวจสอบว่า 3 ตัวต่างกันหมด
                if len(set(coin_list)) == 3:
                    # ถ้าทั้ง 3 ตัวต่างกันหมด ให้วนทำใหม่
                    print(f"{serial}-{current_file} : Movefile - ค่า coin ไม่ตรงกัน: {coin_list} วนทำใหม่")
                    continue
                
                # เอาค่าที่มีมากที่สุด
                counter = Counter(coin_list)
                gold_coin = counter.most_common(1)[0][0]
                print(f"{serial}-{current_file} : Movefile - ค่า coin: {coin_list} → ผลลัพธ์: {gold_coin}")
                break
        
        gold_detection(serial)
        
        temp_current_file = current_file

        print(f'{current_file} -> {current_folder}')

        name_list = current_file.rsplit('_')

        if mode == 'farm' and len(name_list[0].rsplit('-')) > 1:
            pre_accumulate_date = name_list[0].rsplit('-')[1]
        else:
            pre_accumulate_date = name_list[0]

        print('accumulat: ', accumulat)
        if accumulat == 0:
            accumulate_date = str(pre_accumulate_date)
        else:
            accumulate_date = str(accumulat)

        print(f'accumulate_date: {accumulate_date}')
        print(f'pre_accumulate_date: {pre_accumulate_date}')

        old_name_list_split = []  # Initialize before conditional blocks
        if len(name_list) == 3:
            # rsplit('-') name_list[1]
            old_name_list_split = name_list[1].rsplit('-')
        if len(name_list) == 4:
            # rsplit('-') name_list[2]
            old_name_list_split = name_list[2].rsplit('-')

        # extend กับ fined_gacha_name
        combined_list = old_name_list_split + fined_gacha_name
        
        if len(fined_gacha_name) > 0:
            # กำจัด 'New Samak' ถ้ามีอยู่
            combined_list = [item for item in combined_list if item != 'New Samak']
        
        # sort
        combined_list.sort()
        
        # กำหนด old_name_list
        old_name_list = '-'.join(combined_list).replace('_','')

        print(f'combined_list: {combined_list}')
        print(f'old_name_list: {old_name_list}')
        
        # join ด้วย -
        if mode == 'farm':
            current_file = f'Farm-{accumulate_date}_[{gold_coin}]_{old_name_list}_{date}.dat'
        else:
            current_file = f'{accumulate_date}_[{gold_coin}]_{old_name_list}_{date}.dat'
            
        new_file_name = current_file
        
        target_path = os.path.join(main_configs.get('re_reroll_file_path',''), current_folder, temp_current_file)
        move_file(f'{accumulate_date} [{gold_coin}] {old_name_list}', target_path, new_file_name)

        time.sleep(2)

        ui_queue.put(('substage', serial, 'gacha loop 6.1 : ลบคิว'))
        remove_from_on_stage_by_filename(temp_current_file, local_manager)

        ui_queue.put(('substage', serial, 'gacha loop 7 : ก่อน reset'))
        ui_queue.put(('reset', serial, None))

    def loop_tutorial_one(mode):
        is_break = False

        def set_break():
            nonlocal is_break
            is_break = True

        # wait_for(
        #     serial=serial,
        #     detection_type='text',
        #     target_file='pass', # Pass
        #     text_action=lambda:[],
        #     text_crop_area=(707, 427, 771, 461),
        # )

        while True:

            if is_break:
                break

            time.sleep(3),

            if mode == 's':
                swipe_down(serial, 104, 375, 420, 375, 3500)
                tap_location(serial, 903, 442)
                tap_location(serial, 897, 294)
            elif mode == 'p':
                swipe_down(serial, 147, 380, 274, 306, 1000)
                tap_location(serial, 741, 445)
                time.sleep(1)
                swipe_down(serial, 104, 375, 420, 375, 500)
                tap_location(serial, 897, 294)
            
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='complete', # Complete
                text_action=lambda:[
                    set_break(),
                    loop_confirm_wait_for(
                        target_file='complete', # Complete
                        text_action=lambda:[
                            tap_location(serial, 480, 390)
                        ],
                        text_crop_area=(424, 218, 541, 278), # พื้นที่คำว่า Complete
                    )
                ],
                text_crop_area=(424, 229, 541, 267), # พื้นที่คำว่า Complete
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                break

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='tip', # Tips
                text_action=lambda:[tap_location(serial, 480, 380)],
                text_crop_area=(452, 128, 512, 163), # พื้นที่คำว่า Tips
                extract_mode = 'name',
                is_loop=False
            )

    def loop_tutorial_two(mode):
        is_break = False

        def set_break():
            nonlocal is_break
            is_break = True

        # wait_for(
        #     serial=serial,
        #     detection_type='text',
        #     target_file='sw', # swich
        #     text_action=lambda:[],
        #     text_crop_area=(707, 427, 771, 461),
        # )

        while True:

            if is_break:
                break

            time.sleep(3.3)
           
            swipe_down(serial, 880, 420, 880, 420, 5000)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='complete', # Complete
                text_action=lambda:[
                    set_break(),
                    loop_confirm_wait_for(
                        target_file='complete', # Complete
                        text_action=lambda:[
                            tap_location(serial, 480, 390)
                        ],
                        text_crop_area=(424, 218, 541, 278), # พื้นที่คำว่า Complete
                    )
                ],
                text_crop_area=(424, 218, 541, 278), # พื้นที่คำว่า Complete
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                break

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='tip', # Tips
                text_action=lambda:[tap_location(serial, 480, 380)],
                text_crop_area=(449, 150, 512, 184), # พื้นที่คำว่า Tips
                extract_mode = 'name',
                is_loop=False
            )
    
    def loop_tutorial_three(mode):
        is_break = False

        def set_break():
            nonlocal is_break
            is_break = True

        # wait_for(
        #     serial=serial,
        #     detection_type='text',
        #     target_file='pass', # Pass
        #     text_action=lambda:[],
        #     text_crop_area=(707, 427, 771, 461),
        # )

        while True:

            if is_break:
                break

            time.sleep(3)

            if mode == 's':
                swipe_down(serial, 143, 413, 232, 347, 500)
                tap_location(serial, 768, 320)
                time.sleep(1.5)
                tap_location(serial, 897, 294)
            elif mode == 'p':
                swipe_down(serial, 122, 295, 167, 342, 1000)
                tap_location(serial, 768, 320)
                time.sleep(1.5)
                tap_location(serial, 897, 294)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='complete', # Complete
                text_action=lambda:[
                    set_break(),
                    loop_confirm_wait_for(
                        target_file='complete', # Complete
                        text_action=lambda:[
                            tap_location(serial, 480, 390)
                        ],
                        text_crop_area=(424, 218, 541, 278), # พื้นที่คำว่า Complete
                    )
                ],
                text_crop_area=(424, 218, 541, 278), # พื้นที่คำว่า Complete
                extract_mode = 'name',
                is_loop=False
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='tip', # Tips
                text_action=lambda:[tap_location(serial, 480, 380)],
                text_crop_area=(449, 150, 512, 184), # พื้นที่คำว่า Tips
                extract_mode = 'name',
                is_loop=False
            )
    
    def loop_skip_tutorial(text_crop_area, x, y):
        is_break = False

        def set_break():
            nonlocal is_break
            is_break = True

        while True:
            # print(f"is_break : {is_break}")

            if is_break:
                break
            
            tap_location(serial, 884, 503)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    set_break()
                ],
                text_crop_area=text_crop_area, # พื้นที่คำว่า Skip
                extract_mode = 'name',
                is_loop=False
            )
    
    def loop_select_player():
        is_break = True
        def set_break():
            nonlocal is_break
            is_break = False
        
        while True:
            is_break = True

            swipe_down(serial, 436, 233, 499, 181, 4000), # กด  

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='sort',
                text_action=lambda:[set_break()],
                text_crop_area=(438, 14, 493, 46),
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                break

    def main_loop_normal(serial, stage, last_func= lambda: []):
        def loop_check_unable_download():
            is_break = False

            def set_break():
                nonlocal is_break
                is_break = True

            while True:

                if is_break:
                    break

                ui_queue.put(('substage', serial, 'sub stage 2 : ตรวจสอบ download'))
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='unable',  # unable
                    sub_target_file='unabie',  # unable
                    text_action=lambda:[
                        loop_confirm_wait_for(
                            target_file='unable', # unable
                            text_action=lambda:[
                                tap_location(serial, 460, 344), # กด Ok
                            ],
                            text_crop_area=(274, 170, 687, 208), # พื้นที่คำว่า Download Latest Data?
                        ),
                        loop_confirm_wait_for(
                            target_file='logged', # logged
                            text_action=lambda:[
                                tap_location(serial, 623, 455), # กด login
                            ],
                            text_crop_area=(644, 154, 754, 195), # พื้นที่คำว่า Download Latest Data?
                        ),
                        wait_for(
                            serial=serial,
                            detection_type='text',
                            target_file='konam',  # konam
                            text_action=lambda:[
                                tap_location(serial, 608, 356), # กดเริ่มต้นหน้าหลัก
                            ],
                            text_crop_area=(628, 480, 688, 507), # พื้นที่คำว่า take
                            extract_mode = 'name',
                        ),
                        loop_confirm_wait_for(
                            target_file='download', # Download
                            text_action=lambda:[
                                tap_location(serial, 608, 356), # กด Download
                            ],
                            text_crop_area=(358, 157, 600, 188), # พื้นที่คำว่า Download Latest Data?
                        )
                    ],
                    text_crop_area=(274, 170, 687, 208), # พื้นที่คำว่า take
                    extract_mode = 'name',
                    is_loop=False
                )
                
                if is_break:
                    break

                tap_location(serial, 836, 500)
                
                ui_queue.put(('substage', serial, 'sub stage 2 : take'))
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='follow',  # follow
                    text_action=lambda:[
                        set_break()
                    ],
                    text_crop_area=(567, 260, 637, 290), # พื้นที่คำว่า follow
                    extract_mode = 'name',
                    is_loop=False
                )
                
                if is_break:
                    break
        
        if stage == 1:
            home_key(serial)

            pre_stage()

            tap_location(serial, 438, 507)

            # sub stage 1
            ui_queue.put(('substage', serial, 'sub stage 1 : เลือกภาษา'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='done', # Done
                text_action=lambda:[
                    tap_location(serial, 840, 509), # ปุ่ม Done ขวาล่าง
                    time.sleep(1),
                    tap_location(serial, 864, 509) # ปุ่ม Done ขวาล่าง
                ],
                text_crop_area=(836, 492, 899, 521), # พื้นที่คำว่า Done
                extract_mode = 'name'
            )

            # sub stage 2
            ui_queue.put(('substage', serial, 'sub stage 2 : เลือกภูมิภาค'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='next', # Taiwan
                text_action=lambda:[
                    tap_location(serial, 840, 509), # ปุ่ม Next ขวาล่าง
                    time.sleep(1),
                    tap_location(serial, 864, 509), # ปุ่ม Done ขวาล่าง
                    time.sleep(1.5),
                    tap_location(serial, 603, 346) # ปุ่ม Confirm ตรง Modal
                ],
                text_crop_area=(836, 492, 899, 521), # พื้นที่คำว่า Next
                extract_mode = 'name'
            )

            # sub stage 3
            ui_queue.put(('substage', serial, 'sub stage 3 : เลือกวันเกิด'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='dateofbirth', # Date of Birth
                text_action=lambda:[
                    tap_location(serial, 47, 236), # กดเลือกวันเกิด
                    time.sleep(2),
                    tap_location(serial, 480, 480), # ปุ่ม Done ตรง Modal
                    time.sleep(2),
                    tap_location(serial, 864, 509), # ปุ่ม Next ขวาล่าง
                    time.sleep(1.5),
                    tap_location(serial, 603, 346) # ปุ่ม Confirm ตรง Modal
                ],
                text_crop_area=(47, 236, 182, 272), # พื้นที่คำว่า Date of Birth
                extract_mode = 'name'
            )

            # sub stage 4
            ui_queue.put(('substage', serial, 'sub stage 4 : หน้าหลัก'))
            loop_confirm_wait_for(
                target_file='konam', # Konami
                text_action=lambda:[
                    tap_location(serial, 460, 422), # กดเริ่มต้นหน้าหลัก
                ],
                text_crop_area=(628, 480, 688, 507), # พื้นที่คำว่า Konami
            )

            # sub stage 5
            ui_queue.put(('substage', serial, 'sub stage 5 : Terms of Use'))
            loop_confirm_wait_for(
                target_file='term', # Terms of Use
                text_action=lambda:[
                    tap_location(serial, 110, 402), # กด I agree to the above
                    time.sleep(1),
                    tap_location(serial, 672, 480), # กด accept
                ],
                text_crop_area=(107, 28, 358, 74), # พื้นที่คำว่า Terms of Use
            )

            ui_queue.put(('substage', serial, 'sub stage 5 : Privacy Policy'))
            loop_confirm_wait_for(
                target_file='privacy', # Privacy Policy
                sub_target_file='prlvacy', # Privacy Policy
                text_action=lambda:[
                    tap_location(serial, 110, 402), # กด I agree to the above
                    time.sleep(1),
                    tap_location(serial, 672, 480), # กด accept
                ],
                text_crop_area=(107, 28, 374, 74), # พื้นที่คำว่า Privacy Policy
            )

            # ui_queue.put(('substage', serial, 'sub stage 5 : Were you familiar'))
            # loop_confirm_wait_for(
            #     target_file='were', # Were you familiar
            #     text_action=lambda:[
            #         tap_location(serial, 440, 200), # กดเลือกคำตอบ 1
            #         time.sleep(1),
            #         tap_location(serial, 480, 480), # กด Continue 1
            #     ],
            #     text_crop_area=(107, 28, 374, 74), # พื้นที่คำว่า Were you familiar
            # )

            # loop_confirm_wait_for(
            #     target_file='what', # Were you familiar
            #     text_action=lambda:[
            #         tap_location(serial, 440, 200), # กดเลือกคำตอบ 2
            #         time.sleep(1),
            #         tap_location(serial, 480, 480), # กด Continue 2
            #     ],
            #     text_crop_area=(107, 28, 374, 74), # พื้นที่คำว่า Were you familiar
            # )

            # loop_confirm_wait_for(
            #     target_file='have', # Were you familiar
            #     text_action=lambda:[
            #         tap_location(serial, 440, 200), # กดเลือกคำตอบ 3
            #         time.sleep(1),
            #         tap_location(serial, 480, 480), # กด Continue 3
            #     ],
            #     text_crop_area=(107, 28, 374, 74), # พื้นที่คำว่า Were you familiar
            # )

            ui_queue.put(('substage', serial, 'sub stage 5 : Confirm Username'))
            loop_confirm_wait_for(
                target_file='confirm', # Confirm Username
                sub_target_file='conflrm', # Confirm Username
                text_action=lambda:[
                    tap_location(serial, 480, 480), # กด Continue 4 Confirm Username
                ],
                text_crop_area=(107, 28, 374, 74), # พื้นที่คำว่า Confirm Username
            )

        elif stage == 2:
            pre_stage()

            # sub stage 1
            ui_queue.put(('substage', serial, 'sub stage 1 : Download Latest Data'))
            loop_confirm_wait_for(
                target_file='download', # Download
                text_action=lambda:[
                    tap_location(serial, 608, 356), # กด Download
                ],
                text_crop_area=(358, 157, 600, 188), # พื้นที่คำว่า Download Latest Data?
            )
            
            loop_check_unable_download()

            # # sub stage 2
            # ui_queue.put(('substage', serial, 'sub stage 2 : ฝึกช่วงแรก'))
            # loop_confirm_wait_for(
            #     target_file='take', # take
            #     text_action=lambda:[
            #         swipe_down(serial, 855, 361, 855, 200), # เลื่อนขึ้น
            #     ],                
            #     text_crop_area=(282, 474, 680, 513), # พื้นที่คำว่า take
            # )

            # sub stage 3
            ui_queue.put(('substage', serial, 'sub stage 3 : Welcome to eFootball'))
            loop_confirm_wait_for(
                target_file='follow', # follow the tutorial to learn how to play
                text_action=lambda:[
                    tap_location(serial, 730, 417), # กด Continue
                ],
                text_crop_area=(567, 260, 637, 290), # พื้นที่คำว่า follow
            )

        elif stage == 3:
            pre_stage()

            # sub stage 1
            ui_queue.put(('substage', serial, 'sub stage 1 : Select your favorite team'))
            loop_confirm_wait_for(
                target_file='select', # Select your favorite team
                text_action=lambda:[
                    tap_location(serial, 480, 408), # กด Ok
                    time.sleep(1.7),
                    tap_location(serial, 650, 408), # กด Start
                ],
                text_crop_area=(340, 270, 617, 302), # พื้นที่คำว่า Select your favorite team
            )
            
            ui_queue.put(('substage', serial, 'sub stage 1 : Select your favorite team 2'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='base', # Base team
                text_action=lambda:[
                    tap_location(serial, 480, 408)
                ],
                text_crop_area=(363, 275, 597, 312), # พื้นที่คำว่า Base team
                extract_mode = 'name'
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Select your favorite team 3'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='continue', # Continue
                sub_target_file='contlnue', # Continue
                text_action=lambda:[
                    time.sleep(1),
                    loop_close_promo(True)
                ],
                text_crop_area=(426, 409, 533, 441), # พื้นที่คำว่า Continue
                extract_mode = 'name'
            )

            # sub stage 2
            ui_queue.put(('substage', serial, 'sub stage 2 : หน้า main'))
            loop_confirm_wait_for(
                target_file='contract', # Contracts
                text_action=lambda:[
                    tap_location(serial, 475, 482), # กด เข้าหน้าเลือกสุ่ม
                ],
                text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='back', # Back
                text_action=lambda:[
                    tap_location(serial, 180, 255), # กด เข้าหน้าตู้สุ่ม
                ],
                text_crop_area=(60, 492, 124, 523), # พื้นที่คำว่า Back
                extract_mode = 'name'
            )

            ui_queue.put(('substage', serial, 'เช็ค Show Ad'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='show', 
                text_action=lambda:[tap_location(serial, 561, 377)], 
                text_crop_area=(375, 144, 585, 176), # Show Ad | zone 8
                extract_mode = 'name',
            ),

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='contract', # Contracts
                text_action=lambda:[
                    tap_location(serial, 543, 411), # กด free
                ],
                text_crop_area=(366, 490, 591, 528), # พื้นที่คำว่า Contracts
                extract_mode = 'name'
            )

            loop_confirm_wait_for(
                target_file='payment', # Contracts
                text_action=lambda:[
                    tap_location(serial, 606, 330), # กด 
                ],
                text_crop_area=(354, 180, 606, 221), # พื้นที่คำว่า Contracts
            )

            # sub stage 1
            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have Been Locked'))
            loop_confirm_wait_for(
                target_file='player', # Players Have Been Locked
                text_action=lambda:[
                    tap_location(serial, 480, 340), # กด Ok
                ],
                text_crop_area=(335, 167, 627, 207), # พื้นที่คำว่า Players Have Been Locked
            )

            loop_confirm_wait_for(
                target_file='continue', # Players Have Been Locked
                sub_target_file='contlnue', # Continue
                text_action=lambda:[
                    tap_location(serial, 864, 509), # ปุ่ม Continue ขวาล่าง
                ],
                text_crop_area=(796, 491, 899, 522), # พื้นที่คำว่า Players Have Been Locked
            )

            loop_confirm_wait_for(
                target_file='signed',
                sub_target_file='slgned', 
                text_action=lambda:[
                    tap_location(serial, 480, 413), # ปุ่ม Ok 1
                ],
                text_crop_area=(395, 273, 569, 310), # พื้นที่คำว่า Players Have Been Locked
            )

            loop_confirm_wait_for(
                target_file='team',
                text_action=lambda:[
                    tap_location(serial, 480, 413), # ปุ่ม Ok 2
                ],
                text_crop_area=(351, 269, 600, 309), # พื้นที่คำว่า Players Have Been Locked
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='back', # Back
                text_action=lambda:[
                    tap_location(serial, 118, 507), # ปุ่ม Back 1
                ],
                text_crop_area=(60, 492, 124, 523), # พื้นที่คำว่า Back
                extract_mode = 'name'
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='dev',
                text_action=lambda:[
                    tap_location(serial, 118, 507), # ปุ่ม Back 2
                ],
                text_crop_area=(283, 491, 389, 524), # พื้นที่คำว่า Players Have Been Locked
                extract_mode = 'name'
            )
            
            # sub stage 1
            ui_queue.put(('substage', serial, 'sub stage 1 : หน้า main'))
            loop_confirm_wait_for(
                target_file='contract', # Contracts
                text_action=lambda:[
                    tap_location(serial, 300, 440),
                ],
                text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 1'))
            loop_confirm_wait_for(
                target_file='dev',
                text_action=lambda:[
                    tap_location(serial, 180, 255),
                ],
                text_crop_area=(283, 491, 389, 524), # พื้นที่คำว่า Players Have Been Locked
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 2'))
            loop_confirm_wait_for(
                target_file='strength',
                text_action=lambda:[
                    tap_location(serial, 84, 410),
                ],
                text_crop_area=(830, 83, 918, 112), # พื้นที่คำว่า Players Have Been Locked
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 3'))
            loop_confirm_wait_for(
                target_file='work',
                text_action=lambda:[
                    tap_location(serial, 490, 414), 
                    time.sleep(2),
                    loop_select_player()
                ],
                text_crop_area=(355, 271, 627, 307), # พื้นที่คำว่า Players Have Been Locked
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 4'))
            # sub stage 2
            loop_confirm_wait_for(
                target_file='deploy', # deploy
                text_action=lambda:[
                    tap_location(serial, 480, 433), 
                ],
                text_crop_area=(377, 261, 579, 297), # พื้นที่คำว่า deploy
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 5'))
            loop_confirm_wait_for(
                target_file='head', # deploy
                text_action=lambda:[
                    tap_location(serial, 480, 433),
                ],
                text_crop_area=(283, 256, 422, 289), # พื้นที่คำว่า deploy
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 6'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='back',
                text_action=lambda:[
                    tap_location(serial, 118, 507), # ปุ่ม Back 2
                ],
                text_crop_area=(60, 492, 124, 523), # พื้นที่คำว่า Players Have Been Locked
                extract_mode = 'name'
            )
            

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 7'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='dev',
                text_action=lambda:[
                    tap_location(serial, 118, 507), # ปุ่ม Back 2
                ],
                text_crop_area=(283, 491, 389, 524), # พื้นที่คำว่า Players Have Been Locked
                extract_mode = 'name'
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 8'))
            # sub stage 4
            loop_confirm_wait_for(
                target_file='contract', # Contracts
                text_action=lambda:[
                    tap_location(serial, 593, 74),
                    time.sleep(2),
                    tap_location(serial, 195, 300), # กด 
                ],
                text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 8'))
            loop_confirm_wait_for(
                target_file='back', # Contracts
                text_action=lambda:[
                    tap_location(serial, 195, 300),
                ],
                text_crop_area=(60, 492, 124, 523), # พื้นที่คำว่า Contracts
            )

            ui_queue.put(('substage', serial, 'sub stage 1 : Players Have 8'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='select', # Contracts
                text_action=lambda:[
                    tap_location(serial, 516, 328), # กด 
                    time.sleep(2),
                    tap_location(serial, 440, 328), # กด 
                ],
                text_crop_area=(365, 183, 592, 219), # พื้นที่คำว่า Contracts
                extract_mode = 'name'
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='select', # Contracts
                text_action=lambda:[
                    tap_location(serial, 516, 328), # กด 
                ],
                text_crop_area=(380, 170, 580, 200), # พื้นที่คำว่า Contracts
                extract_mode = 'name'
            )

        elif stage == 4:
            pre_stage()

            # sub stage 5
            ui_queue.put(('substage', serial, 'sub stage 5 : ฝึกด่าน 1'))

            loop_skip_tutorial(text_crop_area=(320, 140, 645, 177), x=480, y=373)

            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 373),
                ],
                text_crop_area=(320, 140, 645, 177), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_one('s')

            loop_skip_tutorial(text_crop_area=(320, 140, 645, 177), x=480, y=373)

            ui_queue.put(('substage', serial, 'sub stage 5 : ฝึกด่าน 1'))
            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 373), # กด 
                ],
                text_crop_area=(320, 140, 645, 177), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_one('p')

            loop_confirm_wait_for(
                target_file='item', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 426),
                    time.sleep(2),
                    tap_location(serial, 800, 265),
                ],
                text_crop_area=(363, 254, 600, 296), # พื้นที่คำว่า Contracts
            )

        elif stage == 5:
            pre_stage()

            ui_queue.put(('substage', serial, 'sub stage 5 : ฝึกด่าน 2'))

            loop_skip_tutorial(text_crop_area=(316, 166, 640, 203), x=480, y=350)
            
            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 350), # กด
                ],
                text_crop_area=(316, 166, 640, 203), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_two('s')

            loop_skip_tutorial(text_crop_area=(316, 166, 640, 203), x=480, y=350)

            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 350), # กด 
                ],
                text_crop_area=(316, 166, 640, 203), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_two('p')

            loop_confirm_wait_for(
                target_file='item', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 426),
                ],
                text_crop_area=(363, 254, 600, 296), # พื้นที่คำว่า Contracts
            )

            loop_confirm_wait_for(
                target_file='continue', # Take on the Skill Up Challenge
                sub_target_file='contlnue', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 213, 426)
                ],
                text_crop_area=(165, 398, 277, 436), # พื้นที่คำว่า Contracts
            )

            loop_confirm_wait_for(
                target_file='complete', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 342, 421),
                    time.sleep(2),
                    tap_location(serial, 833, 266),
                ],
                text_crop_area=(293, 294, 511, 333), # พื้นที่คำว่า Contracts
            )

        elif stage == 6:
            pre_stage()

            ui_queue.put(('substage', serial, 'sub stage 6 : ฝึกด่าน 3'))

            loop_skip_tutorial(text_crop_area=(316, 144, 640, 180), x=480, y=350)

            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 350), # กด 
                ],
                text_crop_area=(316, 144, 640, 180), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_three('s')

            loop_skip_tutorial(text_crop_area=(316, 146, 640, 179), x=480, y=360)

            loop_confirm_wait_for(
                target_file='take', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 360), # กด 
                ],
                text_crop_area=(316, 146, 640, 179), # พื้นที่คำว่า Contracts
            )

            loop_tutorial_three('p')

            loop_confirm_wait_for(
                target_file='item', # Take on the Skill Up Challenge
                text_action=lambda:[
                    tap_location(serial, 480, 426),
                    time.sleep(1.5),
                    tap_location(serial, 118, 507), # ปุ่ม Back 1
                    loop_close_promo(),
                ],
                text_crop_area=(363, 254, 600, 296), # พื้นที่คำว่า Contracts
            )

            loop_confirm_wait_for(
                target_file='shop', # Contracts
                text_action=lambda:[
                    tap_location(serial, 755, 34),
                ],
                text_crop_area=(337, 60, 404, 93), # พื้นที่คำว่า Contracts
            )

            loop_confirm_wait_for(
                target_file='receive', # Receive
                sub_target_file='recelve', # Receive
                text_action=lambda:[
                    ui_queue.put(('substage', serial, 'เช็ค Show Ad')),
                    time.sleep(1.5),
                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='show', 
                        text_action=lambda:[tap_location(serial, 561, 377)], 
                        text_crop_area=(375, 144, 585, 176), # Show Ad | zone 8
                        extract_mode = 'name',
                        is_loop=False
                    ),
                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='retry', 
                        text_action=lambda:[tap_location(serial, 643, 364)], 
                        text_crop_area=(568, 324, 643, 364),
                        extract_mode = 'name',
                        is_loop=False
                    ),
                    time.sleep(1),
                    tap_location(serial, 464, 500), # กด Receive All
                    time.sleep(1),
                    tap_location(serial, 480, 400), # กด OK รับของขวัญ 1
                    time.sleep(1),
                    tap_location(serial, 480, 400), # กด OK รับของขวัญ 2
                    time.sleep(1),
                    tap_location(serial, 480, 400), # กด OK รับของขวัญ 3
                    time.sleep(1),
                    tap_location(serial, 480, 400), # กด OK รับของขวัญ 4
                    time.sleep(1),
                    tap_location(serial, 480, 400), # กด OK รับของขวัญ 5
                    time.sleep(1),
                    tap_location(serial, 118, 507) # ปุ่ม Back
                ],
                text_crop_area=(420, 492, 540, 525), # พื้นที่คำว่า Receive All
            )

        elif stage == 7:
            pre_stage()

            is_random = main_configs.get('is_random')

            fined_gacha_name = []
            date = int(time.time() * 1000)

            if is_random:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='home', # Contracts
                    text_action=lambda:[
                        tap_location(serial, 188, 93), # กด
                    ],
                    text_crop_area=(117, 60, 188, 93), # พื้นที่คำว่า Contracts
                    extract_mode = 'name'
                )

                fined_gacha_name.extend(random_main(date))

            file_name = 'New Samak'

            if len(fined_gacha_name) > 0:
                fined_gacha_name.sort()
                file_name = '-'.join(fined_gacha_name)

            file_transfer(serial, file_name, 1, date)

            time.sleep(1)
                    
            ui_queue.put(('reset', serial, None))

    def random_main(num_name) -> list:
        fined_gacha_name = []

        ui_queue.put(('substage', serial, 'หน้า main'))
        loop_confirm_wait_for(
            target_file='contract',
            text_action=lambda:[
                tap_location(serial, 480, 440), # กด Contracts
            ],
            text_crop_area=(438, 497, 520, 520), # Contracts | zone 8
        )

        ui_queue.put(('substage', serial, 'หน้าเลือกสุ่ม'))
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='back', 
            text_action=lambda:[
                tap_location(serial, 180, 255), # กด เข้าหน้าเลือกตู้สุ่ม
            ], 
            text_crop_area=(60, 490, 130, 521), # Nominating Contracts | zone 8
            extract_mode = 'name'
        )

        ui_queue.put(('substage', serial, 'หน้าสุ่ม'))
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='contract', 
            text_action=lambda:[], 
            text_crop_area=(494, 490, 591, 521), # Nominating Contracts | zone 8
            extract_mode = 'name'
        )

        ui_queue.put(('substage', serial, 'เช็ค Show Ad'))
        time.sleep(1.5)
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='show', 
            text_action=lambda:[tap_location(serial, 561, 377)], 
            text_crop_area=(375, 144, 585, 176), # Show Ad | zone 8
            extract_mode = 'name',
            is_loop=False
        )
        time.sleep(1.5)

        gacha_slot_list = main_configs.get('gacha_slot_list', []) if main_configs.get('gacha_slot_list') else None
        count_gacha = len(gacha_slot_list) if gacha_slot_list else int(main_configs.get('count_gacha', 3))

        is_free = False

        for i in range(1, count_gacha + 1):
            logger.info(f"{num_name} - random_main: round {i}/{count_gacha} START")  # ← เพิ่ม
            ui_queue.put(('substage', serial, f'ดอง 3 : กาชา รอบที่ {i}'))

            current_slot = gacha_slot_list[i-1] if gacha_slot_list and i-1 < len(gacha_slot_list) else None
            prev_slot = gacha_slot_list[i-2] if gacha_slot_list and i-2 < len(gacha_slot_list) else 0
            mode = 'main' if count_gacha == 1 else 'multi'
            mode = 'select' if current_slot else mode
            index = current_slot if mode == 'select' else i

            if not is_free :
                loop_select_gacha_slot(serial, mode=mode, index=index, is_minus_slot=False, prev_index=prev_slot )
            
            if index == 2 and is_free :
                is_free = False

            is_first_btn = False

            is_run_normal_soom = True
            
            def set_is_run_normal_soom():
                nonlocal is_run_normal_soom
                is_run_normal_soom = False

            def set_first_btn():
                nonlocal is_first_btn
                is_first_btn = True

            def set_is_free():
                nonlocal is_free
                is_free = True

            if index == 1:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='free', # Free
                    text_action=lambda:[set_is_free()],
                    text_crop_area=(506, 390, 561, 425),
                    extract_mode = 'name',
                    is_loop=False
                )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='100', # 100
                sub_target_file='1', # 50
                text_action=lambda:[set_is_run_normal_soom(), set_first_btn()],
                text_crop_area=(217, 390, 262, 425), # พื้นที่คำว่า Contracts
                extract_mode = 'name',
                is_loop=False
            )
            
            if is_run_normal_soom:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='100', # 100
                    sub_target_file='1', # 50
                    text_action=lambda:[set_first_btn()],
                    text_crop_area=(331, 390, 374, 425), # พื้นที่คำว่า Contracts
                    extract_mode = 'name',
                    is_loop=False
                )

            if is_first_btn:
                logger.info(f"{num_name} - random_main: calling loop_push_gacha (224, 410)")  # ← เพิ่ม
                gacha_name, is_limit = loop_push_gacha(serial, 224, 410, num_name)
                logger.info(f"{num_name} - random_main: loop_push_gacha returned: {gacha_name}")  # ← เพิ่ม
                fined_gacha_name.extend(gacha_name)
            else:
                logger.info(f"{num_name} - random_main: calling loop_push_gacha (498, 410)")  # ← เพิ่ม
                gacha_name, is_limit = loop_push_gacha(serial, 498, 410, num_name)
                logger.info(f"{num_name} - random_main: loop_push_gacha returned: {gacha_name}")  # ← เพิ่ม
                fined_gacha_name.extend(gacha_name)
            
            logger.info(f"{num_name} - random_main: round {i} END, is_limit={is_limit}")  # ← เพิ่ม

            if is_limit:
                is_first_btn = False
                esc_key(serial)
                
                time.sleep(2)
                
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='100', # 100
                    sub_target_file='1', # 50
                    text_action=lambda:[set_first_btn()],
                    text_crop_area=(331, 390, 374, 425), # พื้นที่คำว่า Contracts
                    extract_mode = 'name',
                    is_loop=False
                )

                if is_first_btn:
                    gacha_name, is_limit = loop_push_gacha(serial, 298, 410, num_name)
                    fined_gacha_name.extend(gacha_name)
                else:
                    gacha_name, is_limit = loop_push_gacha(serial, 498, 410, num_name)
                    fined_gacha_name.extend(gacha_name)
        
        logger.info(f"{num_name} - random_main: RETURNING fined_gacha_name={fined_gacha_name}")  # ← เพิ่ม
        return fined_gacha_name

    @log_exception_to_json
    def normal_mode(current_stage):

        def last_func():
            # After 5 iterations, reset to Stage 1 9999 
            update_stage(serial, 1)

        try:

            for stage in range(current_stage, workflow_count + 1):
                #print('stage', stage) 000
                ui_queue.put(('stage', serial, stage))
                main_loop_normal(serial, stage, last_func)

            time.sleep(1)
            current_stage = 1

        except Exception as e:
            print(f'{serial}: Unexpected exception in launch_app_loop: {e}')
            # รอแล้ว retry รอบใหม่
        time.sleep(1)
    
    @log_exception_to_json
    def next_folder(serial, folder_list, index_folder, folder_path):
        """คืนค่า (index_folder, current_folder, re_reroll_folder, sorted_files) ของโฟลเดอร์ถัดไป"""
        index_folder = (index_folder + 1) % len(folder_list)
        if index_folder == 0:
            return None, None, None, None  # หมุนครบทุกโฟลเดอร์แล้ว
        current_folder = folder_list[index_folder]
        re_reroll_folder = os.path.join(folder_path, current_folder)
        sorted_files = [f for f in os.listdir(re_reroll_folder) if os.path.isfile(os.path.join(re_reroll_folder, f))]
        sorted_files.sort()
        # ถ้าไม่มีไฟล์ ให้ลบโฟลเดอร์นี้
        if not sorted_files:
            try:
                if os.path.isdir(re_reroll_folder):
                    bin_folder = os.path.join(folder_path, 'bin')
                    os.makedirs(bin_folder, exist_ok=True)

                    shutil.move(re_reroll_folder, bin_folder)
                    #print(f"{serial}: Removed empty folder {re_reroll_folder}")
            except Exception as e:
                print(f"{serial}: Failed to remove folder {re_reroll_folder}: {e}")
            return next_folder(serial, folder_list, index_folder, folder_path)  # ขยับไปโฟลเดอร์ถัดไปทันที
        return index_folder, current_folder, re_reroll_folder, sorted_files
    
    def loop_close_promo(breack_check_promo=False):
        is_break = False
        login = 0
        is_break_check_promo = breack_check_promo

        def set_break():
            nonlocal is_break
            is_break = True

        def set_break_check_promo():
            nonlocal is_break_check_promo
            is_break_check_promo = True

        while True:
            # print(f"is_break : {is_break}")

            if is_break:
                break

            def pp(se=1):
                nonlocal login

                screen_path = capture_screen(serial)
                count, result_img = count_checkmarks_in_image(screen_path)

                if se == 1:
                    login = count

                print(f"พบติ๊กถูก {count} อัน")

                # บันทึกภาพผลลัพธ์
                name = os.path.basename(screen_path)
                out_path = os.path.join("outputs", f"{se}_result_{name}")
                
                # สร้างโฟลเดอร์ outputs ถ้าไม่มี
                os.makedirs("outputs", exist_ok=True)
                
                # บันทึกภาพไปยัง outputs
                cv2.imwrite(out_path, result_img)
                print(f"บันทึกภาพไปที่: {out_path}")
            
            ui_queue.put(('substage', serial, 'เช็ค Promo'))
            if not is_break_check_promo:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='contract', # Contracts
                    text_action=lambda:[
                        time.sleep(2),
                        wait_for(
                            serial=serial,
                            detection_type='text',
                            target_file='continue', # continue
                            text_action=lambda:[
                                pp(1),
                                esc_key(serial)
                            ],
                            text_crop_area=(430, 405, 531, 496), # พื้นที่คำว่า Contracts
                            extract_mode = 'name',
                            is_loop=False
                        )
                    ],
                    text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
                    extract_mode = 'name',
                    is_loop=False,
                    pre_action=lambda:[tap_location(serial, 936, 52)]
                )
            ui_queue.put(('substage', serial, 'เช็ค Promo 2'))

            set_break_check_promo()

            esc_key(serial)
            esc_key(serial)
            esc_key(serial)
            esc_key(serial)
            esc_key(serial)
            
            time.sleep(1)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='quit', # quit eFootball
                sub_target_file='enable',
                text_action=lambda:[
                    set_break(),
                    esc_key(serial)
                ],
                text_crop_area=(261, 183, 706, 220), # พื้นที่คำว่า quit eFootball
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                break
        
        return login, 0
    
    def loop_select_gacha_slot(serial, mode='main', index=0, is_minus_slot=False, prev_index=0):
        gacha_slot = 1
        swip_start = (628, 252)
        swip_end = (90, 252)

        if mode == 'main':
            gacha_slot = main_configs.get('gacha_slot', 1)
        elif mode == 'free':
            gacha_slot = main_configs.get('select_gacha_slot', 1)
        elif mode == 'multi':
            gacha_slot = min(index, 2)
        elif mode == 'select':
            gacha_slot = index - prev_index + 1 if prev_index and index > prev_index else index

        if is_minus_slot:
            gacha_slot = gacha_slot - main_configs.get('gacha_slot', 1) + 1

        if gacha_slot <= 1:
            return

        for i in range(gacha_slot - 1):
            swipe_down(serial, swip_start[0],swip_start[1], swip_end[0], swip_end[1], duration_ms=5500)
            swipe_down(serial, swip_end[0], swip_end[1], swip_end[0], 300, duration_ms=1000)
            time.sleep(2)
        
    def loop_push_gacha(serial, x, y, num_name):
        is_break = False
        fined_gacha_name = []
        last_gacha_time = 0  # Frame rate limiting: ป้องกัน spinning loop
        
        is_limit = False
        loop_count = 0
        
        logger.info(f"{num_name} - loop_push_gacha: START (x={x}, y={y})")  # ← เพิ่ม

        def set_break():
            nonlocal is_break
            is_break = True

        while True:
            logger.info(f"{num_name} - loop_push_gacha: loop iteration {loop_count+1} (is_break={is_break})")  # ← เพิ่ม

            if is_break:
                logger.info(f"{num_name} - loop_push_gacha: is_break=True, breaking")  # ← เพิ่ม
                break

            # Frame rate limiting: ≥ 3 วินาที ต่อครั้ง เพื่อลด CPU spinning
            current_time = time.time()
            if current_time - last_gacha_time < 3:
                time.sleep(0.5)
                continue
            last_gacha_time = current_time

            time.sleep(1)

            tap_location(serial, x, y)
            logger.info(f"{num_name} - loop_push_gacha: tapped location ({x}, {y})")  # ← เพิ่ม

            logger.info(f"{num_name} - loop_push_gacha: waiting for 'unable'")  # ← เพิ่ม
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='unable', 
                text_action=lambda:[
                    esc_key(serial),
                    set_break(),
                ],
                text_crop_area=(400, 180, 566, 222), # พื้นที่คำว่า Gacha Result
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                logger.info(f"{num_name} - loop_push_gacha: unable found, breaking")  # ← เพิ่ม
                break

            def handle_capture(check_text_crop_area):
                nonlocal num_name
                screen_path = capture_screen(serial)
                tesseract_result = extract_text_tesseract(serial, ui_queue, screen_path, check_text_crop_area, 'name')
                ranger_name = tesseract_result['best_text']
                capture_gacha_screen(serial, f'{ranger_name}', num_name, 'Temp_All_Gacha')

            def check_name():
                nonlocal fined_gacha_name, num_name

                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='player', 
                    text_action=lambda:[],
                    text_crop_area=(523, 93, 676, 133),
                    extract_mode = 'name'
                )

                print('------- check_name ---------')
                folder_temp_name = 'Temp_Gacha'
                check_text_crop_area = (100, 27, 476, 68)
                fined_gacha_name.extend(check_ranger_name(serial, ui_queue, folder_temp_name, check_text_crop_area=check_text_crop_area, random_target='carector', date=num_name))
                # todo: เช็คสุ่ม
                handle_capture(check_text_crop_area)
            
            def loop_skip():
                is_break_loop_skip = False

                def set_break_loop_skip():
                    nonlocal is_break_loop_skip
                    is_break_loop_skip = True

                while True:
                    if is_break_loop_skip:
                        break

                    tap_location(serial, 615, 347)
                    tap_location(serial, 615, 368)

                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='skip',
                        sub_target_file='sklp', 
                        text_action=lambda:[
                            tap_location(serial, 900, 510),
                            set_break_loop_skip(),
                        ],
                        text_crop_area=(791, 489, 900, 527), # พื้นที่คำว่า Continue
                        extract_mode = 'name',
                        is_loop=False
                    )

            logger.info(f"{num_name} - loop_push_gacha: calling FIRST wait_for payment")  # ← เพิ่ม
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='payment',
                text_action=lambda:[
                    logger.info(f"{num_name} - loop_push_gacha: FIRST payment lambda - START"),  # ← เพิ่ม
                    time.sleep(1.5),
                    tap_location(serial, 600, 347),
                    tap_location(serial, 600, 368),
                    loop_skip(),
                    loop_confirm_wait_for(
                        target_file='select', 
                        text_action=lambda:[
                            wait_for(
                                serial=serial,
                                detection_type='text',
                                target_file='players', 
                                text_action=lambda:[
                                    tap_location(serial, 480, 344),
                                ],
                                text_crop_area=(338, 170, 625, 205), # modal players
                                extract_mode = 'name',
                                is_loop=False
                            ),
                            tap_location(serial, 480, 250), # กดเข้าดูรายละเอียดตัวละคร
                        ],
                        text_crop_area=(401, 488, 559, 522), # selection view
                    ),
                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='player',
                        text_action=lambda:[
                            time.sleep(1),
                            check_name(),
                            esc_key(serial),
                        ],
                        text_crop_area=(522, 92, 673, 131), # พื้นที่คำว่า Player Details
                        extract_mode = 'name'
                    ),

                    loop_confirm_wait_for(
                        target_file='select', 
                        text_action=lambda:[
                            tap_location(serial, 840, 506),
                        ], 
                        text_crop_area=(401, 488, 559, 522)
                    ),
                ],
                text_crop_area=(354, 80, 610, 222), # พื้นที่คำว่า Gacha Result
                extract_mode = 'name',
                is_loop=False
            )

            logger.info(f"{num_name} - loop_push_gacha: FIRST payment DONE, calling SECOND wait_for payment")  # ← เพิ่ม
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='payment',
                text_action=lambda:[
                    logger.info(f"{num_name} - loop_push_gacha: SECOND payment lambda - START"),  # ← เพิ่ม
                    tap_location(serial, 600, 347),
                    tap_location(serial, 600, 368),
                    loop_skip(),
                    loop_confirm_wait_for(
                        target_file='select', 
                        text_action=lambda:[
                            wait_for(
                                serial=serial,
                                detection_type='text',
                                target_file='players', 
                                text_action=lambda:[
                                    tap_location(serial, 480, 344),
                                ],
                                text_crop_area=(338, 170, 625, 205), # modal players
                                extract_mode = 'name',
                                is_loop=False
                            ),
                            tap_location(serial, 480, 250), # กดเข้าดูรายละเอียดตัวละคร
                        ],
                        text_crop_area=(401, 488, 559, 522), # selection view
                    ),
                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='player',
                        text_action=lambda:[
                            time.sleep(1),
                            check_name(),
                            esc_key(serial),
                        ],
                        text_crop_area=(522, 92, 673, 131), # พื้นที่คำว่า Player Details
                        extract_mode = 'name'
                    ),

                    loop_confirm_wait_for(
                        target_file='select', 
                        text_action=lambda:[
                            tap_location(serial, 840, 506),
                        ], 
                        text_crop_area=(401, 488, 559, 522)
                    ),
                    logger.info(f"{num_name} - loop_push_gacha: SECOND payment lambda - END"),  # ← เพิ่ม
                ],
                text_crop_area=(354, 185, 610, 222), # พื้นที่คำว่า Gacha Result
                extract_mode = 'name',
                is_loop=False
            )
            logger.info(f"{num_name} - loop_push_gacha: SECOND payment DONE")  # ← เพิ่ม
            
            loop_count += 1
            logger.info(f"{num_name} - loop_push_gacha: loop_count={loop_count}, incrementing")  # ← เพิ่ม
            
            if is_break:
                logger.info(f"{num_name} - loop_push_gacha: is_break=True after iteration, breaking")  # ← เพิ่ม
                break
            
            if loop_count >= 5:
                logger.info(f"{num_name} - loop_push_gacha: loop_count >= 5, setting is_limit=True")  # ← เพิ่ม
                is_limit = True
                break

        logger.info(f"{num_name} - loop_push_gacha: RETURNING fined_gacha_name count={len(fined_gacha_name)}, is_limit={is_limit}")  # ← เพิ่ม
        return fined_gacha_name, is_limit
    
    def check_ranger_name(serial, ui_queue: Queue, folder_temp_name, check_text_crop_area=None, random_target='carector', date='', is_free_player=False) -> list:
        global devices_configs

        screen_path = capture_screen(serial)
        image_dir='bin/carector_ref'

        print(f"{serial} - folder_temp_name : {folder_temp_name}")

        sorted_files = [f for f in os.listdir(image_dir) if os.path.isfile(os.path.join(image_dir, f))]
        sorted_files.sort()

        print(f"{serial} - sorted_files : {sorted_files}")

        tesseract_result = extract_text_tesseract(
            serial=serial, 
            ui_queue=ui_queue, 
            image_path=screen_path, 
            crop_area=check_text_crop_area, 
            extract_mode='name',
            random_target=random_target , 
            dictionary = sorted_files,
            target_file ='',
            save_roi=False
        )

        ranger_name = tesseract_result['best_text']

        print(f"{serial} - ranger_name : {ranger_name}")

        #print('ตรวจสอบตัวละคร: ' + ranger_name)
        ui_queue.put(('substage', serial, 'ตรวจสอบตัวละคร: ' + ranger_name))

        if is_free_player:
            return [ranger_name]

        temp_ref_name_list = []

        for ref_file in sorted_files:
            ref_name = ref_file.replace('-', '').rsplit('.', 1)[0].replace(' ', '_')
            if ref_name.lower() in ranger_name.lower():
                ui_queue.put(('substage', serial, 'ตรวจสอบพบ: ' + ref_name))

                # 💥 FIX: ถ้า configs[serial] ไม่ใช่ dict ก็ให้ override
                if not isinstance(devices_configs.get(serial), dict):
                    devices_configs[serial] = {}

                temp_ref_name_list.append(ref_name)

        if temp_ref_name_list:
            temp_ref_name_list = temp_ref_name_list = [name for name in temp_ref_name_list 
                        if not any(name != other and len(name) < len(other) and name in other 
                                for other in temp_ref_name_list)]
            capture_gacha_screen(serial, temp_ref_name_list[0], date, folder_temp_name)
            time.sleep(1)

        file_order = ['gear']
        def get_sort_key_partial(item):
            item_lower = item.lower()
            
            # เช็คว่ามีคำใน file_order อยู่ในชื่อมั้ย
            for i, order_name in enumerate(file_order):
                if order_name in item_lower:
                    return i
            
            return len(file_order)  # ไม่เจอให้ไปท้ายสุด

        sorted_result_v3 = sorted(temp_ref_name_list, key=get_sort_key_partial)
        #print("Sorted result v3:", sorted_result_v3)

        return temp_ref_name_list
    
    def claim_mission():
        is_comeback = main_configs.get('is_comeback')
        
        missions_count = 1
        is_break = False

        def set_break():
            nonlocal is_break
            is_break = True

        ui_queue.put(('substage', serial, 'ดอง : เข้าหน้า missions'))
        loop_confirm_wait_for(
            target_file='contract', # Contracts
            text_action=lambda:[
                tap_location(serial, 652, 453), # กด missions
            ],
            text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
        )

        def loop_check():

            is_break = False

            def set_break():
                nonlocal is_break
                is_break = True
            
            while True:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='check', 
                    text_action=lambda:[set_break()], 
                    text_crop_area=(736, 391, 808, 429), # Nominating Contracts | zone 8
                    extract_mode = 'name',
                    is_loop=False
                )

                if is_break:
                    break

                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='check', 
                    text_action=lambda:[set_break()], 
                    text_crop_area=(452, 391, 527, 429), # Nominating Contracts | zone 8
                    extract_mode = 'name',
                    is_loop=False
                )

                if is_break:
                    break

        while True:
            if missions_count > 2:
                esc_key(serial)
                break

            ui_queue.put(('substage', serial, f'missions {missions_count}'))
            loop_check()

            ui_queue.put(('substage', serial, f'missions {missions_count}: เช็คหน้าสุดท้าย'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='step', 
                text_action=lambda:[set_break()], 
                text_crop_area=(363, 294, 466, 336), # Nominating Contracts | zone 8
                extract_mode = 'name',
                is_loop=False
            )

            if is_break:
                esc_key(serial)
                break
            
            if missions_count == 2:
                ui_queue.put(('substage', serial, f'missions {missions_count}: กด Check'))
                tap_location(serial, 481, 413)

                ui_queue.put(('substage', serial, f'missions {missions_count}: หน้า reward'))
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='deta',
                    text_action=lambda:[], 
                    text_crop_area=(316, 491, 518, 521),
                    extract_mode='normal'
                )

                ui_queue.put(('substage', serial, f'missions {missions_count}: เช็คปุ่ม receive'))
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='receive',
                    sub_target_file='recelve',
                    text_action=lambda:[
                        tap_location(serial, 680, 511),
                        time.sleep(1),
                        tap_location(serial, 480, 340,),
                        tap_location(serial, 480, 360,),
                        tap_location(serial, 480, 380,),
                        tap_location(serial, 480, 400,),
                        tap_location(serial, 480, 420,),
                        tap_location(serial, 480, 440,),
                        tap_location(serial, 480, 340,),
                        tap_location(serial, 480, 360,),
                        tap_location(serial, 480, 380,),
                        tap_location(serial, 480, 400,),
                        tap_location(serial, 480, 420,),
                        tap_location(serial, 480, 440,),
                        esc_key(serial),
                    ], 
                    text_crop_area=(542, 489, 661, 524), # Nominating Contracts | zone 8
                    extract_mode = 'name',
                    is_loop=False
                )

                time.sleep(1)

                esc_key(serial)

                ui_queue.put(('substage', serial, f'missions {missions_count}: หน้า Check'))
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='check', 
                    text_action=lambda:[], 
                    text_crop_area=(452, 391, 527, 429), # Nominating Contracts | zone 8
                    extract_mode = 'name'
                )

            missions_count += 1

            if missions_count > 2:
                esc_key(serial)
                break

            ui_queue.put(('substage', serial, f'missions {missions_count}: เลื่อนหน้า missions'))
            swip_start = (628, 252)
            swip_end = (90, 252)

            swipe_down(serial, swip_start[0],swip_start[1], swip_end[0], swip_end[1], duration_ms=5500)
            swipe_down(serial, swip_end[0], swip_end[1], swip_end[0], 300, duration_ms=1000)
            time.sleep(2)

            # missions_count += 1
            

    @log_exception_to_json
    def start_re_reroll_mode(serial, ui_queue):
        global main_configs

        home_key(serial)

        close_pes(serial)
        # --- เตรียมข้อมูลโฟลเดอร์และไฟล์ ---
        folder_path = main_configs.get('re_reroll_file_path', '')
        # ✅ Normalize path to use proper backslashes on Windows
        folder_path = os.path.normpath(folder_path) if folder_path else ''
        #print(1)
        # ถ้า path นี้ไม่มีอยู่จริง
        print(f"path : {folder_path}")
        if not folder_path or not os.path.isdir(folder_path):
            print(f"{folder_path} path นี้ไม่มีอยู่จริง")
            logger.warning(f"[{serial}] Path does not exist, attempting to create: {folder_path}")
            os.makedirs(folder_path, exist_ok=True)
            
        try:
            sorted_folder_list = [f for f in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, f))]
            sorted_folder_list.sort()
        except FileNotFoundError as e:
            logger.error(f"[{serial}] Cannot access folder list - path not found: {folder_path}")
            ui_queue.put(('error', serial, f'Path not found: {folder_path}'))
            stop_device(serial)
            return (None, None, None, None)
        except Exception as e:
            logger.error(f"[{serial}] Error listing folders: {e}", exc_info=True)
            ui_queue.put(('error', serial, f'Error reading folder: {str(e)}'))
            stop_device(serial)
            return (None, None, None, None)
            
        print(f"sorted_folder_list[{len(sorted_folder_list)}]: {sorted_folder_list}")

        #print(2)
        # ถ้าไม่มี folder เหลืออยู่แล้ว
        if not sorted_folder_list:
            print(f'No folders found in {folder_path}')
            ui_queue.put(('completed', serial, 'completed'))
            stop_device(serial)
            return (None, None, None, None)

        prev_folder = reroll_state.get(serial, {}).get('current_folder')
        index_folder = sorted_folder_list.index(prev_folder) if prev_folder in sorted_folder_list else 0
        current_folder = sorted_folder_list[index_folder]
        re_reroll_folder = os.path.normpath(os.path.join(folder_path, current_folder))
        
        # ✅ Verify folder exists before listing files
        if not os.path.isdir(re_reroll_folder):
            logger.error(f"[{serial}] Selected folder does not exist: {re_reroll_folder}")
            ui_queue.put(('error', serial, f'Folder not found: {re_reroll_folder}'))
            stop_device(serial)
            return (None, None, None, None)

        try:
            sorted_files = [
                f for f in os.listdir(re_reroll_folder)
                if os.path.isfile(os.path.join(re_reroll_folder, f)) and f.lower().endswith('.dat')
            ]
            sorted_files.sort()
        except FileNotFoundError as e:
            logger.error(f"[{serial}] Cannot access folder: {re_reroll_folder}")
            ui_queue.put(('error', serial, f'Cannot access folder: {re_reroll_folder}'))
            stop_device(serial)
            return (None, None, None, None)
        except Exception as e:
            logger.error(f"[{serial}] Error listing files in {re_reroll_folder}: {e}", exc_info=True)
            ui_queue.put(('error', serial, f'Error reading files: {str(e)}'))
            stop_device(serial)
            return (None, None, None, None)

        print(f"sorted_files[{len(sorted_files)}]")

        if not sorted_files:
            try:
                if os.path.isdir(re_reroll_folder):
                    bin_folder = os.path.join(folder_path, 'bin')
                    os.makedirs(bin_folder, exist_ok=True)

                    shutil.move(re_reroll_folder, bin_folder)
                    #print(f"{serial}: Removed empty folder {re_reroll_folder}")
            except Exception as e:
                print(f"{serial}: Failed to remove folder {re_reroll_folder}: {e}")

        #print(3)
        prev_file = reroll_state.get(serial, {}).get('current_file')
        index_file = sorted_files.index(prev_file) + 1 if prev_file in sorted_files else 0

        # ===== CRITICAL SECTION: ใช้ Lock ครอบทั้งกระบวนการเลือกไฟล์ =====
        with shared_lock:
            #print(f'{serial}: Entering critical section for file selection')
            
            # ดึงข้อมูล on_stage ล่าสุด (ภายใน lock)
            on_stage_files = local_manager.get_all_filenames()
            #print(f'{serial}: Current on_stage_files: {on_stage_files}')

            while True:
                # เช็คว่าไฟล์ปัจจุบันซ้ำหรือไม่
                while index_file < len(sorted_files) and sorted_files[index_file] in on_stage_files:
                    #print(f'{serial}: File {sorted_files[index_file]} is already on stage, skipping...')
                    index_file += 1

                # ถ้าไฟล์หมดในโฟลเดอร์นี้
                if index_file >= len(sorted_files):
                    result = next_folder(serial, sorted_folder_list, index_folder, folder_path)
                    if result == (None, None, None, None):
                        #print(f'{serial}: No more folders/files available')
                        ui_queue.put(("completed", serial, "completed"))
                        stop_device(serial)
                        return []
                    
                    index_folder, current_folder, re_reroll_folder, sorted_files = result
                    index_file = 0
                    # อัปเดต on_stage_files หลังจากเปลี่ยนโฟลเดอร์
                    on_stage_files = local_manager.get_all_filenames()
                    continue
                
                # ถ้าเจอไฟล์ที่ใช้ได้ ให้ break
                break
                
            current_file = sorted_files[index_file]
            
            # เช็คอีกครั้งก่อน add (เผื่อมีการเปลี่ยนแปลงระหว่างทาง)
            on_stage_files = local_manager.get_all_filenames()
            if current_file in on_stage_files:
                #print(f'{serial}: File {current_file} was taken by another process, restarting selection...')
                # รีสตาร์ทฟังก์ชันใหม่ - indicate no selection was made
                return (None, None, None, None)
            
            # บันทึกลงใน runtime state (ไม่เก็บใน main_config.json)
            if serial not in reroll_state:
                reroll_state[serial] = {}
            reroll_state[serial]['current_folder'] = current_folder
            reroll_state[serial]['current_file'] = current_file
            
            # เพิ่มเข้า on_stage (ภายใน lock)
            local_manager.add_on_stage(serial, current_file)
            
            #print(f'{serial}: Successfully claimed file: {current_file}')
            #print(f'{serial}: Updated shared_data: {list(shared_data)}')
        
        # ===== จบ CRITICAL SECTION =====
        
        #print(f'Final assignment - Serial: {serial}, File: {current_file}, Folder: {current_folder}')

        f_name = current_file.split('_')[0] + ' ' + current_file.split('_')[1] + ' ' + current_file.split('_')[2]
        ui_queue.put(('file_name', serial, f_name))

        copy_file_to_main_file_path(serial, re_reroll_folder, current_file)

        num_name = current_file.rsplit('.', 1)[0].rsplit('_', 1)[-1]

        print('num_name',num_name)

        #print('re_reroll_mode')
        pre_stage()

        def pre_main_stage():
            is_break = False

            def set_break():
                nonlocal is_break
                is_break = True
             
            while True:
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='konam', # Contracts
                    text_action=lambda:[
                        tap_location(serial, 617, 356),
                        set_break()
                    ],
                    text_crop_area=(628, 480, 688, 507), # พื้นที่คำว่า Contracts
                    extract_mode = 'name',
                    is_loop=False
                )

                if is_break:
                    break

                tap_location(serial, 38, 423)

        ui_queue.put(('substage', serial, 'ดอง 1 : หน้าหลัก'))
        pre_main_stage()

        accumulat = 0

        def get_accumulated():
            nonlocal accumulat
            login, _ = loop_close_promo()
            if login > 0:
                accumulat = f'{login}'

        ui_queue.put(('substage', serial, 'ดอง 2 : ปิดโปรโมท'))
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='contract', # Contracts
            text_action=lambda:[
                get_accumulated()
            ],
            text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
            extract_mode = 'name',
            pre_action=lambda:[tap_location(serial, 936, 52)]
        )

        time.sleep(1)
        
        is_caim_missions = main_configs.get('is_caim_missions', False)

        if is_caim_missions:
            ui_queue.put(('substage', serial, 'ดอง 3 : รับภารกิจ'))
            claim_mission()

        loop_confirm_wait_for(
            target_file='contract', # Contracts
            text_action=lambda:[
                tap_location(serial, 755, 34), # กด กล่องของขวัญ
            ],
            text_crop_area=(438, 497, 520, 520), # พื้นที่คำว่า Contracts
        )

        loop_confirm_wait_for(
            target_file='receive', # Receive
            sub_target_file='recelve', # Receive
            text_action=lambda:[
                 ui_queue.put(('substage', serial, 'เช็ค Show Ad')),
                time.sleep(1.5),
                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='show', 
                    text_action=lambda:[tap_location(serial, 561, 377)], 
                    text_crop_area=(375, 144, 585, 176), # Show Ad | zone 8
                    extract_mode = 'name',
                    is_loop=False
                ),
                time.sleep(2),
                tap_location(serial, 464, 500), # กด Receive All
                time.sleep(2),
                tap_location(serial, 480, 400), # กด OK รับของขวัญ 1
                time.sleep(1),
                tap_location(serial, 480, 400), # กด OK รับของขวัญ 2
                time.sleep(1),
                tap_location(serial, 480, 400), # กด OK รับของขวัญ 3
                time.sleep(1),
                tap_location(serial, 480, 400), # กด OK รับของขวัญ 4
                time.sleep(1),
                tap_location(serial, 480, 400), # กด OK รับของขวัญ 5
                time.sleep(2),
                tap_location(serial, 118, 507) # ปุ่ม Back
            ],
            text_crop_area=(420, 492, 540, 525), # พื้นที่คำว่า Receive All
        )

        return current_file, current_folder, num_name, accumulat

    def select_free_players(num_name, is_random):

        if not is_random:
            ui_queue.put(('substage', serial, 'หน้า main'))
            loop_confirm_wait_for(
                target_file='contract',
                text_action=lambda:[
                    tap_location(serial, 480, 440), # กด Contracts
                ],
                text_crop_area=(438, 497, 520, 520), # Contracts | zone 8
            )

            ui_queue.put(('substage', serial, 'หน้าเลือกสุ่ม'))
            wait_for(
                serial=serial,
                detection_type='text',
                target_file='back', 
                text_action=lambda:[
                    tap_location(serial, 180, 255), # กด เข้าหน้าเลือกตู้สุ่ม
                ], 
                text_crop_area=(60, 490, 130, 521), # Nominating Contracts | zone 8
                extract_mode = 'name'
            )

        ui_queue.put(('substage', serial, 'หน้าสุ่ม'))
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='contract', 
            text_action=lambda:[], 
            text_crop_area=(494, 490, 591, 521), # Nominating Contracts | zone 8
            extract_mode = 'name'
        )

        loop_select_gacha_slot(serial, mode='free', index=0, is_minus_slot=is_random)

        time.sleep(1)

        is_selection = False

        def set_is_selection():
            nonlocal is_selection
            is_selection = True
        
        wait_for(
            serial=serial,
            detection_type='text',
            target_file='select', # select
            sub_target_file='seiect', # select
            text_action=lambda:[set_is_selection()],
            text_crop_area=(388, 395, 487, 424), # พื้นที่คำว่า select a player
            extract_mode = 'name',
            is_loop=False
        )    

        fined_gacha_name = []

        if is_selection:

            tap_location(serial, 498, 390)

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='select', # select
                text_action=lambda:[tap_location(serial, 480, 328)],
                text_crop_area=(261, 183, 706, 220), # พื้นที่คำว่า select a player
                extract_mode = 'name',
            )

            matrix_position = [
                [(244, 145), (400, 145) , (556, 145) , (712, 145)],
                [(244, 350), (400, 350) , (556, 350) , (712, 350)],
                [(244, 465), (400, 465) , (556, 465) , (712, 465)],
            ]

            free_gacha_slot = main_configs.get('free_gacha_slot')

            time.sleep(1)

            # Convert free_gacha_slot (1-12) to matrix position
            if free_gacha_slot and isinstance(free_gacha_slot, (int, str)):
                try:
                    slot_num = int(free_gacha_slot)
                    # Convert to 0-indexed
                    position_index = slot_num - 1
                    row = position_index // 4
                    col = position_index % 4
                    
                    if 0 <= row < len(matrix_position) and 0 <= col < len(matrix_position[row]):
                        x, y = matrix_position[row][col]
                        tap_location(serial, x, y)
                except (ValueError, TypeError):
                    print(f"Invalid free_gacha_slot value: {free_gacha_slot}")
            else:
                print(f"free_gacha_slot not configured properly")

            time.sleep(1.5)
            tap_location(serial, 840, 506)
            time.sleep(2)
            tap_location(serial, 480, 327)

            def handle_capture(check_text_crop_area):
                    nonlocal num_name
                    screen_path = capture_screen(serial)
                    tesseract_result = extract_text_tesseract(serial, ui_queue, screen_path, check_text_crop_area, 'name')
                    ranger_name = tesseract_result['best_text']
                    capture_gacha_screen(serial, f'{ranger_name}', num_name, 'Temp_All_Gacha')

            def check_name():
                nonlocal fined_gacha_name, num_name

                wait_for(
                    serial=serial,
                    detection_type='text',
                    target_file='player', 
                    text_action=lambda:[],
                    text_crop_area=(523, 93, 676, 133),
                    extract_mode = 'name'
                )

                print('------- check_name ---------')
                folder_temp_name = 'Temp_Gacha'
                check_text_crop_area = (100, 27, 476, 68)
                fined_gacha_name.extend(check_ranger_name(serial, ui_queue, folder_temp_name, check_text_crop_area=check_text_crop_area, random_target='carector', date=num_name, is_free_player=True))
                # todo: เช็คสุ่ม
                handle_capture(check_text_crop_area)
            
            def loop_skip():
                is_break_loop_skip = False

                def set_break_loop_skip():
                    nonlocal is_break_loop_skip
                    is_break_loop_skip = True

                while True:
                    if is_break_loop_skip:
                        break

                    tap_location(serial, 615, 347)
                    tap_location(serial, 615, 368)

                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='skip',
                        sub_target_file='sklp', 
                        text_action=lambda:[
                            tap_location(serial, 900, 510),
                            set_break_loop_skip(),
                        ],
                        text_crop_area=(791, 489, 900, 527), # พื้นที่คำว่า Continue
                        extract_mode = 'name',
                        is_loop=False
                    )

            loop_skip()

            loop_confirm_wait_for(
                target_file='select', 
                text_action=lambda:[
                    wait_for(
                        serial=serial,
                        detection_type='text',
                        target_file='players', 
                        text_action=lambda:[
                            tap_location(serial, 480, 344),
                        ],
                        text_crop_area=(338, 170, 625, 205), # modal players
                        extract_mode = 'name',
                        is_loop=False
                    ),
                    tap_location(serial, 480, 320), # กดเข้าดูรายละเอียดตัวละคร
                ],
                text_crop_area=(401, 488, 559, 522), # selection view
            )

            wait_for(
                serial=serial,
                detection_type='text',
                target_file='player',
                text_action=lambda:[
                    time.sleep(1),
                    check_name(),
                    esc_key(serial),
                ],
                text_crop_area=(522, 92, 673, 131), # พื้นที่คำว่า Player Details
                extract_mode = 'name'
            )

            loop_confirm_wait_for(
                target_file='select', 
                text_action=lambda:[
                    tap_location(serial, 840, 506),
                ], 
                text_crop_area=(401, 488, 559, 522)
            )

        return fined_gacha_name

    def dong_mode(serial, ui_queue):
        # Start re-reroll mode and get necessary data
        result = start_re_reroll_mode(serial, ui_queue)
        
        # Handle case where no files/folders are available
        if not result or len(result) != 4:
            return
        
        current_file, current_folder, num_name, accumulat = result
        
        # Handle gacha logic
        is_random = main_configs.get('is_random')
        is_free_player = main_configs.get('is_free_player')

        gold_coin = 0
        
        def gold_detection(serial):
            nonlocal gold_coin
            from collections import Counter
            
            while True:
                coin_list = []
                
                # เช็ค 3 ครั้ง
                for i in range(3):
                    while True:
                        screen_path = capture_screen(serial)
                                                
                        # ✅ Add null check for screenshot capture
                        if screen_path is None:
                            logger.error(f'{serial}-{current_file}: capture_screen failed in gold_detection (attempt {i+1})')
                            print(f"{serial}-{current_file} : dong - {(i)} - ❌ Screenshot capture failed")
                            time.sleep(1)
                            continue  # Retry capture
                        
                        text_crop_area = (72, 13, 122, 43) # พื้นที่คำว่า gold coin
                        extract_mode = 'number'
                        
                        tesseract_result = extract_text_tesseract(
                            serial=serial, 
                            ui_queue=ui_queue, 
                            image_path=screen_path, 
                            crop_area=text_crop_area, 
                            extract_mode=extract_mode,
                            random_target='carector' , 
                            dictionary = None,
                            target_file ='',
                            save_roi=False,
                            is_ignore_x=True
                        )

                        if 'error' in tesseract_result and tesseract_result['error'] == 'Failed to load screenshot':
                            print(f"{serial}-{current_file} : dong - {(i)} - Gold coin OCR result: {tesseract_result['error']}")
                            time.sleep(1)
                            continue  # ลองใหม่ถ้าโหลดภาพไม่สำเร็จ

                        break
                    print(f"{serial}-{current_file} : dong - {(i)} - Gold coin OCR result: {tesseract_result}")

                    if 'error' in tesseract_result:
                        print(f"{serial}-{current_file} : dong - {(i)} - ❌ GG")
                        coin_value = '0'
                    else:
                        coin_value = tesseract_result['original'].replace(' ', '').replace('\n', '').lower()
                    
                    coin_list.append(coin_value)
                    time.sleep(1)  # เพิ่มเวลารอระหว่างเช็ก
                
                # ตรวจสอบว่า 3 ตัวต่างกันหมด
                if len(set(coin_list)) == 3:
                    # ถ้าทั้ง 3 ตัวต่างกันหมด ให้วนทำใหม่
                    print(f"{serial}-{current_file} : dong - ค่า coin ไม่ตรงกัน: {coin_list} วนทำใหม่")
                    logger.error(f"{serial}-{current_file} : dong - ค่า coin ไม่ตรงกัน: {coin_list} วนทำใหม่")
                    continue
                
                # เอาค่าที่มีมากที่สุด
                counter = Counter(coin_list)
                gold_coin = counter.most_common(1)[0][0]
                print(f"{serial}-{current_file} : dong - ค่า coin: {coin_list} → ผลลัพธ์: {gold_coin}")
                logger.error(f"{serial}-{current_file} : dong - ค่า coin: {coin_list} → ผลลัพธ์: {gold_coin}")
                break

        # gold_detection(serial)

        print(f"{num_name} - ตรวจสอบ gold coin: {int(gold_coin)}, is_random: {is_random}, ss: {is_random and int(gold_coin) >= 100}")
        logger.info(f"{num_name} - ตรวจสอบ gold coin: {int(gold_coin)}, is_random: {is_random}, ss: {is_random and int(gold_coin) >= 100}")

        is_random_gg = is_random
        # is_random_gg = is_random and int(gold_coin) >= 100

        fined_gacha_name = []

        logger.info(f"{num_name} - is_random: {is_random_gg}")
        logger.info(f"{num_name} - is_random_gg: {is_random_gg}, type: {type(is_random_gg)}, bool check: {bool(is_random_gg)}")

        if is_random_gg:
            logger.info(f"{num_name} - Entering random_main with is_random={is_random_gg}")
            try:
                random_result = random_main(num_name)
                logger.info(f"{num_name} - random_main returned: {random_result}")
                fined_gacha_name.extend(random_result)
            except Exception as e:
                logger.error(f"{num_name} - Error in random_main: {e}", exc_info=True)
        # else:
        #     logger.info(f"{num_name} - Skipped random_main (is_random={is_random_gg})")

        if is_free_player:
            fined_gacha_name.extend(select_free_players(num_name, is_random_gg))
        
        print('accumulat 1: ', accumulat)
        handle_move_file(current_file, current_folder, fined_gacha_name, num_name, mode='normal', accumulat=accumulat)
        print('accumulat 3: ', accumulat)
        
        return fined_gacha_name
    
    def test_mode(serial, ui_queue):
        gold_coin = 0

        def gold_detection(serial):
            nonlocal gold_coin
            from collections import Counter
            
            while True:
                coin_list = []
                
                # เช็ค 3 ครั้ง
                for i in range(3):
                    while True:
                        screen_path = capture_screen(serial)

                        # ✅ Add null check for screenshot capture
                        if screen_path is None:
                            logger.error(f'{serial}: capture_screen failed in gold_detection (attempt {i+1})')
                            print(f"{serial} : test - {(i)} - ❌ Screenshot capture failed")
                            time.sleep(1)
                            continue  # Retry capture

                        text_crop_area = (72, 13, 122, 43) # พื้นที่คำว่า gold coin
                        extract_mode = 'number'
                        
                        tesseract_result = extract_text_tesseract(
                            serial=serial, 
                            ui_queue=ui_queue, 
                            image_path=screen_path, 
                            crop_area=text_crop_area, 
                            extract_mode=extract_mode,
                            random_target='carector' , 
                            dictionary = None,
                            target_file ='',
                            save_roi=False,
                            is_ignore_x=True
                        )

                        if 'error' in tesseract_result and tesseract_result['error'] == 'Failed to load screenshot':
                            print(f"{serial} : test - {(i)} - Gold coin OCR result: {tesseract_result['error']}")
                            time.sleep(1)
                            continue  # ลองใหม่ถ้าโหลดภาพไม่สำเร็จ

                        break
                    print(f"{serial} : test - {(i)} - Gold coin OCR result: {tesseract_result}")

                    if 'error' in tesseract_result:
                        print(f"{serial} : test - {(i)} - ❌ GG")
                        coin_value = '0'
                    else:
                        coin_value = tesseract_result['original'].replace(' ', '').replace('\n', '').lower()
                    
                    coin_list.append(coin_value)
                    time.sleep(1)  # เพิ่มเวลารอระหว่างเช็ก
                
                # ตรวจสอบว่า 3 ตัวต่างกันหมด
                if len(set(coin_list)) == 3:
                    # ถ้าทั้ง 3 ตัวต่างกันหมด ให้วนทำใหม่
                    print(f"{serial} : test - ค่า coin ไม่ตรงกัน: {coin_list} วนทำใหม่")
                    continue
                
                # เอาค่าที่มีมากที่สุด
                counter = Counter(coin_list)
                gold_coin = counter.most_common(1)[0][0]
                print(f"{serial} : test - ค่า coin: {coin_list} → ผลลัพธ์: {gold_coin}")
                logger.error(f"{serial} : test - ค่า coin: {coin_list} → ผลลัพธ์: {gold_coin}")
                break
        
        gold_detection(serial)

        # farm_mode(
        #     serial,
        #     ui_queue,
        #     start_farm_mode=start_re_reroll_mode,
        #     loop_confirm_wait_for=loop_confirm_wait_for,
        #     wait_for=wait_for,
        #     tap_location=tap_location,
        #     swipe_down=swipe_down,
        #     esc_key=esc_key,
        #     capture_screen=capture_screen,
        #     extract_text_tesseract=extract_text_tesseract,
        #     capture_gacha_screen=capture_gacha_screen,
        #     update_stage=update_stage,
        #     get_workflow=lambda: workflow,
        #     get_current_stage=get_current_stage,
        #     loop_close_promo=loop_close_promo,
        #     is_pes_visible=is_pes_visible,
        #     open_pes=open_pes,
        #     loop_select_gacha_slot=loop_select_gacha_slot,
        # )

    # Continuous workflow while not stopped
    selected_mode = main_configs.get('selected_mode', '')
    if selected_mode == 'รีปกติ':
        normal_mode(current_stage)
    elif selected_mode == 'ดอง':
        dong_mode(serial, ui_queue)
    elif selected_mode == 'ฟาร์ม':
        farm_mode(
            serial,
            ui_queue,
            start_farm_mode=start_re_reroll_mode,
            loop_confirm_wait_for=loop_confirm_wait_for,
            wait_for=wait_for,
            tap_location=tap_location,
            swipe_down=swipe_down,
            esc_key=esc_key,
            capture_screen=capture_screen,
            extract_text_tesseract=extract_text_tesseract,
            capture_gacha_screen=capture_gacha_screen,
            update_stage=update_stage,
            get_workflow=lambda: workflow,
            get_current_stage=get_current_stage,
            loop_close_promo=loop_close_promo,
            is_pes_visible=is_pes_visible,
            open_pes=open_pes,
            loop_select_gacha_slot=loop_select_gacha_slot,
            handle_move_file=handle_move_file,
            scale_crop_area=scale_crop_area
        )
    elif selected_mode == 'ทดสอบ':
        test_mode(serial, ui_queue)
    else:
        logger.warning(f"Unknown mode: {selected_mode}. Supported modes: รีปกติ, ดอง, ฟาร์ม, ทดสอบ")

@log_exception_to_json
def load_image(path, scale=1.0):
    abs_path = path if os.path.isabs(
        path) else resource_path(path, readonly=False)
    img = Image.open(abs_path)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)

# Main GUI and polling
@log_exception_to_json
def poll_queues(app):
    # Safety check: ensure app window still exists
    if not app or not app.winfo_exists():
        return
    
    # Create a snapshot to avoid "dictionary changed size during iteration" error
    for serial, q in list(device_queues.items()):
        try:
            while True:
                try:
                    msg_type, s, payload = q.get_nowait()
                except Exception as queue_error:
                    logger.debug(f"Queue error for {serial}: {queue_error}")
                    break
                
                try:
                    if msg_type == 'stage':
                        update_stage(s, payload)

                    elif msg_type == 'substage':
                        lbl = sub_stage_labels.get(s)
                        if lbl and lbl.winfo_exists():
                            lbl.configure(text=payload)

                    elif msg_type == 'confidence':
                        lbl = confidence_labels.get(s)
                        if lbl and lbl.winfo_exists():
                            lbl.configure(text=payload, text_color='red' if 'NOT FOUND' in payload else 'green')
                            
                    elif msg_type == 'stopped':
                        update_status_label(s)

                    elif msg_type == 'reset':
                        device_reset_queue.put(serial)  # เพิ่มเข้า queue
                        time.sleep(2)
                        process_device_reset_queue()

                    elif msg_type == 'file_size':
                        lbl = file_size_labels.get(s)
                        if lbl and lbl.winfo_exists():
                            lbl.configure(text=f'file size: {payload} kb', text_color='red' if int(payload) < 700 else 'green')

                    elif msg_type == 'file_name':
                        lbl = file_size_labels.get(s)
                        if lbl and lbl.winfo_exists():
                            lbl.configure(text=f'{payload}', text_color='white')
                        
                    elif msg_type == 'remaining':
                        lbl = device_labels.get(s)
                        if lbl and lbl.winfo_exists():
                            try:
                                # Extract only the last two octets (e.g., 10.0.0.34:5555 -> 0_34, 10.0.1.34:5555 -> 1_34)
                                if ':' in s:
                                    ip_part = s.split(':')[0]  # 10.0.0.34
                                    octets = ip_part.split('.')
                                    if len(octets) >= 4:
                                        port_num = f"{octets[2]}_{octets[3]}"
                                    else:
                                        port_num = s
                                else:
                                    port_num = s
                                
                                if isinstance(payload, str):
                                    # ถ้า payload เป็น string (error message) ให้แสดงตรง ๆ
                                    lbl.configure(text=f'{port_num} : {payload}', text_color='red')
                                else:
                                    # ถ้า payload เป็น int (วินาที) ให้แสดง m:ss format
                                    minutes = int(payload) // 60
                                    seconds = int(payload) % 60
                                    lbl.configure(text=f'{port_num} : {minutes}:{seconds:02d}', text_color='white')
                            except Exception as e:
                                logger.warning(f"Error updating remaining time for {s}: {e}")
                                if lbl and lbl.winfo_exists():
                                    lbl.configure(text=f'{s} : {payload}', text_color='yellow')
                    
                    elif msg_type == 'completed':
                        #print(f'{s} : COMPLETED')
                        lbl_device = device_labels.get(s)
                        if lbl_device and lbl_device.winfo_exists():
                            # Extract only the last two octets
                            if ':' in s:
                                ip_part = s.split(':')[0]  # 10.0.0.34
                                octets = ip_part.split('.')
                                if len(octets) >= 4:
                                    port_num = f"{octets[2]}_{octets[3]}"
                                else:
                                    port_num = s
                            else:
                                port_num = s
                            lbl_device.configure(text=f'{port_num}', text_color='white')

                        lbl_file_size = file_size_labels.get(s)
                        if lbl_file_size and lbl_file_size.winfo_exists():
                            lbl_file_size.configure(text='COMPLETED', text_color='green')
                        
                        lbl_stage = stage_labels.get(s)
                        if lbl_stage and lbl_stage.winfo_exists():
                            lbl_stage.configure(text='STOP', text_color='green')
                        
                        lbl_sub_stage = sub_stage_labels.get(s)
                        if lbl_sub_stage and lbl_sub_stage.winfo_exists():
                            lbl_sub_stage.configure(text='ดองครบทุก ID แล้ว', text_color='white')
                        
                        lbl_confidence = confidence_labels.get(s)
                        if lbl_confidence and lbl_confidence.winfo_exists():
                            lbl_confidence.configure(text='+++++++++++++', text_color='green')
                except Exception as msg_handler_error:
                    logger.error(f"Error handling message {msg_type} for {s}: {msg_handler_error}")
                    # Continue to next message instead of crashing

        except Exception:
            pass
    
    # ================================
    # Health Check: Monitor Process Status
    # ================================
    for serial, p in list(device_procs.items()):
        if p is None:
            continue
        if not p.is_alive():
            exit_code = p.exitcode
            if exit_code is not None and exit_code != 0:
                logger.error(f"[ALERT] Process for device {serial} crashed! Exit code: {exit_code}")
                if exit_code == -9:
                    logger.error(f"  -> Killed by system (likely OOM or forced kill)")
                elif exit_code == 1:
                    logger.error(f"  -> Generic error, check logs")
                elif exit_code == -15:
                    logger.error(f"  -> SIGTERM received")
                else:
                    logger.error(f"  -> Unknown exit code {exit_code}")
                
                # ส่ง notification ไป UI
                if serial in device_queues:
                    safe_queue_put(device_queues[serial], 
                                 ('remaining', serial, f'[FAIL] CRASHED (exit code: {exit_code})'),
                                 device_serial=serial)
    
    # ================================
    # Periodic Resource Cleanup (every 30s)
    # ================================
    if not hasattr(poll_queues, '_cleanup_counter'):
        poll_queues._cleanup_counter = 0
    
    poll_queues._cleanup_counter += 1
    if poll_queues._cleanup_counter >= 60:  # Every 60 iterations (30s with 500ms interval)
        try:
            screens_dir = resource_path('screens', readonly=False)
            safe_cleanup_screenshots(screens_dir, keep_count=5)
            poll_queues._cleanup_counter = 0
        except Exception as e:
            logger.warning(f"Error in periodic cleanup: {e}")
    
    # ================================
    # Recursive call: เรียกตัวเองซ้ำทุก 500ms เพื่อ process queue อย่างต่อเนื่อง
    # ================================
    if app and app.winfo_exists():
        app.after(500, poll_queues, app)
    

# หลัง poll_queues(app) และก่อน app.mainloop()
@log_exception_to_json
def check_stage_timeouts():
    global devices_configs, main_configs, stage_timeout_at
    
    now = time.time()
    
    # ตรวจเวลา timeout ของแต่ละ device ที่กำลังรัน
    for serial in list(stage_start_times.keys()):
        if serial not in stage_timeout_at:
            continue
        
        timeout_at = stage_timeout_at[serial]
        remaining = max(timeout_at - now, 0)
        
        # ส่งข้อมูลเวลาเหลือไปยัง UI ทุกวินาที
        if serial not in device_queues:
            device_queues[serial] = Queue()
        device_queues[serial].put(('remaining', serial, int(remaining)))
        
        # ตรวจเวลาครั้งเดียว: ถ้า remaining <= 0 แล้วต้อง reset
        if remaining <= 0 and serial in stage_start_times:
            logger.warning(f"⏱️ TIMEOUT TRIGGERED for device {serial} - Preparing to reset")
            device_reset_queue.put(serial)
            process_device_reset_queue()
            
            # ล้างข้อมูล timeout เพื่อไม่ให้ reset อีก
            stage_timeout_at.pop(serial, None)
            stage_start_times.pop(serial, None)

    if app and app.winfo_exists():
        # เรียกตัวเองใหม่ทุก 1000 มิลลิวินาที
        app.after(1000, check_stage_timeouts)

MAX_COLS = 4
image_vars = []

# --- Config Tab GUI Setup ---
@log_exception_to_json
def setup_config_tab(config_tab, main_configs, save_main_config, load_config):
    config_tab.grid_columnconfigure(1, weight=1)

    # Gacha slot input
    ctk.CTkLabel(config_tab, text='Gacha Slot:', anchor='w').grid(
        row=1, column=0, columnspan=1, padx=(10, 5), pady=(10, 5), sticky='w')
    gacha_var = ctk.StringVar(value=str(main_configs.get('gacha_slot', '')))
    gacha_entry = ctk.CTkEntry(config_tab, textvariable=gacha_var)
    gacha_entry.grid(row=1, column=1, columnspan=1, padx=(5, 10), pady=(10, 5), sticky='ew')

    # Gacha slot input
    ctk.CTkLabel(config_tab, text='สุ่มกี่ตู้:', anchor='w').grid(
        row=1, column=2, columnspan=1, padx=(10, 5), pady=(10, 5), sticky='w')
    gacha_var = ctk.StringVar(value=str(main_configs.get('count_gacha', '')))
    gacha_entry = ctk.CTkEntry(config_tab, textvariable=gacha_var)
    gacha_entry.grid(row=1, column=3, columnspan=1, padx=(5, 10), pady=(10, 5), sticky='ew')

    ctk.CTkLabel(config_tab, text='ตำแหน่งตู้ฟรี:', anchor='w').grid(
        row=1, column=4, columnspan=1, padx=(10, 5), pady=(10, 5), sticky='w')
    gacha_var = ctk.StringVar(value=str(main_configs.get('select_gacha_slot', '')))
    gacha_entry = ctk.CTkEntry(config_tab, textvariable=gacha_var)
    gacha_entry.grid(row=1, column=5, columnspan=1, padx=(5, 10), pady=(10, 5), sticky='ew')

    ctk.CTkLabel(config_tab, text='เลือกตัวไหน:', anchor='w').grid(
        row=1, column=6, columnspan=1, padx=(10, 5), pady=(10, 5), sticky='w')
    gacha_var = ctk.StringVar(value=str(main_configs.get('free_gacha_slot', '')))
    gacha_entry = ctk.CTkEntry(config_tab, textvariable=gacha_var)
    gacha_entry.grid(row=1, column=7, columnspan=1, padx=(5, 10), pady=(10, 5), sticky='ew')

    # Backup file path input with browse
    ctk.CTkLabel(config_tab, text='Backup File Path:', anchor='w').grid(
        row=2, column=0, padx=(10, 5), pady=(5, 10), sticky='w')
    path_var = ctk.StringVar(value=main_configs.get('backup_file_path', ''))
    path_entry = ctk.CTkEntry(config_tab, textvariable=path_var)
    path_entry.grid(row=2, column=1, columnspan=6, padx=(5, 5), pady=(5, 10), sticky='ew')

    @log_exception_to_json
    def browse_path():
        selected = filedialog.askdirectory()
        if selected:
            path_var.set(selected)
    ctk.CTkButton(config_tab, text='Browse...', width=80, command=browse_path).grid(
        row=2, column=7, padx=(5, 5), columnspan=1, pady=(5, 10), sticky='e')

    # Frame to hold images and rename entries
    images_frame = ctk.CTkFrame(config_tab)
    images_frame.grid(row=3, column=0, columnspan=4,
                      sticky='nsew', padx=10, pady=(5, 10))
    config_tab.grid_rowconfigure(4, weight=1)

    # Load and display
    @log_exception_to_json
    def load_images():
        # clear existing
        for info in image_vars:
            info['label_widget'].destroy()
            info['entry_widget'].destroy()
        image_vars.clear()

        # files = [f for f in os.listdir(RANGERS_FOLDER) if f.lower().endswith(
        #     ('.png', '.jpg', '.jpeg', '.gif'))]
        # for idx, filename in enumerate(files):
        #     orig_path = os.path.join(RANGERS_FOLDER, filename)
        #     row = idx // MAX_COLS
        #     col = idx % MAX_COLS

        #     # load thumbnail and wrap in CTkImage for HiDPI scaling
        #     img = Image.open(orig_path)
        #     img.thumbnail((100, 100))
        #     thumb_size = img.size
        #     photo = CTkImage(light_image=img, dark_image=img, size=thumb_size)

        #     lbl = ctk.CTkLabel(images_frame, image=photo)
        #     lbl.image = photo
        #     lbl.grid(row=row*2, column=col, padx=5, pady=(5, 2))

        #     # show only name without extension
        #     name_no_ext = os.path.splitext(filename)[0]
        #     var = ctk.StringVar(value=name_no_ext)
        #     entry = ctk.CTkEntry(images_frame, textvariable=var, width=100)
        #     entry.grid(row=row*2+1, column=col, padx=5, pady=(0, 5))

        #     image_vars.append({
        #         'orig_path': orig_path,
        #         'var': var,
        #         'label_widget': lbl,
        #         'entry_widget': entry
        #     })

    load_images()

    # Save config button
    @log_exception_to_json
    def on_save():
        # save main config values
        # อัปเดตค่าที่ต้องการลงใน main_configs เท่านั้น
        load_config()
        try:
            main_configs['gacha_slot'] = int(gacha_var.get())
            main_configs['select_gacha_slot'] = int(gacha_var.get())
            main_configs['free_gacha_slot'] = int(gacha_var.get())
            main_configs['count_gacha'] = int(gacha_var.get())
        except ValueError:
            main_configs['gacha_slot'] = main_configs.get('gacha_slot', 0)
            main_configs['select_gacha_slot'] = main_configs.get('select_gacha_slot', 0)
            main_configs['free_gacha_slot'] = main_configs.get('free_gacha_slot', 0)
            main_configs['count_gacha'] = main_configs.get('count_gacha', 0)

        main_configs['backup_file_path'] = path_var.get()

        # บันทึก config เต็มชุด
        save_main_config()
        load_config()

        # เปลี่ยนชื่อไฟล์ใน carector_ref ตามเดิม
        for info in image_vars:
            base = info['var'].get()
            ext = os.path.splitext(info['orig_path'])[1]
            new_name = f'{base}{ext}'
            orig = info['orig_path']
            new_path = os.path.join(RANGERS_FOLDER, new_name)
            if new_name and new_path != orig:
                try:
                    os.rename(orig, new_path)
                except Exception as e:
                    print(f'Failed to rename {orig} -> {new_path}: {e}')

        # reload configs and images
        load_config()
        load_images()

    ctk.CTkButton(config_tab, text='Save Config', command=on_save).grid(
        row=2, column=6, padx=(5, 5), columnspan=1, pady=(5, 10), sticky='e')

if __name__ == '__main__':
    freeze_support()

    # === GUI SETUP ===
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('blue')
    app = ctk.CTk()
    app.title('Game Character Detection & Auto Tap')
    app.geometry('660x540')

    matcher = FeatureMatcher(method='ORB', min_matches=8, conf_thresh=0.6)
    on_stage_manager = OnStageManager()

    # TabView setup
    tabview = ctk.CTkTabview(app)
    tabview.pack(fill='both', expand=True, padx=20, pady=20)
    tabview.add('Status')
    tabview.add('Config')

    # --- Status Tab ---
    status_tab = tabview.tab('Status')
    status_tab.grid_rowconfigure(2, weight=1)
    status_tab.grid_columnconfigure(0, weight=1)

    # ===========================================================

    status_label = ctk.CTkLabel(status_tab, text='Ready', text_color='white')
    status_label.grid(row=0, column=0, pady=(10, 5), sticky='e')

    render_ports = ctk.CTkLabel(
        status_tab, text='Checking ports...', text_color='white')
    render_ports.grid(row=0, column=0, pady=(0, 2), sticky='w')

    ports_var = ctk.StringVar(value=','.join(str(p)
                              for p in main_configs.get('port_list', [])))
    ports_entry = ctk.CTkEntry(status_tab, textvariable=ports_var)
    ports_entry.grid(row=1, column=0, sticky='ew', padx=(5, 10), pady=(0, 2))

    @log_exception_to_json
    def save_ports():
        text = ports_var.get()
        ports = []
        for token in text.split(','):
            token = token.strip()
            if not token:
                continue
            normalized = normalize_adb_tcpip_address(token)
            if normalized:
                ports.append(normalized)
            else:
                print(f'Invalid ADB TCP/IP entry: {token}')

        load_main_config()
        main_configs['port_list'] = ports
        save_main_config()
        load_main_config()
        refresh_connected_ports_label()

    save_ports_btn = ctk.CTkButton(
        status_tab, text='Save Ports', command=save_ports)
    save_ports_btn.grid(row=1, column=0, padx=(5, 10),
                        pady=(0, 2), sticky='e')
    # ===========================================================

    btn_frame = ctk.CTkFrame(status_tab)
    btn_frame.grid(row=3, column=0, pady=(0, 20), padx=10, sticky='ew')

    # กำหนดว่ามี 4 คอลัมน์
    btn_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform='btn')
    
    # === Global Variable ===
    selected_mode = tk.StringVar(value=main_configs.get('selected_mode', ''))  # ค่าเริ่มต้น
    connect_button = None  # เก็บ reference ของปุ่ม

    # === Function to handle change ===
    @log_exception_to_json
    def on_select_mode(choice):
        global connect_button
        #print('Selected mode:', choice)
        selected_mode.set(choice)  # อัปเดตค่าลงใน global variable
        load_main_config()
        main_configs['selected_mode'] = choice
        save_main_config()
        load_main_config()

        if connect_button:
            colspan = 1 if choice == 'ดอง' or choice == 'ฟาร์ม' else 2
            connect_button.grid(row=0, column=1, padx=5, pady=5, sticky='ew', columnspan=colspan)
        update_path_frame_visibility()

    # แถวที่ 1: 
    # === Dropdown ===
    ctk.CTkOptionMenu(btn_frame,values=['รีปกติ','ดอง','ฟาร์ม','ทดสอบ'],command=on_select_mode,variable=selected_mode,fg_color='white',text_color='black') \
        .grid(row=0, column=0, padx=5, pady=5, sticky='ew')
    initial_colspan = 1 if selected_mode.get() == 'ดอง' or selected_mode.get() == 'ฟาร์ม' else 2

    # === Connect Button ===
    connect_button = ctk.CTkButton(btn_frame, text='Connect & Start All', 
                                command=lambda: [connect_devices_async(main_configs.get('re_reroll_file_path', ''))], 
                                fg_color='#309975')
    initial_colspan = 1 if selected_mode.get() == 'ดอง' or selected_mode.get() == 'ฟาร์ม' else 2
    connect_button.grid(row=0, column=1, padx=5, pady=5, sticky='ew', columnspan=initial_colspan)

    path_frame = ctk.CTkFrame(btn_frame, fg_color='transparent')
    path_frame.grid(row=0, column=2, padx=5, pady=5, sticky='ew')
    path_frame.grid_columnconfigure(0, weight=1)

    path_var = ctk.StringVar(value=main_configs.get('re_reroll_file_path', ''))
    path_entry = ctk.CTkEntry(path_frame, textvariable=path_var)
    path_entry.grid(row=0, column=0, padx=5, pady=5, sticky='ew')

    @log_exception_to_json
    def browse_path():
        selected = filedialog.askdirectory()
        if selected:
            path_var.set(selected)
            load_main_config()
            main_configs['re_reroll_file_path'] = selected

            save_main_config()
            load_main_config()

    # ctk.CTkButton(path_frame, text='Browse', width=50, command=browse_path).grid(
    #     row=0, column=1, padx=(5), pady=(5, 10))

    def update_path_frame_visibility():
        if selected_mode.get() == 'ดอง' or selected_mode.get() == 'ฟาร์ม':
            path_frame.grid()
        else:
            path_frame.grid_remove()
        
    update_path_frame_visibility()

    # ปุ่ม Start Stop
    ctk.CTkButton(btn_frame, text='Stop All',
                  command=lambda: [stop_device_async(d.get_serial_no()) for d in devices]) \
        .grid(row=0, column=3, padx=5, pady=5, sticky='ew')
    
    # ปุ่ม Reset All
    ctk.CTkButton(
        btn_frame, 
        text='Reset All', 
        fg_color='#b33636',
        command=lambda: [
            on_stage_manager.clear_on_stage(),
            [device_reset_queue.put(d.get_serial_no()) for d in devices],
            process_device_reset_queue()
        ]
    ).grid(row=0, column=4, padx=5, pady=5, sticky='ew')

    # ===========================================================

    # === Checkbox ดองตัว ===
    is_random_var = ctk.BooleanVar(value=main_configs.get('is_random', False))

    def on_check_random():
        main_configs['is_random'] = is_random_var.get()
        save_main_config()
        load_main_config()

    # Checkbox ดองตัว
    random_cb = ctk.CTkCheckBox(
        btn_frame,
        text='สุ่มมั้ย?',
        variable=is_random_var,
        command=on_check_random
    )
    random_cb.grid(row=1, column=0, padx=2, pady=2, sticky='w')

    # === Checkbox รับ missions ===
    is_caim_missions_var = ctk.BooleanVar(value=main_configs.get('is_caim_missions', False))

    def on_check_caim_missions():
        main_configs['is_caim_missions'] = is_caim_missions_var.get()
        save_main_config()
        load_main_config()

    # Checkbox รับ missions
    free_player_cb = ctk.CTkCheckBox(
        btn_frame,
        text='รับ missions?',
        variable=is_caim_missions_var,
        command=on_check_caim_missions
    )
    free_player_cb.grid(row=1, column=1, padx=2, pady=2, sticky='w')
    
    # === Checkbox ฟรีเพลเยอร์ ===
    is_free_player_var = ctk.BooleanVar(value=main_configs.get('is_free_player', False))

    def on_check_free_player():
        main_configs['is_free_player'] = is_free_player_var.get()
        save_main_config()
        load_main_config()

    # Checkbox ฟรีเพลเยอร์
    free_player_cb = ctk.CTkCheckBox(
        btn_frame,
        text='ฟรีเพลเยอร์?',
        variable=is_free_player_var,
        command=on_check_free_player
    )
    free_player_cb.grid(row=1, column=2, padx=2, pady=2, sticky='w')

    # === Checkbox รับ comeback ===
    is_comeback_var = ctk.BooleanVar(value=main_configs.get('is_comeback', False))

    def on_check_comeback():
        main_configs['is_comeback'] = is_comeback_var.get()
        save_main_config()
        load_main_config()

    # Checkbox รับ comeback
    comeback_cb = ctk.CTkCheckBox(
        btn_frame,
        text='รับ comeback?',
        variable=is_comeback_var,
        command=on_check_comeback
    )
    comeback_cb.grid(row=1, column=3, padx=2, pady=2, sticky='w')

    # ด้วยโค้ดนี้:
    container = ctk.CTkFrame(status_tab)
    container.grid(row=4, column=0, sticky='nsew', pady=(0, 20))

    # กำหนดความสูงคงที่ (เช่น 400px) และปิด propagation
    container.configure(height=500)
    container.grid_propagate(False)

    # ให้แถว 4 ขยายได้ (เอา weight=1 ให้ row นี้)
    status_tab.grid_rowconfigure(4, weight=1)

    # สร้าง Canvas และ Scrollbar
    canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
    light_color, dark_color = container._fg_color
    canvas.configure(bg=dark_color)

    scrollbar = ctk.CTkScrollbar(
        container, orientation='vertical', command=canvas.yview)

    # Frame จริงๆ ที่จะแปะ device frames ลงไป
    devices_frame = ctk.CTkFrame(canvas)

    # ปรับให้ canvas scroll ได้เมื่อ content เปลี่ยน
    devices_frame.bind(
        '<Configure>',
        lambda e: canvas.configure(scrollregion=canvas.bbox('all'))
    )

    # วาง devices_frame ลงใน canvas
    canvas.create_window((0, 0), window=devices_frame, anchor='nw')
    canvas.configure(yscrollcommand=scrollbar.set)

    # Layout canvas กับ scrollbar
    canvas.grid(row=0, column=0, sticky='nsew')
    scrollbar.grid(row=0, column=1, sticky='ns')
    container.grid_columnconfigure(0, weight=1)
    container.grid_rowconfigure(0, weight=1)

    # ===========================================================

    config_tab = tabview.tab('Config')
    setup_config_tab(
        config_tab,
        main_configs,
        save_main_config,
        load_main_config
    )

    poll_queues(app)
    if app and app.winfo_exists():
        app.after(1000, check_stage_timeouts)

    # Auto-refresh ports
    threading.Thread(target=lambda: (time.sleep(
        5), refresh_connected_ports_label()), daemon=True).start()
    refresh_connected_ports_label()

    auto_connect_mumu_async()

    # ตรงหลังสร้าง app (ก่อน app.mainloop())
    @log_exception_to_json
    def on_closing():
        # หยุดทุก device process
        for serial, p in device_procs.items():
            if p and p.is_alive():
                p.terminate()
                p.join()
        app.destroy()

    # ผูก callback
    app.protocol('WM_DELETE_WINDOW', on_closing)

    app.mainloop()
