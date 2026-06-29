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
    """获取文章图片，返回带ID的列表"""
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
        else:
            return []
            
    except Exception as e:
        print(f"获取图片出错: {e}")
        return []


def download_image(img_url, save_path):
    """下载图片"""
    try:
        response = requests.get(img_url, cookies=COOKIES, headers=IMG_HEADERS, timeout=30)
        
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            return True
        return False
            
    except Exception as e:
        print(f"下载出错: {e}")
        return False


def download_article(article, base_dir='dataresource'):
    """下载单篇文章的图片，使用图片ID命名"""
    date_folder = article['date_folder']
    link = article['link']
    title = article['title']
    
    print(f"\n{date_folder}: {title}")
    
    save_dir = os.path.join(base_dir, date_folder)
    os.makedirs(save_dir, exist_ok=True)
    
    images = get_article_images(link)
    
    if not images:
        print(f"  未找到图片")
        return 0
    
    print(f"  找到 {len(images)} 张图片")
    
    success = 0
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
            time.sleep(0.2)
    
    # 保存图片顺序映射（便于查找第04张）
    mapping_file = os.path.join(save_dir, '_order.txt')
    with open(mapping_file, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n")
        f.write(f"# 日期: {article['pub_time']}\n")
        f.write(f"# 图片顺序映射\n\n")
        for i, img in enumerate(images, 1):
            f.write(f"{i:02d}. {img['id']}.png\n")
    
    print(f"  完成: {success}/{len(images)} 张")
    return success


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
    
    # 检查参数
    if len(sys.argv) > 1 and sys.argv[1] == 'all':
        # 下载所有文章
        print("下载所有文章...\n")
        total = 0
        for i, article in enumerate(articles, 1):
            print(f"[{i}/{len(articles)}]", end='')
            count = download_article(article, base_dir)
            total += count
            time.sleep(0.5)
        
        print("\n" + "=" * 60)
        print(f"下载完成！共 {total} 张图片")
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
        print("=" * 60)


if __name__ == '__main__':
    main()
