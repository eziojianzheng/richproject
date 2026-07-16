#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘股吧图片下载API服务
提供按日期范围下载湖南人涨停复盘图片的接口
"""

# ============== 调试配置/日志 ==============
# debug.enabled 只控制开发诊断能力；默认关闭，config.yml 或 AI_KANPAN_DEBUG=1 可开启。
import os as _os
import time as _startup_time
import uuid as _uuid


def _load_debug_config():
    try:
        import yaml as _yaml
        with open('config.yml', 'r', encoding='utf-8') as _f:
            cfg = (_yaml.safe_load(_f) or {}).get('debug') or {}
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


_DEBUG_CONFIG = _load_debug_config()
_debug_env = _os.environ.get('AI_KANPAN_DEBUG')
DEBUG_MODE = (_debug_env == '1') if _debug_env is not None else bool(_DEBUG_CONFIG.get('enabled', False))
SERVER_STARTED_AT = _startup_time.strftime('%Y-%m-%d %H:%M:%S')
SERVER_INSTANCE_ID = f'{_os.getpid()}-{_uuid.uuid4().hex[:8]}'

import logging as _logging
_dbg_logger = _logging.getLogger('ai_kanpan')
# 抑制 tdxpy/mootdx 内部 NotImplementedError 噪音日志(不影响功能)
_logging.getLogger('tdxpy').setLevel(_logging.CRITICAL)
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
    'flask_socketio': 'flask-socketio>=5.3.0',
    'requests': 'requests>=2.31.0',
    'bs4': 'beautifulsoup4>=4.12.0',
    'yaml': 'PyYAML>=6.0',
    'openpyxl': 'openpyxl>=3.1.0',
    'rapidocr_onnxruntime': 'rapidocr_onnxruntime>=1.3.0',
    'mootdx': 'mootdx>=0.11.0',
    'psycopg2': 'psycopg2-binary>=2.9.0',
})

from flask import Flask, request, jsonify, render_template, make_response
import requests
from bs4 import BeautifulSoup
import os
import re
import json
import time
from datetime import datetime, timedelta
import threading

app = Flask(__name__)
from flask_socketio import SocketIO
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')


@app.after_request
def _debug_no_cache(response):
    """Debug 模式禁用页面/API的浏览器HTTP缓存，并标记实际服务进程实例。"""
    if DEBUG_MODE and (request.path.startswith('/api/') or response.mimetype == 'text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.headers['X-AI-Kanpan-Instance'] = SERVER_INSTANCE_ID
    return response

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

# 历史爆发股票汇总扫描任务状态
explosive_tasks = {}

# 招财猫复盘扫描任务状态
caizhaomao_tasks = {}

# 最近一次热门股计算结果(服务端持久化, 刷新后恢复, 不受浏览器localStorage配额限制)
hot_last = None
HOT_LAST_FILE = '.hot_last_result.json'


def _slim_by_date_tracks(data):
    """裁剪 by_date 里每只股票的 track 字段: 只保留当天数据。
    前端只用 s.track[当天], 其余日期是冗余(可占总体积~90%)。
    注意: 同一股票在多个日期的 by_date 里共享同一对象(累积制),
    必须用浅拷贝隔离, 否则先处理的日期会把 track 裁成 {date1:...},
    后续日期找不到自己的 key 反而被清空成 {}。"""
    for day in data.get('by_date', []):
        day_date = day['date']
        for block in day.get('blocks', []):
            new_stocks = []
            for stock in block.get('stocks', []):
                tr = stock.get('track', {})
                if day_date in tr:
                    slim = dict(stock)
                    slim['track'] = {day_date: tr[day_date]}
                    new_stocks.append(slim)
                else:
                    new_stocks.append(stock)
            block['stocks'] = new_stocks


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
            # 启动时裁剪旧缓存里冗余的 track 数据
            if hot_last and hot_last.get('result'):
                _slim_by_date_tracks(hot_last['result'])
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


# ===== 自选股持久化 (.watchlist.json) =====
WATCHLIST_FILE = '.watchlist.json'


def _load_watchlist():
    """读取自选股列表 [{code, name, added_at}]"""
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_watchlist(wl):
    try:
        with open(WATCHLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(wl, f, ensure_ascii=False)
    except Exception as e:
        print(f'保存自选股失败: {e}')


# ===== 爆发股已选列表持久化 (.exp_picks.json) =====
EXP_PICKS_FILE = '.exp_picks.json'


def _load_exp_picks():
    """读取爆发股已选列表 [{range_start, range_end, range_label, code, name, gain, picked_at}]"""
    try:
        if os.path.exists(EXP_PICKS_FILE):
            with open(EXP_PICKS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_exp_picks(picks):
    try:
        with open(EXP_PICKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(picks, f, ensure_ascii=False)
    except Exception as e:
        print(f'保存爆发股已选失败: {e}')


@app.route('/api/hot/explosive/picks', methods=['GET'])
def api_explosive_picks_get():
    """获取已保存的爆发股已选列表"""
    return jsonify({'success': True, 'picks': _load_exp_picks()})


@app.route('/api/hot/explosive/picks', methods=['POST'])
def api_explosive_picks_save():
    """保存爆发股已选列表 (全量覆盖)"""
    try:
        data = request.get_json(silent=True) or {}
        picks = data.get('picks', [])
        _save_exp_picks(picks)
        return jsonify({'success': True, 'count': len(picks)})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ===== 爆发股扫描结果持久化 (.exp_scan.json) =====
EXP_SCAN_FILE = '.exp_scan.json'


def _load_exp_scan():
    """读取上次扫描结果 {month, window, threshold, candidates, saved_at}"""
    try:
        if os.path.exists(EXP_SCAN_FILE):
            with open(EXP_SCAN_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_exp_scan(scan_data):
    try:
        with open(EXP_SCAN_FILE, 'w', encoding='utf-8') as f:
            json.dump(scan_data, f, ensure_ascii=False)
    except Exception as e:
        print(f'保存爆发股扫描结果失败: {e}')


@app.route('/api/hot/explosive/scan-result', methods=['GET'])
def api_explosive_scan_result_get():
    """获取上次保存的扫描结果"""
    return jsonify({'success': True, 'scan': _load_exp_scan()})


@app.route('/api/hot/explosive/scan-result', methods=['POST'])
def api_explosive_scan_result_save():
    """保存扫描结果 (全量覆盖)"""
    try:
        data = request.get_json(silent=True) or {}
        _save_exp_scan(data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


def get_article_list(user_id='444409'):
    """获取博客文章列表（首页，最近约30篇）"""
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


# ============== 多页文章列表（moreTopic 分页） ==============
# 淘股吧博客首页只展示最近约30篇，更早的文章需通过 moreTopic 分页接口获取。
# moreTopic URL: https://www.tgb.cn/user/blog/moreTopic?userID=xxx&pageNo=N&sortFlag=T
# 每页约100篇，总页数在页面隐藏 input[name=pageNum] 中。

def _parse_moretopic_page(html_text):
    """
    解析 moreTopic 页面的文章列表。
    返回: [{title, link, pub_time, date_folder}, ...]
    """
    soup = BeautifulSoup(html_text, 'html.parser')
    articles = []
    seen_hrefs = set()

    # moreTopic 页面用 <tr> 行展示文章，每行含 <a href="/a/xxx"> 标题链接
    for tr in soup.find_all('tr'):
        link_tag = tr.find('a', href=re.compile(r'^/a/'))
        if not link_tag:
            continue
        href = link_tag.get('href', '')
        if href in seen_hrefs:
            continue  # 同一文章可能在多行出现（跟帖行），去重
        seen_hrefs.add(href)

        title = link_tag.get('title', '') or link_tag.text.strip()
        link = f"https://www.tgb.cn{href}" if href.startswith('/') else href

        # 从行文本中提取日期
        time_match = re.search(r'\d{4}-\d{2}-\d{2}', tr.text)
        pub_time = time_match.group() if time_match else ''
        date_folder = pub_time.replace('-', '') if pub_time else 'unknown'

        articles.append({
            'title': title,
            'link': link,
            'pub_time': pub_time,
            'date_folder': date_folder
        })

    return articles


def _get_moretopic_total_pages(html_text):
    """从 moreTopic 页面 HTML 中提取总页数"""
    soup = BeautifulSoup(html_text, 'html.parser')
    # 隐藏 input: <input type="hidden" name="pageNum" value="27">
    inp = soup.find('input', attrs={'name': 'pageNum'})
    if inp:
        try:
            return int(inp.get('value', '1'))
        except ValueError:
            pass
    # 回退：从 JS gotoPage 函数中提取
    m = re.search(r'var\s+pageNum\s*=\s*(\d+)', html_text)
    if m:
        return int(m.group(1))
    return 1


def get_article_list_paginated(user_id='444409', start_date=None, end_date=None,
                               max_pages=None, progress_callback=None):
    """
    获取博客文章列表（支持分页，覆盖更早日期的文章）。

    通过 moreTopic 分页接口逐页抓取，直到：
    - 已覆盖 start_date（文章日期早于 start_date 时停止）
    - 到达最后一页
    - 达到 max_pages 限制

    参数:
        user_id: 博客用户ID
        start_date: 起始日期 (YYYY-MM-DD)，文章早于此日期时停止翻页（None=翻到最后一页）
        end_date: 结束日期 (YYYY-MM-DD)，用于日志展示
        max_pages: 最大翻页数限制（None=无限制）
        progress_callback: 回调函数 (page, total_pages, article_count) -> None

    返回: [{title, link, pub_time, date_folder}, ...] 按日期降序（新→旧）
    """
    moretopic_url = 'https://www.tgb.cn/user/blog/moreTopic'
    all_articles = []
    seen_hrefs = set()

    # 先抓第1页，获取总页数
    params = {'userID': user_id, 'pageNo': '1', 'sortFlag': 'T'}
    try:
        response = request_with_retry(
            moretopic_url + '?' + '&'.join(f'{k}={v}' for k, v in params.items()),
            HEADERS
        )
    except NetworkError as e:
        print(f"获取文章列表失败（网络）: {e}")
        # 回退到首页方式
        return get_article_list(user_id)

    response.encoding = 'utf-8'
    if response.status_code != 200:
        print(f"获取文章列表失败（HTTP {response.status_code}）")
        return get_article_list(user_id)

    total_pages = _get_moretopic_total_pages(response.text)
    if max_pages:
        total_pages = min(total_pages, max_pages)

    page_articles = _parse_moretopic_page(response.text)
    for a in page_articles:
        if a['link'] not in seen_hrefs:
            seen_hrefs.add(a['link'])
            all_articles.append(a)

    print(f"[分页] 第1/{total_pages}页: {len(page_articles)}篇, 累计{len(all_articles)}篇")
    if progress_callback:
        progress_callback(1, total_pages, len(all_articles))

    # 逐页抓取后续页
    for page_no in range(2, total_pages + 1):
        # 如果最早的文章已经早于 start_date，可以停止
        if start_date and all_articles:
            oldest = min(a.get('pub_time', '9999') for a in all_articles)
            if oldest <= start_date:
                print(f"[分页] 已覆盖到 {start_date}（最早文章 {oldest}），停止翻页")
                break

        params = {'userID': user_id, 'pageNo': str(page_no), 'sortFlag': 'T'}
        try:
            response = request_with_retry(
                moretopic_url + '?' + '&'.join(f'{k}={v}' for k, v in params.items()),
                HEADERS
            )
        except NetworkError as e:
            print(f"[分页] 第{page_no}页获取失败（网络）: {e}，跳过")
            continue

        response.encoding = 'utf-8'
        if response.status_code != 200:
            print(f"[分页] 第{page_no}页获取失败（HTTP {response.status_code}），跳过")
            continue

        page_articles = _parse_moretopic_page(response.text)
        new_count = 0
        for a in page_articles:
            if a['link'] not in seen_hrefs:
                seen_hrefs.add(a['link'])
                all_articles.append(a)
                new_count += 1

        print(f"[分页] 第{page_no}/{total_pages}页: {len(page_articles)}篇(新增{new_count}), 累计{len(all_articles)}篇")
        if progress_callback:
            progress_callback(page_no, total_pages, len(all_articles))

        # 如果本页没有新文章，说明已无更多内容
        if new_count == 0:
            print(f"[分页] 第{page_no}页无新文章，停止翻页")
            break

        # 礼貌延时，避免请求过快
        time.sleep(0.3)

    return all_articles


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
    返回A股交易日集合(YYYYMMDD)，用 mootdx 交易日历(新浪数据源)，带内存缓存。
    获取失败返回 None（调用方回退到工作日判断）。
    """
    global _TRADE_DAYS, _TRADE_DAYS_MAX
    if _TRADE_DAYS is not None:
        return _TRADE_DAYS
    try:
        from mootdx.utils.holiday import holidays
        df = holidays()
        # mootdx holidays() 返回列 ['date', 'year'], date 为 datetime.date
        days = {str(d).replace('-', '') for d in df['date']}
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


def _get_article_range(articles):
    """返回文章列表中最早/最晚可用发布日期 (YYYY-MM-DD)。"""
    dates = sorted(a.get('pub_time', '') for a in articles if a.get('pub_time'))
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _validate_download_range(articles, start_date, end_date):
    """检查请求日期是否在当前可下载文章范围内。"""
    if not articles:
        return False, '无法获取文章列表，请检查网络或目标博客页面是否可访问。'
    avail_start, avail_end = _get_article_range(articles)
    if not avail_start or not avail_end:
        return False, '当前文章列表没有有效发布日期，无法执行下载。'

    errors = []
    if start_date and start_date < avail_start:
        errors.append(f'当前最早可下载日期为 {avail_start}，start_date 不应早于此日期')
    if end_date and end_date > avail_end:
        errors.append(f'当前最晚可下载日期为 {avail_end}，end_date 不应晚于此日期')
    if errors:
        return False, '；'.join(errors)
    return True, None


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
        # 用户请求中断：停止后续下载
        if download_tasks[task_id].get('cancel_requested'):
            break
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
    
    download_tasks[task_id]['status'] = 'cancelled' if download_tasks[task_id].get('cancel_requested') else 'completed'
    download_tasks[task_id]['total'] = total_images
    download_tasks[task_id]['downloaded'] = success_images
    download_tasks[task_id]['skipped_folders'] = skipped_folders
    download_tasks[task_id]['failed_dates'] = failed_dates
    download_tasks[task_id]['failed_details'] = failed_details


