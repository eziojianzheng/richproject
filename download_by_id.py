#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘股吧图片下载 - 使用图片ID命名
确保图片顺序变化后仍能找到正确的图片
"""

import requests
from bs4 import BeautifulSoup
import os
import re
import time
import hashlib

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
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
    获取文章图片，返回带ID的列表

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

            # 提取图片ID
            # URL格式: https://image.tgb.cn/img/2026/06/26/n05q339s0imk.png_760w.png
            match = re.search(r'/([a-z0-9]+)\.png', img_url)
            img_id = match.group(1) if match else None

            images.append({
                'url': img_url,
                'id': img_id
            })

    # 去重
    seen = set()
    unique = []
    for img in images:
        if img['id'] and img['id'] not in seen:
            seen.add(img['id'])
            unique.append(img)

    return unique


def download_image(img_url, save_path):
    """
    下载图片

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


def folder_has_images(folder_path):
    """检查文件夹是否已有图片（用于文章级前置跳过，避免重复网络请求）"""
    if not os.path.isdir(folder_path):
        return False
    for f in os.listdir(folder_path):
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            return True
    return False


def parse_order_file(folder_path):
    """
    解析 _order.txt，返回应存在的图片文件名列表（按顺序）

    格式: "01. n05q339s0imk.png"
    返回: ['n05q339s0imk.png', ...]，文件不存在或无有效行时返回 []
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
            # 形如 "01. xxxx.png"
            m = re.match(r'^\d+\.\s*(\S+\.(?:png|jpg|jpeg|gif|webp))$', line, re.I)
            if m:
                expected.append(m.group(1))
    return expected


def check_folder_complete(folder_path):
    """
    依据 _order.txt 精确判断文件夹完整性

    返回:
        (status, missing)
        status:
            'complete'   - 有 _order.txt 且所列图片全部存在
            'incomplete' - 有 _order.txt 但部分图片缺失（missing 为缺失文件名列表）
            'no_order'   - 无 _order.txt（无法判断，需请求页面核对）
        missing: 缺失的文件名列表
    """
    expected = parse_order_file(folder_path)
    if not expected:
        return 'no_order', []

    missing = [name for name in expected
               if not os.path.exists(os.path.join(folder_path, name))]
    if missing:
        return 'incomplete', missing
    return 'complete', []


def download_article(article, base_dir='dataresource', skip_existing=True):
    """
    下载单篇文章的图片，使用图片ID命名

    参数:
        skip_existing: 若文件夹依据 _order.txt 判断为完整则整篇跳过（不再请求文章页）；
                       部分缺失时只补下缺失图片；无 _order.txt 时请求页面核对

    返回:
        dict: {
            'date': 日期文件夹,
            'title': 标题,
            'status': 'ok' | 'partial' | 'empty' | 'failed' | 'skipped',
            'downloaded': 成功数,
            'total': 图片总数
        }
        status 含义:
            ok      - 全部图片下载成功
            partial - 部分图片下载失败（可重试补齐）
            empty   - 文章确实没有图片
            failed  - 网络失败，未能获取图片列表（需整篇重试）
            skipped - 依据 _order.txt 判断已完整，整篇跳过
    """
    date_folder = article['date_folder']
    link = article['link']
    title = article['title']

    print(f"\n{date_folder}: {title}")

    save_dir = os.path.join(base_dir, date_folder)

    # 文章级前置跳过：依据 _order.txt 精确判断完整性
    if skip_existing:
        status, missing = check_folder_complete(save_dir)
        if status == 'complete':
            print(f"  跳过（_order.txt 所列图片已齐全）")
            return {'date': date_folder, 'title': title, 'status': 'skipped',
                    'downloaded': 0, 'total': 0}
        elif status == 'incomplete':
            print(f"  检测到缺失 {len(missing)} 张图片，将补下: "
                  f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")
        # status == 'no_order': 无映射文件，继续请求页面核对

    os.makedirs(save_dir, exist_ok=True)

    # 获取图片列表，区分网络失败与无图片
    try:
        images = get_article_images(link)
    except NetworkError as e:
        print(f"  获取图片列表失败（网络），标记为失败待重试: {e}")
        return {'date': date_folder, 'title': title, 'status': 'failed',
                'downloaded': 0, 'total': 0}

    if not images:
        print(f"  未找到图片")
        return {'date': date_folder, 'title': title, 'status': 'empty',
                'downloaded': 0, 'total': 0}

    print(f"  找到 {len(images)} 张图片")

    success = 0          # 已就位的图片总数（含已存在）
    newly = 0            # 本次实际新下载的图片数
    for i, img in enumerate(images, 1):
        img_url = img['url']
        img_id = img['id']

        # 使用图片ID命名
        filename = f"{img_id}.png"
        save_path = os.path.join(save_dir, filename)

        if os.path.exists(save_path):
            print(f"  [{i}] 已存在: {filename}")
            success += 1
            continue

        print(f"  [{i}] 下载: {filename}")
        if download_image(img_url, save_path):
            success += 1
            newly += 1
            time.sleep(0.2)

    # 保存图片顺序映射（便于查找第04张）
    mapping_file = os.path.join(save_dir, '_order.txt')
    with open(mapping_file, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n")
        f.write(f"# 日期: {article['pub_time']}\n")
        f.write(f"# 图片顺序映射\n\n")
        for i, img in enumerate(images, 1):
            f.write(f"{i:02d}. {img['id']}.png\n")

    status = 'ok' if success == len(images) else 'partial'
    print(f"  完成: {success}/{len(images)} 张（本次新增 {newly} 张）[{status}]")
    return {'date': date_folder, 'title': title, 'status': status,
            'downloaded': newly, 'present': success, 'total': len(images)}


def download_articles(articles, base_dir='dataresource', max_round=3, skip_existing=True):
    """
    批量下载文章，自动重试失败/部分失败的文章

    参数:
        articles: 文章列表
        base_dir: 保存目录
        max_round: 失败重试的最大轮数
        skip_existing: 文件夹已有图片则整篇跳过

    返回:
        (results, failed) 最终结果列表，以及仍然失败的文章列表
    """
    pending = list(articles)
    results = {}

    for round_no in range(1, max_round + 1):
        if not pending:
            break

        if round_no > 1:
            wait = RETRY_BACKOFF * (2 ** (round_no - 2))
            print("\n" + "=" * 60)
            print(f"第 {round_no} 轮：重试 {len(pending)} 篇失败/未完成的文章 "
                  f"（等待 {wait:.0f}s）")
            print("=" * 60)
            time.sleep(wait)

        next_pending = []
        for i, article in enumerate(pending, 1):
            print(f"[{round_no}-{i}/{len(pending)}]", end='')
            result = download_article(article, base_dir, skip_existing=skip_existing)
            results[article['date_folder']] = result

            # failed(网络失败) / partial(部分图片失败) 需要重试
            # skipped / ok / empty 不重试
            if result['status'] in ('failed', 'partial'):
                next_pending.append(article)

            time.sleep(0.5)

        pending = next_pending

    result_list = list(results.values())
    failed = [r for r in result_list if r['status'] in ('failed', 'partial')]
    return result_list, failed


def main():
    """主函数"""
    import sys

    print("=" * 60)
    print("淘股吧图片下载 - 使用图片ID命名")
    print("=" * 60)

    articles = get_article_list('444409')

    if not articles:
        print("未获取到文章列表")
        return

    print(f"找到 {len(articles)} 篇文章\n")

    base_dir = 'dataresource'
    os.makedirs(base_dir, exist_ok=True)

    # 可选：按日期前缀过滤，如 python download_by_id.py 202606
    month_filter = None
    target = articles

    if len(sys.argv) > 1 and sys.argv[1] != 'all':
        month_filter = sys.argv[1]
        target = [a for a in articles if a['date_folder'].startswith(month_filter)]
        print(f"按前缀 '{month_filter}' 过滤，共 {len(target)} 篇\n")

    if len(sys.argv) > 1:
        # 下载全部 / 过滤后的文章（带自动重试）
        if month_filter:
            print(f"下载 {month_filter} 的文章...\n")
        else:
            print("下载所有文章...\n")

        results, failed = download_articles(target, base_dir)

        total = sum(r['downloaded'] for r in results)
        ok = sum(1 for r in results if r['status'] == 'ok')
        empty = sum(1 for r in results if r['status'] == 'empty')
        skipped = sum(1 for r in results if r['status'] == 'skipped')

        print("\n" + "=" * 60)
        print(f"下载完成！本次新增 {total} 张图片")
        print(f"  成功: {ok} 篇  |  已存在跳过: {skipped} 篇  |  "
              f"无图片: {empty} 篇  |  仍失败: {len(failed)} 篇")
        if failed:
            print("  以下文章未能完整下载（可稍后重跑）:")
            for r in failed:
                print(f"    - {r['date']} [{r['status']}] "
                      f"{r['downloaded']}/{r['total']}")
        print(f"保存目录: {base_dir}/")
        print("=" * 60)
    else:
        # 只下载第一篇测试
        print("测试下载第一篇文章...")
        download_article(articles[0], base_dir)

        print("\n" + "=" * 60)
        print("测试完成！")
        print(f"图片保存在: {base_dir}/日期/")
        print("每个文件夹有 _order.txt 记录图片顺序")
        print("\n下载全部图片请运行: python download_by_id.py all")
        print("按月份下载请运行:   python download_by_id.py 202606")
        print("=" * 60)


if __name__ == '__main__':
    main()
