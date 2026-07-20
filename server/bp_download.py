# -*- coding: utf-8 -*-
"""server.bp_download - 淘股吧文章下载/提取/入库/OCR/调试。

原 api_server.py 行 156-1024 (任务字典+爬虫层+hot_last持久化) + 1051-1774 + 2307-2314 (路由)。
"""
import os
import re
import json
import time
import shutil
import threading
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, render_template, send_file
from bs4 import BeautifulSoup

from .common import (
    ht,  # noqa: F401
    app,
    trading_days_in_range,
    is_trading_day,
    get_trade_days,
)

download_bp = Blueprint('download', __name__)


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



@download_bp.route('/api/articles', methods=['GET'])

@download_bp.route('/api/download', methods=['POST'])

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



@download_bp.route('/api/download/sync', methods=['POST'])

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



@download_bp.route('/api/status/<task_id>', methods=['GET'])

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



@download_bp.route('/api/download/cancel/<task_id>', methods=['POST'])

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



@download_bp.route('/api/files', methods=['GET'])

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



@download_bp.route('/api/files/<date>', methods=['GET'])

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



@download_bp.route('/api/extract', methods=['POST'])

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



@download_bp.route('/api/extract/status/<task_id>', methods=['GET'])

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



@download_bp.route('/api/extract/cancel/<task_id>', methods=['POST'])

def cancel_extract(task_id):
    """请求中断提取任务（当前日期处理完后停止后续日期）"""
    if task_id not in extract_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    extract_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})



@download_bp.route('/api/db/status', methods=['GET'])

def db_status():
    """探测数据库连接状态"""
    import db as _db
    ok, msg = _db.ping()
    return jsonify({'success': True, 'connected': ok, 'message': msg})



@download_bp.route('/api/db/submit', methods=['POST'])

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



@download_bp.route('/api/db/submit-batch', methods=['POST'])

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



@download_bp.route('/api/db/submit-batch/status/<task_id>', methods=['GET'])

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



@download_bp.route('/api/db/submit-batch/cancel/<task_id>', methods=['POST'])

def cancel_submit_batch(task_id):
    """请求中断批量入库任务（当前日期处理完后停止后续日期）"""
    if task_id not in submit_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    submit_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})



@download_bp.route('/api/excel/list', methods=['GET'])

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



@download_bp.route('/api/ocr/title', methods=['GET'])

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



@download_bp.route('/api/ocr/recognize', methods=['POST'])

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


@download_bp.route('/api/debug/runtime', methods=['GET'])

@download_bp.route('/api/debug/quote_sources', methods=['GET'])

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