def extract_task(task_id, dates, submit_to_db=True, base_dir='dataresource',
                 output_dir='excelDataSource', force=False):
    """
    后台提取任务：对每个日期跑 extract_glm 生成 Excel，可选提交入库。
    dates: 交易日列表(YYYYMMDD)
    force: True 时忽略"已存在Excel"，强制重新提取(重新覆盖生成)。
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
        # 用户请求中断：停止后续提取
        if t.get('cancel_requested'):
            break
        it = items[i]
        folder = os.path.join(base_dir, d)
        try:
            # 默认跳过已存在 Excel(verified 或 manualcheck), 避免重复提取; force=True 时不跳过
            exist_fp, exist_st = _db.find_excel(d, output_dir)
            if exist_fp and not force:
                it['status'] = 'skipped'
                it['message'] = f'已存在Excel({exist_st})，跳过提取'
                extracted += 1
                # 若需入库且尚未入库, 仍执行入库
                if submit_to_db:
                    submitted_dates = _db.get_submitted_dates()
                    if d in submitted_dates:
                        it['status'] = 'skipped'
                        it['message'] = f'已存在Excel({exist_st})且已入库，跳过'
                    else:
                        it['status'] = 'submitting'
                        it['message'] = 'Excel已存在，正在入库…'
                        try:
                            _db.submit_date(d, output_dir)
                            submitted += 1
                            it['status'] = 'submitted'
                            it['message'] = f'入库成功(复用已有Excel {exist_st})'
                            _invalidate_hot_cache()
                        except _db.NotVerifiedError:
                            it['status'] = 'need_review'
                            it['message'] = f'已存在Excel({exist_st})，需人工复核后入库'
                        except _db.DBError as e:
                            it['status'] = 'failed'
                            it['message'] = f'入库失败: {e}'
                        except Exception as e:
                            it['status'] = 'failed'
                            it['message'] = f'入库异常: {e}'
            elif not os.path.isdir(folder) or not check_folder_has_images(folder):
                it['status'] = 'failed'
                it['message'] = '未下载图片（无 dataresource 文件夹）'
            else:
                it['status'] = 'extracting'
                it['message'] = '正在重新提取…' if (force and exist_fp) else '正在提取…'
                ok = eg.extract_date(d, base_dir, output_dir)
                if not ok:
                    it['status'] = 'failed'
                    it['message'] = '提取失败（未找到03/04或识别失败）'
                else:
                    extracted += 1
                    # 重新提取成功后, 清理与新结果状态不一致的旧Excel(避免 verified/manualcheck 并存)
                    if force:
                        _new_fp, _new_st = _db.find_excel(d, output_dir)
                        for _st in ('verified', 'manualcheck'):
                            if _st != _new_st:
                                _stale = os.path.join(output_dir, f'{d}_涨停复盘_{_st}.xlsx')
                                try:
                                    if os.path.exists(_stale):
                                        os.remove(_stale)
                                except Exception:
                                    pass
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

    t['status'] = 'cancelled' if t.get('cancel_requested') else 'completed'
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

    # 预取已入库日期集合, 跳过已入库的日期(避免重复覆盖入库)
    try:
        submitted_dates = _db.get_submitted_dates()
    except Exception:
        submitted_dates = set()

    for i, d in enumerate(dates):
        # 用户请求中断：停止后续入库
        if t.get('cancel_requested'):
            break
        it = items[i]
        try:
            if d in submitted_dates:
                it['status'] = 'skipped'
                it['message'] = '已入库，跳过'
                continue
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

    t['status'] = 'cancelled' if t.get('cancel_requested') else 'completed'
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
    
    # 获取文章列表（支持分页，自动翻页直到覆盖 start_date）
    articles = get_article_list_paginated('444409', start_date=start_date, end_date=end_date)
    if not articles:
        return jsonify({
            'success': False,
            'error': '无法获取文章列表，请检查网络或目标博客页面是否可访问。'
        }), 400
    
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
    
    # 获取文章列表（支持分页，自动翻页直到覆盖 start_date）
    articles = get_article_list_paginated('444409', start_date=start_date, end_date=end_date)
    if not articles:
        return jsonify({
            'success': False,
            'error': '无法获取文章列表，请检查网络或目标博客页面是否可访问。'
        }), 400

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


@app.route('/api/download/cancel/<task_id>', methods=['POST'])
def cancel_download(task_id):
    """请求中断下载任务（当前图片下载完后停止后续日期）"""
    if task_id not in download_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    download_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


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
    force = bool(data.get('force', False))

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
        'submit_to_db': bool(submit_to_db), 'force': force,
        'items': [{'date': d, 'status': 'pending', 'message': '待处理'} for d in dates],
    }
    thread = threading.Thread(
        target=extract_task,
        args=(task_id, dates, bool(submit_to_db)),
        kwargs={'force': force},
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


@app.route('/api/extract/cancel/<task_id>', methods=['POST'])
def cancel_extract(task_id):
    """请求中断提取任务（当前日期处理完后停止后续日期）"""
    if task_id not in extract_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    extract_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


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


@app.route('/api/db/submit-batch/cancel/<task_id>', methods=['POST'])
def cancel_submit_batch(task_id):
    """请求中断批量入库任务（当前日期处理完后停止后续日期）"""
    if task_id not in submit_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    submit_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


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
        _db.init_db()  # 幂等建表, 避免表不存在导致查询抛 ProgrammingError
        submitted_status = _db.get_submitted_status()
    except _db.DBError as e:
        db_connected = False
        db_msg = str(e)
    except Exception as e:
        # 表不存在/连接失败等非 DBError 异常也降级, 避免接口 500
        db_connected = False
        db_msg = f'{type(e).__name__}: {e}'

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


# ============== 盯盘页面 ==============

@app.route('/monitor', methods=['GET'])
def monitor_page():
    """盯盘页面: 上证指数日K + 分时图"""
    return render_template(
        'monitor.html',
        debug_mode=DEBUG_MODE,
        server_instance_id=SERVER_INSTANCE_ID if DEBUG_MODE else '',
    )


@app.route('/api/debug/runtime', methods=['GET'])
def debug_runtime():
    """仅 Debug 模式开放：浏览器诊断器用于识别旧 Flask 进程/旧模板。"""
    if not DEBUG_MODE:
        return jsonify({'success': False, 'error': 'debug mode disabled'}), 404

    def file_info(path):
        try:
            stat = os.stat(path)
            return {'path': path, 'mtime': stat.st_mtime, 'size': stat.st_size}
        except OSError:
            return {'path': path, 'missing': True}

    return jsonify({
        'success': True,
        'debug': True,
        'pid': os.getpid(),
        'instance_id': SERVER_INSTANCE_ID,
        'started_at': SERVER_STARTED_AT,
        'files': {
            'api_server': file_info('api_server.py'),
            'monitor_template': file_info(os.path.join('templates', 'monitor.html')),
        },
    })


@app.route('/api/monitor/health', methods=['GET'])
def monitor_health():
    """盯盘健康检查: 诊断 tqcenter / 通达信路径 / mootdx 连通性。
    不持 _tdx_lock、不调 mootdx 客户端(避免自身被卡死), 用独立线程+超时探测。"""
    import time as _time
    import socket as _socket
    import threading
    result = {'ts': _time.time()}

    # 1. 通达信路径 + tqcenter 状态 (从 tdx_source 拿, 不触发 mootdx)
    try:
        import tdx_source as ts
        result['tdx'] = ts.get_tdx_info()
    except Exception as e:
        result['tdx'] = {'error': f'{type(e).__name__}: {e}'}

    # 2. mootdx 远程服务器连通性 (独立线程+5s超时, 不建 mootdx 客户端)
    def _probe_servers():
        servers = [
            ('218.75.126.9', 7709),
            ('115.238.90.165', 7709),
            ('115.238.56.198', 7709),
            ('124.70.199.56', 7709),
        ]
        reachable = []
        for ip, port in servers:
            try:
                _sk = _socket.create_connection((ip, port), timeout=2)
                _sk.close()
                reachable.append(ip)
            except Exception:
                pass
        return reachable
    _probe_result = [None]
    _pt = threading.Thread(target=lambda: _probe_result.__setitem__(0, _probe_servers()), daemon=True)
    _pt.start()
    _pt.join(timeout=8)
    result['mootdx'] = {
        'remote_servers_reachable': _probe_result[0] if _probe_result[0] is not None else [],
        'probe_timeout': _probe_result[0] is None,
    }

    # 3. 综合判断
    tq_ok = result.get('tdx', {}).get('tqcenter_available', False)
    mootdx_ok = bool(result['mootdx']['remote_servers_reachable'])
    result['status'] = 'ok' if (tq_ok or mootdx_ok) else 'error'
    result['tqcenter_ok'] = tq_ok
    result['mootdx_ok'] = mootdx_ok
    return jsonify(result)


# 分时时间轴生成 (9:30-11:30, 13:00-15:00 共240个点)
def _minute_time_axis(n):
    """生成 n 个分时时间点 (HH:MM)。
    A股连续竞价: 9:30-11:30 (120分钟) + 13:00-15:00 (120分钟) = 240点。
    集合竞价 9:25-9:29 的数据由前端固定轴补齐(后端 mootdx minute 不含集合竞价段)。"""
    from datetime import datetime, timedelta
    times = []
    am_start = datetime(2026, 1, 1, 9, 30)
    pm_start = datetime(2026, 1, 1, 13, 0)
    for i in range(min(n, 240)):
        if i < 120:
            t = am_start + timedelta(minutes=i)
        else:
            t = pm_start + timedelta(minutes=i - 120)
        times.append(t.strftime('%H:%M'))
    return times


@app.route('/api/monitor/index/daily', methods=['GET'])
def monitor_index_daily():
    """上证指数日K线
    优先 tqcenter index_kline (客户端开着最稳定, 走DLL不封IP), 回退 mootdx index_bars。
    参数: count=120 (取最近N根日K)
    """
    try:
        count = request.args.get('count', default=120, type=int)
        count = max(20, min(count, 800))
        # 优先 tqcenter (带 3s 超时保护)
        try:
            import tdx_source as ts
            if ts.is_available():
                import threading
                _bars_result = [None]
                def _fetch_tq_bars():
                    try:
                        _bars_result[0] = ts.index_kline('000001.SH', count=count, period='1d')
                    except Exception:
                        pass
                _t = threading.Thread(target=_fetch_tq_bars, daemon=True)
                _t.start()
                _t.join(timeout=3.0)
                bars = _bars_result[0]
                if bars and len(bars) >= 5:
                    return jsonify({'success': True, 'bars': bars, 'source': 'tqcenter'})
        except Exception as e:
            print(f'[TQ] 指数K线异常: {e}')
        # 回退 mootdx index_bars (带应用层超时, 防止卡死)
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'index_bars', timeout=8,
                                           symbol='000001', frequency=9, start=0, offset=count)
        if df is not None and not df.empty:
            bars = []
            for _, row in df.iterrows():
                bars.append({
                    'date': str(row['datetime'])[:10],
                    'open': round(float(row['open']), 2),
                    'close': round(float(row['close']), 2),
                    'high': round(float(row['high']), 2),
                    'low': round(float(row['low']), 2),
                    'vol': float(row['vol']),
                    'amount': float(row['amount']),
                })
            return jsonify({'success': True, 'bars': bars, 'source': 'mootdx'})
        return jsonify({'success': False, 'error': '无数据'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


_minute_cache = {'ts': 0, 'data': None}  # 大盘分时缓存
_minute_fail_streak = 0  # 连续失败次数(后端侧)
_minute_cache_ttl = 3    # 动态缓存时间(秒), 失败时自动增大

@app.route('/api/monitor/index/minute', methods=['GET'])
def monitor_index_minute():
    """上证指数当日分时 (mootdx minute, 代码用 1A0001)。自适应缓存。"""
    import time as _time
    global _minute_fail_streak, _minute_cache_ttl
    # 动态缓存: 正常3s, 连续失败后自动增大到15s(减轻服务器压力)
    if _minute_cache['data'] and (_time.time() - _minute_cache['ts'] < _minute_cache_ttl):
        return jsonify(_minute_cache['data'])
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minute', timeout=8, symbol='1A0001')
        if df is None or df.empty:
            return jsonify({'success': False, 'error': '无当日分时数据(可能非交易时段)'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        result = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx'}
        # tqcenter 补充: 涨跌幅/上涨下跌家数/均价(领先指标) - 超时1秒防止卡死
        try:
            import tdx_source as ts
            if ts.is_available():
                import threading
                _idx_result = [None]
                def _fetch_idx():
                    try:
                        _idx_result[0] = ts.index_snapshot()
                    except Exception:
                        pass
                _t = threading.Thread(target=_fetch_idx, daemon=True)
                _t.start()
                _t.join(timeout=1.0)
                idx = _idx_result[0]
                if idx:
                    result['index_price'] = idx['price']
                    result['index_pct'] = idx['pct']
                    result['up_home'] = idx['up_home']
                    result['down_home'] = idx['down_home']
                    result['average'] = idx['average']
        except Exception:
            pass
        _minute_cache['data'] = result
        _minute_cache['ts'] = _time.time()
        # 成功: 恢复正常缓存时间
        _minute_fail_streak = 0
        _minute_cache_ttl = 3
        return jsonify(result)
    except Exception as e:
        # 失败: 增大缓存时间, 减轻服务器压力
        _minute_fail_streak += 1
        if _minute_fail_streak >= 2:
            _minute_cache_ttl = 15
        # 连接异常: 尝试切换服务器
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                ht._reconnect_tdx()
        except Exception:
            pass
        # 缓存还有数据就返回旧数据(降级服务), 避免前端空白
        if _minute_cache['data']:
            return jsonify(_minute_cache['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/index/minutes', methods=['GET'])
def monitor_index_minutes():
    """上证指数历史分时 (mootdx minutes, 代码用 1A0001)
    参数: date=20260703 (YYYYMMDD)
    """
    date = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'date 参数需为 YYYYMMDD'}), 400
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minutes', timeout=8, symbol='1A0001', date=int(date))
        if df is None or df.empty:
            return jsonify({'success': False, 'error': f'{date} 无分时数据'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        # 格式化日期显示
        date_fmt = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
        return jsonify({'success': True, 'points': points, 'date': date_fmt, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/index/leading', methods=['GET'])
def monitor_index_leading():
    """上证领先指标(小盘不加权平均): 拉全市场A股实时报价算算术平均涨跌幅。
    返回 leading_pct(领先指标涨跌幅%) 和 index_last_close(上证昨收),
    前端用 index_last_close * (1+leading_pct/100) 换算黄线点位。
    60秒缓存避免频繁拉取全市场报价(单次约2s)。"""
    global _leading_cache
    import time as _time
    if '_leading_cache' not in globals():
        _leading_cache = None
    if _leading_cache and (_time.time() - _leading_cache['ts'] < 60):
        return jsonify(_leading_cache['data'])
    import re as _re
    try:
        import hot_track as ht
        lock = ht._get_tdx_lock()
        with lock:
            client = ht._get_tdx_client()
            # 取沪深全部股票代码(缓存1小时, 代码列表变化少)
            global _astock_codes_cache
            if '_astock_codes_cache' not in globals():
                _astock_codes_cache = None
            if _astock_codes_cache and (_time.time() - _astock_codes_cache['ts'] < 3600):
                codes = _astock_codes_cache['codes']
            else:
                sh = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=1)
                sz = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=0)
                if sh is None or sz is None:
                    return jsonify({'success': False, 'error': '获取股票列表超时'}), 504
                def _is_a(code):
                    return _re.match(r'^(60|00|30|68)\d{4}$', str(code)) is not None
                codes = [c for c in sh['code'].tolist() if _is_a(c)] + \
                        [c for c in sz['code'].tolist() if _is_a(c)]
                _astock_codes_cache = {'codes': codes, 'ts': _time.time()}
            # 批量拉报价(每次最多80只)
            import pandas as pd
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)
            if not frames:
                return jsonify({'success': False, 'error': '无报价数据'}), 404
            all_q = pd.concat(frames, ignore_index=True)
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)]
            if valid.empty:
                return jsonify({'success': False, 'error': '无有效报价'}), 404
            pct = ((valid['price'] - valid['last_close']) / valid['last_close'] * 100).mean()
            # 取上证指数昨收(用于前端换算黄线点位)
            idx_q = ht._tdx_call_with_timeout(client, 'quotes', timeout=5, symbol='1A0001')
            idx_last_close = float(idx_q['last_close'].iloc[0]) if idx_q is not None and not idx_q.empty else 0
        result = {
            'success': True,
            'leading_pct': round(float(pct), 3),
            'index_last_close': round(idx_last_close, 2),
            'source': 'mootdx',
        }
        _leading_cache = {'data': result, 'ts': _time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ===== 个股相关接口 (盯盘第二列) =====

_stock_list_cache = {}  # {(segment, sort): {'data':..., 'ts':...}}
_all_quotes_cache = None  # 全市场A股报价缓存(已废弃, 改用分板块缓存)
_segment_quotes_cache = {}  # {'cyb': {'codes':[], 'names':{}, 'data':[], 'codes_ts':.., 'ts':..}, 'kcb': {...}}
_segment_codes_cache = {}  # 板块代码列表缓存(1小时)


def _get_segment_codes(segment):
    """获取板块的股票代码和名称。优先 tqcenter -> 本地文件缓存 -> mootdx。"""
    import re as _re
    import time as _time
    import os as _os
    import json as _json
    cached = _segment_codes_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 3600):
        return cached['codes'], cached['names']
    # 本地文件缓存(新股上市才会变化, 每天更新一次足够)
    cache_file = _os.path.join(_os.path.dirname(__file__), f'_codes_{segment}.json')
    if _os.path.exists(cache_file):
        mtime = _os.path.getmtime(cache_file)
        if _time.time() - mtime < 86400:  # 24小时内有效
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            codes = data['codes']
            names = data['names']
            _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
            return codes, names
    # 优先 tqcenter (走自己账号, 不封IP)
    try:
        import tdx_source as ts
        if ts.is_available():
            codes, names = ts.segment_codes(segment)
            if codes:
                _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
                try:
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        _json.dump({'codes': codes, 'names': names}, f, ensure_ascii=False)
                except Exception:
                    pass
                return codes, names
    except Exception as e:
        print(f'[TQ] 板块代码 {segment} 异常: {e}')
    # 回退 mootdx
    import hot_track as ht
    with ht._get_tdx_lock():
        client = ht._get_tdx_client()
        if segment == 'cyb':
            # 创业板300开头, 在深市market=0
            sz = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=0)
            if sz is None:
                return [], {}
            codes = [c for c in sz['code'].tolist() if _re.match(r'^30\d{4}$', str(c))]
            names = {}
            for _, r in sz.iterrows():
                if _re.match(r'^30\d{4}$', str(r['code'])):
                    names[str(r['code'])] = str(r.get('name', '')).replace('\x00', '').strip()
        else:
            # 科创板688开头, 在沪市market=1
            sh = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=1)
            if sh is None:
                return [], {}
            codes = [c for c in sh['code'].tolist() if _re.match(r'^688\d{3}$', str(c))]
            names = {}
            for _, r in sh.iterrows():
                if _re.match(r'^688\d{3}$', str(r['code'])):
                    names[str(r['code'])] = str(r.get('name', '')).replace('\x00', '').strip()
    _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
    # 写入本地文件
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            _json.dump({'codes': codes, 'names': names}, f, ensure_ascii=False)
    except Exception:
        pass
    return codes, names


def _fetch_all_a_quotes():
    """拉全市场A股实时报价并计算涨跌幅, 10秒缓存。cyb/kcb共用, 避免重复拉取。"""
    import time as _time
    global _all_quotes_cache
    if _all_quotes_cache and (_time.time() - _all_quotes_cache['ts'] < 10):
        return _all_quotes_cache['data']
    # 合并两个板块的缓存数据
    all_records = []
    for seg in ('cyb', 'kcb'):
        seg_data = _fetch_segment_quotes(seg)
        if seg_data:
            all_records.extend(seg_data)
    if not all_records:
        return None
    _all_quotes_cache = {'data': all_records, 'ts': _time.time()}
    return all_records


def _fetch_segment_quotes(segment):
    """拉单个板块(创业板/科创板)的实时报价, 10秒缓存。
    优先 tqcenter batch_pricevol (客户端开着最稳定), 回退 mootdx 批量 quotes。"""
    import time as _time
    import hot_track as ht
    cached = _segment_quotes_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 10):
        return cached['data']
    codes, names = _get_segment_codes(segment)
    if not codes:
        return None
    # 优先 tqcenter batch_pricevol (走DLL, 不封IP, 客户端开着最稳定)
    try:
        import tdx_source as ts
        if ts.is_available():
            pv = ts.batch_pricevol(codes)
            if pv:
                records = []
                for c in codes:
                    info = pv.get(c)
                    if info:
                        records.append({
                            'code': c,
                            'name': names.get(c, ''),
                            'price': info['price'],
                            'pct': info['pct'],
                            'amount': 0.0,
                            'vol': info['vol'],
                        })
                if records:
                    _segment_quotes_cache[segment] = {'data': records, 'ts': _time.time(), 'source': 'tqcenter'}
                    return records
    except Exception as e:
        print(f'[TQ] 板块报价 {segment} 异常: {e}')
    # 回退 mootdx 批量 quotes (带应用层超时, 80只一批)
    try:
        import pandas as pd
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)
        if frames:
            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price', 'amount', 'vol'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            valid = all_q.dropna(subset=['last_close'])
            valid = valid[valid['last_close'] > 0].copy()
            valid['price'] = valid.apply(
                lambda r: r['price'] if pd.notna(r['price']) and r['price'] > 0 else r['last_close'], axis=1)
            valid['pct'] = valid.apply(
                lambda r: ((r['price'] - r['last_close']) / r['last_close'] * 100)
                          if r['last_close'] > 0 and r['price'] != r['last_close'] else 0.0, axis=1)
            records = []
            for _, row in valid.iterrows():
                code = str(row.get('code', ''))
                records.append({
                    'code': code,
                    'name': names.get(code, ''),
                    'price': round(float(row['price']), 2),
                    'pct': round(float(row['pct']), 2),
                    'amount': round(float(row.get('amount', 0)) / 1e8, 2),
                    'vol': float(row.get('vol', 0)),
                })
            if records:
                _segment_quotes_cache[segment] = {'data': records, 'ts': _time.time(), 'source': 'mootdx'}
                return records
    except Exception as e:
        print(f'[mootdx] 板块报价 {segment} 异常: {e}')
    return None


@app.route('/api/monitor/stocks/list', methods=['GET'])
def monitor_stocks_list():
    """创业板/科创板个股列表, 按涨幅/成交额/成交量排序。
    参数: segment=cyb(创业板30开头)|kcb(科创板688开头), sort=pct|amount|vol, limit=50"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    sort = request.args.get('sort', 'pct')
    limit = request.args.get('limit', default=50, type=int)
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    if sort not in ('pct', 'amount', 'vol'):
        sort = 'pct'
    limit = max(5, min(limit, 200))
    # 10秒缓存(支持前端自动刷新, 又避免高频打TDX)
    cache_key = (segment, sort)
    cached = _stock_list_cache.get(cache_key)
    if cached and (_time.time() - cached['ts'] < 10):
        return jsonify({'success': True, 'stocks': cached['data'][:limit], 'source': cached.get('source', '')})
    try:
        # 直接拉对应板块(不拉全市场), 10秒缓存
        all_records = _fetch_segment_quotes(segment)
        if not all_records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        stocks = all_records
        # 排序
        stocks.sort(key=lambda r: r.get(sort, 0), reverse=True)
        # 从板块报价缓存取数据源标签
        seg_cache = _segment_quotes_cache.get(segment, {})
        src = seg_cache.get('source', '')
        _stock_list_cache[cache_key] = {'data': stocks, 'ts': _time.time(), 'source': src}
        return jsonify({'success': True, 'stocks': stocks[:limit], 'source': src})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/debug/quote_sources', methods=['GET'])
def debug_quote_sources():
    """诊断: 对比 tqcenter batch_pricevol / tqcenter snapshot / mootdx quotes 三种数据源。"""
    code = request.args.get('code', '300017')
    import time as _time
    result = {'code': code, 'ts': _time.time()}
    # 1. tqcenter batch_pricevol
    try:
        import tdx_source as ts
        if ts.is_available():
            pv = ts.batch_pricevol([code])
            if pv and code in pv:
                result['tq_pricevol'] = pv[code]
            else:
                result['tq_pricevol'] = None
            # 2. tqcenter snapshot (get_market_snapshot)
            snap = ts.snapshot(code)
            if snap:
                result['tq_snapshot'] = {k: snap[k] for k in ('price', 'vol', 'pct', 'amount', 'last_close') if k in snap}
            else:
                result['tq_snapshot'] = None
        else:
            result['tq_pricevol'] = 'tqcenter unavailable'
            result['tq_snapshot'] = 'tqcenter unavailable'
    except Exception as e:
        result['tq_error'] = f'{type(e).__name__}: {e}'
    # 3. mootdx quotes
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=[code])
        if df is not None and not df.empty:
            import pandas as pd
            row = df.iloc[0].to_dict()
            result['mootdx_quotes'] = {
                'price': float(row.get('price', 0) or 0),
                'last_close': float(row.get('last_close', 0) or 0),
                'vol': float(row.get('vol', 0) or 0),
                'amount': float(row.get('amount', 0) or 0),
            }
        else:
            result['mootdx_quotes'] = None
    except Exception as e:
        result['mootdx_error'] = f'{type(e).__name__}: {e}'
    return jsonify(result)


