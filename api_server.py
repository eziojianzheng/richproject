#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘股吧图片下载API服务
提供按日期范围下载湖南人涨停复盘图片的接口
"""

# 启动时自动检测并安装缺失依赖
from bootstrap import ensure_dependencies
ensure_dependencies({
    'flask': 'flask>=3.0.0',
    'requests': 'requests>=2.31.0',
    'bs4': 'beautifulsoup4>=4.12.0',
})

from flask import Flask, request, jsonify, render_template
import requests
from bs4 import BeautifulSoup
import os
import time
from datetime import datetime, timedelta
import threading

app = Flask(__name__)

# 导入热门股追踪模块
import hot_track as ht

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

# 重试配置
MAX_RETRIES = 3        # 最大尝试次数（含首次）
RETRY_BACKOFF = 1.0    # 退避基数（秒），按 1s -> 2s -> 4s 递增
REQUEST_TIMEOUT = 30   # 单次请求超时（秒）

# 需要重试的网络类异常
_RETRYABLE_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class NetworkError(Exception):
    """网络请求在多次重试后仍失败时抛出"""
    pass


def request_with_retry(url, headers, max_retries=MAX_RETRIES, backoff=RETRY_BACKOFF):
    """
    带指数退避重试的 GET 请求

    - 仅对网络类异常（超时/连接错误）重试，HTTP 状态码错误不在此处重试
    - 重试间隔按指数递增: backoff * 2^(n-1)

    返回:
        requests.Response

    异常:
        NetworkError: 所有重试均失败时抛出
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return requests.get(
                url, cookies=COOKIES, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff * (2 ** (attempt - 1))
                print(f"    网络异常({type(e).__name__})，{wait:.0f}s 后重试 "
                      f"[{attempt}/{max_retries - 1}]...")
                time.sleep(wait)
            else:
                print(f"    已重试 {max_retries} 次仍失败: {type(e).__name__}")
    raise NetworkError(str(last_exc))


# 下载任务状态
download_tasks = {}


def get_article_list(user_id='444409'):
    """获取博客文章列表"""
    url = f'https://www.tgb.cn/blog/{user_id}'

    try:
        response = request_with_retry(url, HEADERS)
    except NetworkError as e:
        print(f"获取文章列表失败（网络）: {e}")
        return []

    response.encoding = 'utf-8'

    if response.status_code != 200:
        print(f"获取文章列表失败（HTTP {response.status_code}）")
        return []

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


def get_article_images(url):
    """
    获取文章正文中的图片URL

    异常:
        NetworkError: 网络请求重试后仍失败时抛出（由调用方区分"网络失败"与"无图片"）
    """
    response = request_with_retry(url, HEADERS)
    response.encoding = 'utf-8'

    if response.status_code != 200:
        print(f"  获取图片失败（HTTP {response.status_code}）")
        return []

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


def download_image(img_url, save_path):
    """
    下载单张图片

    返回:
        True  下载成功
        False 下载失败（网络重试失败或HTTP错误）
    """
    try:
        response = request_with_retry(img_url, IMG_HEADERS)
    except NetworkError as e:
        print(f"    下载失败（网络）: {e}")
        return False

    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return True

    print(f"    下载失败（HTTP {response.status_code}）")
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
    failed_dates = []

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
            
            # 获取图片列表（网络失败时标记该日期待重试）
            try:
                images = get_article_images(link)
            except NetworkError as e:
                print(f"获取图片列表失败（网络）: {date_folder} - {e}")
                failed_dates.append(date_folder)
                progress = int((i + 1) / len(articles) * 100)
                download_tasks[task_id]['progress'] = progress
                continue
            
            if not images:
                continue
            
            # 下载图片
            article_failed = False
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
                else:
                    article_failed = True
            
            # 部分图片下载失败，标记该日期待重试
            if article_failed and date_folder not in failed_dates:
                failed_dates.append(date_folder)
            
            # 更新进度
            progress = int((i + 1) / len(articles) * 100)
            download_tasks[task_id]['progress'] = progress
            download_tasks[task_id]['downloaded'] = success_images
            
        except Exception as e:
            print(f"处理文章出错: {e}")
            failed_dates.append(article.get('date_folder', 'unknown'))
    
    download_tasks[task_id]['status'] = 'completed'
    download_tasks[task_id]['total'] = total_images
    download_tasks[task_id]['downloaded'] = success_images
    download_tasks[task_id]['skipped_folders'] = skipped_folders
    download_tasks[task_id]['failed_dates'] = failed_dates


# ============== API 接口 ==============

@app.route('/', methods=['GET'])
def index():
    """API首页"""
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>淘股吧数据服务</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 900px; margin: 50px auto; padding: 20px; }
            h1 { color: #333; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }
            .version { color: #666; font-size: 14px; }
            .nav-links { margin: 30px 0; }
            .nav-links a { display: inline-block; background: #4CAF50; color: white; padding: 12px 24px; margin-right: 10px; text-decoration: none; border-radius: 6px; font-weight: 600; }
            .nav-links a:hover { background: #45a049; }
            .nav-links a.secondary { background: #2196F3; }
            .nav-links a.secondary:hover { background: #1976D2; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { text-align: left; padding: 12px; border-bottom: 1px solid #ddd; }
            th { background-color: #4CAF50; color: white; }
            tr:hover { background-color: #f5f5f5; }
            code { background-color: #f4f4f4; padding: 2px 6px; border-radius: 3px; color: #d63384; }
            .method-get { color: #28a745; font-weight: bold; }
            .method-post { color: #007bff; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>淘股吧数据服务</h1>
        <p class="version">版本: 1.2.0</p>
        
        <div class="nav-links">
            <a href="/hot">🔥 热门股追踪</a>
            <a href="/api/files" class="secondary">📁 已下载文件</a>
        </div>
        
        <h2>API接口</h2>
        <table>
            <tr><th>方法</th><th>路径</th><th>说明</th></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/hot</code></td><td>热门股追踪页面</td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/articles</code></td><td>获取文章列表</td></tr>
            <tr><td><span class="method-post">POST</span></td><td><code>/api/download</code></td><td>按日期范围下载图片<br><small>参数: start_date, end_date (格式: YYYY-MM-DD)</small></td></tr>
            <tr><td><span class="method-post">POST</span></td><td><code>/api/download/sync</code></td><td>同步下载图片（阻塞式）</td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/status/&lt;task_id&gt;</code></td><td>查询下载任务状态</td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/files</code></td><td>查看已下载的文件列表</td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/files/&lt;date&gt;</code></td><td>查看指定日期的文件列表</td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/ocr/title</code></td><td>通过OCR查找"湖南人涨停复盘"标题图片<br><small>参数: date, keyword</small></td></tr>
            <tr><td><span class="method-post">POST</span></td><td><code>/api/ocr/recognize</code></td><td>OCR识别图片文字<br><small>参数: image_path</small></td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/hot/track</code></td><td>热门股追踪数据<br><small>参数: start, end (格式: YYYYMMDD)</small></td></tr>
            <tr><td><span class="method-get">GET</span></td><td><code>/api/hot/dates</code></td><td>获取已有数据的日期列表</td></tr>
        </table>
        
        <h2>使用示例</h2>
        <pre style="background:#f4f4f4;padding:15px;border-radius:5px;overflow-x:auto;">
# 下载图片
curl -X POST http://127.0.0.1:5000/api/download \\
  -H "Content-Type: application/json" \\
  -d '{"start_date":"2026-05-14","end_date":"2026-05-20"}'

# 查询任务状态
curl http://127.0.0.1:5000/api/status/abc123

# 热门股追踪
curl "http://127.0.0.1:5000/api/hot/track?start=20260601&end=20260605"
        </pre>
    </body>
    </html>
    '''
    return html


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
        
        try:
            images = get_article_images(link)
        except NetworkError as e:
            results.append({
                'date': article['pub_time'],
                'title': article['title'],
                'status': 'failed',
                'reason': f'网络请求失败: {e}'
            })
            continue
        
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
        'articles_count': task['articles_count'],
        'failed_dates': task.get('failed_dates', [])
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


# ============== 热门股追踪页面 ==============

@app.route('/hot', methods=['GET'])
def hot_track_page():
    """热门股追踪页面"""
    return render_template('hot_track.html')


@app.route('/api/hot/track', methods=['GET'])
def api_hot_track():
    """热门股追踪数据API"""
    import re
    
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    sort = request.args.get('sort', 'stock_count')
    with_price = request.args.get('price', '1') not in ('0', 'false', 'no')
    
    # 验证日期格式
    if not (start and re.match(r'^\d{8}$', start)) or not (end and re.match(r'^\d{8}$', end)):
        return jsonify({'success': False, 'error': '日期格式错误, 请用 YYYYMMDD'}), 400
    
    if start > end:
        start, end = end, start
    
    if sort not in ('stock_count', 'days', 'times', 'total'):
        sort = 'stock_count'
    
    # 转换排序参数
    sort_map = {'times': 'stock_count', 'total': 'days'}
    actual_sort = sort_map.get(sort, sort)
    
    try:
        data = ht.track_hot_stocks(start, end, sort=actual_sort, with_price=with_price)
    except Exception as e:
        return jsonify({'success': False, 'error': f'统计失败: {e}'}), 500
    
    # 检查数据完整性
    missing_stats = {
        'pct': 0,        # 缺少涨幅
        'ma10': 0,       # 缺少MA10
        'below_ma10': 0, # 缺少跌破状态
        'details': []    # 详细信息
    }
    
    if with_price:
        for day in data.get('by_date', []):
            for block in day.get('blocks', []):
                for stock in block.get('stocks', []):
                    track = stock.get('track', {}).get(day['date'], {})
                    missing = []
                    
                    if track.get('pct') is None:
                        missing_stats['pct'] += 1
                        missing.append('涨幅')
                    
                    if track.get('ma10') is None:
                        missing_stats['ma10'] += 1
                        missing.append('MA10')
                    
                    if track.get('below_ma10') is None:
                        missing_stats['below_ma10'] += 1
                        missing.append('跌破状态')
                    
                    if missing and len(missing_stats['details']) < 10:
                        missing_stats['details'].append({
                            'code': stock['code'],
                            'name': stock['name'],
                            'date': day['date'],
                            'missing': missing
                        })
    
    return jsonify({
        'success': True, 
        **data,
        'data_integrity': missing_stats
    })


@app.route('/api/hot/dates', methods=['GET'])
def api_hot_dates():
    """返回当前已有数据的所有日期"""
    dates = sorted(ht.list_excel_dates().keys())
    return jsonify({'success': True, 'dates': dates})


@app.route('/api/hot/sync', methods=['POST'])
def api_hot_sync():
    """
    同步指定板块的股票涨幅数据
    
    参数:
        codes: 股票代码列表
        date: 日期 (YYYYMMDD)
    """
    data = request.get_json() or {}
    codes = data.get('codes', [])
    date = data.get('date', '')
    
    if not codes or not date:
        return jsonify({'success': False, 'error': '缺少参数'}), 400
    
    # 只获取缺失的股票数据
    cache = ht._load_price_cache()
    need_fetch = [c for c in codes if f'{c}_{date}' not in cache]
    
    if not need_fetch:
        return jsonify({'success': True, 'message': '所有股票数据已存在', 'fetched': 0})
    
    # 同步获取
    import time
    success = 0
    for c in need_fetch:
        try:
            if ht.fetch_range(c, date, date):
                success += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"同步{c}失败: {e}")
    
    return jsonify({
        'success': True,
        'message': f'成功获取 {success}/{len(need_fetch)} 只股票数据',
        'fetched': success,
        'total': len(need_fetch)
    })


@app.route('/api/hot/sync-ma10', methods=['POST'])
def api_hot_sync_ma10():
    """
    同步所有股票的MA10数据
    
    参数:
        start: 开始日期 (YYYYMMDD)
        end: 结束日期 (YYYYMMDD)
    
    返回:
        同步报告，包含成功/失败列表
    """
    data = request.get_json() or {}
    start = data.get('start', '')
    end = data.get('end', '')
    
    if not start or not end:
        return jsonify({'success': False, 'error': '缺少日期参数'}), 400
    
    # 获取该日期范围内的所有股票
    try:
        track_data = ht.track_hot_stocks(start, end, with_price=False)
    except Exception as e:
        return jsonify({'success': False, 'error': f'获取股票列表失败: {e}'}), 500
    
    # 收集所有股票代码和日期
    codes = set()
    dates = track_data.get('dates', [])
    for day in track_data.get('by_date', []):
        for block in day.get('blocks', []):
            for stock in block.get('stocks', []):
                codes.add(stock['code'])
    
    if not codes:
        return jsonify({'success': True, 'message': '没有需要同步的股票', 'report': {}})
    
    # 检查缓存中缺少MA10的数据
    cache = ht._load_price_cache()
    missing_report = {
        'total_stocks': len(codes),
        'total_dates': len(dates),
        'missing_ma10': [],
        'missing_below': [],
        'already_have': 0
    }
    
    for code in codes:
        for d in dates:
            has_ma10 = f'{code}_{d}_ma10' in cache
            has_below = f'{code}_{d}_below_ma10' in cache
            
            if not has_ma10:
                missing_report['missing_ma10'].append({'code': code, 'date': d})
            if not has_below:
                missing_report['missing_below'].append({'code': code, 'date': d})
            if has_ma10 and has_below:
                missing_report['already_have'] += 1
    
    # 重新获取缺少MA10数据的股票
    need_fetch = set(item['code'] for item in missing_report['missing_ma10'])
    
    import time
    success = 0
    failed = []
    for i, code in enumerate(need_fetch):
        try:
            # 获取更长时间范围的数据以确保有足够历史数据计算MA10
            if ht.fetch_range_akshare(code, start, end):
                success += 1
            time.sleep(0.5)
            if (i + 1) % 10 == 0:
                print(f"同步MA10进度: {i + 1}/{len(need_fetch)}")
        except Exception as e:
            failed.append({'code': code, 'error': str(e)})
            time.sleep(1)
    
    # 重新检查
    cache = ht._load_price_cache()
    still_missing = []
    for item in missing_report['missing_ma10']:
        if f"{item['code']}_{item['date']}_ma10" not in cache:
            still_missing.append(item)
    
    return jsonify({
        'success': True,
        'message': f'完成: 成功获取 {success}/{len(need_fetch)} 只股票MA10数据',
        'report': {
            'total_stocks': len(codes),
            'total_dates': len(dates),
            'missing_before': len(missing_report['missing_ma10']),
            'fetched': success,
            'failed': failed,
            'still_missing': still_missing[:20],  # 只返回前20条
            'still_missing_count': len(still_missing)
        }
    })


@app.route('/api/hot/cache/clear', methods=['POST'])
def api_hot_cache_clear():
    """
    清除涨幅缓存
    """
    try:
        cache_file = ht._PRICE_CACHE_FILE
        if os.path.exists(cache_file):
            os.remove(cache_file)
            ht._price_cache = None  # 重置内存缓存
            return jsonify({'success': True, 'message': '缓存已清除'})
        else:
            return jsonify({'success': True, 'message': '缓存文件不存在'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hot/refetch', methods=['POST'])
def api_hot_refetch():
    """
    重新获取股票涨幅数据（仅获取缺失数据）
    
    参数:
        codes: 股票代码列表
        start: 开始日期 (YYYYMMDD)
        end: 结束日期 (YYYYMMDD)
    """
    data = request.get_json() or {}
    codes = data.get('codes', [])
    start = data.get('start', '')
    end = data.get('end', '')
    
    if not codes or not start or not end:
        return jsonify({'success': False, 'error': '缺少参数'}), 400
    
    # 生成日期范围内所有交易日
    from datetime import datetime, timedelta
    try:
        s_dt = datetime.strptime(start, '%Y%m%d')
        e_dt = datetime.strptime(end, '%Y%m%d')
    except:
        return jsonify({'success': False, 'error': '日期格式错误'}), 400
    
    query_dates = []
    current = s_dt
    while current <= e_dt:
        if current.weekday() < 5:  # 只统计工作日
            query_dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    
    # 检查缓存，只获取缺失数据的股票
    cache = ht._load_price_cache()
    need_fetch = []
    for c in codes:
        # 检查该股票是否有任意一天的涨幅数据
        has_data = any(f'{c}_{d}' in cache for d in query_dates)
        if not has_data:
            need_fetch.append(c)
    
    if not need_fetch:
        return jsonify({
            'success': True, 
            'message': f'所有 {len(codes)} 只股票数据已存在，无需重新获取',
            'fetched': 0,
            'skipped': len(codes)
        })
    
    import time
    success = 0
    for i, c in enumerate(need_fetch):
        try:
            if ht.fetch_range(c, start, end):
                success += 1
            time.sleep(0.5)
            if (i + 1) % 10 == 0:
                print(f"已处理 {i + 1}/{len(need_fetch)}，成功 {success}")
        except Exception as e:
            print(f"获取 {c} 失败: {e}")
            time.sleep(1)
    
    return jsonify({
        'success': True,
        'message': f'完成: 成功获取 {success}/{len(need_fetch)} 只股票数据（跳过 {len(codes) - len(need_fetch)} 只有数据）',
        'fetched': success,
        'skipped': len(codes) - len(need_fetch)
    })


if __name__ == '__main__':
    print("=" * 60)
    print("淘股吧数据服务")
    print("=" * 60)
    print("\n启动服务: http://127.0.0.1:5000")
    print("\n页面:")
    print("  GET  /                      - API首页")
    print("  GET  /hot                   - 热门股追踪页面")
    print("\nAPI接口:")
    print("  GET  /api/articles          - 获取文章列表")
    print("  POST /api/download          - 异步下载 (按日期范围)")
    print("  POST /api/download/sync     - 同步下载 (按日期范围)")
    print("  GET  /api/status/<task_id>  - 查询下载状态")
    print("  GET  /api/files             - 查看已下载文件")
    print("  GET  /api/files/<date>      - 查看指定日期文件")
    print("  GET  /api/ocr/title         - OCR查找标题图片")
    print("  POST /api/ocr/recognize     - OCR识别图片文字")
    print("  GET  /api/hot/track         - 热门股追踪数据")
    print("  GET  /api/hot/dates         - 获取已有数据日期")
    print("\n示例:")
    print('  curl -X POST http://127.0.0.1:5000/api/download -H "Content-Type: application/json" -d \'{"start_date":"2026-05-14","end_date":"2026-05-20"}\'')
    print('  curl "http://127.0.0.1:5000/api/hot/track?start=20260601&end=20260605"')
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=True)
