#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘股吧图片下载API服务
提供按日期范围下载湖南人涨停复盘图片的接口
"""

# ============== 调试日志 ==============
# 正式使用时设为 False，_dlog 变成空函数，零性能开销
DEBUG_MODE = True

import logging as _logging
_dbg_logger = _logging.getLogger('ai_kanpan')
if DEBUG_MODE:
    _dbg_handler = _logging.FileHandler('_debug.log', encoding='utf-8')
    _dbg_handler.setFormatter(_logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
    _dbg_logger.addHandler(_dbg_handler)
    _dbg_logger.setLevel(_logging.DEBUG)

def _dlog(msg, level='DEBUG'):
    if not DEBUG_MODE:
        return
    getattr(_dbg_logger, level.lower(), _dbg_logger.debug)(msg)

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
import re
import json
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

# 提取任务状态
extract_tasks = {}

# 入库(上传)任务状态
submit_tasks = {}

# 热门股计算任务状态
hot_tasks = {}

# 最近一次热门股计算结果(服务端持久化, 刷新后恢复, 不受浏览器localStorage配额限制)
hot_last = None
HOT_LAST_FILE = '.hot_last_result.json'


def _save_hot_last():
    try:
        with open(HOT_LAST_FILE, 'w', encoding='utf-8') as f:
            json.dump(hot_last, f, ensure_ascii=False)
    except Exception as e:
        print(f'保存热门股结果缓存失败: {e}')


def _load_hot_last():
    global hot_last
    try:
        if os.path.exists(HOT_LAST_FILE):
            with open(HOT_LAST_FILE, 'r', encoding='utf-8') as f:
                hot_last = json.load(f)
    except Exception:
        hot_last = None


def _invalidate_hot_cache():
    """新数据入库后失效热门股缓存，防止前端展示过期结果"""
    global hot_last
    hot_last = None
    try:
        if os.path.exists(HOT_LAST_FILE):
            os.remove(HOT_LAST_FILE)
    except Exception:
        pass


_load_hot_last()  # 启动时载入上次计算结果


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


# ============== A股交易日历 ==============

_TRADE_DAYS = None       # set of 'YYYYMMDD'
_TRADE_DAYS_MAX = None   # 日历覆盖的最大日期(用于判断"超出日历范围")


def get_trade_days():
    """
    返回A股交易日集合(YYYYMMDD)，用 akshare 交易日历，带内存缓存。
    获取失败返回 None（调用方回退到工作日判断）。
    """
    global _TRADE_DAYS, _TRADE_DAYS_MAX
    if _TRADE_DAYS is not None:
        return _TRADE_DAYS
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = {str(d).replace('-', '') for d in df['trade_date']}
        if not days:
            return None
        _TRADE_DAYS = days
        _TRADE_DAYS_MAX = max(days)
        print(f"[交易日历] 已加载 {len(days)} 个交易日 (至 {_TRADE_DAYS_MAX})")
        return _TRADE_DAYS
    except Exception as e:
        print(f"[交易日历] 获取失败，回退到工作日判断: {e}")
        return None


def is_trading_day(date_folder):
    """
    判断 date_folder(YYYYMMDD) 是否为A股交易日。
    - 交易日历可用: 命中集合即为交易日；若日期超出日历覆盖范围则回退到工作日判断
    - 交易日历不可用: 回退到周一~周五判断
    """
    from datetime import datetime
    days = get_trade_days()
    if days:
        # 超出日历覆盖范围（比最新已知交易日还新）时回退到工作日判断
        if _TRADE_DAYS_MAX and date_folder > _TRADE_DAYS_MAX:
            try:
                return datetime.strptime(date_folder, '%Y%m%d').weekday() < 5
            except ValueError:
                return True
        return date_folder in days
    # 日历不可用: 工作日
    try:
        return datetime.strptime(date_folder, '%Y%m%d').weekday() < 5
    except ValueError:
        return True


def filter_trading_articles(articles):
    """只保留A股交易日的文章，返回 (保留列表, 被跳过的非交易日日期列表)"""
    kept, dropped = [], []
    for a in articles:
        if is_trading_day(a.get('date_folder', '')):
            kept.append(a)
        else:
            dropped.append(a.get('date_folder', 'unknown'))
    if dropped:
        print(f"跳过非交易日文章: {dropped}")
    return kept, dropped


def trading_days_in_range(start8, end8):
    """返回 [start8, end8] 区间内的所有A股交易日(YYYYMMDD)，升序。"""
    from datetime import datetime as _dt, timedelta as _td
    try:
        s = _dt.strptime(start8, '%Y%m%d')
        e = _dt.strptime(end8, '%Y%m%d')
    except ValueError:
        return []
    if s > e:
        s, e = e, s
    days = []
    cur = s
    while cur <= e:
        d8 = cur.strftime('%Y%m%d')
        if is_trading_day(d8):
            days.append(d8)
        cur += _td(days=1)
    return days


def download_task(task_id, articles, base_dir='dataresource', skip_existing=True):
    """后台下载任务"""
    global download_tasks
    
    download_tasks[task_id]['status'] = 'running'
    total_images = 0
    success_images = 0
    skipped_folders = 0
    failed_dates = []
    failed_details = []  # [{'date':..., 'reason':...}]

    def _mark_failed(date_folder, reason):
        if date_folder not in failed_dates:
            failed_dates.append(date_folder)
            failed_details.append({'date': date_folder, 'reason': reason})

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
                _mark_failed(date_folder, f"获取图片列表失败（网络）: {e}")
                progress = int((i + 1) / len(articles) * 100)
                download_tasks[task_id]['progress'] = progress
                continue
            
            if not images:
                continue
            
            # 下载图片（按图片ID命名，与CLI下载器一致，便于 _order.txt 完整性核对与提取查找）
            article_failed = False
            id_list = []
            for j, img_url in enumerate(images, 1):
                m = re.search(r'/([a-z0-9]+)\.(?:png|jpg|jpeg|gif|webp)', img_url, re.I)
                img_id = m.group(1) if m else f"{date_folder}_{j:02d}"
                id_list.append(img_id)
                filename = f"{img_id}.png"
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
            
            # 写入 _order.txt（图片顺序映射，供完整性核对与提取查找）
            try:
                with open(os.path.join(save_dir, '_order.txt'), 'w', encoding='utf-8') as f:
                    f.write(f"# {title}\n")
                    f.write(f"# 日期: {article.get('pub_time', '')}\n")
                    f.write(f"# 图片顺序映射\n\n")
                    for k, iid in enumerate(id_list, 1):
                        f.write(f"{k:02d}. {iid}.png\n")
            except Exception as e:
                print(f"  写入_order.txt失败: {e}")
            
            # 强制重下且已补齐时，清理旧命名/多余的图片文件，避免与ID命名文件重复
            if not skip_existing and not article_failed:
                expected = {f"{iid}.png" for iid in id_list}
                for fn in os.listdir(save_dir):
                    if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')) and fn not in expected:
                        try:
                            os.remove(os.path.join(save_dir, fn))
                            print(f"  清理旧文件: {fn}")
                        except Exception:
                            pass
            
            # 部分图片下载失败，标记该日期待重试
            if article_failed:
                _mark_failed(date_folder, "部分图片下载失败（网络重试后仍失败）")
            
            # 更新进度
            progress = int((i + 1) / len(articles) * 100)
            download_tasks[task_id]['progress'] = progress
            download_tasks[task_id]['downloaded'] = success_images
            
        except Exception as e:
            print(f"处理文章出错: {e}")
            _mark_failed(article.get('date_folder', 'unknown'), f"处理异常: {e}")
    
    download_tasks[task_id]['status'] = 'completed'
    download_tasks[task_id]['total'] = total_images
    download_tasks[task_id]['downloaded'] = success_images
    download_tasks[task_id]['skipped_folders'] = skipped_folders
    download_tasks[task_id]['failed_dates'] = failed_dates
    download_tasks[task_id]['failed_details'] = failed_details


def extract_task(task_id, dates, submit_to_db=True, base_dir='dataresource',
                 output_dir='excelDataSource'):
    """
    后台提取任务：对每个日期跑 extract_glm 生成 Excel，可选提交入库。
    dates: 交易日列表(YYYYMMDD)
    """
    global extract_tasks
    import extract_glm as eg
    import db as _db

    t = extract_tasks[task_id]
    t['status'] = 'running'
    total = len(dates)
    done = extracted = submitted = 0

    # 每个日期一条实时状态：pending/extracting/submitting/submitted/extracted/failed
    items = [{'date': d, 'status': 'pending', 'message': '待处理'} for d in dates]
    t['items'] = items

    for i, d in enumerate(dates):
        it = items[i]
        folder = os.path.join(base_dir, d)
        try:
            if not os.path.isdir(folder) or not check_folder_has_images(folder):
                it['status'] = 'failed'
                it['message'] = '未下载图片（无 dataresource 文件夹）'
            else:
                it['status'] = 'extracting'
                it['message'] = '正在提取…'
                ok = eg.extract_date(d, base_dir, output_dir)
                if not ok:
                    it['status'] = 'failed'
                    it['message'] = '提取失败（未找到03/04或识别失败）'
                else:
                    extracted += 1
                    if submit_to_db:
                        it['status'] = 'submitting'
                        it['message'] = '正在入库…'
                        try:
                            _db.submit_date(d, output_dir)
                            submitted += 1
                            it['status'] = 'submitted'
                            it['message'] = '入库成功'
                            _invalidate_hot_cache()
                        except _db.NotVerifiedError:
                            it['status'] = 'need_review'
                            it['message'] = '已提取(manualcheck)，需人工复核后再入库'
                        except _db.DBError as e:
                            it['status'] = 'failed'
                            it['message'] = f'入库失败: {e}'
                        except Exception as e:
                            it['status'] = 'failed'
                            it['message'] = f'入库异常: {e}'
                    else:
                        it['status'] = 'extracted'
                        it['message'] = '提取成功'
        except Exception as e:
            # 单个日期异常不中断，继续下一个
            it['status'] = 'failed'
            it['message'] = f'提取异常: {e}'

        done += 1
        t['progress'] = int(done / max(total, 1) * 100)
        t['extracted'] = extracted
        t['submitted'] = submitted

    t['status'] = 'completed'
    t['extracted'] = extracted
    t['submitted'] = submitted


def submit_batch_task(task_id, dates, output_dir='excelDataSource'):
    """
    后台批量入库任务：按交易日逐个把已有 Excel 上传到数据库。
    Excel 不存在的日期标记 no_excel（在前端下方显示），失败则继续下一个。
    """
    global submit_tasks
    import db as _db

    t = submit_tasks[task_id]
    t['status'] = 'running'
    total = len(dates)
    done = submitted = 0
    items = [{'date': d, 'status': 'pending', 'message': '待处理'} for d in dates]
    t['items'] = items

    try:
        _db.init_db()
    except Exception:
        pass

    for i, d in enumerate(dates):
        it = items[i]
        try:
            fp, status = _db.find_excel(d, output_dir)
            if not fp:
                it['status'] = 'no_excel'
                it['message'] = 'Excel不存在（需先提取）'
            elif status != 'verified':
                it['status'] = 'need_review'
                it['message'] = f'{status} 需人工复核，未入库'
            else:
                it['status'] = 'submitting'
                it['message'] = '正在入库…'
                _db.submit_date(d, output_dir)
                submitted += 1
                it['status'] = 'submitted'
                it['message'] = '入库成功（verified）'
                _invalidate_hot_cache()
        except _db.DBError as e:
            it['status'] = 'failed'
            it['message'] = f'入库失败: {e}'
        except Exception as e:
            it['status'] = 'failed'
            it['message'] = f'入库异常: {e}'

        done += 1
        t['progress'] = int(done / max(total, 1) * 100)
        t['submitted'] = submitted

    t['status'] = 'completed'
    t['submitted'] = submitted


# ============== API 接口 ==============

@app.route('/', methods=['GET'])
def index():
    """导航首页：更新数据 / 热门股追踪 / 盯盘(待开发)"""
    return render_template('nav.html')


@app.route('/update', methods=['GET'])
def update_page():
    """更新数据页面：按日期下载涨停复盘图片、查看已下载数据"""
    return render_template('update.html')


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

    # 只保留A股交易日的文章（跳过周末/节假日等非交易日）
    articles, _dropped_non_trading = filter_trading_articles(articles)

    if not articles:
        return jsonify({
            'success': False,
            'error': '没有找到符合条件的交易日文章'
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

    # 只保留A股交易日的文章（跳过周末/节假日等非交易日）
    articles, _dropped_non_trading = filter_trading_articles(articles)

    if not articles:
        return jsonify({
            'success': False,
            'error': '没有找到符合条件的交易日文章'
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
        'failed_dates': task.get('failed_dates', []),
        'failed_details': task.get('failed_details', [])
    })


def _parse_order_expected(folder_path):
    """
    解析 _order.txt，返回应存在的图片文件名列表（按顺序）。
    格式: "01. n05q339s0imk.png"；无文件或无有效行返回 []。
    """
    order_file = os.path.join(folder_path, '_order.txt')
    if not os.path.exists(order_file):
        return []
    expected = []
    with open(order_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^\d+\.\s*(\S+\.(?:png|jpg|jpeg|gif|webp))$', line, re.I)
            if m:
                expected.append(m.group(1))
    return expected


def _folder_completeness(folder_path, files):
    """
    依据 _order.txt 判断文件夹完整性。
    返回: (status, expected_count, missing_list)
      status: 'complete' 完整 | 'incomplete' 缺失 | 'unknown' 无_order.txt无法判断
    """
    expected = _parse_order_expected(folder_path)
    if not expected:
        # 无 _order.txt：只要有图片就视为未知（无法核对应有张数）
        return ('unknown', None, [])
    present = set(files)
    missing = [name for name in expected if name not in present]
    if missing:
        return ('incomplete', len(expected), missing)
    return ('complete', len(expected), [])


@app.route('/api/files', methods=['GET'])
def list_files():
    """查看已下载的文件列表（含完整性状态）"""
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
            status, expected_count, missing = _folder_completeness(folder_path, files)
            folders.append({
                'date': date_folder,
                'file_count': len(files),
                'expected_count': expected_count,
                'status': status,               # complete | incomplete | unknown
                'missing_count': len(missing),
                'missing': missing,
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


@app.route('/api/extract', methods=['POST'])
def extract_data():
    """
    按日期区间提取涨停复盘数据(生成Excel到本地)，可选提交入库。
    参数:
        start_date, end_date: YYYY-MM-DD（缺省时默认取当天）
        submit_to_db: 是否提交入库(默认 true)
    """
    data = request.get_json() or {}
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    submit_to_db = data.get('submit_to_db', True)

    # 默认当天
    if not start_date and not end_date:
        start_date = end_date = datetime.now().strftime('%Y-%m-%d')
    elif start_date and not end_date:
        end_date = start_date
    elif end_date and not start_date:
        start_date = end_date

    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'error': '日期格式错误，请使用 YYYY-MM-DD'}), 400

    start8 = start_date.replace('-', '')
    end8 = end_date.replace('-', '')
    dates = trading_days_in_range(start8, end8)
    if not dates:
        return jsonify({'success': False, 'error': '区间内没有A股交易日'}), 404

    import uuid
    task_id = str(uuid.uuid4())[:8]
    extract_tasks[task_id] = {
        'status': 'pending', 'progress': 0,
        'total': len(dates), 'extracted': 0, 'submitted': 0,
        'submit_to_db': bool(submit_to_db),
        'items': [{'date': d, 'status': 'pending', 'message': '待处理'} for d in dates],
    }
    thread = threading.Thread(
        target=extract_task,
        args=(task_id, dates, bool(submit_to_db)),
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True, 'task_id': task_id, 'dates_count': len(dates),
        'submit_to_db': bool(submit_to_db),
        'message': f'开始提取 {len(dates)} 个交易日',
        'status_url': f'/api/extract/status/{task_id}',
    })


@app.route('/api/extract/status/<task_id>', methods=['GET'])
def extract_status(task_id):
    """查询提取任务状态"""
    if task_id not in extract_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = extract_tasks[task_id]
    return jsonify({
        'success': True, 'task_id': task_id,
        'status': t['status'], 'progress': t['progress'],
        'total': t['total'], 'extracted': t.get('extracted', 0),
        'submitted': t.get('submitted', 0),
        'items': t.get('items', []),
    })


@app.route('/api/db/status', methods=['GET'])
def db_status():
    """探测数据库连接状态"""
    import db as _db
    ok, msg = _db.ping()
    return jsonify({'success': True, 'connected': ok, 'message': msg})


@app.route('/api/db/submit', methods=['POST'])
def db_submit():
    """把某日期的 Excel 上传(入库)到 PostgreSQL"""
    import db as _db
    data = request.get_json() or {}
    date = (data.get('date') or '').replace('-', '')
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': '日期格式错误，请用 YYYYMMDD'}), 400
    try:
        _db.init_db()
        result = _db.submit_date(date)
        _invalidate_hot_cache()
        return jsonify({'success': True, **result})
    except FileNotFoundError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except _db.NotVerifiedError as e:
        return jsonify({'success': False, 'error': str(e), 'need_review': True}), 409
    except _db.DBError as e:
        return jsonify({'success': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': f'入库失败: {e}'}), 500


@app.route('/api/db/submit-batch', methods=['POST'])
def db_submit_batch():
    """
    按A股交易日区间批量入库(上传已有Excel)。
    参数: start_date, end_date (YYYY-MM-DD, 缺省取当天)
    """
    import db as _db
    data = request.get_json() or {}
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not start_date and not end_date:
        start_date = end_date = datetime.now().strftime('%Y-%m-%d')
    elif start_date and not end_date:
        end_date = start_date
    elif end_date and not start_date:
        start_date = end_date

    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'error': '日期格式错误，请使用 YYYY-MM-DD'}), 400

    dates = trading_days_in_range(start_date.replace('-', ''), end_date.replace('-', ''))
    if not dates:
        return jsonify({'success': False, 'error': '区间内没有A股交易日'}), 404

    ok, msg = _db.ping()
    if not ok:
        return jsonify({'success': False, 'error': f'数据库未连接: {msg}'}), 503

    import uuid
    task_id = str(uuid.uuid4())[:8]
    submit_tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'total': len(dates), 'submitted': 0,
        'items': [{'date': d, 'status': 'pending', 'message': '待处理'} for d in dates],
    }
    threading.Thread(target=submit_batch_task, args=(task_id, dates), daemon=True).start()

    return jsonify({
        'success': True, 'task_id': task_id, 'dates_count': len(dates),
        'message': f'开始入库 {len(dates)} 个交易日',
        'status_url': f'/api/db/submit-batch/status/{task_id}',
    })


@app.route('/api/db/submit-batch/status/<task_id>', methods=['GET'])
def db_submit_batch_status(task_id):
    """查询批量入库任务状态"""
    if task_id not in submit_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = submit_tasks[task_id]
    return jsonify({
        'success': True, 'task_id': task_id,
        'status': t['status'], 'progress': t['progress'],
        'total': t['total'], 'submitted': t.get('submitted', 0),
        'items': t.get('items', []),
    })


@app.route('/api/excel/list', methods=['GET'])
def excel_list():
    """
    按A股交易日列出应有的Excel及其状态。
    参数: start, end (YYYYMMDD, 可选)。缺省时取已有数据(下载/Excel)的最早~最晚区间。
    返回每个交易日: excel状态(verified/manualcheck/none) + 是否已入库(submitted)
    """
    import db as _db
    base_dir = 'dataresource'
    excel_dir = 'excelDataSource'

    # 收集已有日期，确定默认区间
    known = set()
    if os.path.isdir(base_dir):
        known |= {d for d in os.listdir(base_dir) if re.match(r'^\d{8}$', d)}
    if os.path.isdir(excel_dir):
        for f in os.listdir(excel_dir):
            m = re.match(r'^(\d{8})_', f)
            if m:
                known.add(m.group(1))

    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not (re.match(r'^\d{8}$', start) and re.match(r'^\d{8}$', end)):
        if not known:
            return jsonify({'success': True, 'items': [], 'db_connected': False})
        start, end = min(known), max(known)

    days = trading_days_in_range(start, end)

    # 已入库状态(数据库不可用时降级)
    submitted_status = {}
    db_connected = True
    db_msg = 'ok'
    try:
        submitted_status = _db.get_submitted_status()
    except _db.DBError as e:
        db_connected = False
        db_msg = str(e)

    items = []
    for d in sorted(days, reverse=True):
        fp, status = _db.find_excel(d, excel_dir)
        items.append({
            'date': d,
            'excel': status or 'none',           # verified / manualcheck / none
            'has_excel': fp is not None,
            'downloaded': os.path.isdir(os.path.join(base_dir, d)),
            'submitted': (d in submitted_status) if db_connected else None,
            'db_status': submitted_status.get(d) if db_connected else None,  # 库里存的状态
        })

    return jsonify({
        'success': True,
        'items': items,
        'db_connected': db_connected,
        'db_message': db_msg,
        'range': {'start': start, 'end': end},
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
    """返回数据库中已入库的所有日期(热门股追踪的可选范围)"""
    import db as _db
    try:
        dates = sorted(_db.get_submitted_dates())
    except _db.DBError as e:
        return jsonify({'success': True, 'dates': [], 'db_connected': False, 'db_message': str(e)})
    return jsonify({'success': True, 'dates': dates, 'db_connected': True})


def _build_missing_report(result):
    """
    扫描计算结果, 按"每日 -> 板块"列出当天在榜但缺涨幅/缺MA10的个股。
    返回: (missing_by_date, missing_codes)
      missing_by_date: [{date, blocks:[{block, stocks:[{code,name,missing:[...]}]}]}]
      missing_codes: 去重的缺失股票代码列表
    """
    missing_by_date = []
    missing_codes = set()
    for day in result.get('by_date', []):
        d = day['date']
        blocks_out = []
        for b in day.get('blocks', []):
            miss_stocks = []
            for s in b.get('stocks', []):
                cell = s.get('track', {}).get(d) or {}
                # 不再跳过 present=False: 历史入榜跟踪股当日未涨停时仍需涨幅/MA10
                missing = []
                if cell.get('pct') is None:
                    missing.append('涨幅')
                if cell.get('ma10') is None:
                    missing.append('MA10')
                if missing:
                    miss_stocks.append({'code': s['code'], 'name': s['name'], 'missing': missing})
                    missing_codes.add(s['code'])
            if miss_stocks:
                blocks_out.append({'block': b['block'], 'stocks': miss_stocks})
        if blocks_out:
            missing_by_date.append({'date': d, 'blocks': blocks_out})
    return missing_by_date, sorted(missing_codes)


def hot_compute_task(task_id, start, end, with_price):
    _dlog(f'=== hot_compute_task 启动 task={task_id} {start}~{end} price={with_price} ===')
    """
    后台热门股计算任务(分阶段):
      阶段1 build: 载入板块/个股
      阶段2 price: 获取行情
      -> 检查数据缺失: 缺失则暂停询问(再次同步/跳过), 因为数据不全时移除判断不可靠
      阶段3 remove: 用户确认后才应用移除规则, 出最终结果
    """
    global hot_tasks
    t = hot_tasks[task_id]
    t['status'] = 'running'

    def prog(stage, msg, cur=None, total=None):
        t['stage'] = stage
        t['logs'].append({'stage': stage, 'msg': msg, 'cur': cur, 'total': total})
        if cur is not None and total:
            t['progress'] = int(cur / max(total, 1) * 100)

    try:
        missing_by_date, missing_codes = [], []

        if with_price:
            # 循环: 构建+行情(不移除) -> 检查缺失 -> 询问; 直到无缺失或用户跳过
            while True:
                _dlog(f'[{task_id}] 第二步: 开始 track_hot_stocks apply_removal=False')
                data = ht.track_hot_stocks(start, end, with_price=True, progress=prog,
                                           source='db', apply_removal=False)

                # 从 fetch_report 里取出本轮拉取失败的票
                fetch_report = data.get('fetch_report') or {}
                fetch_failed = fetch_report.get('failed', [])  # [{code, reason}, ...]
                _dlog(f'[{task_id}] fetch_report: cached={fetch_report.get("cached")} success={fetch_report.get("success")} failed={len(fetch_failed)}')

                # 第二步结束: 如果有拉取失败的票, 立即进入 awaiting
                if fetch_failed and not t.get('_skip'):
                    _dlog(f'[{task_id}] 进入 awaiting，失败票: {[f["code"] for f in fetch_failed]}')
                    fail_codes = [f['code'] for f in fetch_failed]
                    lines = [f"{f['code']} ({f['reason']})" for f in fetch_failed]
                    prog('await',
                         f'行情获取完成，以下 {len(fetch_failed)} 只股票无法获取数据:\n'
                         + '\n'.join(f'  {l}' for l in lines)
                         + '\n请选择「再次同步」重试，或「跳过」直接进入第三步',
                         None, None)
                    t['missing_report'] = []   # 此时还没做 remove, 用 fetch_failed 代替
                    t['missing_codes'] = fail_codes
                    t['status'] = 'awaiting'
                    t['_event'].clear()
                    t['_event'].wait()
                    action = t.get('_action')
                    t['_action'] = None
                    t['status'] = 'running'
                    _dlog(f'[{task_id}] awaiting 用户选择: action={action}')
                    if action == 'resync':
                        prog('price', f'再次同步 {len(fail_codes)} 只个股行情(通达信)…', 0, len(fail_codes))
                        ok_cnt, still_fail = 0, []
                        for i, c in enumerate(fail_codes):
                            # 先清除之前写入的 None 占位和 _no_data 标记, 让 fetch 重新尝试
                            _cache = ht._load_price_cache()
                            with ht._cache_lock:
                                _cache.pop(f'{c}_no_data', None)
                                _cache.pop(f'{c}_no_data_reason', None)
                                for _d in data.get('dates', []):
                                    for _sfx in ('', '_ma10', '_below_ma10', '_20d', '_60d'):
                                        _cache.pop(f'{c}_{_d}{_sfx}', None)
                                ht._save_price_cache()
                            try:
                                if ht.fetch_range(c, start, end):
                                    ok_cnt += 1
                                else:
                                    still_fail.append(c)
                            except Exception:
                                still_fail.append(c)
                            prog('price', f'同步 {i + 1}/{len(fail_codes)} ({c})', i + 1, len(fail_codes))
                        msg = f'同步完成: 成功 {ok_cnt}/{len(fail_codes)}'
                        if still_fail:
                            msg += f', 仍失败 {len(still_fail)} 只: ' + ', '.join(still_fail)
                        prog('price', msg, len(fail_codes), len(fail_codes))
                        continue  # 重新构建 + 再检查
                    else:
                        t['_skip'] = True
                        # 跳过: 保留 None 占位, 直接进第三步

                missing_by_date, missing_codes = _build_missing_report(data)
                if not missing_codes or t.get('_skip'):
                    break
                # 兜底: fetch 全部成功但缓存里仍有 None (理论上不应再触发)
                n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
                t['missing_report'] = missing_by_date
                t['missing_codes'] = missing_codes
                prog('await', f'检测到 {len(missing_codes)} 只个股共 {n} 处缺数据(缺涨幅/MA10)。'
                              f'数据不全时无法可靠执行移除规则，请选择「再次同步」或「跳过直接显示」', None, None)
                t['status'] = 'awaiting'
                t['_event'].clear()
                t['_event'].wait()
                action = t.get('_action')
                t['_action'] = None
                t['status'] = 'running'
                if action == 'resync':
                    prog('price', f'再次同步 {len(missing_codes)} 只个股行情(通达信)…', 0, len(missing_codes))
                    ok_cnt, fail_list = 0, []
                    for i, c in enumerate(missing_codes):
                        try:
                            if ht.fetch_range(c, start, end):
                                ok_cnt += 1
                            else:
                                fail_list.append(c)
                        except Exception:
                            fail_list.append(c)
                        prog('price', f'同步 {i + 1}/{len(missing_codes)} ({c})', i + 1, len(missing_codes))
                    msg = f'同步完成: 成功 {ok_cnt}/{len(missing_codes)}'
                    if fail_list:
                        msg += f', 失败 {len(fail_list)} 只'
                    prog('price', msg, len(missing_codes), len(missing_codes))
                    continue
                else:
                    t['_skip'] = True
                    break

        # 阶段3: 直接在第二步的数据上应用移除规则, 不再重新拉行情
        _dlog(f'[{task_id}] 第三步: 应用移除规则 with_price={with_price}')
        prog('remove', '数据就绪，应用移除规则(跌破10日线次日删除)…', 0, 1)
        if not with_price:
            # 未拉行情时才需要重新跑
            data = ht.track_hot_stocks(start, end, with_price=False, progress=prog,
                                       source='db', apply_removal=True)
        else:
            # 复用第二步数据, 只重跑移除逻辑部分
            data = ht.apply_removal_rules(data, progress=prog)
        final = data
        final['missing_report'] = missing_by_date
        final['missing_codes'] = missing_codes
        n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
        prog('done', (f'计算完成(跳过 {len(missing_codes)} 只缺数据个股, 共 {n} 处)'
                      if missing_codes else '计算完成，数据完整'), 1, 1)
        _dlog(f'[{task_id}] 计算完成 missing_codes={missing_codes}')
        t['result'] = final
        t['status'] = 'completed'
        t['progress'] = 100
        # 服务端持久化最近结果(供刷新恢复)
        global hot_last
        hot_last = {'start': start, 'end': end, 'price': with_price,
                    'saved_at': time.time(), 'result': final}
        _save_hot_last()
    except Exception as e:
        _dlog(f'[{task_id}] 计算异常: {e}', 'ERROR')
        t['status'] = 'failed'
        t['error'] = str(e)
        t['logs'].append({'stage': 'error', 'msg': f'计算失败: {e}', 'cur': None, 'total': None})


@app.route('/api/hot/last', methods=['GET'])
def api_hot_last():
    """返回最近一次计算结果(服务端缓存, 刷新恢复用)。无则 has=false。
    额外检查新鲜度: 若DB日期范围超出缓存覆盖，返回 stale=true。"""
    if not hot_last or not hot_last.get('result'):
        return jsonify({'success': True, 'has': False})
    # 新鲜度检查: 对比缓存覆盖范围 vs DB实际范围
    stale = False
    stale_reason = None
    try:
        import db as _db
        db_dates = sorted(_db.get_submitted_dates())
        if db_dates:
            cached_start = hot_last.get('start', '')
            cached_end = hot_last.get('end', '')
            db_min, db_max = min(db_dates), max(db_dates)
            if db_min < cached_start or db_max > cached_end:
                stale = True
                stale_reason = f'数据库已有新数据({db_min}~{db_max})，缓存仅覆盖{cached_start}~{cached_end}，需重新计算'
    except Exception:
        pass
    return jsonify({
        'success': True, 'has': True,
        'start': hot_last.get('start'), 'end': hot_last.get('end'),
        'price': hot_last.get('price', True), 'saved_at': hot_last.get('saved_at'),
        'result': hot_last['result'],
        'stale': stale,
        'stale_reason': stale_reason,
    })


@app.route('/api/hot/compute', methods=['POST'])
def api_hot_compute():
    _dlog(f'POST /api/hot/compute body={request.get_data(as_text=True)}')
    """启动热门股计算(异步, 带分阶段进度)。范围受数据库已入库日期约束。"""
    import db as _db
    data = request.get_json() or {}
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    with_price = data.get('price', True) not in (0, '0', False, 'false', 'no')

    if not (re.match(r'^\d{8}$', start) and re.match(r'^\d{8}$', end)):
        return jsonify({'success': False, 'error': '日期格式错误, 请用 YYYYMMDD'}), 400
    if start > end:
        start, end = end, start

    try:
        db_dates = _db.get_submitted_dates()
    except _db.DBError as e:
        return jsonify({'success': False, 'error': f'数据库未连接: {e}'}), 503
    if not db_dates:
        return jsonify({'success': False, 'error': '数据库暂无已入库数据, 请先在数据同步页入库'}), 404

    dmin, dmax = min(db_dates), max(db_dates)
    if start < dmin or end > dmax:
        return jsonify({'success': False,
                        'error': f'日期超出已入库范围({dmin}~{dmax})'}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    hot_tasks[task_id] = {
        'status': 'pending', 'stage': '', 'progress': 0,
        'logs': [], 'result': None, 'error': None,
        'missing_report': [], 'missing_codes': [],
        '_event': threading.Event(), '_action': None, '_skip': False,
    }
    threading.Thread(target=hot_compute_task,
                     args=(task_id, start, end, with_price), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/compute/status/{task_id}'})


@app.route('/api/hot/resync', methods=['POST'])
def api_hot_resync():
    """强制用通达信 mootdx 重新拉取指定个股的行情(覆盖缓存)，用于补齐缺失的涨幅/MA10。"""
    data = request.get_json() or {}
    codes = data.get('codes', [])
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    if not codes:
        return jsonify({'success': False, 'error': '没有需要同步的股票'}), 400

    import time
    ok, failed = 0, []
    for c in codes:
        try:
            if ht.fetch_range(c, start, end):
                ok += 1
            else:
                failed.append(c)
            time.sleep(0.05)
        except Exception as e:
            failed.append(c)
    return jsonify({'success': True, 'fetched': ok, 'total': len(codes), 'failed': failed})


@app.route('/api/hot/compute/status/<task_id>', methods=['GET'])
def api_hot_compute_status(task_id):
    """查询热门股计算进度; 支持 ?since=N 增量拉日志; awaiting时返回缺失报告; 完成时返回 result"""
    if task_id not in hot_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = hot_tasks[task_id]
    since = request.args.get('since', default=0, type=int)
    resp = {
        'success': True, 'status': t['status'], 'stage': t['stage'],
        'progress': t['progress'],
        'logs': t['logs'][since:], 'log_count': len(t['logs']),
    }
    if t['status'] == 'awaiting':
        resp['missing_report'] = t.get('missing_report', [])
        resp['missing_codes'] = t.get('missing_codes', [])
    elif t['status'] == 'completed':
        resp['result'] = t['result']
    elif t['status'] == 'failed':
        resp['error'] = t['error']
    return jsonify(resp)


@app.route('/api/hot/compute/resolve', methods=['POST'])
def api_hot_compute_resolve():
    _dlog(f'POST /api/hot/compute/resolve body={request.get_data(as_text=True)}')
    """
    响应缺失数据询问: action='resync'(再次同步后重算) 或 'skip'(跳过缺失直接出结果)
    """
    data = request.get_json() or {}
    task_id = data.get('task_id')
    action = data.get('action')
    if task_id not in hot_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    if action not in ('resync', 'skip'):
        return jsonify({'success': False, 'error': 'action 必须是 resync 或 skip'}), 400
    t = hot_tasks[task_id]
    if t['status'] != 'awaiting':
        return jsonify({'success': False, 'error': '任务当前不在等待状态'}), 409
    t['_action'] = action
    t['_event'].set()  # 唤醒后台线程继续
    return jsonify({'success': True, 'action': action})


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
