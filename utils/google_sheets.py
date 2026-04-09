import gspread
import os
from google.oauth2.service_account import Credentials
from typing import Optional, List, Dict, Any

# Global instance สำหรับเก็บ GoogleSheetsManager
_sheets_manager_instance: Optional['GoogleSheetsManager'] = None

class GoogleSheetsManager:
    """
    Google Sheets Manager สำหรับจัดการ Google Sheets อย่างง่ายๆ
    """
    
    def __init__(self, service_account_path: Optional[str] = None):
        """
        Initialize Google Sheets Manager
        
        Args:
            service_account_path: Path ไปยัง service account json file
                                  ถ้าไม่ระบุ จะหา farm-pes-log-*.json ใน directory นี้
        """
        self.scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.client = None
        self.service_account_path = service_account_path
        
        # ===== Cache data structure =====
        # {spreadsheet_name: {worksheet_name: [records]}}
        self._cached_data: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize gspread client จาก service account"""
        try:
            # ถ้าไม่ระบุ path ให้หามั่ว
            if not self.service_account_path:
                self.service_account_path = self._find_service_account_file()
            
            if not os.path.exists(self.service_account_path):
                raise FileNotFoundError(f"Service account file not found: {self.service_account_path}")
            
            creds = Credentials.from_service_account_file(
                self.service_account_path,
                scopes=self.scopes
            )
            self.client = gspread.authorize(creds)
            print(f"✓ Google Sheets initialized successfully")
            
        except Exception as e:
            print(f"✗ Error initializing Google Sheets: {e}")
            self.client = None
    
    def _find_service_account_file(self) -> str:
        """
        หา service account json file ในโปรเจกต์
        
        Returns:
            Path ไปยัง service account file
        """
        # ลองหาในหลายที่
        possible_paths = [
            "farm-pes-log-463e413e2ef1.json",
            "../farm-pes-log-463e413e2ef1.json",
            "service_account.json",
            "../service_account.json",
        ]
        
        for path in possible_paths:
            full_path = os.path.join(os.path.dirname(__file__), "..", path)
            if os.path.exists(full_path):
                return os.path.abspath(full_path)
        
        # ถ้าหาไม่เจอ ให้ส่งค่า default
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "farm-pes-log-463e413e2ef1.json"))