@app.route('/api/monitor/stock/daily', methods=['GET'])
def monitor_stock_daily():
    """个股日K线
    优先 tqcenter kline (客户端开着最稳定), 回退 mootdx bars。
    参数: code=300001, count=120"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    count = request.args.get('count', default=120, type=int)
    count = max(20, min(count, 800))
    # 优先 tqcenter (带 3s 超时保护)
    try:
        import tdx_source as ts
        if ts.is_available():
            import threading
            _bars_result = [None]
            def _fetch_tq_kline():
                try:
                    _bars_result[0] = ts.kline(code, count=count, period='1d', dividend_type='none')
                except Exception:
                    pass
            _t = threading.Thread(target=_fetch_tq_kline, daemon=True)
            _t.start()
            _t.join(timeout=3.0)
            bars = _bars_result[0]
            if bars and len(bars) >= 5:
                # 补 last_close (前一根收盘)
                for i in range(len(bars)):
                    bars[i]['last_close'] = bars[i-1]['close'] if i > 0 else bars[i]['open']
                return jsonify({'success': True, 'bars': bars, 'source': 'tqcenter'})
    except Exception as e:
        print(f'[TQ] 个股K线异常: {e}')
    # 回退 mootdx bars (带应用层超时, 防止卡死)
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'bars', timeout=8, symbol=code, frequency=9, offset=count)
        if df is not None and len(df) > 0:
            df = df.sort_index()
            bars = []
            prev_close = None
            for idx, row in df.iterrows():
                close = round(float(row['close']), 2)
                bars.append({
                    'date': str(idx)[:10],
                    'open': round(float(row['open']), 2),
                    'close': close,
                    'high': round(float(row['high']), 2),
                    'low': round(float(row['low']), 2),
                    'vol': float(row['vol']),
                    'amount': float(row.get('amount', 0)),
                    'last_close': prev_close if prev_close is not None else round(float(row['open']), 2),
                })
                prev_close = close
            return jsonify({'success': True, 'bars': bars, 'source': 'mootdx'})
    except Exception as e:
        print(f'[mootdx] 个股K线异常: {e}')
    return jsonify({'success': False, 'error': f'{code} 无数据'}), 404


_stock_minute_cache = {}  # {code: {'ts':.., 'data':..}} 个股分时3秒缓存

@app.route('/api/monitor/stock/minute', methods=['GET'])
def monitor_stock_minute():
    """个股当日分时 (mootdx minute, 纯6位代码)。3秒缓存。
    参数: code=300001"""
    import time as _time
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    cached = _stock_minute_cache.get(code)
    if cached and (_time.time() - cached['ts'] < 3):
        return jsonify(cached['data'])
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minute', timeout=8, symbol=code)
        if df is None or df.empty:
            return jsonify({'success': False, 'error': '无当日分时数据(可能非交易时段)'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        result = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx'}
        # 补充昨收(供前端算涨幅/涨跌停尺度)
        # mootdx bars 无 last_close 列, 取倒数第二根 close 当昨收(最后一根=当日)
        # 优先 tqcenter 快照(含真实 LastClose), 回退 mootdx bars
        try:
            import tdx_source as ts
            if ts.is_available():
                snap = ts.snapshot(code)
                if snap and snap.get('last_close', 0) > 0:
                    result['last_close'] = snap['last_close']
        except Exception:
            pass
        if 'last_close' not in result:
            try:
                with ht._get_tdx_lock():
                    df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=2)
                if df_k is not None and len(df_k) >= 2:
                    result['last_close'] = round(float(df_k.sort_index().iloc[-2]['close']), 3)
            except Exception:
                pass
        _stock_minute_cache[code] = {'ts': _time.time(), 'data': result}
        return jsonify(result)
    except Exception as e:
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                ht._reconnect_tdx()
        except Exception:
            pass
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/stock/minutes', methods=['GET'])
def monitor_stock_minutes():
    """个股历史分时 (mootdx minutes, 纯6位代码)。
    参数: code=300001, date=20260703 (YYYYMMDD)"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    date = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'date 参数需为 YYYYMMDD'}), 400
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minutes', timeout=8, symbol=code, date=int(date))
        if df is None or df.empty:
            return jsonify({'success': False, 'error': f'{date} 无分时数据'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        date_fmt = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
        return jsonify({'success': True, 'points': points, 'date': date_fmt, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


_m5_cache = {}  # {(code): {'data':..., 'ts':...}} 5日/10日分时缓存, 30秒TTL

def _load_stock_minutes_n(code, days, label):
    """个股N日分时拼接 (逐日拉 minutes, 拼成一条线)。
    code: 6位股票代码, days: 天数, label: '5'/'10' (用于错误提示)
    30秒缓存, 避免切股票来回切换时重复远程拉取。"""
    if not re.match(r'^\d{6}$', code):
        return {'success': False, 'error': 'code 需为6位数字'}, 400
    # 30秒缓存
    import time as _time
    ck = (code, days)
    cached = _m5_cache.get(ck)
    if cached and (_time.time() - cached['ts'] < 30):
        return cached['data'], 200
    try:
        import hot_track as ht
        lock = ht._get_tdx_lock()
        with lock:
            client = ht._get_tdx_client()
            # 先取最近N个交易日日期
            df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=8, symbol=code, frequency=9, offset=days)
            if df_k is None or len(df_k) == 0:
                return {'success': False, 'error': f'{code} 无K线数据'}, 404
            df_k = df_k.sort_index()
            dates = [str(idx)[:10].replace('-', '') for idx in df_k.index]
            # 逐日拉分时, 拼接
            all_points = []
            day_labels = []
            for d in dates:
                df_m = ht._tdx_call_with_timeout(client, 'minutes', timeout=5, symbol=code, date=int(d))
                if df_m is None or df_m.empty:
                    continue
                times = _minute_time_axis(len(df_m))
                day_labels.append(f'{d[4:6]}-{d[6:8]}')
                for i, row in df_m.iterrows():
                    all_points.append({
                        'time': times[i] if i < len(times) else str(i),
                        'price': round(float(row['price']), 2),
                        'vol': float(row['vol']),
                        'day': f'{d[4:6]}-{d[6:8]}',
                    })
        if not all_points:
            return {'success': False, 'error': f'无{label}日分时数据'}, 404
        result = {'success': True, 'points': all_points, 'days': day_labels, 'source': 'mootdx'}
        _m5_cache[ck] = {'data': result, 'ts': _time.time()}
        return result, 200
    except Exception as e:
        return {'success': False, 'error': f'{type(e).__name__}: {e}'}, 500


@app.route('/api/monitor/stock/minutes5', methods=['GET'])
def monitor_stock_minutes5():
    """个股5日分时拼接 (逐日拉 minutes, 拼成一条线)。
    参数: code=300001"""
    code = request.args.get('code', '')
    result, status = _load_stock_minutes_n(code, 5, '5')
    return jsonify(result), status


@app.route('/api/monitor/stock/minutes10', methods=['GET'])
def monitor_stock_minutes10():
    """个股近10个交易日分时拼接 (逐日拉 minutes, 拼成一条连续线)。
    参数: code=300001"""
    code = request.args.get('code', '')
    result, status = _load_stock_minutes_n(code, 10, '10')
    return jsonify(result), status


# ===== 自选股接口 =====

@app.route('/api/monitor/watchlist', methods=['GET'])
def monitor_watchlist():
    """获取自选股列表(含实时 price/pct, 复用全市场报价缓存)。"""
    try:
        wl = _load_watchlist()
        if not wl:
            return jsonify({'success': True, 'watchlist': []})
        all_records = _fetch_all_a_quotes()
        quote_map = {r['code']: r for r in all_records} if all_records else {}
        result = []
        for item in wl:
            code = item.get('code', '')
            q = quote_map.get(code, {})
            result.append({
                'code': code,
                'name': item.get('name', q.get('name', '')),
                'added_at': item.get('added_at', ''),
                'price': q.get('price'),
                'pct': q.get('pct'),
            })
        return jsonify({'success': True, 'watchlist': result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/watchlist/add', methods=['POST'])
def monitor_watchlist_add():
    """加入自选股 {code, name}"""
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    name = str(data.get('name', '')).strip()
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    wl = _load_watchlist()
    if not any(w.get('code') == code for w in wl):
        import time as _time
        wl.append({'code': code, 'name': name, 'added_at': int(_time.time())})
        _save_watchlist(wl)
    return jsonify({'success': True, 'watchlist': wl})


@app.route('/api/monitor/watchlist/remove', methods=['POST'])
def monitor_watchlist_remove():
    """移除自选股 {code}"""
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    wl = _load_watchlist()
    wl = [w for w in wl if w.get('code') != code]
    _save_watchlist(wl)
    return jsonify({'success': True, 'watchlist': wl})


# ===== 量比筛选接口 =====
_volume_ratio_cache = {}  # {segment: {data, ts}}


@app.route('/api/monitor/stocks/volume-ratio', methods=['GET'])
def monitor_stocks_volume_ratio():
    """计算板块内个股 当日成交量 / 5日均量 比值(量比), 用于条件筛选。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, ratios: {code: {day_vol, avg5_vol, ratio, pass}}}
    注: 量比 = 当日总成交量 / 过去5日平均总成交量。两者都用日K的vol(全天总量), 口径一致。"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _volume_ratio_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'ratios': cached['data']})
    try:
        import hot_track as ht
        import pandas as pd
        # 取板块股票列表(复用列表接口逻辑)
        all_records = _fetch_all_a_quotes()
        if not all_records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        prefix = '30' if segment == 'cyb' else '688'
        stocks = [r for r in all_records if r['code'].startswith(prefix)]
        stocks.sort(key=lambda r: r.get('pct', 0), reverse=True)
        stocks = stocks[:50]  # Top50, 与列表一致
        ratios = {}
        # 优先 mootdx bars (稳定, 不卡), 回退 tqcenter
        try:
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                for s in stocks:
                    code = s['code']
                    try:
                        # 一次 bars 调用同时取当日量和5日均量(日K vol=全天总成交量, 单位:手)
                        df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=6)
                        if df_k is None or len(df_k) == 0:
                            ratios[code] = {'day_vol': 0, 'avg5_vol': 0, 'ratio': 0, 'pass': False}
                            continue
                        df_k = df_k.sort_index()
                        day_vol = float(df_k['vol'].iloc[-1])           # 最后一根 = 当日总量
                        # 盘前/未开盘: 当日量为0时, 回退到最近一个交易日的数据
                        if day_vol == 0 and len(df_k) >= 2:
                            day_vol = float(df_k['vol'].iloc[-2])       # 昨日总量
                            prev_vols = df_k['vol'].iloc[:-2] if len(df_k) > 2 else []
                            avg5_vol = float(prev_vols.iloc[-5:].mean()) if len(prev_vols) > 0 else 0
                        else:
                            prev_vols = df_k['vol'].iloc[:-1]
                            avg5_vol = float(prev_vols.iloc[-5:].mean()) if len(prev_vols) > 0 else 0
                        ratio = (day_vol / avg5_vol) if avg5_vol > 0 else 0
                        ratios[code] = {
                            'day_vol': round(day_vol, 0),
                            'avg5_vol': round(avg5_vol, 0),
                            'ratio': round(ratio, 2),
                            'pass': ratio >= 3.0,
                        }
                    except Exception:
                        ratios[code] = {'day_vol': 0, 'avg5_vol': 0, 'ratio': 0, 'pass': False}
        except Exception as e:
            print(f'[mootdx] 量比异常: {e}')
        _volume_ratio_cache[segment] = {'data': ratios, 'ts': _time.time()}
        return jsonify({'success': True, 'ratios': ratios, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


_minute_vol_ratio_cache = {}  # {segment: {data, ts}}


@app.route('/api/monitor/stocks/minute-vol-ratio', methods=['GET'])
def monitor_stocks_minute_vol_ratio():
    """计算板块内个股 当日最大分时量 / 前5天平均最大分时量。
    用于"当日最大分时量>前5天平均分时量×2"条件筛选。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, ratios: {code: {max_minute_vol, avg5_max_minute_vol, ratio, pass}}}"""
    import time as _time
    import hot_track as ht
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _minute_vol_ratio_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'ratios': cached['data']})
    # 取板块 Top 50 (复用已排序的报价缓存)
    all_records = _fetch_segment_quotes(segment)
    if not all_records:
        return jsonify({'success': False, 'error': '无报价数据'}), 404
    stocks = sorted(all_records, key=lambda r: r.get('pct', 0), reverse=True)[:50]
    ratios = {}
    try:
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            for s in stocks:
                code = s['code']
                try:
                    # 当日分时最大量
                    df_today = ht._tdx_call_with_timeout(client, 'minute', timeout=5, symbol=code)
                    if df_today is not None and not df_today.empty:
                        max_minute_vol = float(df_today['vol'].max())
                    else:
                        max_minute_vol = 0
                    # 前5天: 先取6根日K(最后一根=今日), 对前5天逐日拉分时取max
                    df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=6)
                    if df_k is None or len(df_k) < 2:
                        ratios[code] = {'max_minute_vol': max_minute_vol, 'avg5_max_minute_vol': 0, 'ratio': 0, 'pass': False}
                        continue
                    df_k = df_k.sort_index()
                    # 取前5天日期(排除最后一根=今日)
                    prev_dates = [str(idx)[:10].replace('-', '') for idx in df_k.index[:-1]]
                    prev_max_vols = []
                    for d in prev_dates[-5:]:  # 最近5天
                        df_m = ht._tdx_call_with_timeout(client, 'minutes', timeout=5, symbol=code, date=int(d))
                        if df_m is not None and not df_m.empty:
                            prev_max_vols.append(float(df_m['vol'].max()))
                        else:
                            continue
                    avg5_max = float(sum(prev_max_vols) / len(prev_max_vols)) if prev_max_vols else 0
                    # 盘前 fallback: 当日无分时数据时, 用昨日最大分时量代替
                    if max_minute_vol == 0 and prev_max_vols:
                        max_minute_vol = prev_max_vols[-1]
                    ratio = (max_minute_vol / avg5_max) if avg5_max > 0 else 0
                    ratios[code] = {
                        'max_minute_vol': round(max_minute_vol, 0),
                        'avg5_max_minute_vol': round(avg5_max, 0),
                        'ratio': round(ratio, 2),
                        'pass': ratio >= 2.0,
                    }
                except Exception:
                    ratios[code] = {'max_minute_vol': 0, 'avg5_max_minute_vol': 0, 'ratio': 0, 'pass': False}
        _minute_vol_ratio_cache[segment] = {'data': ratios, 'ts': _time.time()}
        return jsonify({'success': True, 'ratios': ratios, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


_open_gain_cache = {}  # {segment: {data, ts}}


@app.route('/api/monitor/stocks/open-gain', methods=['GET'])
def monitor_stocks_open_gain():
    """计算板块内个股 开盘涨幅 = (当日开盘价 - 昨收) / 昨收 * 100, 用于"9:25开盘>3%"等条件筛选。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, gains: {code: {open, last_close, open_gain, pass}}}
    数据源: 优先 tqcenter batch_snapshots(含Open/LastClose, 走本地DLL不封IP), 回退 mootdx quotes(含open列)。"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _open_gain_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'gains': cached['data']})
    try:
        import hot_track as ht
        # 取板块 Top50 股票列表(与列表接口一致)
        records = _fetch_segment_quotes(segment)
        if not records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        codes = [r['code'] for r in records[:50]]
        gains = {}

        # 辅助: 当 open=0(盘前/未开盘)时, 用日K取最近交易日数据
        # 返回 (open, last_close) 或 None
        def _fallback_prev_day(code):
            try:
                with ht._get_tdx_lock():
                    client = ht._get_tdx_client()
                    df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=3)
                if df_k is None or len(df_k) < 3:
                    return None
                df_k = df_k.sort_index()
                prev_open = float(df_k['open'].iloc[-2])      # 昨日开盘价
                prev_prev_close = float(df_k['close'].iloc[-3])  # 前日收盘
                if prev_open <= 0 or prev_prev_close <= 0:
                    return None
                return (prev_open, prev_prev_close)
            except Exception:
                return None

        # 优先 tqcenter batch_snapshots(含 Open + LastClose)
        try:
            import tdx_source as ts
            if ts.is_available():
                snaps = ts.batch_snapshots(codes)
                if snaps:
                    for c in codes:
                        s = snaps.get(c)
                        if s:
                            open_p = s.get('open', 0) or 0
                            last_close = s.get('last_close', 0) or 0
                            # 盘前/未开盘: open=0 时回退到昨日数据
                            if open_p <= 0:
                                fb = _fallback_prev_day(c)
                                if fb:
                                    open_p, last_close = fb
                            og = ((open_p - last_close) / last_close * 100) if last_close > 0 and open_p > 0 else 0
                            gains[c] = {
                                'open': round(open_p, 3),
                                'last_close': round(last_close, 3),
                                'open_gain': round(og, 2),
                                'pass': og > 3.0,
                            }
                    if gains:
                        _open_gain_cache[segment] = {'data': gains, 'ts': _time.time()}
                        return jsonify({'success': True, 'gains': gains, 'source': 'tqcenter'})
        except Exception as e:
            print(f'[TQ] 开盘涨幅 {segment} 异常: {e}')
        # 回退 mootdx quotes(含 open / last_close 列)
        import pandas as pd
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is None or df.empty:
                    continue
                for col in ('open', 'last_close', 'price'):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                for _, row in df.iterrows():
                    c = str(row.get('code', ''))
                    open_p = float(row.get('open', 0) or 0)
                    last_close = float(row.get('last_close', 0) or 0)
                    if last_close <= 0:
                        continue
                    # 盘前/未开盘: open=0 时回退到昨日数据
                    if open_p <= 0:
                        fb = _fallback_prev_day(c)
                        if fb:
                            open_p, last_close = fb
                    og = ((open_p - last_close) / last_close * 100) if open_p > 0 else 0
                    gains[c] = {
                        'open': round(open_p, 3),
                        'last_close': round(last_close, 3),
                        'open_gain': round(og, 2),
                        'pass': og > 3.0,
                    }
        _open_gain_cache[segment] = {'data': gains, 'ts': _time.time()}
        return jsonify({'success': True, 'gains': gains, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ===== 概念板块涨停统计 =====
_concept_members_cache = None    # {concept: [(code, name), ...]}, 1小时缓存
_concept_members_ts = 0
_concept_zt_cache = None         # {'data':..., 'ts':...}, 5秒缓存
_concept_zt_timeline = []        # [{ts, '概念':count, ...}, ...], 最多200个点
_concept_zt_top10_ever = set()   # 曾进过top10的概念名(折线图用)
_concept_dist_timelines = {}     # {concept: [{ts, trade_min, buckets:{...}}]}, 每概念最多200点
_concept_dist_cache = {}         # {concept: {'data':..., 'ts':...}}, 5秒缓存
_concept_dist6_timeline = []     # [{ts, trade_min, buckets:{concept:{g3_5,g5_7,zt_up,d3_5,d5_7,zt_down}}}], 最多200点
_concept_zt_session_date = None  # 当前排名时间线所属交易日, 跨日自动清空

# 排除的宽泛概念(成分股过多, 无实际板块意义)
_EXCLUDED_CONCEPTS = {'国企改革', '深股通', '沪股通', '融资融券', '市场连板股', 'ST板块'}

def _load_concept_members():
    """从 ths.concept_member 加载概念->成分股映射(沪深主板60/00 + 创业板30 + 科创板68, 非ST, 排除宽泛概念)。1小时缓存。"""
    global _concept_members_cache, _concept_members_ts
    import time as _time
    if _concept_members_cache and (_time.time() - _concept_members_ts < 3600):
        return _concept_members_cache
    try:
        import db as _db
        conn = _db.get_conn()
        with conn.cursor() as cur:
            # 取主板(60/00) + 创业板(30) + 科创板(68), 排除ST(名称含ST, 在调用方按名称过滤)
            cur.execute("""
                SELECT concept, stock_code FROM ths.concept_member
                WHERE stock_code ~ '^(60|00|30|68)'
                ORDER BY concept, stock_code
            """)
            rows = cur.fetchall()
        conn.close()
        # 按概念分组(排除宽泛概念)
        result = {}
        for concept, code in rows:
            if concept in _EXCLUDED_CONCEPTS:
                continue
            result.setdefault(concept, []).append(code)
        _concept_members_cache = result
        _concept_members_ts = _time.time()
        _report_log = f'[概念] 加载 {len(result)} 个概念, {len(rows)} 条成分股'
        print(_report_log)
        return result
    except Exception as e:
        print(f'[概念] 加载成分股失败: {e}')
        return _concept_members_cache or {}


def _time_to_trade_min(time_str):
    """HH:MM 或 HH:MM:SS -> trade_min (9:25=0, 11:30=125, 13:00=126, 15:00=246)"""
    try:
        parts = str(time_str).strip().split(':')
        h, m = int(parts[0]), int(parts[1])
    except Exception:
        return 0
    hm = h * 60 + m
    if hm < 9 * 60 + 25:
        return 0
    elif hm <= 11 * 60 + 30:
        return hm - (9 * 60 + 25)
    elif hm < 13 * 60:
        return 125
    elif hm <= 15 * 60:
        return hm - (13 * 60) + 126
    else:
        return 246


_concept_hist_minute_cache = {}      # {date_int: {code: [price, ...]}} 多日期分时缓存, 每天只拉一次


def _fetch_zt_minutes(zt_codes, date_int, cancel_check=None):
    """批量拉涨停股的逐分钟分时数据, 按日期缓存(支持多日)。
    返回 {code: [price_per_minute, ...]}, 240个点(9:30-15:00)。
    涨停价 = 当日分时最高价; price == 最高价 即为封板状态。
    优先级: 本地 .lc1 文件(秒级) > mootdx minutes 逐只远程(兜底)。
    cancel_check: 可选的无参回调, 返回True时中止。
    """
    global _concept_hist_minute_cache
    day_cache = _concept_hist_minute_cache.get(date_int, {})
    missing = [c for c in zt_codes if c not in day_cache]
    if not missing:
        return day_cache

    import time as _time
    fetched = 0
    failed = 0
    src = ''

    # ---------- 优先: 本地 .lc1 文件(需通达信盘后下载1分钟线) ----------
    try:
        local = _read_lc1_minutes_batch(missing, date_int)
        if local:
            for code, prices in local.items():
                day_cache[code] = prices
                fetched += 1
                missing.remove(code)
            src = 'lc1'
            print(f'[概念] 分时 .lc1 本地读取 date={date_int}: {len(local)}只')
    except Exception as e:
        print(f'[概念] .lc1 本地读取失败: {e}')

    # ---------- 兜底: mootdx minutes 逐只远程(仅本地缺失的) ----------
    if missing and not (cancel_check and cancel_check()):
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                for code in missing:
                    if cancel_check and cancel_check():
                        break
                    try:
                        t0 = _time.time()
                        df = ht._tdx_call_with_timeout(client, 'minutes', timeout=5, symbol=code, date=date_int)
                        dt = _time.time() - t0
                        if dt > 2:
                            print(f'[概念] 分时 {code} 耗时{dt:.1f}s (慢)')
                        if df is not None and not df.empty and 'price' in df.columns:
                            prices = df['price'].tolist()
                            if prices:
                                day_cache[code] = prices
                                fetched += 1
                        else:
                            failed += 1
                    except Exception as e:
                        failed += 1
                        print(f'[概念] 分时 {code} 异常: {e}')
            src = src or 'mootdx'
        except Exception as e:
            print(f'[概念] 拉分时失败: {e}')

    _concept_hist_minute_cache[date_int] = day_cache
    print(f'[概念] 分时拉取完成 date={date_int} [{src}]: 成功{fetched} 失败{failed}')

    return day_cache


def _build_historical_timeline(realtime_zt_codes=None, date_str=None, zt_codes_override=None):
    """用逐分钟分时数据还原某日概念涨停数变化(可上可下, 能反映炸板/回封)。
    参数:
    - realtime_zt_codes: 实时报价判定的涨停股(monitor场景, 用今天日期)
    - date_str: 指定日期 YYYYMMDD(概念追踪场景)
    - zt_codes_override: 直接传入涨停股集合
    原理:
    1. 涨停股集合 = zt_codes_override > realtime_zt_codes > zt_stocks
    2. 对每只涨停股拉 mootdx minutes(code, date), 得到240个分钟价格
    3. 涨停价 = 当日分时最高价; price == 涨停价 即为该分钟处于封板状态
    4. 逐分钟统计每个概念有多少只成分股处于封板 -> 可上可下的曲线
    返回 (timeline_list, top_concepts_set) 或 ([], set())"""
    try:
        import db as _db
        from datetime import datetime as _dt

        # 确定日期
        if date_str:
            date_int = int(date_str)
            query_date = date_str
        elif realtime_zt_codes:
            date_int = int(_dt.now().strftime('%Y%m%d'))
            query_date = _dt.now().strftime('%Y-%m-%d')
        else:
            conn = _db.get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT max(trade_date) FROM zt_stocks")
                row = cur.fetchone()
                if not row or not row[0]:
                    return [], set()
                query_date = row[0]
            conn.close()
            date_int = int(str(query_date).replace('-', ''))

        # 涨停股集合: zt_codes_override > realtime_zt_codes > zt_stocks
        if zt_codes_override:
            zt_codes = set(str(c).zfill(6) for c in zt_codes_override)
        elif realtime_zt_codes:
            zt_codes = set(str(c).zfill(6) for c in realtime_zt_codes)
        else:
            conn = _db.get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code FROM zt_stocks
                    WHERE trade_date = %s AND (code LIKE '60%%' OR code LIKE '00%%')
                      AND name NOT LIKE '%%ST%%'
                """, (query_date,))
                zt_codes = set(r[0].zfill(6) for r in cur.fetchall())
            conn.close()
        if not zt_codes:
            return [], set()

        # 拉分时数据(按日期缓存)
        minute_map = _fetch_zt_minutes(zt_codes, date_int)
        if not minute_map:
            return [], set()

        # 概念体系
        concept_map = _load_concept_members()
        if not concept_map:
            return [], set()

        # code -> concepts 反向映射
        code_to_concepts = {}
        for concept, codes in concept_map.items():
            for c in codes:
                code_to_concepts.setdefault(c, set()).add(concept)

        # 逐分钟(0-239)统计每个概念的封板数
        from collections import defaultdict
        minute_concept_counts = defaultdict(lambda: defaultdict(int))
        final_counts = defaultdict(int)

        for code, prices in minute_map.items():
            if code not in zt_codes:
                continue
            concepts = code_to_concepts.get(code, set())
            if not concepts:
                continue
            max_p = max(prices)
            for i, p in enumerate(prices):
                if abs(p - max_p) < 0.005:  # 该分钟处于涨停价
                    for con in concepts:
                        minute_concept_counts[i][con] += 1
            # 收盘(最后一分钟)封板?
            if abs(prices[-1] - max_p) < 0.005:
                for con in concepts:
                    final_counts[con] += 1

        if not minute_concept_counts:
            return [], set()

        # 取最终封板数 top10 的概念
        top_concepts = set(sorted(final_counts, key=final_counts.get, reverse=True)[:10])

        # 生成 timeline: 只在有变化的时间点输出(减少数据量)
        def min_idx_to_trade_min(idx):
            if idx < 120:
                return idx + 5  # 9:30 = trade_min 5
            else:
                return idx + 6  # 13:00 = idx 120 -> trade_min 126

        all_mins = sorted(minute_concept_counts.keys())
        timeline = []
        prev_counts = {}
        for m in all_mins:
            counts = dict(minute_concept_counts[m])
            if counts != prev_counts:
                tm = min_idx_to_trade_min(m)
                timeline.append({
                    'ts': _time_to_label(tm),
                    'trade_min': tm,
                    'counts': counts,
                })
                prev_counts = counts

        return timeline, top_concepts
    except Exception as e:
        print(f'[概念] 历史回放失败: {e}')
        return [], set()


def _time_to_label(trade_min):
    """trade_min -> HH:MM 标签"""
    if trade_min <= 125:
        h = 9
        m = 25 + trade_min
    else:
        h = 13
        m = trade_min - 126
    h += m // 60
    m = m % 60
    return f'{h}:{m:02d}'

@app.route('/api/monitor/concept/zt-stats', methods=['GET'])
def monitor_concept_zt_stats():
    """概念板块涨停统计(仅主板非ST, 5秒缓存)。
    返回: {success, ts, concepts: [...], timeline: [...], top10_ever: [...]}
    concepts: [{concept, zt_count, zt_stocks:[{code,pct}], dist:{lt3,g3_5,g5_7}, total_members}]
    timeline: [{ts, concepts:{概念:涨停数}}]  供折线图用
    """
    import time as _time
    global _concept_zt_cache, _concept_zt_timeline, _concept_zt_top10_ever, _concept_dist6_timeline, _concept_zt_session_date

    # 排名/分时数据只属于当天, 服务跨日运行时必须清空, 避免把昨日曾入榜概念混进今天。
    session_date = _time.strftime('%Y%m%d')
    if _concept_zt_session_date != session_date:
        _concept_zt_session_date = session_date
        _concept_zt_cache = None
        _concept_zt_timeline = []
        _concept_zt_top10_ever = set()
        _concept_dist6_timeline = []

    # 10秒缓存
    if _concept_zt_cache and (_time.time() - _concept_zt_cache['ts'] < 10):
        return jsonify(_concept_zt_cache['data'])

    try:
        import hot_track as ht
        import pandas as pd
        concept_map = _load_concept_members()
        if not concept_map:
            return jsonify({'success': False, 'error': '无概念成分股数据'}), 404

        # 收集所有需要拉报价的主板股票(去重)
        all_codes = set()
        for codes in concept_map.values():
            all_codes.update(codes)
        all_codes = sorted(all_codes)
        if not all_codes:
            return jsonify({'success': False, 'error': '无主板成分股'}), 404

        # 优先 tqcenter batch_pricevol (走DLL, 1秒取全部, 不封IP), 回退 mootdx 批量 quotes
        quote_map = {}
        used_tq = False
        try:
            import tdx_source as ts
            if ts.is_available():
                # TQ批量报价不返回名称，先取股票名排除ST/退市，与mootdx分支口径一致。
                try:
                    stock_rows = ts.stock_list('5', with_name=True) or []
                    excluded_codes = {
                        str(row.get('code', '')).zfill(6)
                        for row in stock_rows
                        if 'ST' in str(row.get('name', '')).upper() or '退' in str(row.get('name', ''))
                    }
                    if excluded_codes:
                        all_codes = [code for code in all_codes if code not in excluded_codes]
                except Exception as e:
                    print(f'[TQ] zt-stats 股票名称/ST过滤异常: {e}')
                pv = ts.batch_pricevol(all_codes)
                if pv:
                    for code, info in pv.items():
                        if info.get('last_close', 0) > 0:
                            quote_map[code] = {
                                'pct': info['pct'],
                                'price': info['price'],
                                'name': '',
                            }
                    used_tq = True
        except Exception as e:
            print(f'[TQ] zt-stats 批量报价异常: {e}')

        if not used_tq:
            # 回退 mootdx 批量拉报价(80只/批)
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                frames = []
                for i in range(0, len(all_codes), 80):
                    batch = all_codes[i:i + 80]
                    df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                    if df is not None and not df.empty:
                        frames.append(df)
            if not frames:
                return jsonify({'success': False, 'error': '无报价数据(可能非交易时段)'}), 404

            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price', 'amount', 'vol'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            # 排除ST(quotes返回的name列)
            if 'name' in all_q.columns:
                all_q = all_q[~all_q['name'].astype(str).str.contains('ST', case=False, na=False)]
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
            if valid.empty:
                return jsonify({'success': False, 'error': '无有效报价'}), 404
            valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
            valid['code'] = valid['code'].astype(str).str.zfill(6)
            quote_map = {r['code']: r for _, r in valid.iterrows()}

        # 按概念分组统计
        now_ts = _time.time()
        now_str = _time.strftime('%H:%M:%S')
        # 交易分钟序号: 9:25=0, 11:30=125, 跳过午休, 13:00=126, 15:00=246
        from datetime import datetime as _dt
        _now_hm = _dt.now().hour * 60 + _dt.now().minute
        if _now_hm < 9 * 60 + 25:
            _trade_min = 0
        elif _now_hm <= 11 * 60 + 30:
            _trade_min = _now_hm - (9 * 60 + 25)
        elif _now_hm < 13 * 60:
            _trade_min = 125  # 午休期间固定为午前最后一刻
        elif _now_hm <= 15 * 60:
            _trade_min = _now_hm - (13 * 60) + 126  # 126 = 上午125分钟 + 1
        else:
            _trade_min = 246
        concepts_result = []
        timeline_entry = {'ts': now_str, 'trade_min': _trade_min, 'concepts': {}}
        dist6_entry = {'ts': now_str, 'trade_min': _trade_min, 'buckets': {}}
        realtime_zt_codes = set()  # 收集所有实时涨停股代码(传给历史回放, 保证数量一致)

        for concept, codes in concept_map.items():
            zt_stocks = []
            dist = {'lt3': 0, 'g3_5': 0, 'g5_7': 0}
            dist6 = {'g3_5': 0, 'g5_7': 0, 'zt_up': 0, 'd3_5': 0, 'd5_7': 0, 'zt_down': 0}
            total = 0
            for code in codes:
                q = quote_map.get(code)
                if q is None:
                    continue
                total += 1
                pct = float(q['pct'])
                apct = abs(pct)
                # 涨跌幅分布(abs口径, 旧)
                if apct < 3:
                    dist['lt3'] += 1
                elif apct < 5:
                    dist['g3_5'] += 1
                elif apct < 7:
                    dist['g5_7'] += 1
                # 6档正负分桶(新, 涨停阈值按板块区分)
                thresh = 19.8 if code.startswith(('30', '68')) else 9.8
                if pct >= thresh:
                    dist6['zt_up'] += 1
                elif pct >= 5:
                    dist6['g5_7'] += 1
                elif pct >= 3:
                    dist6['g3_5'] += 1
                elif pct <= -thresh:
                    dist6['zt_down'] += 1
                elif pct <= -5:
                    dist6['d5_7'] += 1
                elif pct <= -3:
                    dist6['d3_5'] += 1
                # 概念涨停数与6档分布统一使用分板阈值:
                # 创业板/科创板20cm取19.8%, 其余10cm取9.8%。
                if pct >= thresh:
                    zt_stocks.append({'code': code, 'pct': round(pct, 2)})
                    realtime_zt_codes.add(code)
            if total == 0:
                continue
            concepts_result.append({
                'concept': concept,
                'zt_count': len(zt_stocks),
                'zt_stocks': zt_stocks[:20],  # 最多返回20只
                'dist': dist,
                'dist6': dist6,
                'total_members': total,
            })
            timeline_entry['concepts'][concept] = len(zt_stocks)
            dist6_entry['buckets'][concept] = dict(dist6)

        # 按涨停数降序；同数量时按概念名稳定排序，避免每次请求边界抖动。
        concepts_result.sort(key=lambda x: (-x['zt_count'], x['concept']))

        # 取当前前10, 记录曾进top10的概念
        top10_now = [c['concept'] for c in concepts_result if c['zt_count'] > 0][:10]
        _concept_zt_top10_ever.update(top10_now)

        # 返回top15(前端取top10, 多给几条避免边界抖动)
        top_concepts = [c for c in concepts_result if c['zt_count'] > 0][:15]

        def _build_rank_map(counts, candidates):
            """按涨停数生成Top10竞争排名；同数量并列同名次(如1,1,3)。"""
            ordered = sorted(
                [(c, counts.get(c, 0)) for c in candidates if counts.get(c, 0) > 0],
                key=lambda x: (-x[1], x[0])
            )[:10]
            rank_map = {}
            prev_count = None
            current_rank = 0
            for pos, (concept_name, count) in enumerate(ordered, 1):
                if count != prev_count:
                    current_rank = pos
                    prev_count = count
                rank_map[concept_name] = current_rank
            return rank_map

        # 维护时间线(最多200个点, 同一trade_min更新最新值)
        # 交易时段正常采样; 非交易时段(收盘后)也更新最后一点保持与concepts同步
        if _concept_zt_timeline and _concept_zt_timeline[-1].get('trade_min') == _trade_min:
            _concept_zt_timeline[-1] = timeline_entry  # 同分钟更新
        else:
            _concept_zt_timeline.append(timeline_entry)
            if len(_concept_zt_timeline) > 200:
                _concept_zt_timeline = _concept_zt_timeline[-200:]
        if _concept_dist6_timeline and _concept_dist6_timeline[-1].get('trade_min') == _trade_min:
            _concept_dist6_timeline[-1] = dist6_entry  # 同分钟更新
        else:
            _concept_dist6_timeline.append(dist6_entry)
            if len(_concept_dist6_timeline) > 200:
                _concept_dist6_timeline = _concept_dist6_timeline[-200:]

        # 折线图数据: 只保留曾进top10的概念
        timeline_out = []
        ever_list = sorted(_concept_zt_top10_ever)
        for entry in _concept_zt_timeline:
            timeline_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'counts': {c: entry['concepts'].get(c, 0) for c in ever_list},
            })

        # 排名时间线: 每个时间点按涨停数排Top10；同数量并列同名次。
        ranks_out = []
        for entry in _concept_zt_timeline:
            counts = entry.get('concepts', {})
            rank_map = _build_rank_map(counts, ever_list)
            ranks_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'ranks': {c: rank_map.get(c) for c in ever_list},
            })

        # 分桶时间线: 只保留 ever_list 概念的分桶
        dist6_out = []
        for entry in _concept_dist6_timeline:
            dist6_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'buckets': {c: entry['buckets'].get(c, {}) for c in ever_list},
            })

        # 用逐分钟分时数据还原当天涨停轨迹(9:25起), 补全实时点之前的开盘段。
        # 传入实时涨停股集合, 使回放的最终数量与柱状图完全一致。
        # 盘中始终触发(不再仅<3点), 让涨停数/排名曲线从9:25铺满到当前时间。
        hist_timeline, hist_concepts = _build_historical_timeline(realtime_zt_codes)
        if hist_timeline:
            ever_list = sorted(set(hist_concepts) | _concept_zt_top10_ever)
            # 历史回放只覆盖到首个实时点之前; 实时点更精确, 保留实时段。
            # 取实时timeline的首个trade_min作为分界点。
            first_real_min = _concept_zt_timeline[0].get('trade_min', 0) if _concept_zt_timeline else 999
            hist_part = [e for e in hist_timeline if e.get('trade_min', 0) < first_real_min]
            # 合并: 历史段(仅counts) + 实时段
            timeline_out = []
            for entry in hist_part:
                timeline_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'counts': {c: entry['counts'].get(c, 0) for c in ever_list},
                })
            for entry in _concept_zt_timeline:
                timeline_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'counts': {c: entry['concepts'].get(c, 0) for c in ever_list},
                })
            # 排名: 历史段+实时段都用各自counts重建(同数量并列; dist6无历史, 留空)
            ranks_out = []
            for entry in hist_part:
                counts = entry.get('counts', {})
                rank_map = _build_rank_map(counts, ever_list)
                ranks_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'ranks': {c: rank_map.get(c) for c in ever_list},
                })
            for entry in _concept_zt_timeline:
                counts = entry.get('concepts', {})
                rank_map = _build_rank_map(counts, ever_list)
                ranks_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'ranks': {c: rank_map.get(c) for c in ever_list},
                })

        # 追加当前实时数据作为最后一个点, 确保排名/涨停数与concepts完全一致
        # (历史回放的分时封板判断 与 实时报价的pct判断 可能不一致)
        cur_counts = {c['concept']: c['zt_count'] for c in concepts_result}
        current_concepts_map = {c['concept']: c for c in concepts_result}
        cur_rank_map = _build_rank_map(cur_counts, ever_list)
        cur_point = {
            'ts': now_str,
            'trade_min': _trade_min,
            'counts': {c: cur_counts.get(c, 0) for c in ever_list},
        }
        cur_rank_point = {
            'ts': now_str,
            'trade_min': _trade_min,
            'ranks': {c: cur_rank_map.get(c) for c in ever_list},
        }
        # 若最后一点trade_min不同则追加, 相同则替换
        if timeline_out and timeline_out[-1].get('trade_min') == _trade_min:
            timeline_out[-1] = cur_point
            ranks_out[-1] = cur_rank_point
        else:
            timeline_out.append(cur_point)
            ranks_out.append(cur_rank_point)

        # 当前Top10概念名->排名(轻量, 供前端独立接口查股票所属概念)
        top10_concept_ranks = {c: cur_rank_map.get(c) for c in top10_now if cur_rank_map.get(c)}

        result = {
            'success': True,
            'ts': now_str,
            'concepts': top_concepts,
            # 下方曾入榜卡片需要完整当前值，不能仅依赖前15 concepts。
            'ever_current': {
                c: {
                    'concept': c,
                    'zt_count': cur_counts.get(c, 0),
                    'total_members': current_concepts_map.get(c, {}).get('total_members', 0),
                }
                for c in ever_list
            },
            'top10_ever': ever_list,
            'timeline': timeline_out,
            'ranks_timeline': ranks_out,
            'dist6_timeline': dist6_out,
            'top10_concept_ranks': top10_concept_ranks,
            'source': 'tqcenter' if used_tq else 'mootdx',
        }
        _concept_zt_cache = {'data': result, 'ts': now_ts}
        return jsonify(result)
    except Exception as e:
        # 缓存还有数据就返回旧的(降级)
        if _concept_zt_cache:
            return jsonify(_concept_zt_cache['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/stock/concepts', methods=['GET'])
def monitor_stock_concepts():
    """批量查询股票所属的当前Top10概念及排名(轻量, 供个股列表"所属概念"列)。
    参数: codes=300001,300002,688001  (逗号分隔, 最多200只)
    返回: {success, concepts: {code: [[concept, rank], ...]}}
    """
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if re.match(r'^\d{6}$', c.strip())][:200]
    if not codes:
        return jsonify({'success': False, 'error': '缺少 codes 参数'}), 400
    try:
        # 从zt-stats缓存取当前Top10概念排名(不重新计算, 复用10秒缓存)
        cached = _concept_zt_cache
        if not cached or not cached.get('data'):
            return jsonify({'success': True, 'concepts': {}})
        zt_data = cached['data']
        top10_ranks = zt_data.get('top10_concept_ranks', {})
        if not top10_ranks:
            return jsonify({'success': True, 'concepts': {}})

        # 取概念成分股, 只查Top10概念
        concept_map = _load_concept_members()
        # 构建 code -> [(concept, rank)] 只针对请求的codes
        result = {}
        for concept_name, rank in top10_ranks.items():
            members = set(concept_map.get(concept_name, []))
            for code in codes:
                if code in members:
                    result.setdefault(code, []).append([concept_name, rank])
        # 按排名排序
        for code in result:
            result[code].sort(key=lambda x: x[1])
        return jsonify({'success': True, 'concepts': result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/monitor/concept/dist-timeline', methods=['GET'])
def monitor_concept_dist_timeline():
    """单概念板块成员股涨跌分布的实时分时(5秒采样, 5秒缓存)。
    参数: concept=概念名
    返回: {success, concept, members_count, timeline:[{ts,trade_min,buckets}], current}
    buckets: {g3_5, g5_7, zt_up, d3_5, d5_7, zt_down} 各档家数
    涨停阈值按板块: 30/68开头=19.8, 其余=9.8
    """
    import time as _time
    from datetime import datetime as _dt

    concept = request.args.get('concept', '').strip()
    if not concept:
        return jsonify({'success': False, 'error': '缺少 concept 参数'}), 400

    # 5秒缓存命中直接返回
    cached = _concept_dist_cache.get(concept)
    if cached and (_time.time() - cached['ts'] < 5):
        return jsonify(cached['data'])

    try:
        import hot_track as ht
        import pandas as pd

        concept_map = _load_concept_members()
        codes = concept_map.get(concept)
        if not codes:
            return jsonify({'success': False, 'error': f'概念「{concept}」无成分股'}), 404

        # mootdx 批量拉报价(80只/批, 同 zt-stats 模式)
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)

        # 分6档计数(仅看正/负, 不计abs; 涨停按板块阈值)
        buckets = {'g3_5': 0, 'g5_7': 0, 'zt_up': 0, 'd3_5': 0, 'd5_7': 0, 'zt_down': 0}
        members_count = 0
        if frames:
            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            if 'name' in all_q.columns:
                all_q = all_q[~all_q['name'].astype(str).str.contains('ST', case=False, na=False)]
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
            if not valid.empty:
                valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
                valid['code'] = valid['code'].astype(str).str.zfill(6)
                members_count = len(valid)
                for _, r in valid.iterrows():
                    pct = float(r['pct'])
                    code = str(r['code'])
                    thresh = 19.8 if code.startswith(('30', '68')) else 9.8
                    if pct >= thresh:
                        buckets['zt_up'] += 1
                    elif pct >= 5:
                        buckets['g5_7'] += 1
                    elif pct >= 3:
                        buckets['g3_5'] += 1
                    elif pct <= -thresh:
                        buckets['zt_down'] += 1
                    elif pct <= -5:
                        buckets['d5_7'] += 1
                    elif pct <= -3:
                        buckets['d3_5'] += 1

        # 交易分钟序号(9:25=0, 11:30=125, 13:00=126, 15:00=246)
        _now_hm = _dt.now().hour * 60 + _dt.now().minute
        if _now_hm < 9 * 60 + 25:
            _trade_min = 0
        elif _now_hm <= 11 * 60 + 30:
            _trade_min = _now_hm - (9 * 60 + 25)
        elif _now_hm < 13 * 60:
            _trade_min = 125
        elif _now_hm <= 15 * 60:
            _trade_min = _now_hm - (13 * 60) + 126
        else:
            _trade_min = 246

        # 追加采样点(有报价数据时才采, 避免非交易时段产生空点)
        now_str = _time.strftime('%H:%M:%S')
        tl = _concept_dist_timelines.setdefault(concept, [])
        if members_count > 0:
            # 同一 trade_min 不重复采(缓存miss但仍在同一分钟内)
            if not tl or tl[-1].get('trade_min') != _trade_min:
                tl.append({
                    'ts': now_str,
                    'trade_min': _trade_min,
                    'buckets': dict(buckets),
                })
                if len(tl) > 200:
                    _concept_dist_timelines[concept] = tl[-200:]

        if not tl:
            return jsonify({'success': False, 'error': '暂无分时数据(可能非交易时段且无历史采样)'}), 404

        result = {
            'success': True,
            'concept': concept,
            'members_count': members_count or (len(codes)),
            'timeline': tl[:],
            'current': {'ts': now_str, 'buckets': buckets, 'trade_min': _trade_min},
            'source': 'mootdx',
        }
        _concept_dist_cache[concept] = {'data': result, 'ts': _time.time()}
        return jsonify(result)
    except Exception as e:
        cached = _concept_dist_cache.get(concept)
        if cached:
            return jsonify(cached['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ===== WebSocket 实时推送 =====
# tqcenter batch_pricevol 批量报价(1秒取1400只) + mootdx 分时(分时数据 tqcenter 盘中拿不到)
_ws_push_thread = None
_ws_subscribed_stocks = set()  # 前端订阅的个股分时代码
_ws_subscribed_quote_codes = {}  # 前端订阅的报价股票: {'cyb': [code,...], 'kcb': [code,...]}
_tq_emit_lock = threading.Lock()  # socketio.emit 线程安全

@socketio.on('connect')
def ws_connect():
    print(f'[WS] 客户端连接')

@socketio.on('disconnect')
def ws_disconnect():
    print(f'[WS] 客户端断开')

@socketio.on('subscribe_stock')
def ws_subscribe_stock(code):
    """前端订阅个股分时"""
    if code and re.match(r'^\d{6}$', code):
        _ws_subscribed_stocks.add(code)

@socketio.on('unsubscribe_stock')
def ws_unsubscribe_stock(code):
    _ws_subscribed_stocks.discard(code)

@socketio.on('subscribe_quotes')
def ws_subscribe_quotes(data):
    """前端订阅板块报价, data={'segment':'cyb', 'codes':['300001','300002',...]}"""
    if isinstance(data, dict):
        seg = data.get('segment')
        codes = data.get('codes', [])
        if seg in ('cyb', 'kcb') and isinstance(codes, list):
            _ws_subscribed_quote_codes[seg] = [c for c in codes if re.match(r'^\d{6}$', str(c))][:80]

@socketio.on('unsubscribe_quotes')
def ws_unsubscribe_quotes(seg):
    _ws_subscribed_quote_codes.pop(seg, None)

def _is_trade_time():
    """判断是否交易时段"""
    from datetime import datetime
    now = datetime.now()
    h, m, w = now.hour, now.minute, now.weekday()
    if w >= 5:
        return False
    mins = h * 60 + m
    return (mins >= 555 and mins <= 695) or (mins >= 780 and mins <= 905)


def _fetch_quotes_batch(codes):
    """批量拉报价(WS推送用)。优先 mootdx quotes(实时性最好), 回退 tqcenter batch_pricevol。
    用 WS 专用客户端(独立连接不抢主锁), 避免阻塞 HTTP 请求。"""
    # 优先 mootdx 批量 quotes (实时数据, 和上证分时同源)
    try:
        import pandas as pd
        import hot_track as ht
        with ht._get_ws_tdx_lock():
            client = ht._get_ws_tdx_client()
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)
        if frames:
            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price', 'amount', 'vol'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            stocks = []
            for _, row in all_q.iterrows():
                code = str(row.get('code', ''))
                last_close = float(row.get('last_close', 0) or 0)
                price = float(row.get('price', 0) or 0)
                if last_close <= 0:
                    continue
                if price <= 0:
                    price = last_close
                pct = ((price - last_close) / last_close * 100) if last_close > 0 else 0
                stocks.append({
                    'code': code,
                    'price': round(price, 2),
                    'pct': round(pct, 2),
                    'vol': float(row.get('vol', 0) or 0),
                    'amount': round(float(row.get('amount', 0) or 0) / 10000, 2),
                })
            if stocks:
                return stocks
    except Exception as e:
        print(f'[mootdx] 批量报价异常: {e}')
    # 回退 tqcenter batch_pricevol (非交易时段或mootdx不可用时)
    try:
        import tdx_source as ts
        if ts.is_available():
            pv = ts.batch_pricevol(codes)
            if pv:
                stocks = []
                for c in codes:
                    info = pv.get(c)
                    if info:
                        stocks.append({
                            'code': c,
                            'price': info['price'],
                            'pct': info['pct'],
                            'vol': info['vol'],
                            'amount': 0.0,
                        })
                if stocks:
                    return stocks
    except Exception as e:
        print(f'[TQ] 批量报价异常: {e}')
    return []


def _fetch_minute_mootdx(symbol):
    """用 mootdx 拉分时数据(WS推送用)。用 WS 专用客户端, 不抢主锁。"""
    try:
        import hot_track as ht
        with ht._get_ws_tdx_lock():
            client = ht._get_ws_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minute', timeout=8, symbol=symbol)
        if df is None or df.empty or len(df) < 2:
            return None
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        return points
    except Exception:
        return None


def _ws_push_loop():
    """后台推送线程。
    tqcenter 可用: 报价走 batch_pricevol(2秒一轮全量推送), 分时走 mootdx。
    tqcenter 不可用: 全走 mootdx 1秒轮询。"""
    import time
    print('[WS] 推送线程启动')
    tq_mode = False
    while True:
        try:
            if not _is_trade_time():
                time.sleep(10)
                continue

            # 检测 tqcenter 可用性
            if not tq_mode:
                try:
                    import tdx_source as ts
                    if ts.is_available():
                        tq_mode = True
                        print('[WS] tqcenter 可用, 报价走 batch_pricevol')
                except Exception:
                    pass

            t0 = time.time()

            # 1. 大盘分时 (mootdx minute)
            try:
                points = _fetch_minute_mootdx('1A0001')
                if points:
                    _minute_cache['data'] = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx'}
                    _minute_cache['ts'] = time.time()
                    # tqcenter 补充: 涨跌幅/上涨下跌家数/均价 (超时1.5秒, 防止卡死推送循环)
                    if tq_mode:
                        try:
                            import tdx_source as ts
                            import threading as _th
                            _idx_res = [None]
                            def _fetch_idx():
                                try:
                                    _idx_res[0] = ts.index_snapshot()
                                except Exception:
                                    pass
                            _t = _th.Thread(target=_fetch_idx, daemon=True)
                            _t.start()
                            _t.join(timeout=1.5)
                            idx = _idx_res[0]
                            if idx:
                                _minute_cache['data']['index_price'] = idx['price']
                                _minute_cache['data']['index_pct'] = idx['pct']
                                _minute_cache['data']['up_home'] = idx['up_home']
                                _minute_cache['data']['down_home'] = idx['down_home']
                                _minute_cache['data']['average'] = idx['average']
                        except Exception:
                            pass
                    with _tq_emit_lock:
                        socketio.emit('index_minute', _minute_cache['data'])
            except Exception as e:
                print(f'[WS] 大盘分时异常: {e}')

            # 2. 订阅的个股分时 (mootdx minute)
            for code in list(_ws_subscribed_stocks):
                try:
                    points = _fetch_minute_mootdx(code)
                    if points:
                        result = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx-ws'}
                        _stock_minute_cache[code] = {'ts': time.time(), 'data': result}
                        with _tq_emit_lock:
                            socketio.emit('stock_minute', {'code': code, **result})
                except Exception:
                    pass

            # 3. 板块报价 (batch_pricevol 批量, 2秒一轮)
            for seg, codes in list(_ws_subscribed_quote_codes.items()):
                if not codes:
                    continue
                stocks = _fetch_quotes_batch(codes)
                if stocks:
                    with _tq_emit_lock:
                        socketio.emit('stock_quotes', {'segment': seg, 'stocks': stocks})

            # 控制频率: tqcenter 模式 2秒, mootdx 模式 1秒
            interval = 2.0 if tq_mode else 1.0
            elapsed = time.time() - t0
            time.sleep(max(0.5, interval - elapsed))
        except Exception as e:
            print(f'[WS] 推送循环异常: {e}')
            time.sleep(3)

def start_ws_push():
    """启动WebSocket推送线程"""
    global _ws_push_thread
    if _ws_push_thread is None:
        _ws_push_thread = threading.Thread(target=_ws_push_loop, daemon=True)
        _ws_push_thread.start()


# ============== 同花顺概念数据同步 ==============

_ths_sync_running = False
_ths_sync_progress = {'current': 0, 'total': 0, 'name': '', 'count': 0, 'ok': True, 'done': False, 'result': None}


@app.route('/api/sync/ths/status', methods=['GET'])
def ths_sync_status():
    """获取同花顺概念同步状态 (CSV + DB 数据量统计)。"""
    try:
        import ths_sync
        status = ths_sync.get_status()
        return jsonify({'success': True, 'status': status, 'syncing': _ths_sync_running, 'progress': _ths_sync_progress})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/sync/ths/concepts', methods=['POST'])
def ths_sync_concepts():
    """同步同花顺概念数据 (拉取 adata -> CSV -> PostgreSQL)。
    异步执行, 立即返回; 前端轮询 /api/sync/ths/status 获取进度。"""
    global _ths_sync_running, _ths_sync_progress
    if _ths_sync_running:
        return jsonify({'success': False, 'error': '同步正在进行中'}), 409

    refresh = request.json.get('refresh', False) if request.json else False

    _ths_sync_running = True
    _ths_sync_progress = {'current': 0, 'total': 0, 'name': '初始化...', 'count': 0, 'ok': True, 'done': False, 'result': None}

    def _run():
        global _ths_sync_running, _ths_sync_progress
        try:
            import ths_sync

            def on_progress(idx, total, name, count, ok):
                _ths_sync_progress['current'] = idx
                _ths_sync_progress['total'] = total
                _ths_sync_progress['name'] = name
                _ths_sync_progress['count'] = count
                _ths_sync_progress['ok'] = ok

            # 1. 拉取概念数据 -> CSV
            fetch_result = ths_sync.fetch_all_concepts(refresh=refresh, on_progress=on_progress)

            # 2. 入库 PostgreSQL
            db_result = ths_sync.load_to_db()

            _ths_sync_progress['done'] = True
            _ths_sync_progress['result'] = {
                'fetch': fetch_result,
                'db': db_result,
            }
        except Exception as e:
            _ths_sync_progress['done'] = True
            _ths_sync_progress['result'] = {'error': f'{type(e).__name__}: {e}'}
        finally:
            _ths_sync_running = False

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': '同步已启动'})


@app.route('/api/sync/ths/concepts/list', methods=['GET'])
def ths_concepts_list():
    """列出所有同花顺概念及其成分股数量。"""
    try:
        import ths_sync
        limit = request.args.get('limit', default=100, type=int)
        offset = request.args.get('offset', default=0, type=int)
        result = ths_sync.list_concepts(limit=min(limit, 500), offset=offset)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/sync/ths/stock-concepts', methods=['GET'])
def ths_stock_concepts():
    """查询单只股票的同花顺概念列表。"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    try:
        import ths_sync
        concepts = ths_sync.get_stock_concepts(code)
        return jsonify({'success': True, 'code': code, 'concepts': concepts})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ============== 热门股追踪页面 ==============

@app.route('/hot', methods=['GET'])
def hot_track_page():
    """热门股追踪页面"""
    resp = make_response(render_template('hot_track.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/api/hot/track', methods=['GET'])
def api_hot_track():
    """热门股追踪数据API"""
    import re
    
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    sort = request.args.get('sort', 'stock_count')
    with_price = request.args.get('price', '1') not in ('0', 'false', 'no')
    # 筛选条件: filter_and(与门, 全部满足) / filter_or(或门, 任一满足), 逗号分隔的条件ID
    filter_and = [x for x in request.args.get('filter_and', '').split(',') if x] or None
    filter_or = [x for x in request.args.get('filter_or', '').split(',') if x] or None
    
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
        data = ht.track_hot_stocks(start, end, sort=actual_sort, with_price=with_price,
                                   filter_and=filter_and, filter_or=filter_or)
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
                    # 停牌日不计为缺失
                    if track.get('suspended'):
                        continue
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
    
    # 瘦身: by_date 里每只股票只保留当天的 track(前端只用 s.track[当天])
    _slim_by_date_tracks(data)

    return jsonify({
        'success': True, 
        **data,
        'data_integrity': missing_stats
    })


@app.route('/api/hot/dates', methods=['GET'])
def api_hot_dates():
    """返回数据库中已入库的所有日期(热门股追踪的可选范围)。
    自动追加今天(如果是工作日且未入库), 使同花顺概念追踪能选到今天。"""
    import db as _db
    from datetime import datetime as _dt
    try:
        dates = sorted(_db.get_submitted_dates())
    except _db.DBError as e:
        return jsonify({'success': True, 'dates': [], 'db_connected': False, 'db_message': str(e)})
    # 追加今天(工作日且不在列表中)
    today_str = _dt.now().strftime('%Y%m%d')
    if _dt.now().weekday() < 5 and today_str not in dates:
        dates.append(today_str)
        dates.sort()
    return jsonify({'success': True, 'dates': dates, 'db_connected': True})


_concept_daily_zt_cache = {}  # {date_int: {code: name}} 每日涨停股缓存


def _read_day_files_zt(date_int, all_codes):
    """读通达信本地 .day 日线文件, 判定某日涨停股。
    .day 文件每条记录32字节: date(I) open(I*100) high(I*100) low(I*100) close(I*100)
                            amount(f) vol(I) reserved(I)
    3189只主板股票约1秒读完, 无网络依赖。
    返回 {code: name} 涨停股字典(name暂用code代替, .day不含名称)。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'lday')
    sz_dir = os.path.join(base, 'sz', 'lday')
    if not (os.path.isdir(sh_dir) and os.path.isdir(sz_dir)):
        return {}

    result = {}
    target = int(date_int)
    for code in all_codes:
        if code.startswith('6'):
            fp = os.path.join(sh_dir, f'sh{code}.day')
        else:
            fp = os.path.join(sz_dir, f'sz{code}.day')
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            n = len(data) // 32
            # 倒序找目标日期(.day按时间升序, 倒序更快)
            for i in range(n - 1, 0, -1):
                rec = data[i * 32:(i + 1) * 32]
                d = struct.unpack('I', rec[:4])[0]
                if d == target:
                    close = struct.unpack('I', rec[16:20])[0] / 100.0
                    prev_rec = data[(i - 1) * 32:i * 32]
                    prev_close = struct.unpack('I', prev_rec[16:20])[0] / 100.0
                    if prev_close > 0 and (close - prev_close) / prev_close * 100 >= 9.8:
                        result[code] = code
                    break
                if d < target:
                    break  # 已越过目标日期, 文件无该日数据
        except Exception:
            continue
    return result


def _read_day_closes_batch(codes):
    """批量读通达信本地 .day 文件, 返回每只股票的 {date_int: close} 日线收盘价。
    支持全市场(沪市6/科创板688 -> sh, 深市0/创业板300 -> sz)。
    约5000只2秒读完, 无网络依赖。
    返回 {code: {date_int: close_price}}。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'lday')
    sz_dir = os.path.join(base, 'sz', 'lday')
    if not (os.path.isdir(sh_dir) and os.path.isdir(sz_dir)):
        return {}

    result = {}
    for code in codes:
        # 沪市: 6开头(主板60/科创板688) / 9开头 -> sh
        # 深市: 0/3开头(主板00/创业板300) -> sz
        if code.startswith('6') or code.startswith('9'):
            fp = os.path.join(sh_dir, f'sh{code}.day')
        else:
            fp = os.path.join(sz_dir, f'sz{code}.day')
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            closes = {}
            for i in range(0, len(data), 32):
                rec = data[i:i + 32]
                if len(rec) < 32:
                    break
                d = struct.unpack('I', rec[:4])[0]
                close = struct.unpack('I', rec[16:20])[0] / 100.0
                if close > 0:
                    closes[str(d)] = close
            if closes:
                result[code] = closes
        except Exception:
            continue
    return result


def _read_lc1_minutes(code, date_int):
    """读通达信本地 .lc1 1分钟线文件, 提取指定日期的逐分钟价格。
    .lc1 每条记录32字节: date(I) time(I) open(f) high(f) low(f) close(f) amount(f) vol(I)
    返回 [price_per_minute, ...] (用close作为price, 对齐mootdx minutes的price列) 或 None。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return None
    if code.startswith('6') or code.startswith('9'):
        fp = os.path.join(base, 'sh', 'minline', f'sh{code}.lc1')
    else:
        fp = os.path.join(base, 'sz', 'minline', f'sz{code}.lc1')
    if not os.path.exists(fp):
        return None

    target = int(date_int)
    try:
        with open(fp, 'rb') as f:
            data = f.read()
        prices = []
        for i in range(0, len(data), 32):
            rec = data[i:i + 32]
            if len(rec) < 32:
                break
            d = struct.unpack('I', rec[:4])[0]
            if d < target:
                continue
            if d > target:
                break  # 已越过目标日期
            # d == target: 取close作为price
            close = struct.unpack('f', rec[16:20])[0]
            if close > 0:
                prices.append(round(close, 3))
        return prices if prices else None
    except Exception:
        return None


def _read_lc1_minutes_batch(codes, date_int):
    """批量读本地 .lc1 文件, 提取指定日期的逐分钟分时。
    返回 {code: [price, ...]}, 只含成功读取的股票。
    若 minline 目录无 .lc1 文件(未下载分钟线), 返回空dict, 调用方回退mootdx。
    """
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'minline')
    sz_dir = os.path.join(base, 'sz', 'minline')
    if not os.path.isdir(sh_dir) and not os.path.isdir(sz_dir):
        return {}

    result = {}
    for code in codes:
        p = _read_lc1_minutes(code, date_int)
        if p:
            result[code] = p
    return result


def _fetch_daily_zt_codes(date_int, concept_map, cancel_check=None):
    """判定某日涨停股(主板60/00, pct>=9.8%), 按日缓存。
    优先级:
    - 今天: tqcenter batch_pricevol(走本地通达信, ~0.8秒) > mootdx quotes兜底
    - 历史: 本地 .day 文件(~1秒) > mootdx bars逐只兜底(极慢)
    cancel_check: 可选的无参回调, 返回True时中止
    返回 {code: name} 涨停股字典。
    """
    if date_int in _concept_daily_zt_cache:
        return _concept_daily_zt_cache[date_int]

    # 收集所有主板成分股代码
    all_codes = set()
    for codes in concept_map.values():
        all_codes.update(codes)
    all_codes = sorted(all_codes)

    result = {}
    from datetime import datetime as _dt
    is_today = (date_int == int(_dt.now().strftime('%Y%m%d')))
    src = ''

    # ---------- 优先路径 ----------
    try:
        if is_today:
            # 今天: tqcenter 批量报价(本地通达信, 不封IP, ~0.8秒)
            import tdx_source as _ts
            if _ts.is_available():
                pv = _ts.batch_pricevol(all_codes)
                if pv:
                    for code, v in pv.items():
                        if float(v.get('pct', 0)) >= 9.8:
                            result[code] = code
                    src = 'tqcenter'
        else:
            # 历史: 本地 .day 文件(~1秒, 无网络)
            r = _read_day_files_zt(date_int, all_codes)
            if r:
                result = r
                src = 'dayfile'
    except Exception as e:
        print(f'[概念] 优先路径失败, 回退mootdx: {e}')

    # ---------- 兜底: mootdx (优先路径无结果时) ----------
    if not result and not (cancel_check and cancel_check()):
        try:
            import hot_track as ht
            import pandas as pd
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()

                if is_today:
                    # 兜底: mootdx 批量 quotes (80只/批)
                    frames = []
                    for i in range(0, len(all_codes), 80):
                        if cancel_check and cancel_check():
                            break
                        batch = all_codes[i:i + 80]
                        df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                        if df is not None and not df.empty:
                            frames.append(df)
                    if frames:
                        all_q = pd.concat(frames, ignore_index=True)
                        for col in ('last_close', 'price'):
                            if col in all_q.columns:
                                all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
                        valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
                        valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
                        valid['code'] = valid['code'].astype(str).str.zfill(6)
                        for _, r in valid.iterrows():
                            if float(r['pct']) >= 9.8:
                                name = r.get('name', '') if 'name' in valid.columns else ''
                                result[str(r['code'])] = str(name)
                        src = src or 'mootdx-quotes'
                else:
                    # 兜底: mootdx 逐只 bars (极慢~16分钟, 仅在.day文件缺失时)
                    target = str(date_int)
                    for code in all_codes:
                        if cancel_check and cancel_check():
                            break
                        try:
                            df = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=10)
                            if df is None or df.empty:
                                continue
                            for idx in range(len(df)):
                                row = df.iloc[idx]
                                dt = str(row.get('datetime', ''))
                                if dt[:10].replace('-', '') == target:
                                    close = float(row['close'])
                                    if idx > 0:
                                        prev_close = float(df.iloc[idx - 1]['close'])
                                    else:
                                        prev_close = float(row['open'])
                                    if prev_close > 0:
                                        pct = (close - prev_close) / prev_close * 100
                                        if pct >= 9.8:
                                            result[code] = code
                                    break
                        except Exception:
                            pass
                    src = src or 'mootdx-bars'
        except Exception as e:
            print(f'[概念] mootdx兜底失败: {e}')

    _concept_daily_zt_cache[date_int] = result
    print(f'[概念] 涨停判定 date={date_int} [{src}]: {len(result)}只')
    return result


# ===== 同花顺概念追踪: 异步任务 (带进度+取消) =====
_concept_tasks = {}  # {task_id: {status, progress, cur_date, total_dates, logs, result, error, _cancel, days_partial}}
# 按天结果缓存(跨任务复用): {date_str: day_result}。今天的数据不缓存(实时变化)。
# 持久化到磁盘, 重启后历史日期不再重复计算(历史分时数据不变, 结果确定)。
_CONCEPT_DAY_CACHE_FILE = '.concept_day_cache.json'
_concept_day_cache = {}
_concept_day_cache_lock = threading.Lock()


def _load_concept_day_cache():
    global _concept_day_cache
    try:
        if os.path.exists(_CONCEPT_DAY_CACHE_FILE):
            with open(_CONCEPT_DAY_CACHE_FILE, 'r', encoding='utf-8') as f:
                _concept_day_cache = json.load(f) or {}
            print(f'[概念] 已载入按天缓存 {len(_concept_day_cache)} 天(历史日期不再重算)')
    except Exception as e:
        print(f'[概念] 载入按天缓存失败: {e}')
        _concept_day_cache = {}


def _save_concept_day_cache():
    """全量落盘(按天缓存, 数量不大, 几十~几百天)。"""
    try:
        with _concept_day_cache_lock:
            with open(_CONCEPT_DAY_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(_concept_day_cache, f, ensure_ascii=False)
    except Exception as e:
        print(f'[概念] 保存按天缓存失败: {e}')


_load_concept_day_cache()

@app.route('/api/hot/concept-track', methods=['GET'])
def api_hot_concept_track():
    """同花顺概念涨停追踪: 启动异步任务, 返回 task_id。
    后续用 /api/hot/concept-track/status/<task_id> 轮询进度。
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    force = request.args.get('force') in ('1', 'true', 'True', 'yes')
    if not start or not end:
        return jsonify({'success': False, 'error': '需要 start 和 end 参数'}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    _concept_tasks[task_id] = {
        'status': 'pending', 'progress': 0,
        'cur_date': '', 'total_dates': 0, 'cur_idx': 0,
        'start': start, 'end': end, 'force': force,
        'logs': [], 'result': None, 'error': None,
        '_cancel': False,
    }
    threading.Thread(target=_concept_track_task,
                     args=(task_id, start, end, force), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/concept-track/status/{task_id}'})


@app.route('/api/hot/concept-track/status/<task_id>', methods=['GET'])
def api_hot_concept_track_status(task_id):
    """查询概念追踪任务进度; ?since=N 增量拉日志; 完成时返回 result"""
    if task_id not in _concept_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = _concept_tasks[task_id]
    since = request.args.get('since', default=0, type=int)
    resp = {
        'success': True, 'status': t['status'],
        'progress': t['progress'],
        'cur_date': t['cur_date'],
        'total_dates': t['total_dates'],
        'cur_idx': t['cur_idx'],
        'logs': t['logs'][since:], 'log_count': len(t['logs']),
    }
    if t['status'] == 'completed':
        resp['result'] = t['result']
        # 完成后清理任务(保留30秒让前端取结果)
    elif t['status'] == 'failed':
        resp['error'] = t['error']
    elif t['status'] == 'cancelled':
        resp['error'] = t.get('error', '已取消')
    return jsonify(resp)


@app.route('/api/hot/concept-track/cancel/<task_id>', methods=['POST'])
def api_hot_concept_track_cancel(task_id):
    """取消概念追踪任务"""
    if task_id not in _concept_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = _concept_tasks[task_id]
    if t['status'] in ('completed', 'failed', 'cancelled'):
        return jsonify({'success': True, 'status': t['status'], 'msg': '任务已结束'})
    t['_cancel'] = True
    return jsonify({'success': True, 'msg': '已发送取消信号'})


def _concept_track_task(task_id, start, end, force=False):
    """概念追踪后台任务: 逐日拉取涨停分时, 重建概念涨停轨迹。
    force=True 时忽略按天缓存, 强制重算并覆盖缓存。"""
    global _concept_tasks
    t = _concept_tasks[task_id]
    t['status'] = 'running'

    def log(msg):
        t['logs'].append(msg)

    try:
        import db as _db
        # 用 zt_daily 的日期作为交易日参考
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT to_char(trade_date, 'YYYYMMDD') AS d
                FROM zt_daily
                WHERE trade_date BETWEEN %s AND %s
                ORDER BY d
            """, (f'{start[:4]}-{start[4:6]}-{start[6:8]}', f'{end[:4]}-{end[4:6]}-{end[6:8]}'))
            trade_dates = [r[0] for r in cur.fetchall()]
        conn.close()

        # 也加入今天(如果今天在范围内且是交易日)
        from datetime import datetime as _dt
        today_str = _dt.now().strftime('%Y%m%d')
        if start <= today_str <= end and today_str not in trade_dates:
            if _dt.now().weekday() < 5:
                trade_dates.append(today_str)
                trade_dates.sort()

        if not trade_dates:
            t['status'] = 'failed'
            t['error'] = '该日期范围无交易日'
            return

        t['total_dates'] = len(trade_dates)
        log(f'共 {len(trade_dates)} 个交易日: {", ".join(trade_dates)}')

        # 概念体系 + code->concepts 反向映射
        concept_map = _load_concept_members()
        if not concept_map:
            t['status'] = 'failed'
            t['error'] = '无概念成分股数据'
            return
        code_to_concepts = {}
        for concept, codes in concept_map.items():
            for c in codes:
                code_to_concepts.setdefault(c, set()).add(concept)

        days_result = []
        from datetime import datetime as _dt
        _today_str = _dt.now().strftime('%Y%m%d')
        for di, date_str in enumerate(trade_dates):
            # 检查取消
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return

            t['cur_idx'] = di + 1
            t['cur_date'] = date_str
            t['progress'] = int(di / max(len(trade_dates), 1) * 100)
            date_int = int(date_str)
            d_display = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'

            # 缓存命中: 非今天且已计算过且非强制刷新 -> 直接复用, 跳过重算
            if not force and date_str != _today_str and date_str in _concept_day_cache:
                cached = _concept_day_cache[date_str]
                days_result.append(cached)
                top_name = cached['top10'][0]['concept'] if cached['top10'] else '无'
                top_cnt = cached['top10'][0]['zt_count'] if cached['top10'] else 0
                log(f'⏩ [{di+1}/{len(trade_dates)}] {d_display}: 命中缓存, Top概念 {top_name}({top_cnt}只)')
                continue

            log(f'📍 [{di+1}/{len(trade_dates)}] 处理 {d_display} ...')

            # 用日K线判定涨停股(不依赖zt_stocks)
            log(f'   {d_display}: 判定涨停股(拉日K)...')
            zt_codes_map = _fetch_daily_zt_codes(date_int, concept_map,
                                                  cancel_check=lambda: t['_cancel'])
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return
            zt_codes = set(zt_codes_map.keys())
            if not zt_codes:
                log(f'   {d_display}: 无涨停股, 跳过')
                continue
            log(f'   {d_display}: 涨停{len(zt_codes)}只, 拉分时数据...')

            # 拉分时(按日期缓存)
            minute_map = _fetch_zt_minutes(zt_codes, date_int,
                                            cancel_check=lambda: t['_cancel'])
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return
            if not minute_map:
                log(f'   {d_display}: 无分时数据, 跳过')
                continue
            log(f'   {d_display}: 分时数据就绪 {len(minute_map)}/{len(zt_codes)}只')

            # 逐分钟统计封板
            from collections import defaultdict
            minute_concept_counts = defaultdict(lambda: defaultdict(int))
            final_counts = defaultdict(int)
            concept_zt_stocks = defaultdict(list)

            for code, prices in minute_map.items():
                if code not in zt_codes:
                    continue
                concepts = code_to_concepts.get(code, set())
                if not concepts:
                    continue
                max_p = max(prices)
                for i, p in enumerate(prices):
                    if abs(p - max_p) < 0.005:
                        for con in concepts:
                            minute_concept_counts[i][con] += 1
                # 收盘封板
                if abs(prices[-1] - max_p) < 0.005:
                    for con in concepts:
                        final_counts[con] += 1
                        concept_zt_stocks[con].append({
                            'code': code, 'name': zt_codes_map.get(code, '')
                        })

            if not minute_concept_counts:
                log(f'   {d_display}: 无封板数据, 跳过')
                continue

            # Top10 概念(按最终封板数)
            top10_sorted = sorted(final_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            top10_concepts = set(c for c, _ in top10_sorted)

            # 柱状图数据
            top10 = []
            for con, cnt in top10_sorted:
                if cnt == 0:
                    continue
                top10.append({
                    'concept': con,
                    'zt_count': cnt,
                    'zt_stocks': concept_zt_stocks[con][:20],
                })

            # 折线图 timeline (只保留 top10 概念, 只输出变化点)
            def min_idx_to_trade_min(idx):
                return idx + 5 if idx < 120 else idx + 6

            all_mins = sorted(minute_concept_counts.keys())
            timeline = []
            prev_counts = {}
            for m in all_mins:
                counts = {c: minute_concept_counts[m].get(c, 0) for c in top10_concepts if minute_concept_counts[m].get(c, 0) > 0}
                if counts != prev_counts:
                    tm = min_idx_to_trade_min(m)
                    timeline.append({
                        'ts': _time_to_label(tm),
                        'trade_min': tm,
                        'counts': counts,
                    })
                    prev_counts = counts

            day_result = {
                'date': date_str,
                'top10': top10,
                'timeline': timeline,
                'top10_ever': sorted(top10_concepts),
            }
            days_result.append(day_result)
            # 非今天的结果存入跨任务缓存并落盘(今天实时变化, 不缓存)
            if date_str != _today_str:
                _concept_day_cache[date_str] = day_result
                _save_concept_day_cache()
            log(f'   ✅ {d_display}: 涨停{len(zt_codes)}只, Top概念 {top10[0]["concept"] if top10 else "无"}({top10[0]["zt_count"] if top10 else 0}只)')

        t['progress'] = 100
        t['result'] = {
            'success': True,
            'start': start,
            'end': end,
            'days': days_result,
        }
        t['status'] = 'completed'
        log(f'🎉 全部完成: {len(days_result)}/{len(trade_dates)} 天有数据')

    except Exception as e:
        import traceback
        traceback.print_exc()
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'


def _build_missing_report(result):
    """
    扫描计算结果, 按"每日 -> 板块"列出当天在榜但缺涨幅/缺MA10的个股。
    返回: (missing_by_date, missing_codes)
      missing_by_date: [{date, blocks:[{block, stocks:[{code,name,missing:[...]}]}]}]
      missing_codes: 去重的缺失股票代码列表
    注意: 停牌日(suspended=True)不计为缺失——当日无交易, 无涨幅/MA10 是正常的。
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
                # 停牌日跳过: 当日无交易, 涨幅/MA10 缺失是正常的
                if cell.get('suspended'):
                    continue
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
                    # 构建 await_stocks 详情(带名称)供前端勾选移除
                    _name_map = {}
                    for _day in data.get('by_date', []):
                        for _b in _day.get('blocks', []):
                            for _s in _b.get('stocks', []):
                                _name_map.setdefault(_s['code'], _s.get('name', ''))
                    t['await_stocks'] = [{'code': f['code'],
                                          'name': _name_map.get(f['code'], ''),
                                          'reason': f['reason']} for f in fetch_failed]
                    prog('await',
                         f'行情获取完成，以下 {len(fetch_failed)} 只股票无法获取数据:\n'
                         + '\n'.join(f'  {l}' for l in lines)
                         + '\n可「再次同步」重试、「移除选中」剔除不再追踪，或「跳过」直接进入第三步',
                         None, None)
                    t['missing_report'] = []   # 此时还没做 remove, 用 await_stocks 代替
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
                if not missing_codes:
                    break
                # 兜底: fetch 全部成功但缓存里仍有 None (理论上不应再触发)
                n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
                # 构建 await_stocks 详情供前端勾选移除
                _await_map = {}
                for _day in missing_by_date:
                    for _b in _day['blocks']:
                        for _s in _b['stocks']:
                            if _s['code'] not in _await_map:
                                _await_map[_s['code']] = {'code': _s['code'], 'name': _s['name'],
                                                           'reason': '缺' + '、'.join(_s['missing'])}
                t['await_stocks'] = list(_await_map.values())
                t['missing_report'] = missing_by_date
                t['missing_codes'] = missing_codes
                prog('await', f'检测到 {len(missing_codes)} 只个股共 {n} 处缺数据(缺涨幅/MA10)。'
                              f'可「再次同步」补数据、「移除选中」剔除不再追踪，或「跳过」按现有数据执行', None, None)
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
            # 复用第二步数据, 只重跑移除逻辑部分(含用户手动剔除的票)
            _remove_codes = t.get('_remove_codes') or []
            data = ht.apply_removal_rules(data, progress=prog, manual_remove_codes=_remove_codes)
        final = data
        final['missing_report'] = missing_by_date
        final['missing_codes'] = missing_codes
        n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
        _removed = t.get('_remove_codes') or []
        if _removed:
            _removed_set = set(_removed)
            _still_missing = [c for c in missing_codes if c not in _removed_set]
            _msg = f'计算完成(已移除 {len(_removed)} 只不再追踪'
            if _still_missing:
                _msg += f', 另跳过 {len(_still_missing)} 只缺数据个股'
            _msg += ')'
            prog('done', _msg, 1, 1)
        elif missing_codes:
            prog('done', f'计算完成(跳过 {len(missing_codes)} 只缺数据个股, 共 {n} 处)', 1, 1)
        else:
            prog('done', '计算完成，数据完整', 1, 1)
        _dlog(f'[{task_id}] 计算完成 missing_codes={missing_codes}')
        t['result'] = final
        t['status'] = 'completed'
        t['progress'] = 100
        # 瘦身: 裁剪 by_date 里非当天 track(前端只用 s.track[当天]), 减小体积~95%
        _slim_by_date_tracks(final)
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
    """返回最近一次计算结果(服务端缓存, 刷新恢复用)。"""
    if not hot_last or not hot_last.get('result'):
        return jsonify({'success': True, 'has': False})
    return jsonify({
        'success': True, 'has': True,
        'start': hot_last.get('start'), 'end': hot_last.get('end'),
        'price': hot_last.get('price', True), 'saved_at': hot_last.get('saved_at'),
        'result': hot_last['result'],
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
        'missing_report': [], 'missing_codes': [], 'await_stocks': [],
        '_event': threading.Event(), '_action': None, '_skip': False,
        '_remove_codes': [],
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
        resp['await_stocks'] = t.get('await_stocks', [])
    elif t['status'] == 'completed':
        resp['result'] = t['result']
    elif t['status'] == 'failed':
        resp['error'] = t['error']
    return jsonify(resp)


@app.route('/api/hot/compute/resolve', methods=['POST'])
def api_hot_compute_resolve():
    _dlog(f'POST /api/hot/compute/resolve body={request.get_data(as_text=True)}')
    """
    响应缺失数据询问:
      action='resync'  -> 再次同步后重算
      action='skip'    -> 跳过缺失直接出结果
      action='remove'  -> 移除指定 codes(不再追踪)后直接出结果, 需带 codes 列表
    """
    data = request.get_json() or {}
    task_id = data.get('task_id')
    action = data.get('action')
    if task_id not in hot_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    if action not in ('resync', 'skip', 'remove'):
        return jsonify({'success': False, 'error': 'action 必须是 resync / skip / remove'}), 400
    t = hot_tasks[task_id]
    if t['status'] != 'awaiting':
        return jsonify({'success': False, 'error': '任务当前不在等待状态'}), 409
    if action == 'remove':
        codes = data.get('codes', [])
        if not codes or not isinstance(codes, list):
            return jsonify({'success': False, 'error': '移除操作需要提供 codes 列表'}), 400
        t['_remove_codes'] = codes
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
    failed_codes = []
    for c in need_fetch:
        try:
            if ht.fetch_range(c, date, date):
                success += 1
            else:
                failed_codes.append(c)
            time.sleep(0.5)
        except Exception as e:
            print(f"同步{c}失败: {e}")
            failed_codes.append(c)
    
    return jsonify({
        'success': True,
        'message': f'成功获取 {success}/{len(need_fetch)} 只股票数据',
        'fetched': success,
        'total': len(need_fetch),
        'failed_codes': failed_codes
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
            # 走统一入口 fetch_range (mootdx 远程 -> 本地通达信), 不再直调废弃的 akshare
            if ht.fetch_range(code, start, end):
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


# ============== 历史爆发股票汇总 ==============

def _bar_date8(b):
    """K线 bar 的 date('YYYY-MM-DD') -> 'YYYYMMDD'"""
    return str(b.get('date', ''))[:10].replace('-', '')


def _build_windows(days, size, step):
    """按交易日切窗口。days: 升序 YYYYMMDD 列表。
    返回 [(start8, end8, [d,...]), ...]，相邻窗口以上一窗最后一日为下一窗起点(overlap=size-step)。"""
    windows = []
    i = 0
    n = len(days)
    while i + size <= n:
        seg = days[i:i + size]
        windows.append((seg[0], seg[-1], seg))
        i += step
    return windows


def explosive_scan_task(task_id, month, size, step, threshold):
    """
    后台扫描：对某月按交易日切 size 天窗口(步进 step)，
    找出窗口内 N 日涨幅 > threshold% 的非ST个股(数据来自通达信tq)。
    N日涨幅 = (窗口最后一日收盘 - 窗口首日前一交易日收盘) / 前一交易日收盘 * 100
    """
    t = explosive_tasks[task_id]
    t['status'] = 'running'

    try:
        import tdx_source as ts
        if not ts.is_available():
            t['status'] = 'failed'
            t['error'] = '通达信tq接口不可用(请确认量化版客户端已开启并登录)'
            return

        month_start = month.replace('-', '') + '01'
        # 月末
        y, m = int(month[:4]), int(month[5:7])
        if m == 12:
            nm_y, nm = y + 1, 1
        else:
            nm_y, nm = y, m + 1
        from datetime import date as _date, timedelta as _td2
        month_end = (_date(nm_y, nm, 1) - _td2(days=1)).strftime('%Y%m%d')

        days = trading_days_in_range(month_start, month_end)
        if len(days) < size:
            t['status'] = 'failed'
            t['error'] = f'{month} 内交易日不足 {size} 天'
            return
        windows = _build_windows(days, size, step)
        t['windows_count'] = len(windows)

        today8 = datetime.now().strftime('%Y%m%d')
        # 取窗口首日前 ~10 个自然日作为回溯起点(拿到前一交易日收盘)
        scan_from = (datetime.strptime(month_start, '%Y%m%d') - timedelta(days=12)).strftime('%Y%m%d')
        need = len(trading_days_in_range(scan_from, today8)) + 8
        need = max(30, min(need, 1200))

        # 全市场A股(含名称), 过滤ST/退市/北交所
        lst = ts.stock_list('5', with_name=True) or []
        universe = []
        for it in lst:
            code = str(it.get('code', '')).strip()
            name = str(it.get('name', '') or '').strip()
            if not re.match(r'^\d{6}$', code):
                continue
            if code.startswith(('4', '8')):  # 跳过北交所
                continue
            up = name.upper()
            if 'ST' in up or '退' in name:  # 跳过ST/退市
                continue
            universe.append((code, name))

        total = len(universe)
        t['total'] = total
        candidates = []

        # 批量读本地 .day 日线文件(全市场~2秒), 替代逐只 ts.kline(~18分钟)
        all_codes = [c for c, _ in universe]
        day_closes = _read_day_closes_batch(all_codes)
        t['day_loaded'] = len(day_closes)
        log_msg = f'本地日线加载 {len(day_closes)}/{len(all_codes)} 只'
        # 统计本地缺失的股票(后续逐只回退tqcenter)
        missing_codes = [c for c in all_codes if c not in day_closes]

        for i, (code, name) in enumerate(universe):
            if t.get('cancel_requested'):
                break
            t['processed'] = i
            t['progress'] = int(i / max(total, 1) * 100)
            try:
                closes = day_closes.get(code)
                order = None
                if not closes:
                    # 本地无该股.day文件, 回退 tqcenter kline
                    bars = ts.kline(code, count=need, period='1d', dividend_type='none')
                    if not bars or len(bars) < size + 1:
                        continue
                    closes = {}
                    for b in bars:
                        d8 = _bar_date8(b)
                        c = float(b.get('close', 0) or 0)
                        if d8 and c > 0:
                            closes[d8] = c
                if not closes or len(closes) < size + 1:
                    continue
                order = sorted(closes.keys())
                for (w_start, w_end, seg) in windows:
                    if w_start not in closes or w_end not in closes:
                        continue
                    # 窗口首日的前一交易日
                    try:
                        pos = order.index(w_start)
                    except ValueError:
                        continue
                    if pos == 0:
                        continue
                    prev_close = closes[order[pos - 1]]
                    if prev_close <= 0:
                        continue
                    gain = (closes[w_end] - prev_close) / prev_close * 100
                    if gain >= threshold:
                        candidates.append({
                            'range_start': w_start,
                            'range_end': w_end,
                            'range_label': f'{w_start[4:6]}/{w_start[6:8]}~{w_end[4:6]}/{w_end[6:8]}',
                            'code': code,
                            'name': name,
                            'gain': round(gain, 2),
                        })
            except Exception:
                continue

        # 按窗口起始 + 涨幅降序
        candidates.sort(key=lambda x: (x['range_start'], -x['gain']))
        t['processed'] = total
        t['progress'] = 100
        t['candidates'] = candidates
        t['status'] = 'cancelled' if t.get('cancel_requested') else 'completed'
    except Exception as e:
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'


@app.route('/api/hot/explosive/scan', methods=['POST'])
def api_explosive_scan():
    """启动历史爆发股票扫描(异步)。参数: month(YYYY-MM), window(默认3), step(默认2), threshold(默认18)"""
    data = request.get_json() or {}
    month = str(data.get('month', '')).strip()
    if not re.match(r'^\d{4}-\d{2}$', month):
        return jsonify({'success': False, 'error': 'month 需为 YYYY-MM 格式'}), 400
    try:
        size = int(data.get('window', 3))
        step = int(data.get('step', size - 1 if size > 1 else 1))
        threshold = float(data.get('threshold', 18))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '参数格式错误'}), 400
    size = max(2, min(size, 10))
    step = max(1, min(step, size))

    import uuid
    task_id = str(uuid.uuid4())[:8]
    explosive_tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'processed': 0, 'total': 0,
        'month': month, 'window': size, 'step': step, 'threshold': threshold,
        'candidates': [],
    }
    threading.Thread(target=explosive_scan_task,
                     args=(task_id, month, size, step, threshold), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/explosive/status/{task_id}'})


@app.route('/api/hot/explosive/status/<task_id>', methods=['GET'])
def api_explosive_status(task_id):
    """查询扫描任务状态/结果"""
    if task_id not in explosive_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = explosive_tasks[task_id]
    return jsonify({
        'success': True, 'task_id': task_id,
        'status': t['status'], 'progress': t.get('progress', 0),
        'processed': t.get('processed', 0), 'total': t.get('total', 0),
        'threshold': t.get('threshold'), 'window': t.get('window'),
        'error': t.get('error'),
        'candidates': t.get('candidates', []),
    })


@app.route('/api/hot/explosive/cancel/<task_id>', methods=['POST'])
def api_explosive_cancel(task_id):
    """请求中断扫描任务"""
    if task_id not in explosive_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    explosive_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


@app.route('/api/hot/explosive/kline', methods=['GET'])
def api_explosive_kline():
    """个股日K线, 截止到 end(YYYYMMDD) 当日, 返回最近 count 根(默认250≈1年)。数据来自通达信tq。"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    end = request.args.get('end', '')
    if not re.match(r'^\d{8}$', end):
        return jsonify({'success': False, 'error': 'end 需为 YYYYMMDD'}), 400
    disp = request.args.get('count', default=250, type=int)
    disp = max(60, min(disp, 500))
    # 复权方式: 默认 front(前复权, 主流看盘软件默认, K线形态连续); none=不复权(可走本地.day更快)
    fq = (request.args.get('fq', 'front') or 'front').lower()
    if fq not in ('front', 'back', 'none'):
        fq = 'front'
    today8 = datetime.now().strftime('%Y%m%d')
    need = len(trading_days_in_range(end, today8)) + disp + 10
    need = max(disp + 10, min(need, 1400))
    try:
        import tdx_source as ts
        if fq == 'none':
            # 不复权: 优先读本地 .day 文件(快, 无需客户端在线), 读不到再回退 tqcenter
            bars, source = ts.kline_daily(code, count=need, prefer_local=True)
        else:
            # 前/后复权: 只能走 tqcenter(本地 .day 仅存不复权原始数据)
            if not ts.is_available():
                return jsonify({'success': False, 'error': '前复权需通达信tq接口, 当前不可用'}), 503
            bars = ts.kline(code, count=need, period='1d', dividend_type=fq)
            source = 'tqcenter'
        if not bars:
            if not ts.is_available():
                return jsonify({'success': False, 'error': '无K线数据且通达信tq接口不可用'}), 503
            return jsonify({'success': False, 'error': f'{code} 无K线数据'}), 404
        # 截止到 end 当日
        bars = [b for b in bars if _bar_date8(b) <= end]
        if not bars:
            return jsonify({'success': False, 'error': f'{code} 无 {end} 前的K线数据'}), 404
        bars = bars[-disp:]
        for i in range(len(bars)):
            bars[i]['last_close'] = bars[i - 1]['close'] if i > 0 else bars[i]['open']
        # source: local_day=本地通达信.day / tqcenter=通达信量化接口; fq: 复权方式
        return jsonify({'success': True, 'code': code, 'end': end, 'bars': bars, 'source': source, 'fq': fq})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


# ============== 复盘: 每日概念热力(今天在炒啥) ==============

# ===== 概念成员每日收盘缓存(供"当日在涨谁/在杀谁"统计) =====
_MEMBER_CLOSES_FILE = '.member_closes.json'
_member_closes = {}                 # {code: {YYYYMMDD: close}}
_member_closes_lock = threading.Lock()
_daymap_build_tasks = {}            # {task_id: {...}}
_daymap_members_cache = {'ts': 0, 'data': None}


def _load_member_closes():
    global _member_closes
    try:
        if os.path.exists(_MEMBER_CLOSES_FILE):
            with open(_MEMBER_CLOSES_FILE, 'r', encoding='utf-8') as f:
                _member_closes = json.load(f) or {}
            print(f'[daymap] 已载入成员收盘缓存 {len(_member_closes)} 只')
    except Exception as e:
        print(f'[daymap] 载入收盘缓存失败: {e}')
        _member_closes = {}


def _save_member_closes():
    try:
        with _member_closes_lock:
            with open(_MEMBER_CLOSES_FILE, 'w', encoding='utf-8') as f:
                json.dump(_member_closes, f, ensure_ascii=False)
    except Exception as e:
        print(f'[daymap] 保存收盘缓存失败: {e}')


_load_member_closes()


def _member_pct(code, date, prev):
    """从收盘缓存算某日涨跌幅%(相对前一交易日)。缺数据返回 None。"""
    m = _member_closes.get(code)
    if not m:
        return None
    c = m.get(date)
    p = m.get(prev)
    if c and p and p > 0:
        return (c - p) / p * 100
    return None


def _get_daymap_members(cur):
    """全部产业链概念的成员映射 {concept: set(code)}, 1小时缓存。"""
    import time as _t
    import concept_chain as _cc
    if _daymap_members_cache['data'] and _t.time() - _daymap_members_cache['ts'] < 3600:
        return _daymap_members_cache['data']
    concepts = list(set(_cc.meaningful_concepts()))
    cur.execute("SELECT concept, stock_code FROM ths.concept_member WHERE concept = ANY(%s)", (concepts,))
    d = {}
    for con, code in cur.fetchall():
        d.setdefault(con, set()).add(code)
    _daymap_members_cache['data'] = d
    _daymap_members_cache['ts'] = _t.time()
    return d


def _daymap_build_task(task_id):
    """拉取全部产业链概念成员的日线收盘, 写入持久缓存(供当日涨/杀统计)。"""
    import concept_chain as _cc
    import db as _db
    import tdx_source as _ts
    t = _daymap_build_tasks[task_id]
    t['status'] = 'running'
    try:
        concepts = list(set(_cc.meaningful_concepts()))
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM ths.concept_member WHERE concept = ANY(%s)", (concepts,))
            codes = sorted({r[0] for r in cur.fetchall()})
        conn.close()
        if not _ts.is_available():
            t['status'] = 'failed'
            t['error'] = '通达信tq接口不可用(请确认量化版客户端已开启并登录)'
            return
        t['total'] = len(codes)
        done = 0
        for code in codes:
            if t.get('_cancel'):
                t['status'] = 'cancelled'
                _save_member_closes()
                return
            bars = _ts.kline(code, count=260, period='1d', dividend_type='none')
            if bars:
                m = _member_closes.setdefault(code, {})
                for b in bars:
                    m[b['date'].replace('-', '')] = b['close']
            done += 1
            t['done'] = done
            t['progress'] = int(done / max(len(codes), 1) * 100)
            if done % 500 == 0:
                _save_member_closes()
        _save_member_closes()
        t['status'] = 'completed'
    except Exception as e:
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'


@app.route('/api/hot/daymap/build', methods=['POST'])
def api_daymap_build():
    """构建/刷新 成员日收盘缓存(供当日涨/杀统计)。首次较慢(拉全部成员日线)。"""
    import uuid
    for tid, t in _daymap_build_tasks.items():
        if t['status'] in ('pending', 'running'):
            return jsonify({'success': True, 'task_id': tid, 'reused': True})
    task_id = str(uuid.uuid4())[:8]
    _daymap_build_tasks[task_id] = {'status': 'pending', 'progress': 0, 'done': 0,
                                    'total': 0, 'error': None, '_cancel': False}
    threading.Thread(target=_daymap_build_task, args=(task_id,), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/hot/daymap/build/status/<task_id>', methods=['GET'])
def api_daymap_build_status(task_id):
    t = _daymap_build_tasks.get(task_id)
    if not t:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    return jsonify({'success': True, 'status': t['status'], 'progress': t['progress'],
                    'done': t.get('done', 0), 'total': t.get('total', 0), 'error': t.get('error')})


def _parse_board(s):
    """从连板字段解析当前连板数(如 '3板'/'2/3天2板'->取末尾N板)。复盘收录默认至少首板=1。"""
    if not s:
        return 1
    m = re.findall(r'(\d+)\s*板', str(s))
    if m:
        return int(m[-1])
    m2 = re.match(r'^\s*(\d+)\s*$', str(s))
    return int(m2.group(1)) if m2 else 1


@app.route('/api/hot/ladder-concepts', methods=['GET'])
def api_ladder_concepts():
    """板块梯队视图的概念下拉: 返回产业链概念 + 累计复盘活跃度, 按活跃度降序。"""
    import db as _db
    import concept_chain as _cc
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT cm.concept, count(*) FROM zt_stocks zs "
                        "JOIN ths.concept_member cm ON cm.stock_code=zs.code GROUP BY cm.concept")
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': f'{e}'}), 503
    out = []
    for con, n in rows:
        ci = _cc.concept_to_chain(con)
        if not ci or _cc.is_blocked(con):
            continue
        out.append({'concept': con, 'chain': ci.get('chain_name', ''), 'total': n})
    out.sort(key=lambda x: -x['total'])
    return jsonify({'success': True, 'concepts': out})


@app.route('/api/hot/sector-ladder', methods=['GET'])
def api_sector_ladder():
    """板块内部梯队/龙头健康。两套口径都返回:
    emotion(情绪梯队): 每日 龙头高度 + 高位(≥3板)/中位(2板)/低位(首板) 涨停数 + 结构标签。
    trend(趋势主升): 每日 创60日新高数 / 沿均线主升数(收盘>MA20且MA5>MA20) / MA20上方数 + 龙头是否新高 + 标签。
    参数: concept(必填), start,end(YYYYMMDD, 缺省=最近30交易日)。"""
    import db as _db
    from collections import defaultdict
    concept = request.args.get('concept', '').strip()
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not concept:
        return jsonify({'success': False, 'error': '需要 concept 参数'}), 400
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            if not re.match(r'^\d{8}$', end):
                end = all_dates[-1] if all_dates else datetime.now().strftime('%Y%m%d')
            if not re.match(r'^\d{8}$', start):
                idx = all_dates.index(end) if end in all_dates else len(all_dates) - 1
                start = all_dates[max(0, idx - 29)] if all_dates else end
            dates = [d for d in all_dates if start <= d <= end]
            # 概念成员
            cur.execute("SELECT stock_code FROM ths.concept_member WHERE concept=%s", (concept,))
            members = [r[0] for r in cur.fetchall()]
            # 该概念成员在区间内的复盘连板记录
            sd = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
            ed = f'{end[:4]}-{end[4:6]}-{end[6:8]}'
            cur.execute(
                "SELECT to_char(zs.trade_date,'YYYYMMDD') d, zs.code, zs.lianban FROM zt_stocks zs "
                "JOIN ths.concept_member cm ON cm.stock_code=zs.code "
                "WHERE cm.concept=%s AND zs.trade_date>=%s AND zs.trade_date<=%s", (concept, sd, ed))
            zt = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500

    # ---- 情绪梯队 ----
    day_boards = defaultdict(list)
    for d, code, lb in zt:
        day_boards[d].append(_parse_board(lb))
    emotion = []
    prev_top = 0
    for d in dates:
        b = day_boards.get(d, [])
        tot = len(b)
        top = max(b) if b else 0
        hi = sum(1 for x in b if x >= 3)
        mid = sum(1 for x in b if x == 2)
        lo = sum(1 for x in b if x <= 1)
        if tot == 0:
            tag = ''
        elif top >= 4 and hi >= 2:
            tag = '强·高位梯队在走'
        elif top > prev_top and top >= 3:
            tag = '转强·龙头晋级'
        elif hi == 0 and lo >= tot * 0.7:
            tag = '弱·全低位补涨'
        elif prev_top and top < prev_top:
            tag = '退·龙头断板'
        else:
            tag = ''
        emotion.append({'date': d, 'total': tot, 'top': top, 'hi': hi, 'mid': mid, 'lo': lo, 'tag': tag})
        if tot:
            prev_top = top

    # ---- 趋势主升(需成员日收盘缓存) ----
    trend_ready = len(_member_closes) > 0
    trend = []
    if trend_ready:
        # 预排每个成员的收盘日期序列
        mdates = {}
        for c in members:
            mc = _member_closes.get(c)
            if mc:
                mdates[c] = sorted(mc.keys())
        prev_nh = 0
        for d in dates:
            newhigh = rising = above = quoted = 0
            biases = []
            leader_ratio = 0.0
            for c in members:
                mc = _member_closes.get(c)
                dl = mdates.get(c)
                if not mc or not dl or d not in mc:
                    continue
                i = dl.index(d) if d in dl else -1
                if i < 5:
                    continue
                win = dl[max(0, i - 59):i + 1]
                closes = [mc[x] for x in win]
                close = mc[d]
                if close <= 0:
                    continue
                quoted += 1
                hi60 = max(closes)
                ma20 = sum(closes[-20:]) / len(closes[-20:])
                ma5 = sum(closes[-5:]) / len(closes[-5:])
                if close >= hi60 * 0.999:
                    newhigh += 1
                if close > ma20:
                    above += 1
                    if ma5 > ma20:
                        rising += 1
                        biases.append((close - ma20) / ma20 * 100)
                leader_ratio = max(leader_ratio, close / hi60 if hi60 > 0 else 0)
            avg_bias = round(sum(biases) / len(biases), 1) if biases else 0
            leader_nh = leader_ratio >= 0.999
            if quoted == 0:
                tag = ''
            elif leader_nh and newhigh >= prev_nh and newhigh >= 2:
                tag = '强·主升续创新高'
            elif newhigh > 0 and newhigh >= prev_nh:
                tag = '转强·新高增多'
            elif newhigh == 0 and rising > 0:
                tag = '弱·无新高(滞涨)'
            elif prev_nh and newhigh < prev_nh:
                tag = '退·新高减少'
            else:
                tag = ''
            trend.append({'date': d, 'members': quoted, 'newhigh': newhigh,
                          'rising': rising, 'above': above, 'avg_bias': avg_bias,
                          'leader_nh': leader_nh, 'tag': tag})
            if quoted:
                prev_nh = newhigh

    return jsonify({'success': True, 'concept': concept, 'start': start, 'end': end,
                    'all_dates': all_dates, 'dates': dates, 'members': len(members),
                    'emotion': emotion, 'trend': trend, 'trend_ready': trend_ready})


# ---- 新高追踪: 每日创N日新高的个股, 按同花顺板块分类 ----
# 全量计算缓存: {n: (ts, {all_dates, days_by_date: {date: {...}}})}
# .day文件一天才变一次, 一次全量算完所有日期, 切换日期范围只在内存中切片
_newhigh_full_cache = {}  # {n: (ts, full_result)}


def _compute_newhigh_full(n):
    """全量计算所有已入库交易日创N日新高的个股, 按概念分类。
    返回 {all_dates: [...], days: {date_str: {date,total,sectors}}}。
    .day文件不变则结果不变, 缓存1小时。"""
    import time as _time
    cached = _newhigh_full_cache.get(n)
    if cached and (_time.time() - cached[0] < 3600):
        return cached[1]

    from collections import defaultdict
    import db as _db
    from datetime import datetime as _dt

    # 已入库交易日列表
    try:
        all_dates = sorted(_db.get_submitted_dates())
    except Exception:
        all_dates = []
    today_str = _dt.now().strftime('%Y%m%d')
    if _dt.now().weekday() < 5 and today_str not in all_dates:
        all_dates.append(today_str)
        all_dates.sort()
    if not all_dates:
        return {'all_dates': [], 'days': {}}

    # 概念成员
    cm = _load_concept_members()
    if not cm:
        return {'all_dates': all_dates, 'days': {}}
    code_concepts = defaultdict(list)
    all_codes = set()
    for concept, codes in cm.items():
        for c in codes:
            code_concepts[c].append(concept)
            all_codes.add(c)
    all_codes = sorted(all_codes)

    # 批量读 .day 收盘价
    closes_map = _read_day_closes_batch(all_codes)
    if not closes_map:
        return {'all_dates': all_dates, 'days': {}}

    # 股票名称
    names = {}
    try:
        import tdx_source as _ts
        lst = _ts.stock_list('5', with_name=True) or []
        names = {x['code']: x.get('name', '') for x in lst}
    except Exception:
        pass

    # 预排每只股票的有序日期列表(加速索引)
    stock_dates = {}
    for c in all_codes:
        mc = closes_map.get(c)
        if mc and len(mc) > n:
            stock_dates[c] = sorted(mc.keys())

    # 全量计算: 每个交易日
    FWD_DAYS = 10  # 创新高后追踪10个交易日
    days_by_date = {}
    for d in all_dates:
        nh_stocks = {}
        for c in all_codes:
            mc = closes_map.get(c)
            dl = stock_dates.get(c)
            if not mc or not dl or d not in mc:
                continue
            i = dl.index(d)
            if i < n:
                continue
            close = mc[d]
            if close <= 0:
                continue
            win = dl[i - n:i]
            prev_hi = max(mc[x] for x in win)
            if close >= prev_hi * 0.999:
                prev_close = mc.get(dl[i - 1], 0)
                pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                # 创新高后FWD_DAYS日累计涨幅
                fwd = []
                for k in range(1, FWD_DAYS + 1):
                    if i + k < len(dl):
                        c_k = mc[dl[i + k]]
                        fwd.append(round((c_k / close - 1) * 100, 2))
                    else:
                        fwd.append(None)  # 数据不足
                nh_stocks[c] = {'code': c, 'name': names.get(c, c),
                                'close': round(close, 2), 'pct': pct, 'fwd': fwd}
        if not nh_stocks:
            days_by_date[d] = {'date': d, 'total': 0, 'sectors': [], 'fwd_avg': [None] * FWD_DAYS}
            continue
        sector_stocks = defaultdict(list)
        for c, info in nh_stocks.items():
            for con in code_concepts.get(c, []):
                sector_stocks[con].append(info)

        def _avg_fwd(stocks):
            """计算一批股票的平均前向涨幅, 返回[10]数组(None表示无数据)"""
            out = []
            for k in range(FWD_DAYS):
                vals = [s['fwd'][k] for s in stocks if s['fwd'][k] is not None]
                out.append(round(sum(vals) / len(vals), 2) if vals else None)
            return out

        sectors = []
        for con, stocks in sector_stocks.items():
            stocks.sort(key=lambda x: -x['pct'])
            sectors.append({'concept': con, 'count': len(stocks),
                            'stocks': stocks, 'fwd_avg': _avg_fwd(stocks)})
        sectors.sort(key=lambda x: -x['count'])
        # 全日平均前向涨幅(所有创新高个股)
        all_fwd = _avg_fwd(list(nh_stocks.values()))
        days_by_date[d] = {'date': d, 'total': len(nh_stocks), 'sectors': sectors, 'fwd_avg': all_fwd}

    full = {'all_dates': all_dates, 'days': days_by_date, 'closes_map': closes_map,
            'stock_dates': stock_dates}
    _newhigh_full_cache[n] = (_time.time(), full)
    return full


@app.route('/api/hot/newhigh', methods=['GET'])
def api_hot_newhigh():
    """每日创N日收盘新高的个股, 按同花顺概念板块分类。
    参数: start,end(YYYYMMDD), n(60/120/200, 默认60), force=1强制重算。
    全量计算按n缓存1小时(.day文件一天才变), 切换日期范围只在内存中切片, 瞬间返回。"""
    import time as _time
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    try:
        n = int(request.args.get('n', '60'))
    except ValueError:
        n = 60
    if n not in (60, 120, 200):
        n = 60
    force = request.args.get('force', '') == '1'
    if force:
        _newhigh_full_cache.pop(n, None)

    full = _compute_newhigh_full(n)
    all_dates = full['all_dates']
    days_by_date = full['days']
    if not all_dates:
        return jsonify({'success': False, 'error': '无已入库交易日'})

    # 默认日期范围: 最近30天
    if not re.match(r'^\d{8}$', end):
        end = all_dates[-1]
    if not re.match(r'^\d{8}$', start):
        start = all_dates[max(0, len(all_dates) - 30)]
    dates = [d for d in all_dates if start <= d <= end]
    if not dates:
        return jsonify({'success': False, 'error': '区间内无已入库交易日'})

    # 切片: 从全量缓存中取出区间内的天数
    days = [days_by_date[d] for d in dates if d in days_by_date]
    # 给每只stock补算 range_pct: 范围起始日到结束日的累计涨幅
    # 同时计算每只股票K线窗口(250根截止end_date)的最大涨跌幅, 用于统一比例尺
    closes_map = full.get('closes_map', {})
    stock_dates_map = full.get('stock_dates', {})
    start_date = dates[0]
    end_date = dates[-1]
    import bisect
    KLINE_COUNT = 250
    seen_codes = set()
    for day in days:
        for sec in day.get('sectors', []):
            for s in sec.get('stocks', []):
                code = s['code']
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                mc = closes_map.get(code)
                dl = stock_dates_map.get(code)
                if mc and dl:
                    # 找到 >= start_date 和 <= end_date 的最近日期
                    cs = mc.get(start_date)
                    ce = mc.get(end_date)
                    if not cs:
                        for dd in dl:
                            if dd >= start_date:
                                cs = mc.get(dd)
                                break
                    if not ce:
                        for dd in reversed(dl):
                            if dd <= end_date:
                                ce = mc.get(dd)
                                break
                    if cs and ce and cs > 0:
                        s['range_pct'] = round((ce / cs - 1) * 100, 2)
                    else:
                        s['range_pct'] = None
                    # K线窗口(250根截止end_date)的最大涨跌幅, 用于统一比例尺
                    ei = bisect.bisect_right(dl, end_date) - 1
                    if ei >= 0:
                        si = max(0, ei - KLINE_COUNT + 1)
                        base = mc[dl[si]]
                        if base > 0:
                            win_closes = [mc[dl[j]] for j in range(si, ei + 1)]
                            s['kline_max_pct'] = round((max(win_closes) / base - 1) * 100, 2)
                            s['kline_min_pct'] = round((min(win_closes) / base - 1) * 100, 2)
                        else:
                            s['kline_max_pct'] = 0
                            s['kline_min_pct'] = 0
                    else:
                        s['kline_max_pct'] = 0
                        s['kline_min_pct'] = 0
                else:
                    s['range_pct'] = None
                    s['kline_max_pct'] = 0
                    s['kline_min_pct'] = 0
    result = {'success': True, 'n': n, 'start': start, 'end': end,
              'dates': dates, 'days': days}
    return jsonify(result)


@app.route('/api/hot/concept-daymap', methods=['GET'])
def api_concept_daymap():
    """某一交易日的产业链地图: 按 产业链 -> 上下游层级 -> 概念。
    每个概念给出: 复盘涨停数(zt) + 当日涨(>5%)数(up) + 杀(<-5%,含跌停)数(down)。
    涨/杀 来自成员日收盘缓存(需先 /api/hot/daymap/build 构建, 未构建则只有复盘涨停)。
    参数: date(YYYYMMDD, 缺省=最新交易日)。"""
    import db as _db
    import concept_chain as _cc
    from collections import defaultdict
    date = request.args.get('date', '')
    try:
        conn = _db.get_conn()
    except Exception as e:
        return jsonify({'success': False, 'error': f'数据库不可用: {e}'}), 503
    try:
        with conn.cursor() as cur:
            if not re.match(r'^\d{8}$', date):
                cur.execute("SELECT to_char(max(trade_date),'YYYYMMDD') FROM zt_stocks")
                r = cur.fetchone()
                date = (r and r[0]) or datetime.now().strftime('%Y%m%d')
            dd = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
            cur.execute(
                "SELECT zs.code, cm.concept FROM zt_stocks zs "
                "JOIN ths.concept_member cm ON cm.stock_code = zs.code "
                "WHERE zs.trade_date = %s", (dd,))
            zt_rows = cur.fetchall()
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            con_members = _get_daymap_members(cur)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        conn.close()

    # 复盘涨停: 概念 -> 个股集合
    concept_zt = defaultdict(set)
    for code, con in zt_rows:
        ci = _cc.concept_to_chain(con)
        if not ci or _cc.is_blocked(con):
            continue
        concept_zt[con].add(code)

    # 前一交易日(用于算涨跌幅)
    prev = None
    if date in all_dates:
        i = all_dates.index(date)
        if i > 0:
            prev = all_dates[i - 1]
    price_ready = bool(prev) and len(_member_closes) > 0

    def con_sets(con):
        """返回 4 类互斥的成员集合: lu涨停 / up5(5%~涨停) / dn5(-5%~跌停) / ld跌停。"""
        S = {'lu': set(), 'up5': set(), 'dn5': set(), 'ld': set()}
        if price_ready:
            for c in con_members.get(con, ()):
                pct = _member_pct(c, date, prev)
                if pct is None:
                    continue
                thr = _limit_threshold(c)
                if pct >= thr:
                    S['lu'].add(c)
                elif pct > 5:
                    S['up5'].add(c)
                if pct <= -thr:
                    S['ld'].add(c)
                elif pct < -5:
                    S['dn5'].add(c)
        return S

    info = _cc.chain_info()
    all_chains = [{'id': cid, 'name': c['name']} for cid, c in info.items()]
    chains = []
    for cid, c in info.items():
        agg = {'zt': set(), 'lu': set(), 'up5': set(), 'dn5': set(), 'ld': set()}
        n_tiers = len(c['tiers'])
        tiers = []
        for i, t in enumerate(c['tiers']):
            pos = ('中游' if n_tiers == 1 else
                   '上游' if i == 0 else '下游' if i == n_tiers - 1 else '中游')
            cons = []
            for con in t['concepts']:
                zt = concept_zt.get(con, set())
                S = con_sets(con)
                active = len(zt) + len(S['lu']) + len(S['up5']) + len(S['dn5']) + len(S['ld'])
                if active == 0:
                    continue
                agg['zt'] |= zt
                for k in ('lu', 'up5', 'dn5', 'ld'):
                    agg[k] |= S[k]
                cons.append({'concept': con, 'zt': len(zt),
                             'lu': len(S['lu']), 'up5': len(S['up5']),
                             'dn5': len(S['dn5']), 'ld': len(S['ld'])})
            cons.sort(key=lambda x: -(x['lu'] * 100 + x['up5'] * 10 + x['zt']))
            if cons:
                tiers.append({'tier': t['tier'], 'pos': pos, 'concepts': cons})
        tot = sum(len(agg[k]) for k in ('lu', 'up5', 'dn5', 'ld')) + len(agg['zt'])
        if tot > 0:
            chains.append({'id': cid, 'name': c['name'],
                           'zt': len(agg['zt']), 'lu': len(agg['lu']), 'up5': len(agg['up5']),
                           'dn5': len(agg['dn5']), 'ld': len(agg['ld']),
                           'total': tot, 'tiers': tiers})
    chains.sort(key=lambda x: -x['total'])
    return jsonify({'success': True, 'date': date, 'prev': prev, 'price_ready': price_ready,
                    'all_dates': all_dates, 'all_chains': all_chains, 'chains': chains})


@app.route('/api/hot/breadth-scan', methods=['GET'])
def api_breadth_scan():
    """板块广度起爆信号扫描: 在日期区间内, 逐日算每个产业链概念的广度
    (上涨占比、涨>5%占比、涨停数、平均涨幅), 标记"起爆日"(广度突然爆发=资金整体扫货)。
    起爆判定(默认): 有效成员≥MIN, 上涨占比≥UP, 涨>5%占比≥UP5。
    需先构建成员日收盘缓存(/api/hot/daymap/build)。
    参数: start,end(YYYYMMDD, 缺省=最近30交易日); up(默认0.85); up5(默认0.25); min(默认15)。"""
    import db as _db
    import concept_chain as _cc
    if len(_member_closes) == 0:
        return jsonify({'success': False, 'needs_build': True,
                        'error': '未构建成员日收盘缓存, 请先在「每日热力」点「计算涨/杀」构建'}), 400
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    try:
        UP = float(request.args.get('up', 0.85))
        UP5 = float(request.args.get('up5', 0.25))
        MIN = int(request.args.get('min', 15))
        MINLU = int(request.args.get('minlu', 5))
    except Exception:
        UP, UP5, MIN, MINLU = 0.85, 0.25, 15, 5
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            if not re.match(r'^\d{8}$', end):
                end = all_dates[-1] if all_dates else datetime.now().strftime('%Y%m%d')
            if not re.match(r'^\d{8}$', start):
                idx = all_dates.index(end) if end in all_dates else len(all_dates) - 1
                start = all_dates[max(0, idx - 29)] if all_dates else end
            con_members = _get_daymap_members(cur)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        try: conn.close()
        except Exception: pass

    dates = [d for d in all_dates if start <= d <= end]
    prev_of = {}
    for i, d in enumerate(all_dates):
        prev_of[d] = all_dates[i - 1] if i > 0 else None

    events = []           # 起爆事件(concept-day)
    first_ignite = {}     # concept -> 区间内首个起爆日
    for con, mem in con_members.items():
        if _cc.is_blocked(con):
            continue
        ci = _cc.concept_to_chain(con)
        chain = ci.get('chain_name', '') if ci else ''
        mem = list(mem)
        if len(mem) < MIN:
            continue
        for d in dates:
            p = prev_of.get(d)
            if not p:
                continue
            quoted = up = up5 = lu = 0
            ssum = 0.0
            for c in mem:
                pct = _member_pct(c, d, p)
                if pct is None:
                    continue
                quoted += 1
                ssum += pct
                if pct > 0:
                    up += 1
                if pct >= 5:
                    up5 += 1
                if pct >= _limit_threshold(c):
                    lu += 1
            if quoted < MIN:
                continue
            up_ratio = up / quoted
            up5_ratio = up5 / quoted
            avg = ssum / quoted
            ignited = up_ratio >= UP and up5_ratio >= UP5 and lu >= MINLU
            if ignited:
                score = round(up5_ratio * 100 + lu * 2 + avg, 1)
                is_first = con not in first_ignite
                if is_first:
                    first_ignite[con] = d
                events.append({
                    'concept': con, 'chain': chain, 'date': d,
                    'quoted': quoted, 'up': up, 'up5': up5, 'lu': lu,
                    'up_ratio': round(up_ratio * 100, 1), 'up5_ratio': round(up5_ratio * 100, 1),
                    'avg': round(avg, 2), 'score': score, 'first': is_first,
                })
    # 排序: 先日期升序(时间线), 同日按强度降序
    events.sort(key=lambda e: (e['date'], -e['score']))
    return jsonify({'success': True, 'start': start, 'end': end, 'all_dates': all_dates,
                    'threshold': {'up': UP, 'up5': UP5, 'min': MIN, 'minlu': MINLU},
                    'count': len(events), 'events': events})


# ============== 盯盘: 产业链实时盯盘 ==============

_chain_board_cache = {'ts': 0, 'data': None}
_chain_members_cache = {'ts': 0, 'members': None}
_astock_name_cache = {'ts': 0, 'names': {}}


def _limit_threshold(code):
    """涨停判定阈值: 创业板(30x)/科创板(68x)=19.8, 其它主板=9.8。
    口径与zt-stats的 code.startswith(('30','68')) 一致, 保证产业链与涨停分布统计可比。"""
    return 19.8 if code.startswith(('30', '68')) else 9.8


@app.route('/api/monitor/chain-board', methods=['GET'])
def api_chain_board():
    """产业链实时盯盘: 按 产业链->上下游层级->概念 聚合成员股实时涨幅/涨停数/领涨股。
    数据来源: ths.concept_member(成员) + 通达信实时批量报价。结果缓存 15 秒。"""
    import time as _t
    import concept_chain as _cc
    import db as _db
    import tdx_source as _ts
    now = _t.time()
    if _chain_board_cache['data'] and now - _chain_board_cache['ts'] < 15:
        return jsonify(_chain_board_cache['data'])

    ci = _cc.chain_info()
    comp_chains = _cc.company_chains()  # {公司链名: [概念,...]}
    concept_set = set()
    for c in ci.values():
        concept_set.update(c['concepts'])
    for cons in comp_chains.values():
        concept_set.update(cons)

    if not _ts.is_available():
        return jsonify({'success': False, 'error': '通达信tq接口不可用(请确认量化版客户端已开启并登录)'}), 503

    # 名称表(缓存1小时) - 先取, 供下方成员ST过滤使用(口径与zt-stats一致)
    if not _astock_name_cache['names'] or now - _astock_name_cache['ts'] > 3600:
        lst = _ts.stock_list('5', with_name=True) or []
        _astock_name_cache['names'] = {x['code']: x['name'] for x in lst}
        _astock_name_cache['ts'] = now
    names = _astock_name_cache['names']
    # ST/退市代码集合(名称含ST或退), 与zt-stats排除口径一致
    excluded_codes = {
        str(code).zfill(6) for code, nm in names.items()
        if 'ST' in str(nm).upper() or '退' in str(nm)
    }

    # 概念->成员(缓存1小时, 成员变化慢)
    # 口径与zt-stats的_load_concept_members一致: 仅沪深主板(60/00)+创业板(30)+科创板(68),
    # 排除北交所(8/4开头)及ST/退市, 使产业链统计与涨停分布盯盘数值可比。
    if not _chain_members_cache['members'] or now - _chain_members_cache['ts'] > 3600:
        try:
            conn = _db.get_conn()
        except Exception as e:
            return jsonify({'success': False, 'error': f'数据库不可用: {e}'}), 503
        con_members = {}
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT concept, stock_code FROM ths.concept_member "
                            "WHERE concept = ANY(%s) AND stock_code ~ '^(60|00|30|68)'",
                            (list(concept_set),))
                for con, code in cur.fetchall():
                    if code in excluded_codes:
                        continue
                    con_members.setdefault(con, set()).add(code)
        finally:
            conn.close()
        _chain_members_cache['members'] = con_members
        _chain_members_cache['ts'] = now
    con_members = _chain_members_cache['members']

    all_codes = set()
    for s in con_members.values():
        all_codes |= s
    if not all_codes:
        return jsonify({'success': False, 'error': '概念成员为空(请先同步同花顺概念数据)'}), 404

    # 批量实时报价(分块)
    quotes = {}
    codes = list(all_codes)
    for i in range(0, len(codes), 800):
        q = _ts.batch_pricevol(codes[i:i + 800]) or {}
        quotes.update(q)

    def agg(code_set):
        pcts = []
        up = lu = up5 = 0
        tops = []
        for c in code_set:
            q = quotes.get(c)
            if not q:
                continue
            p = q.get('pct', 0)
            pcts.append(p)
            if p > 0:
                up += 1
            if p >= 5:
                up5 += 1
            if p >= _limit_threshold(c):
                lu += 1
            tops.append((c, p))
        tops.sort(key=lambda x: -x[1])
        top3 = [{'code': c, 'name': names.get(c, c), 'pct': round(p, 2)} for c, p in tops[:3]]
        return {'members': len(code_set), 'quoted': len(pcts),
                'avg': round(sum(pcts) / len(pcts), 2) if pcts else 0,
                'up': up, 'up5': up5, 'limitup': lu, 'top': top3}

    chains = []
    for cid, c in ci.items():
        tiers = []
        chain_codes = set()
        for t in c['tiers']:
            cons = []
            for con in t['concepts']:
                m = con_members.get(con, set())
                chain_codes |= m
                a = agg(m)
                a['concept'] = con
                cons.append(a)
            cons.sort(key=lambda x: -x['avg'])
            tiers.append({'tier': t['tier'], 'concepts': cons})
        summ = agg(chain_codes)
        chains.append({'id': cid, 'name': c['name'], 'summary': summ, 'tiers': tiers})
    chains.sort(key=lambda x: -x['summary']['avg'])

    # 公司链(华为链/比亚迪链/宁德时代链等)作为单层链追加,
    # tier=公司名, 含其伞下概念。单独排序后接在产业链之后,
    # 避免小盘公司链(成员少、均值易偏高)挤占产业链位置。
    comp_chain_list = []
    for cname, cons_list in comp_chains.items():
        cons = []
        comp_codes = set()
        for con in cons_list:
            m = con_members.get(con, set())
            comp_codes |= m
            a = agg(m)
            a['concept'] = con
            cons.append(a)
        cons.sort(key=lambda x: -x['avg'])
        summ = agg(comp_codes)
        comp_chain_list.append({'id': 'company:' + cname, 'name': cname, 'summary': summ,
                                'tiers': [{'tier': cname, 'concepts': cons}]})
    comp_chain_list.sort(key=lambda x: -x['summary']['avg'])
    chains.extend(comp_chain_list)

    out = {'success': True, 'ts': datetime.now().strftime('%H:%M:%S'), 'chains': chains}
    _chain_board_cache['data'] = out
    _chain_board_cache['ts'] = now
    return jsonify(out)


# ============== 招财猫复盘: 大涨/大跌个股行为收集 ==============

# 8 种行为分类(右上/右下 × 回调/不回调 × 起涨/续涨)
CAIZHAOMAO_LABELS = [
    '右上角回调后起涨', '右上角回调后续涨', '右上角不回调式起涨', '右上角不回调式续涨',
    '右下角回调后起涨', '右下角回调后续涨', '右下角不回调式起涨', '右下角不回调式续涨',
]
_CAIZHAOMAO_LABELS_FILE = '.caizhaomao_labels.json'
_caizhaomao_labels = {}          # {"code_date": {code,name,date,pct,type,label,ts}}
_caizhaomao_labels_lock = threading.Lock()


def _load_caizhaomao_labels():
    global _caizhaomao_labels
    try:
        if os.path.exists(_CAIZHAOMAO_LABELS_FILE):
            with open(_CAIZHAOMAO_LABELS_FILE, 'r', encoding='utf-8') as f:
                _caizhaomao_labels = json.load(f) or {}
    except Exception as e:
        print(f'[招财猫] 载入分类标签失败: {e}')
        _caizhaomao_labels = {}


def _save_caizhaomao_labels():
    try:
        with _caizhaomao_labels_lock:
            with open(_CAIZHAOMAO_LABELS_FILE, 'w', encoding='utf-8') as f:
                json.dump(_caizhaomao_labels, f, ensure_ascii=False)
    except Exception as e:
        print(f'[招财猫] 保存分类标签失败: {e}')


_load_caizhaomao_labels()


# 概念板块"锁定"标记(按 日期+概念 维度), 用于追踪哪些板块已操作过。持久化到磁盘。
_CAIZHAOMAO_LOCKS_FILE = '.caizhaomao_locks.json'
_caizhaomao_locks = {}           # {"date_concept": {date, concept, ts}}
_caizhaomao_locks_lock = threading.Lock()


def _load_caizhaomao_locks():
    global _caizhaomao_locks
    try:
        if os.path.exists(_CAIZHAOMAO_LOCKS_FILE):
            with open(_CAIZHAOMAO_LOCKS_FILE, 'r', encoding='utf-8') as f:
                _caizhaomao_locks = json.load(f) or {}
    except Exception as e:
        print(f'[招财猫] 载入板块锁定标记失败: {e}')
        _caizhaomao_locks = {}


def _save_caizhaomao_locks():
    try:
        with _caizhaomao_locks_lock:
            with open(_CAIZHAOMAO_LOCKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(_caizhaomao_locks, f, ensure_ascii=False)
    except Exception as e:
        print(f'[招财猫] 保存板块锁定标记失败: {e}')


_load_caizhaomao_locks()


def _price_position(closes, date, lookback=250):
    """用 .day 收盘价字典算某日收盘处于近 lookback 交易日区间的位置分位。
    closes: {date_int_str: close}; date: 当日 YYYYMMDD。
    返回 (pctile 0~100, level) — level in 低位/中位/高位; 数据不足返回 (None,'')。
    分位 = (当日收盘 - 区间最低) / (区间最高 - 区间最低) * 100。"""
    if not closes:
        return None, ''
    # 取截止到 date(含)的最近 lookback 根收盘
    ds = sorted(k for k in closes.keys() if k <= date)
    if len(ds) < 20:   # 太少无意义(次新)
        return None, ''
    ds = ds[-lookback:]
    vals = [closes[k] for k in ds if closes.get(k, 0) > 0]
    if len(vals) < 20:
        return None, ''
    cur = closes.get(date)
    if not cur or cur <= 0:
        return None, ''
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        pct = 50.0
    else:
        pct = (cur - lo) / (hi - lo) * 100
    pct = max(0.0, min(100.0, pct))
    level = '低位' if pct < 33 else ('高位' if pct > 66 else '中位')
    return round(pct, 1), level


def _classify_pct(pct, thr):
    """涨跌幅归类: limitup涨停 / up5(5%~涨停) / limitdown跌停 / down5(-5%~跌停) / None(其它)。"""
    if pct >= thr:
        return 'limitup'
    if pct >= 5:
        return 'up5'
    if pct <= -thr:
        return 'limitdown'
    if pct <= -5:
        return 'down5'
    return None


def caizhaomao_scan_task(task_id, start8, end8):
    """
    扫描日期范围内, 概念成员(主板60/00, 非ST)每个交易日的涨跌幅, 归类:
      limitup 涨停 / up5 涨幅5%~涨停 / limitdown 跌停 / down5 跌幅-5%~跌停
    按概念聚合并出排行(涨停数Top10, 涨>=5%数Top10, 跌停数Top10, 跌<=-5%数Top10)。
    数据来自通达信tq日线。
    """
    t = caizhaomao_tasks[task_id]
    t['status'] = 'running'
    try:
        import tdx_source as ts
        from collections import defaultdict
        if not ts.is_available():
            t['status'] = 'failed'
            t['error'] = '通达信tq接口不可用(请确认量化版客户端已开启并登录)'
            return

        days = trading_days_in_range(start8, end8)
        if not days:
            t['status'] = 'failed'
            t['error'] = '区间内没有A股交易日'
            return

        concept_map = _load_concept_members()  # {concept: [code,...]} 主板
        if not concept_map:
            t['status'] = 'failed'
            t['error'] = '无概念成分股数据(请先在「数据同步」同步同花顺概念)'
            return
        code_to_concepts = defaultdict(set)
        for con, codes in concept_map.items():
            for c in codes:
                code_to_concepts[c].add(con)

        # 名称表(用于展示 + 过滤ST)
        names = {}
        try:
            lst = ts.stock_list('5', with_name=True) or []
            names = {x['code']: x.get('name', '') for x in lst}
        except Exception:
            pass

        # 过滤 ST/退市
        all_codes = []
        for c in sorted(code_to_concepts.keys()):
            nm = (names.get(c, '') or '').upper()
            if 'ST' in nm or '退' in names.get(c, ''):
                continue
            all_codes.append(c)

        total = len(all_codes)
        t['total'] = total

        # 每个目标交易日的"前一交易日"(用于算涨跌幅), 取更早区间以覆盖首日
        ext_start = (datetime.strptime(start8, '%Y%m%d') - timedelta(days=15)).strftime('%Y%m%d')
        cal = trading_days_in_range(ext_start, end8)
        cal_pos = {d: i for i, d in enumerate(cal)}
        prev_of = {}
        for d in days:
            i = cal_pos.get(d)
            prev_of[d] = cal[i - 1] if (i is not None and i > 0) else None

        events = []  # {code,name,date,pct,type}
        need_fetch = []  # 收盘缓存缺数据、需回退tq的股票

        # 主路径: 批量读本地通达信 .day 日线(全市场约2秒, 无网络依赖), 一次拿到所有收盘价。
        # 相比逐只走 tq(每只~0.3s, 5000只≈25分钟)快两个数量级。
        t['progress'] = 5
        day_closes = _read_day_closes_batch(all_codes)  # {code: {date_int_str: close}}
        t['progress'] = 60

        # 用本地日线算涨跌幅并归类
        for i, code in enumerate(all_codes):
            if t.get('cancel_requested'):
                break
            t['processed'] = i + 1
            t['progress'] = 60 + int((i + 1) / max(total, 1) * 40)
            closes = day_closes.get(code)
            thr = _limit_threshold(code)
            if not closes:
                need_fetch.append(code)   # 本地无 .day, 留给 tq 兜底
                continue
            for d in days:
                prev = prev_of.get(d)
                if not prev:
                    continue
                c = closes.get(d)
                p = closes.get(prev)
                if not c or not p or p <= 0:
                    continue
                pct = (c - p) / p * 100
                typ = _classify_pct(pct, thr)
                if typ:
                    ppos, plevel = _price_position(closes, d)
                    events.append({'code': code, 'name': names.get(code, code),
                                   'date': d, 'pct': round(pct, 2), 'type': typ,
                                   'pos': ppos, 'poslevel': plevel})

        # 兜底: 极少数本地无 .day 的股票才走 tq(通常为空)
        nf = len(need_fetch)
        for j, code in enumerate(need_fetch):
            if t.get('cancel_requested'):
                break
            try:
                today8 = datetime.now().strftime('%Y%m%d')
                need = len(trading_days_in_range(start8, today8)) + 12
                need = max(30, min(need, 1400))
                bars = ts.kline(code, count=need, period='1d', dividend_type='none')
                if not bars or len(bars) < 2:
                    continue
                closes = {}
                for b in bars:
                    d8 = _bar_date8(b)
                    cl = float(b.get('close', 0) or 0)
                    if d8 and cl > 0:
                        closes[d8] = cl
                thr = _limit_threshold(code)
                for d in days:
                    prev = prev_of.get(d)
                    if not prev or d not in closes or prev not in closes:
                        continue
                    pc = closes[prev]
                    if pc <= 0:
                        continue
                    pct = (closes[d] - pc) / pc * 100
                    typ = _classify_pct(pct, thr)
                    if typ:
                        ppos, plevel = _price_position(closes, d)
                        events.append({'code': code, 'name': names.get(code, code),
                                       'date': d, 'pct': round(pct, 2), 'type': typ,
                                       'pos': ppos, 'poslevel': plevel})
            except Exception:
                continue

        # 按 (日期 -> 概念) 聚合事件
        # day_concept_events[d][con] = [event,...]
        day_concept_events = defaultdict(lambda: defaultdict(list))
        for ev in events:
            for con in code_to_concepts.get(ev['code'], ()):
                day_concept_events[ev['date']][con].append(ev)

        def build_day_rank(con_map, count_type):
            """某一天内按 count_type 榜单: 只保留该类型的个股。
            count_type: limitup 涨停 / up5 涨幅≥5%(不含涨停) / limitdown 跌停 / down5 跌幅≤-5%(不含跌停)。"""
            rising = count_type in ('limitup', 'up5')
            scored = []
            for con, evs in con_map.items():
                kept = [e for e in evs if e['type'] == count_type]
                if not kept:
                    continue
                kept = sorted(kept, key=lambda e: (-e['pct'] if rising else e['pct']))
                scored.append({'concept': con, 'count': len(kept), 'stocks': kept})
            scored.sort(key=lambda x: -x['count'])
            return scored[:10]

        by_day = []
        for d in days:
            con_map = day_concept_events.get(d, {})
            by_day.append({
                'date': d,
                'rising': {
                    'by_limitup': build_day_rank(con_map, 'limitup'),
                    'by_up5': build_day_rank(con_map, 'up5'),
                },
                'falling': {
                    'by_limitdown': build_day_rank(con_map, 'limitdown'),
                    'by_down5': build_day_rank(con_map, 'down5'),
                },
            })

        t['result'] = {
            'days': days,
            'by_day': by_day,
            'event_count': len(events),
        }
        t['processed'] = total
        t['progress'] = 100
        t['status'] = 'cancelled' if t.get('cancel_requested') else 'completed'
    except Exception as e:
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'


@app.route('/api/hot/caizhaomao/scan', methods=['POST'])
def api_caizhaomao_scan():
    """启动招财猫复盘扫描(异步)。参数: start(YYYY-MM-DD), end(YYYY-MM-DD)"""
    data = request.get_json() or {}
    start = str(data.get('start', '')).strip()
    end = str(data.get('end', '')).strip()
    try:
        datetime.strptime(start, '%Y-%m-%d')
        datetime.strptime(end, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'error': '日期格式错误, 请使用 YYYY-MM-DD'}), 400
    start8 = start.replace('-', '')
    end8 = end.replace('-', '')
    if start8 > end8:
        start8, end8 = end8, start8

    import uuid
    task_id = str(uuid.uuid4())[:8]
    caizhaomao_tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'processed': 0, 'total': 0,
        'start': start8, 'end': end8, 'result': None, 'error': None,
    }
    threading.Thread(target=caizhaomao_scan_task,
                     args=(task_id, start8, end8), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/caizhaomao/status/{task_id}'})


@app.route('/api/hot/caizhaomao/status/<task_id>', methods=['GET'])
def api_caizhaomao_status(task_id):
    """查询扫描任务状态/结果。结果里会带上已保存的行为分类标签。"""
    if task_id not in caizhaomao_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = caizhaomao_tasks[task_id]
    resp = {
        'success': True, 'task_id': task_id,
        'status': t['status'], 'progress': t.get('progress', 0),
        'processed': t.get('processed', 0), 'total': t.get('total', 0),
        'error': t.get('error'),
        'result': t.get('result'),
        'labels': _caizhaomao_labels,
        'locks': _caizhaomao_locks,
    }
    return jsonify(resp)


@app.route('/api/hot/caizhaomao/cancel/<task_id>', methods=['POST'])
def api_caizhaomao_cancel(task_id):
    """请求中断扫描任务"""
    if task_id not in caizhaomao_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    caizhaomao_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


@app.route('/api/hot/caizhaomao/label', methods=['POST'])
def api_caizhaomao_label():
    """保存/更新某个个股(某日,某概念)的行为分类。label 为空则清除。
    参数: code, date(YYYYMMDD), concept, type, pct, name, label
    行为分类按概念板块区分, 同一只票在不同概念下可分别归类。"""
    data = request.get_json() or {}
    code = str(data.get('code', '')).strip()
    date = str(data.get('date', '')).strip()
    concept = str(data.get('concept', '')).strip()
    label = str(data.get('label', '')).strip()
    if not re.match(r'^\d{6}$', code) or not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'code/date 格式错误'}), 400
    if not concept:
        return jsonify({'success': False, 'error': '缺少 concept'}), 400
    if label and label not in CAIZHAOMAO_LABELS:
        return jsonify({'success': False, 'error': '未知分类标签'}), 400
    key = f'{code}_{date}_{concept}'
    with _caizhaomao_labels_lock:
        if not label:
            _caizhaomao_labels.pop(key, None)
        else:
            _caizhaomao_labels[key] = {
                'code': code, 'date': date, 'concept': concept, 'label': label,
                'labels': [label],   # 兼容旧格式(数组)
                'name': str(data.get('name', '')), 'type': str(data.get('type', '')),
                'pct': data.get('pct'), 'ts': time.time(),
            }
    _save_caizhaomao_labels()
    return jsonify({'success': True})


@app.route('/api/hot/caizhaomao/labels', methods=['GET'])
def api_caizhaomao_labels():
    """返回所有已保存的行为分类标签(可选 label / concept 过滤)。
    兼容旧格式: 旧数据用labels(数组), 新数据用label(字符串), 返回时统一补全两者。"""
    label = request.args.get('label', '')
    concept = request.args.get('concept', '')
    items = []
    for x in _caizhaomao_labels.values():
        item = dict(x)
        # 兼容: label缺失时从labels数组取第一个
        if not item.get('label') and item.get('labels'):
            item['label'] = item['labels'][0] if item['labels'] else ''
        if not item.get('labels') and item.get('label'):
            item['labels'] = [item['label']]
        items.append(item)
    if label:
        items = [x for x in items if x.get('label') == label]
    if concept:
        items = [x for x in items if x.get('concept') == concept]
    items.sort(key=lambda x: (x.get('date', ''), x.get('concept', ''), x.get('code', '')))
    return jsonify({'success': True, 'labels': CAIZHAOMAO_LABELS, 'items': items})


@app.route('/api/hot/caizhaomao/lock', methods=['POST'])
def api_caizhaomao_lock():
    """锁定/解锁某个概念板块(按 日期+概念)。用于标记该板块是否已操作过。
    参数: date(YYYYMMDD), concept, locked(bool)。持久化到磁盘。"""
    data = request.get_json() or {}
    date = str(data.get('date', '')).strip()
    concept = str(data.get('concept', '')).strip()
    locked = bool(data.get('locked', False))
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'date 格式错误'}), 400
    if not concept:
        return jsonify({'success': False, 'error': '缺少 concept'}), 400
    key = f'{date}_{concept}'
    with _caizhaomao_locks_lock:
        if locked:
            _caizhaomao_locks[key] = {'date': date, 'concept': concept, 'ts': time.time()}
        else:
            _caizhaomao_locks.pop(key, None)
    _save_caizhaomao_locks()
    return jsonify({'success': True, 'locked': locked})


@app.route('/api/hot/caizhaomao/locks', methods=['GET'])
def api_caizhaomao_locks():
    """返回所有已保存的板块锁定标记。"""
    return jsonify({'success': True, 'locks': _caizhaomao_locks})


# 扫描结果服务端持久化(localStorage只有5MB, 扫描结果常超10MB会静默丢失)
_CAIZHAOMAO_RESULT_FILE = '.caizhaomao_result_server.json'
_caizhaomao_result_lock = threading.Lock()


@app.route('/api/hot/caizhaomao/save', methods=['POST'])
def api_caizhaomao_save():
    """保存扫描结果到服务端文件(供刷新/重开浏览器后恢复)。"""
    try:
        data = request.get_json(force=True)
        if not data or 'result' not in data:
            return jsonify({'success': False, 'error': '缺少 result 字段'}), 400
        payload = {'result': data['result'], 'curDay': data.get('curDay'),
                   'ts': data.get('ts', int(time.time()))}
        with _caizhaomao_result_lock:
            with open(_CAIZHAOMAO_RESULT_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        return jsonify({'success': True, 'size': os.path.getsize(_CAIZHAOMAO_RESULT_FILE)})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/hot/caizhaomao/load', methods=['GET'])
def api_caizhaomao_load():
    """从服务端文件加载上次保存的扫描结果。"""
    try:
        if not os.path.exists(_CAIZHAOMAO_RESULT_FILE):
            return jsonify({'success': False, 'error': '无已保存的扫描结果'})
        with _caizhaomao_result_lock:
            with open(_CAIZHAOMAO_RESULT_FILE, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        if payload and payload.get('result', {}).get('days'):
            return jsonify({'success': True, **payload})
        return jsonify({'success': False, 'error': '扫描结果数据无效'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/hot/caizhaomao/export', methods=['GET'])
def api_caizhaomao_export():
    """导出全部招财猫数据(扫描结果+标签+锁定)为一个JSON, 供跨环境同步。"""
    try:
        result = None
        if os.path.exists(_CAIZHAOMAO_RESULT_FILE):
            with _caizhaomao_result_lock:
                with open(_CAIZHAOMAO_RESULT_FILE, 'r', encoding='utf-8') as f:
                    result = json.load(f)
        # 标签: 兼容label/labels双格式
        labels_out = []
        for x in _caizhaomao_labels.values():
            item = dict(x)
            if not item.get('label') and item.get('labels'):
                item['label'] = item['labels'][0] if item['labels'] else ''
            if not item.get('labels') and item.get('label'):
                item['labels'] = [item['label']]
            labels_out.append(item)
        payload = {
            'version': 2,
            'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'result': result,
            'labels': labels_out,
            'locks': dict(_caizhaomao_locks),
        }
        resp = make_response(json.dumps(payload, ensure_ascii=False))
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        fname = f'caizhaomao_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        resp.headers['Content-Disposition'] = f'attachment; filename="{fname}"'
        return resp
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@app.route('/api/hot/caizhaomao/import', methods=['POST'])
def api_caizhaomao_import():
    """导入招财猫数据(扫描结果+标签+锁定), 合并到当前数据。"""
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'success': False, 'error': '无数据'}), 400
        merged = {'labels': 0, 'locks': 0, 'result': False}
        # 导入标签(合并: 同key覆盖) -- 不在外层加锁, _save_caizhaomao_labels内部自带锁
        if isinstance(data.get('labels'), list):
            for item in data['labels']:
                key = f"{item.get('code','')}_{item.get('date','')}_{item.get('concept','')}"
                if not item.get('code') or not item.get('date'):
                    continue
                label = item.get('label') or (item.get('labels') or [None])[0]
                if not label:
                    continue
                _caizhaomao_labels[key] = {
                    'code': item['code'], 'date': item['date'], 'concept': item.get('concept',''),
                    'label': label, 'labels': [label],
                    'name': item.get('name',''), 'type': item.get('type',''),
                    'pct': item.get('pct'), 'ts': item.get('ts', time.time()),
                }
                merged['labels'] += 1
            _save_caizhaomao_labels()
        # 导入锁定(合并) -- _save_caizhaomao_locks内部自带锁
        if isinstance(data.get('locks'), dict):
            for k, v in data['locks'].items():
                _caizhaomao_locks[k] = v
                merged['locks'] += 1
            _save_caizhaomao_locks()
        # 导入扫描结果(覆盖)
        if data.get('result') and isinstance(data['result'], dict):
            payload = {'result': data['result'], 'curDay': data.get('curDay'),
                       'ts': data.get('ts', int(time.time()))}
            with _caizhaomao_result_lock:
                with open(_CAIZHAOMAO_RESULT_FILE, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False)
            merged['result'] = True
        return jsonify({'success': True, 'merged': merged})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


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

    # ---------- 启动环境自检: 通达信量化版 ----------
    print("\n[环境自检] 通达信量化版(tqcenter)…")
    try:
        import tdx_source as _ts
        info = _ts.get_tdx_info()
        src_label = {'config': 'config.yml', 'env': '环境变量', 'auto': '自动探测', 'default': '默认兜底(未探测到!)'}
        print(f"  安装目录 : {info['install_dir']}  (来源: {src_label.get(info['install_source'], info['install_source'])})")
        print(f"  PYPlugins: {'✓ 存在' if info['pyplugins_exists'] else '✗ 缺失'}")
        print(f"  Vipdoc   : {'✓ 存在' if info['vipdoc_exists'] else '✗ 缺失'}"
              f"  (sh/lday {'✓' if info['sh_lday_exists'] else '✗'} · sz/lday {'✓' if info['sz_lday_exists'] else '✗'})")
        if info.get('tqcenter_available'):
            print("  连接状态 : ✓ tqcenter 可用(量化版客户端已连接)")
        else:
            print("  连接状态 : ✗ tqcenter 不可用")
            print("  ⚠ 请确认: 1)通达信量化版客户端已开启并登录  2)安装目录正确")
            if info['install_source'] == 'default':
                print("  ⚠ 未能自动探测到安装目录！请在 config.yml 增加配置:")
                print("       tdx:")
                print("         install_dir: 'X:\\\\你的通达信量化版目录'")
    except Exception as _e:
        print(f"  ✗ 自检异常: {type(_e).__name__}: {_e}")
    print("=" * 60)

    start_ws_push()
    socketio.run(app, host='0.0.0.0', port=5000, debug=DEBUG_MODE, allow_unsafe_werkzeug=True)
