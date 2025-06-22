#!/usr/bin/env python3
"""
日志查看工具
用于查看和管理网格交易机器人的日志文件
"""

import os
import sys
import glob
from datetime import datetime

def list_log_files():
    """列出所有日志文件"""
    logs_dir = os.path.join(os.path.dirname(__file__), "logs")
    if not os.path.exists(logs_dir):
        print("logs目录不存在，还没有生成日志文件")
        return []
    
    log_files = glob.glob(os.path.join(logs_dir, "*.log"))
    log_files.sort(key=os.path.getmtime, reverse=True)  # 按修改时间倒序排列
    
    if not log_files:
        print("没有找到日志文件")
        return []
    
    print("可用的日志文件：")
    for i, log_file in enumerate(log_files, 1):
        filename = os.path.basename(log_file)
        mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
        size = os.path.getsize(log_file)
        print(f"{i}. {filename} ({mtime.strftime('%Y-%m-%d %H:%M:%S')}) - {size/1024:.1f}KB")
    
    return log_files

def view_log_file(log_file, lines=50):
    """查看指定日志文件的最后几行"""
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
            if len(all_lines) <= lines:
                print(f"\n=== {os.path.basename(log_file)} 完整内容 ===")
                print(''.join(all_lines))
            else:
                print(f"\n=== {os.path.basename(log_file)} 最后{lines}行 ===")
                print(''.join(all_lines[-lines:]))
    except Exception as e:
        print(f"读取日志文件失败: {e}")

def search_logs(keyword, lines=10):
    """在所有日志文件中搜索关键词"""
    logs_dir = os.path.join(os.path.dirname(__file__), "logs")
    if not os.path.exists(logs_dir):
        print("logs目录不存在")
        return
    
    log_files = glob.glob(os.path.join(logs_dir, "*.log"))
    if not log_files:
        print("没有找到日志文件")
        return
    
    print(f"在所有日志文件中搜索 '{keyword}'：")
    found = False
    
    for log_file in log_files:
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                file_lines = f.readlines()
                matching_lines = []
                
                for i, line in enumerate(file_lines, 1):
                    if keyword.lower() in line.lower():
                        matching_lines.append((i, line))
                        if len(matching_lines) >= lines:
                            break
                
                if matching_lines:
                    found = True
                    print(f"\n--- {os.path.basename(log_file)} ---")
                    for line_num, line in matching_lines:
                        print(f"第{line_num}行: {line.rstrip()}")
        except Exception as e:
            print(f"读取 {log_file} 失败: {e}")
    
    if not found:
        print(f"没有找到包含 '{keyword}' 的日志记录")

def main():
    if len(sys.argv) < 2:
        print("使用方法:")
        print("  python view_logs.py list                    # 列出所有日志文件")
        print("  python view_logs.py view <文件名或编号>      # 查看指定日志文件")
        print("  python view_logs.py search <关键词>         # 搜索日志内容")
        print("  python view_logs.py latest                  # 查看最新的日志文件")
        return
    
    command = sys.argv[1].lower()
    
    if command == "list":
        list_log_files()
    
    elif command == "view":
        if len(sys.argv) < 3:
            print("请指定要查看的日志文件名或编号")
            return
        
        log_files = list_log_files()
        if not log_files:
            return
        
        target = sys.argv[2]
        
        # 尝试作为编号处理
        try:
            index = int(target) - 1
            if 0 <= index < len(log_files):
                view_log_file(log_files[index])
            else:
                print(f"编号 {target} 超出范围")
        except ValueError:
            # 尝试作为文件名处理
            log_file = os.path.join(os.path.dirname(__file__), "logs", target)
            if os.path.exists(log_file):
                view_log_file(log_file)
            else:
                print(f"找不到日志文件: {target}")
    
    elif command == "search":
        if len(sys.argv) < 3:
            print("请指定搜索关键词")
            return
        
        keyword = sys.argv[2]
        search_logs(keyword)
    
    elif command == "latest":
        log_files = list_log_files()
        if log_files:
            view_log_file(log_files[0])
    
    else:
        print(f"未知命令: {command}")

if __name__ == "__main__":
    main() 