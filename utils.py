#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
工具函数 - 通过图片内容识别找到"湖南人涨停复盘"图片
策略：先检查第04张，如果没有则遍历所有图片
"""

import os
import json

# 全局变量，缓存OCR reader
_ocr_reader = None


def get_ocr_reader():
    """获取OCR reader（懒加载）"""
    global _ocr_reader
    if _ocr_reader is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_reader = RapidOCR()
        except ImportError:
            print("请先安装: pip install rapidocr_onnxruntime")
            return None
    return _ocr_reader


def ocr_image(image_path):
    """
    OCR识别图片文字（带磁盘缓存）

    缓存: 在图片旁生成同名 .ocr.json，避免对同一张图重复OCR。
          缓存比图片新时直接复用。

    返回: 识别到的文字列表 [(text, confidence), ...]
    """
    cache_path = image_path + '.ocr.json'

    # 命中缓存（缓存不旧于图片）
    if os.path.exists(cache_path):
        try:
            if os.path.getmtime(cache_path) >= os.path.getmtime(image_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return [(t, c) for t, c in data]
        except Exception:
            pass  # 缓存损坏则重新OCR

    reader = get_ocr_reader()
    if reader is None:
        return []

    try:
        result, _ = reader(image_path)
        # result格式: [[box, text, confidence], ...]
        texts = [(item[1], float(item[2])) for item in result] if result else []

        # 写缓存
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(texts, f, ensure_ascii=False)
        except Exception:
            pass

        return texts
    except Exception as e:
        print(f"OCR识别失败: {e}")
        return []


def ocr_image_text(image_path):
    """
    OCR识别图片文字，返回拼接后的文本
    
    返回: 拼接后的完整文本
    """
    texts = ocr_image(image_path)
    return ''.join([t[0] for t in texts])


def check_title_in_text(text, keywords=None):
    """
    检查文本中是否包含关键字
    
    参数:
        text: 文本内容
        keywords: 关键字列表，默认 ["湖南人涨停复盘"]
    
    返回:
        是否包含任一关键字
    """
    if keywords is None:
        keywords = ["湖南人涨停复盘"]
    
    for keyword in keywords:
        if keyword in text:
            return True
    return False


def find_image_by_content(date_folder, keywords=None, base_dir='dataresource'):
    """
    在指定日期文件夹中找到包含特定关键字的图片
    策略：先检查第04张，如果没有则遍历所有图片
    
    参数:
        date_folder: 日期文件夹名，如 '20260626'
        keywords: 要查找的关键字列表，默认 ["湖南人涨停复盘"]
        base_dir: 基础目录
    
    返回:
        找到的图片路径，或 None
    """
    if keywords is None:
        keywords = ["湖南人涨停复盘"]
    
    folder_path = os.path.join(base_dir, date_folder)
    
    if not os.path.exists(folder_path):
        print(f"文件夹不存在: {folder_path}")
        return None
    
    # 获取所有png图片
    images = sorted([f for f in os.listdir(folder_path) if f.endswith('.png')])
    
    if not images:
        print(f"文件夹中没有图片: {folder_path}")
        return None
    
    # 读取顺序文件
    order_file = os.path.join(folder_path, '_order.txt')
    image_order = {}
    
    if os.path.exists(order_file):
        with open(order_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and '.png' in line and not line.startswith('#'):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            order_num = int(parts[0].replace('.', ''))
                            img_name = parts[1]
                            image_order[order_num] = img_name
                        except:
                            pass
    
    # 策略1: 先检查第04张图片
    if 4 in image_order:
        img_04 = image_order[4]
        img_path = os.path.join(folder_path, img_04)
        
        print(f"检查第04张图片: {img_04}")
        text = ocr_image_text(img_path)
        
        if check_title_in_text(text, keywords):
            print(f"✓ 第04张图片包含关键字")
            return img_path
    
    # 策略2: 遍历所有图片
    print(f"第04张未找到关键字，遍历所有图片...")
    
    for img_name in images:
        img_path = os.path.join(folder_path, img_name)
        
        # 跳过已检查的第04张
        if 4 in image_order and img_name == image_order[4]:
            continue
        
        text = ocr_image_text(img_path)
        
        if check_title_in_text(text, keywords):
            print(f"✓ 找到包含关键字的图片: {img_name}")
            return img_path
    
    print(f"✗ 未找到包含关键字的图片")
    return None


# 别名，保持向后兼容
find_image_by_title = find_image_by_content


def find_all_title_images(keywords=None, base_dir='dataresource'):
    """
    在所有日期文件夹中找到包含关键字的图片
    
    参数:
        keywords: 关键字列表，默认 ["湖南人涨停复盘"]
        base_dir: 基础目录
    
    返回:
        字典: {日期: 图片路径}
    """
    if keywords is None:
        keywords = ["湖南人涨停复盘"]
    
    result = {}
    
    if not os.path.exists(base_dir):
        print(f"目录不存在: {base_dir}")
        return result
    
    folders = sorted(os.listdir(base_dir))
    
    for date_folder in folders:
        folder_path = os.path.join(base_dir, date_folder)
        if os.path.isdir(folder_path):
            print(f"\n处理: {date_folder}")
            img_path = find_image_by_content(date_folder, keywords, base_dir)
            if img_path:
                result[date_folder] = img_path
    
    return result


if __name__ == '__main__':
    import sys
    
    print("=" * 60)
    print("查找包含'湖南人涨停复盘'标题的图片")
    print("=" * 60)
    
    if len(sys.argv) > 1:
        # 测试单个日期
        date = sys.argv[1]
        print(f"\n测试日期: {date}")
        img = find_image_by_content(date)
        if img:
            print(f"\n结果: {img}")
    else:
        # 查找所有
        print("\n查找所有日期...")
        results = find_all_title_images()
        
        print("\n" + "=" * 60)
        print("结果汇总:")
        print("=" * 60)
        for date, path in results.items():
            print(f"{date}: {path}")
        print(f"\n共找到 {len(results)} 个")
