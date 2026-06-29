#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘股吧图片下载API服务
提供按日期范围下载湖南人涨停复盘图片的接口
"""

from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import os
import time
from datetime import datetime, timedelta
import threading

app = Flask(__name__)

# Cookies
COOKIES = {
    'Hm_lvt_cc6a63a887a7d811c92b7cc41c441837': '1755527263,1756824779,1756909965',
    '_c_WBKFRo': 'P15XJw928rmXQoJUv77QsRnovgAuNGwWbZaaGvbj',
    'JSESSIONID': 'NjQ3N2ExMjktNGFmYi00YjFkLWE0YTktMDE4NzcwZTU5MzUy',
    'tgbuser': '6316826',
    'tgbpwd': '8eefec381a5d37d21d041b9acc0800041ca3bb1eb4f442d3a56cf91dc2fe10a39zemi0jagimgl1t',
    'loginStatus': 'phone',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://www.tgb.cn/',
}

IMG_HEADERS = HEADERS.copy()
IMG_HEADERS['Accept'] = 'image/webp,image/apng,image/*,*/*;q=0.8'

# 下载任务状态
download_tasks = {}


def get_article_list(user_id='444409'):
    """获取博客文章列表"""
    url = f'https://www.tgb.cn/blog/{user_id}'
    
    try:
        response = requests.get(url, cookies=COOKIES, headers=HEADERS, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = soup.find_all('div', class_='article_tittle')
            
            article_list = []
            for article in articles:
                title_tag = article.find('a')
                if title_tag:
                    title = title_tag.get('title', title_tag.text.strip())
                    href = title_tag.get('href', '')
                    link = f"https://www.tgb.cn{href}" if href.startswith('/') else href
                    
                    time_tag = article.find('div', class_='tittle_fbshijian')
                    pub_time = time_tag.text.strip() if time_tag else ''
                    date_folder = pub_time.replace('-', '') if pub_time else 'unknown'
                    
                    article_list.append({
                        'title': title,
                        'link': link,
                        'pub_time': pub_time,
                        'date_folder': date_folder
                    })
            
            return article_list
        else:
            return []
            
    except Exception as e:
        print(f"获取文章列表出错: {e}")
        return []


def get_article_images(url):
    """获取文章正文中的图片URL"""
    try:
        response = requests.get(url, cookies=COOKIES, headers=HEADERS, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            content_div = soup.find('div', class_='content-left')
            if not content_div:
                content_div = soup
            
            images = []
            content_imgs = content_div.find_all('img', attrs={'data-type': 'contentImage'})
            
            for img in content_imgs:
                img_url = img.get('data-original') or img.get('src2') or img.get('src')
                
                if img_url and 'placeHolder' not in img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        img_url = 'https://www.tgb.cn' + img_url
                    
                    images.append(img_url)
            
            images = list(dict.fromkeys(images))
            return images
        else:
            return []
            
    except Exception as e:
        print(f"获取图片出错: {e}")
        return []


def download_image(img_url, save_path):
    """下载单张图片"""
    try:
        response = requests.get(img_url, cookies=COOKIES, headers=IMG_HEADERS, timeout=30)
        
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            return True
        return False
            
    except Exception as e:
        print(f"下载图片出错: {e}")
        return False


def check_folder_has_images(folder_path):
    """检查文件夹是否已有图片"""
    if not os.path.exists(folder_path):
        return False
    
    # 检查是否有png/jpg/gif/webp文件
    for f in os.listdir(folder_path):
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            return True
    return False


def download_task(task_id, articles, base_dir='dataresource', skip_existing=True):
    """后台下载任务"""
    global download_tasks
    
    download_tasks[task_id]['status'] = 'running'
    total_images = 0
    success_images = 0
    skipped_folders = 0
    
    for i, article in enumerate(articles):
        try:
            date_folder = article['date_folder']
            link = article['link']
            title = article['title']
            
            # 创建保存目录
            save_dir = os.path.join(base_dir, date_folder)
            
            # 检查文件夹是否已有图片
            if skip_existing and check_folder_has_images(save_dir):
                print(f"跳过已存在的文件夹: {date_folder}")
                skipped_folders += 1
                # 更新进度
                progress = int((i + 1) / len(articles) * 100)
                download_tasks[task_id]['progress'] = progress
                continue
            
            os.makedirs(save_dir, exist_ok=True)
            
            # 获取图片列表
            images = get_article_images(link)
            
            if not images:
                continue
            
            # 下载图片
            for j, img_url in enumerate(images, 1):
                ext = '.png'
                if '.jpg' in img_url.lower() or '.jpeg' in img_url.lower():
                    ext = '.jpg'
                elif '.gif' in img_url.lower():
                    ext = '.gif'
                elif '.webp' in img_url.lower():
                    ext = '.webp'
                
                filename = f"{date_folder}_{j:02d}{ext}"
                save_path = os.path.join(save_dir, filename)
                
                total_images += 1
                
                if os.path.exists(save_path):
                    success_images += 1
                    continue
                
                if download_image(img_url, save_path):
                    success_images += 1
                    time.sleep(0.2)
            
            # 更新进度
            progress = int((i + 1) / len(articles) * 100)
            download_tasks[task_id]['progress'] = progress
            download_tasks[task_id]['downloaded'] = success_images
            
        except Exception as e:
            print(f"处理文章出错: {e}")
    
    download_tasks[task_id]['status'] = 'completed'
    download_tasks[task_id]['total'] = total_images
    download_tasks[task_id]['downloaded'] = success_images
    download_tasks[task_id]['skipped_folders'] = skipped_folders


# ============== API 接口 ==============

@app.route('/', methods=['GET'])
def index():
    """API首页"""
    return jsonify({
        'name': '淘股吧图片下载API',
        'version': '1.1.0',
        'endpoints': {
            'GET /api/articles': '获取文章列表',
            'POST /api/download': '按日期范围下载图片 (参数: start_date, end_date, 格式: YYYY-MM-DD)',
            'GET /api/status/<task_id>': '查询下载任务状态',
            'GET /api/files': '查看已下载的文件列表',
            'GET /api/files/<date>': '查看指定日期的文件列表',
            'GET /api/ocr/title': '通过OCR查找"湖南人涨停复盘"标题图片 (参数: date, keyword)',
            'POST /api/ocr/recognize': 'OCR识别图片文字 (参数: image_path)',
        }
    })


@app.route('/api/articles', methods=['GET'])
def list_articles():
    """获取文章列表"""
    articles = get_article_list('444409')
    
    # 可选：按日期过滤
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if start_date or end_date:
        filtered = []
        for article in articles:
            pub_date = article.get('pub_time', '')
            if start_date and pub_date < start_date:
                continue
            if end_date and pub_date > end_date:
                continue
            filtered.append(article)
        articles = filtered
    
    return jsonify({
        'success': True,
        'count': len(articles),
        'articles': articles
    })


@app.route('/api/download', methods=['POST'])
def download_by_date():
    """
    按日期范围下载图片
    参数:
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        skip_existing: 是否跳过已存在的文件夹 (默认true)
    
    示例:
        POST /api/download
        {
            "start_date": "2026-06-20",
            "end_date": "2026-06-26"
        }
    """
    data = request.get_json() or {}
    
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    skip_existing = data.get('skip_existing', True)
    
    # 验证日期格式
    try:
        if start_date:
            datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({
            'success': False,
            'error': '日期格式错误，请使用 YYYY-MM-DD 格式'
        }), 400
    
    # 获取文章列表
    articles = get_article_list('444409')
    
    # 按日期过滤
    if start_date or end_date:
        filtered = []
        for article in articles:
            pub_date = article.get('pub_time', '')
            if start_date and pub_date < start_date:
                continue
            if end_date and pub_date > end_date:
                continue
            filtered.append(article)
        articles = filtered
    
    if not articles:
        return jsonify({
            'success': False,
            'error': '没有找到符合日期范围的文章'
        }), 404
    
    # 创建下载任务
    import uuid
    task_id = str(uuid.uuid4())[:8]
    
    download_tasks[task_id] = {
        'status': 'pending',
        'progress': 0,
        'total': 0,
        'downloaded': 0,
        'articles_count': len(articles),
        'start_date': start_date,
        'end_date': end_date,
        'skip_existing': skip_existing
    }
    
    # 启动后台下载
    thread = threading.Thread(target=download_task, args=(task_id, articles, 'dataresource', skip_existing))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'task_id': task_id,
        'articles_count': len(articles),
        'message': f'开始下载 {len(articles)} 篇文章的图片',
        'skip_existing': skip_existing,
        'status_url': f'/api/status/{task_id}'
    })


@app.route('/api/download/sync', methods=['POST'])
def download_sync():
    """
    同步下载图片（会阻塞直到完成）
    参数:
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        skip_existing: 是否跳过已存在的文件夹 (默认true)
    """
    data = request.get_json() or {}
    
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    skip_existing = data.get('skip_existing', True)
    
    # 验证日期格式
    try:
        if start_date:
            datetime.strptime(start_date, '%Y-%m-%d')
        if end_date:
            datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({
            'success': False,
            'error': '日期格式错误，请使用 YYYY-MM-DD 格式'
        }), 400
    
    # 获取文章列表
    articles = get_article_list('444409')
    
    # 按日期过滤
    if start_date or end_date:
        filtered = []
        for article in articles:
            pub_date = article.get('pub_time', '')
            if start_date and pub_date < start_date:
                continue
            if end_date and pub_date > end_date:
                continue
            filtered.append(article)
        articles = filtered
    
    if not articles:
        return jsonify({
            'success': False,
            'error': '没有找到符合日期范围的文章'
        }), 404
    
    # 开始下载
    base_dir = 'dataresource'
    total_images = 0
    success_images = 0
    skipped_folders = []
    results = []
    
    for article in articles:
        date_folder = article['date_folder']
        link = article['link']
        
        save_dir = os.path.join(base_dir, date_folder)
        
        # 检查文件夹是否已有图片
        if skip_existing and check_folder_has_images(save_dir):
            skipped_folders.append(date_folder)
            results.append({
                'date': article['pub_time'],
                'title': article['title'],
                'status': 'skipped',
                'reason': 'folder already exists with images'
            })
            continue
        
        os.makedirs(save_dir, exist_ok=True)
        
        images = get_article_images(link)
        
        if not images:
            continue
        
        article_result = {
            'date': article['pub_time'],
            'title': article['title'],
            'images_found': len(images),
            'images_downloaded': 0,
            'status': 'downloaded'
        }
        
        for i, img_url in enumerate(images, 1):
            ext = '.png'
            if '.jpg' in img_url.lower():
                ext = '.jpg'
            
            filename = f"{date_folder}_{i:02d}{ext}"
            save_path = os.path.join(save_dir, filename)
            
            total_images += 1
            
            if os.path.exists(save_path):
                success_images += 1
                article_result['images_downloaded'] += 1
                continue
            
            if download_image(img_url, save_path):
                success_images += 1
                article_result['images_downloaded'] += 1
                time.sleep(0.2)
        
        results.append(article_result)
    
    return jsonify({
        'success': True,
        'total_images': total_images,
        'downloaded_images': success_images,
        'skipped_folders': skipped_folders,
        'skip_existing': skip_existing,
        'articles': results
    })


@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """查询下载任务状态"""
    if task_id not in download_tasks:
        return jsonify({
            'success': False,
            'error': '任务不存在'
        }), 404
    
    task = download_tasks[task_id]
    
    return jsonify({
        'success': True,
        'task_id': task_id,
        'status': task['status'],
        'progress': task['progress'],
        'total': task['total'],
        'downloaded': task['downloaded'],
        'articles_count': task['articles_count']
    })


@app.route('/api/files', methods=['GET'])
def list_files():
    """查看已下载的文件列表"""
    base_dir = 'dataresource'
    
    if not os.path.exists(base_dir):
        return jsonify({
            'success': True,
            'folders': [],
            'total_files': 0
        })
    
    folders = []
    total_files = 0
    
    for date_folder in sorted(os.listdir(base_dir), reverse=True):
        folder_path = os.path.join(base_dir, date_folder)
        if os.path.isdir(folder_path):
            files = [f for f in os.listdir(folder_path) if f.endswith(('.png', '.jpg', '.gif', '.webp'))]
            folders.append({
                'date': date_folder,
                'file_count': len(files),
                'files': files
            })
            total_files += len(files)
    
    return jsonify({
        'success': True,
        'folders': folders,
        'total_files': total_files
    })


@app.route('/api/files/<date>', methods=['GET'])
def get_files_by_date(date):
    """获取指定日期的文件列表"""
    base_dir = 'dataresource'
    folder_path = os.path.join(base_dir, date)
    
    if not os.path.exists(folder_path):
        return jsonify({
            'success': False,
            'error': f'日期 {date} 没有下载的文件'
        }), 404
    
    files = [f for f in os.listdir(folder_path) if f.endswith(('.png', '.jpg', '.gif', '.webp'))]
    
    return jsonify({
        'success': True,
        'date': date,
        'file_count': len(files),
        'files': files,
        'folder_path': os.path.abspath(folder_path)
    })


@app.route('/api/ocr/title', methods=['GET'])
def find_title_image():
    """
    通过OCR识别查找包含"湖南人涨停复盘"标题的图片
    
    参数:
        date: 指定日期 (可选，格式: YYYYMMDD)
        keyword: 搜索关键字 (可选，默认: 湖南人涨停复盘)
    
    示例:
        GET /api/ocr/title                    # 查找所有日期的标题图片
        GET /api/ocr/title?date=20260626      # 查找指定日期的标题图片
    """
    from utils import find_image_by_content, find_all_title_images
    
    date = request.args.get('date')
    keyword = request.args.get('keyword', '湖南人涨停复盘')
    
    if date:
        # 查找指定日期
        result = find_image_by_content(date, [keyword])
        
        if result:
            return jsonify({
                'success': True,
                'date': date,
                'keyword': keyword,
                'image_path': result,
                'image_name': os.path.basename(result)
            })
        else:
            return jsonify({
                'success': False,
                'date': date,
                'keyword': keyword,
                'error': '未找到包含关键字的图片'
            }), 404
    else:
        # 查找所有日期
        all_titles = find_all_title_images(keywords=[keyword])
        
        return jsonify({
            'success': True,
            'keyword': keyword,
            'count': len(all_titles),
            'images': [
                {
                    'date': date,
                    'image_path': path,
                    'image_name': os.path.basename(path)
                }
                for date, path in sorted(all_titles.items())
            ]
        })


@app.route('/api/ocr/recognize', methods=['POST'])
def ocr_recognize():
    """
    OCR识别指定图片的文字内容
    
    参数:
        image_path: 图片路径
    
    示例:
        POST /api/ocr/recognize
        {"image_path": "dataresource/20260626/n05q339s0imk.png"}
    """
    from utils import ocr_image
    
    data = request.get_json() or {}
    image_path = data.get('image_path')
    
    if not image_path:
        return jsonify({
            'success': False,
            'error': '请提供 image_path 参数'
        }), 400
    
    if not os.path.exists(image_path):
        return jsonify({
            'success': False,
            'error': f'图片不存在: {image_path}'
        }), 404
    
    texts = ocr_image(image_path)
    
    return jsonify({
        'success': True,
        'image_path': image_path,
        'text_count': len(texts),
        'texts': [
            {'text': t, 'confidence': float(c)}
            for t, c in texts
        ]
    })


if __name__ == '__main__':
    print("=" * 60)
    print("淘股吧图片下载API服务")
    print("=" * 60)
    print("\n启动服务: http://127.0.0.1:5000")
    print("\nAPI接口:")
    print("  GET  /                      - API首页")
    print("  GET  /api/articles          - 获取文章列表")
    print("  POST /api/download          - 异步下载 (按日期范围)")
    print("  POST /api/download/sync     - 同步下载 (按日期范围)")
    print("  GET  /api/status/<task_id>  - 查询下载状态")
    print("  GET  /api/files             - 查看已下载文件")
    print("  GET  /api/files/<date>      - 查看指定日期文件")
    print("  GET  /api/ocr/title         - OCR查找标题图片")
    print("  POST /api/ocr/recognize     - OCR识别图片文字")
    print("\n示例:")
    print('  curl -X POST http://127.0.0.1:5000/api/download -H "Content-Type: application/json" -d \'{"start_date":"2026-06-20","end_date":"2026-06-26"}\'')
    print('  curl "http://127.0.0.1:5000/api/ocr/title?date=20260626"')
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=True)
