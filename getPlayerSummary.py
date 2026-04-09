import json
import os
from pathlib import Path
from collections import defaultdict

def get_player_summary(backup_path):
    """
    Extract player names from folder names and count .dat files.
    Folder structure: [number] [player_name]
    """
    
    player_data = defaultdict(int)  # Store player_name: total_file_count
    
    try:
        # Check if path exists
        if not os.path.exists(backup_path):
            print(f"❌ Path not found: {backup_path}")
            return None
        
        # Get all directories in backup path
        folders = [f for f in os.listdir(backup_path) 
                   if os.path.isdir(os.path.join(backup_path, f))]
        
        if not folders:
            print(f"❌ No folders found in: {backup_path}")
            return None
        
        print("=" * 60)
        print("📊 สรุปข้อมูลนักเตะ (Player Summary)")
        print("=" * 60)
        
        # Process each folder
        for folder_name in folders:
            # New format: 18_[50]_EricCantona_1768404478349
            # Extract player name between underscores
            parts = folder_name.split(' ')

            print("parts:", parts)
            
            if len(parts) >= 3:
                # Player name is typically at index 2
                player_name = parts[2]
            elif len(parts) >= 2:
                # Fallback: try to extract from second part
                player_name = parts[1]
            else:
                # Final fallback: use whole name
                player_name = folder_name
            
            # Count .dat and .xml files in this folder
            folder_path = os.path.join(backup_path, folder_name)
            dat_files = [f for f in os.listdir(folder_path) 
                        if f.endswith(('.dat', '.xml'))]
            file_count = len(dat_files)
            
            if file_count > 0:
                # Accumulate file count for this player
                player_data[player_name] += file_count
            
                print(f"📁 {folder_name}")
                print(f"   └─ ไฟล์: {file_count} files")
        
        # Display summary
        total_folders = len(folders)
        unique_players = len(player_data)
        total_files = sum(player_data.values())
        
        print("\n" + "=" * 60)
        print("🏆 ชื่อนักเตะและจำนวนไฟล์ (Players & File Count)")
        print("=" * 60)
        
        # Sort by player name
        sorted_players = sorted(player_data.items())
        
        for player_name, file_count in sorted_players:
            print(f"{player_name:<30} {file_count:>5} ไฟล์")
        
        # Save to JSON
        result = {
            "total_folders": total_folders,
            "unique_players": unique_players,
            "total_files": total_files,
            "players": dict(sorted_players)
        }
        
        output_file = "player_summary.json"
        # output_file = "ranger_summary.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print("\n✅ บันทึกผลลัพธ์ไปที่: " + output_file)
        print("=" * 60)
        
        print("\n" + "=" * 60)
        print("📋 สรุปรวม (Summary)")
        print("=" * 60)
        
        print(f"รวมโฟลเดอร์ทั้งหมด: {total_folders}")
        print(f"นักเตะที่ไม่ซ้ำ: {unique_players}")
        print(f"รวมไฟล์ทั้งหมด: {total_files}")
        print("\n" + "=" * 60)
        
        return result
        
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาด: {str(e)}")
        return None


if __name__ == "__main__":
    # Load config
    try:
        with open('bin/main_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        backup_path = config.get('summary_player_path', config.get('backup_path', 'C:/backup_bot/Pes'))
    except FileNotFoundError:
        backup_path = 'C:/backup_bot/Pes'
    
    # Convert to Windows path format
    backup_path = backup_path.replace('/', '\\')
    
    get_player_summary(backup_path)
