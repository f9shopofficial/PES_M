import cv2
import numpy as np
from typing import List, Tuple, Dict, Optional
import sys
import os

def loop_action_before_confirm(serial: str, action_function , target_file, text_crop_area, last_action_function=lambda:[], **kwargs):
    wait_for = kwargs.get('wait_for')

    is_break = False

    def set_break():
        nonlocal is_break
        is_break = True

    while True:
        if is_break:
            break

        action_function()

        wait_for(
            serial=serial,
            detection_type='text',
            target_file=target_file,
            text_action=lambda:[set_break()],
            text_crop_area=text_crop_area,
            extract_mode = 'name',
            is_loop=False
        )

        if is_break:
            break
    
    last_action_function()


def detect_color_in_image(
    image_path: str,
    color_lower: Tuple[int, int, int],
    color_upper: Tuple[int, int, int],
    crop_area: Optional[Tuple[int, int, int, int]] = None,
    min_area: int = 50,
    color_space: str = 'HSV'
) -> List[Dict]:
    """
    ตรวจจับสีในรูปภาพ
    
    Args:
        image_path: เส้นทางไปยังรูปภาพ
        color_lower: ค่าสีล่าง (HSV หรือ BGR) เช่น (100, 100, 100)
        color_upper: ค่าสีบน (HSV หรือ BGR) เช่น (130, 255, 255)
        crop_area: พื้นที่ครอป (x1, y1, x2, y2) ถ้า None จะใช้รูปทั้งหมด
        min_area: พื้นที่ต่ำสุดของการตรวจจับ (pixel)
        color_space: ระบบสี 'HSV' หรือ 'BGR'
    
    Returns:
        List[Dict]: รายการผลการตรวจจับ
            {
                'contour': numpy array ของ contour
                'center': (x, y) จุดศูนย์กลาง
                'area': พื้นที่
                'bbox': (x, y, w, h) กรอบขอบเขต
                'moments': moments object สำหรับคำนวณ
            }
    """
    try:
        # อ่านรูปภาพ
        img = cv2.imread(image_path)
        if img is None:
            print(f"ไม่สามารถอ่านรูปภาพ: {image_path}")
            return []
        
        # ครอปรูปถ้ามี crop_area
        if crop_area:
            x1, y1, x2, y2 = crop_area
            img = img[y1:y2, x1:x2]
        
        # แปลงสี
        if color_space.upper() == 'HSV':
            img_color = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        else:
            img_color = img
        
        # สร้าง mask สำหรับสีที่กำหนด
        lower = np.array(color_lower, dtype=np.uint8)
        upper = np.array(color_upper, dtype=np.uint8)
        mask = cv2.inRange(img_color, lower, upper)
        
        # ทำให้ mask ราบเรียบ
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        # หา contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        results = []
        for contour in contours:
            area = cv2.contourArea(contour)
            
            # กรองตามพื้นที่ต่ำสุด
            if area < min_area:
                continue
            
            moments = cv2.moments(contour)
            
            # หาจุดศูนย์กลาง
            if moments['m00'] != 0:
                cx = int(moments['m10'] / moments['m00'])
                cy = int(moments['m01'] / moments['m00'])
            else:
                continue
            
            # หา bounding box
            x, y, w, h = cv2.boundingRect(contour)
            
            # เพิ่มค่า crop_area offset กลับ
            if crop_area:
                x += crop_area[0]
                y += crop_area[1]
                cx += crop_area[0]
                cy += crop_area[1]
            
            results.append({
                'contour': contour,
                'center': (cx, cy),
                'area': area,
                'bbox': (x, y, w, h),
                'moments': moments
            })
        
        # เรียงตามพื้นที่จากมากไปน้อย
        results.sort(key=lambda x: x['area'], reverse=True)
        
        return results
    
    except Exception as e:
        print(f"ผิดพลาดในการตรวจจับสี: {e}")
        return []


def detect_multiple_colors(
    image_path: str,
    color_ranges: Dict[str, Tuple[Tuple[int, int, int], Tuple[int, int, int]]],
    crop_area: Optional[Tuple[int, int, int, int]] = None,
    min_area: int = 50,
    color_space: str = 'HSV'
) -> Dict[str, List[Dict]]:
    """
    ตรวจจับหลายสีในรูปภาพเดียว
    
    Args:
        image_path: เส้นทางไปยังรูปภาพ
        color_ranges: Dict ของสี เช่น {
            'red': ((0, 100, 100), (10, 255, 255)),
            'blue': ((100, 100, 100), (130, 255, 255))
        }
        crop_area: พื้นที่ครอป (x1, y1, x2, y2)
        min_area: พื้นที่ต่ำสุดของการตรวจจับ
        color_space: ระบบสี 'HSV' หรือ 'BGR'
    
    Returns:
        Dict[str, List[Dict]]: ผลการตรวจจับแต่ละสี
    """
    results = {}
    
    for color_name, (lower, upper) in color_ranges.items():
        results[color_name] = detect_color_in_image(
            image_path=image_path,
            color_lower=lower,
            color_upper=upper,
            crop_area=crop_area,
            min_area=min_area,
            color_space=color_space
        )
    
    return results


def count_checkmarks_in_image(image_path):
    """นับจำนวนติ๊กถูก (checkmark) สีเขียวในภาพ"""
    
    img = cv2.imread(image_path)
    if img is None:
        print(f"ไม่สามารถเปิดภาพ: {image_path}")
        return 0
    
    # แปลงเป็น HSV เพื่อกรองสีเขียว
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    
    # กำหนดช่วงสีเขียวใน HSV
    lower_green = np.array([40, 80, 80])
    upper_green = np.array([90, 255, 255])
    
    # สร้าง mask สีเขียว
    mask = cv2.inRange(hsv, lower_green, upper_green)
    
    # ทำ morphology เพื่อลด noise
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    
    # หา contours ของวงกลมสีเขียว
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # กรองเฉพาะวงกลมขนาดพอเหมาะ (ติ๊กถูก)
    checkmark_count = 0
    min_area = 200   # พื้นที่ขั้นต่ำ
    max_area = 1500  # จำกัดขนาด เพื่อไม่นับ icon รางวัลสีเขียวขนาดใหญ่
    
    valid_contours = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if min_area < area < max_area:
            # ตรวจสอบความกลม (circularity)
            perimeter = cv2.arcLength(cnt, True)
            if perimeter > 0:
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity > 0.5:  # ต้องค่อนข้างกลม
                    valid_contours.append(cnt)
                    checkmark_count += 1
    
    # วาดผลลัพธ์
    result_img = img.copy()
    cv2.drawContours(result_img, valid_contours, -1, (0, 0, 255), 2)
    
    # เพิ่มตัวเลขนับ
    for i, cnt in enumerate(valid_contours):
        M = cv2.moments(cnt)
        if M["m00"] != 0:
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            cv2.putText(result_img, str(i+1), (cx-10, cy+5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
    
    return checkmark_count, result_img