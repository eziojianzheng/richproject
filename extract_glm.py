#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
涨停复盘数据提取（GLM-OCR + 红色板块标题识别）

流程:
- 用 GLM-OCR 找 03/04 图片：
    03图: 开头包含 "880005" 或 "涨跌家数"（提取涨跌家数/成交额）
    04图: 含 "湖南人涨停复盘" 或以涨停股表格开头（提取板块个股）
- 03/04 必须来自同一日期文件夹
- 04图板块归属: 红色检测定位板块标题 -> 裁剪单条小图用视觉模型识别板块名
  -> 按 y 坐标把 OCR 提取的股票分配到对应板块（涨停炸板之后的数据丢弃）
- 提取数据并按模板导出 Excel 到 excelDataSource

用法:
    py extract_glm.py 20260629            # 提取单日
    py extract_glm.py 20260629 20260626   # 提取多日
    py extract_glm.py month 202606        # 按月批量(跳过已导出)
    py extract_glm.py all                 # 全部日期批量
"""

# 启动时自动检测依赖
from bootstrap import ensure_dependencies
ensure_dependencies({
    'requests': 'requests>=2.31.0',
    'bs4': 'beautifulsoup4>=4.12.0',
    'yaml': 'PyYAML>=6.0',
    'openpyxl': 'openpyxl>=3.1.0',
    'PIL': 'Pillow>=10.0.0',
    'numpy': 'numpy>=1.24.0',
})

import os
import re
import json
import base64
import sys
import time

import yaml
import requests
from bs4 import BeautifulSoup
import openpyxl


# ============== 配置 ==============

def load_config():
    config_path = 'config.yml'
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


CONFIG = load_config()

GLM_OCR_URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
GLM_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 排除的板块
EXCLUDED_SECTORS = ['其他', '涨停炸板', '其他热点', '其他个股', '首板']

# 提取模板路径
TEMPLATE_PATH = 'systemOriginalData/提取模板.xlsx'


def get_api_key():
    return CONFIG.get('ai', {}).get('zhipu', {}).get('api_key', '')


def get_vision_model():
    return CONFIG.get('ai', {}).get('zhipu', {}).get('vision_model', 'GLM-4.5V')


# ============== GLM-OCR 调用（带缓存） ==============

def glm_ocr(image_path, use_cache=True):
    """
    调用 GLM-OCR 识别图片，返回 layout_details（带磁盘缓存避免重复花token）

    返回: (layout_details: list, error: str|None)
    """
    cache_path = image_path + '.glmocr.json'

    # 命中缓存
    if use_cache and os.path.exists(cache_path):
        try:
            if os.path.getmtime(cache_path) >= os.path.getmtime(image_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f), None
        except Exception:
            pass

    api_key = get_api_key()
    if not api_key or api_key.startswith('在这里') or api_key == 'YOUR_API_KEY_HERE':
        return None, "未配置有效的 API Key（请在 config.yml 填入 ai.zhipu.api_key）"

    with open(image_path, 'rb') as f:
        img_base64 = base64.b64encode(f.read()).decode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "glm-ocr",
        "file": f"data:image/png;base64,{img_base64}"
    }

    # 带重试的请求（GLM-OCR 偶发 SSL/超时）
    result = None
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(GLM_OCR_URL, headers=headers, json=payload, timeout=180)
            result = resp.json()
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < 3:
                wait = 2 ** (attempt - 1)
                print(f"    GLM-OCR 网络异常，{wait}s 后重试 [{attempt}/2]...")
                time.sleep(wait)
    if last_err is not None:
        return None, f"请求异常: {last_err}"

    if 'layout_details' not in result:
        return None, result.get('error', {}).get('message', f'未知错误: {result}')

    layout_details = result['layout_details']

    # 写缓存
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(layout_details, f, ensure_ascii=False)
    except Exception:
        pass

    return layout_details, None


# ============== 文本/表格解析 ==============

def layout_to_text(layout_details, limit=None):
    """把 layout_details 按顺序拼成纯文本（用于关键字判断）"""
    parts = []
    for page in layout_details:
        for item in page:
            content = item.get('content', '')
            if not content:
                continue
            if item.get('label') == 'table':
                soup = BeautifulSoup(content, 'html.parser')
                parts.append(soup.get_text(separator=' '))
            else:
                parts.append(content)
    text = '\n'.join(parts)
    return text[:limit] if limit else text


def parse_html_tables(layout_details):
    """解析 GLM-OCR 返回的 HTML 表格 -> [[row, ...], ...]"""
    all_tables = []
    for page in layout_details:
        for item in page:
            if item.get('label') == 'table':
                soup = BeautifulSoup(item.get('content', ''), 'html.parser')
                for table in soup.find_all('table'):
                    table_data = []
                    for row in table.find_all('tr'):
                        cells = row.find_all(['th', 'td'])
                        row_data = [c.get_text(strip=True) for c in cells]
                        if row_data:
                            table_data.append(row_data)
                    if table_data:
                        all_tables.append(table_data)
    return all_tables


def extract_zhangdie(layout_details):
    """
    从03图提取涨跌数据
    返回: {'上涨家数', '下跌家数', '总成交额'}
    """
    result = {'上涨家数': None, '下跌家数': None, '总成交额': None}
    text = layout_to_text(layout_details)

    m = re.search(r'上涨家数\s*(\d+)', text)
    if m:
        result['上涨家数'] = int(m.group(1))
    m = re.search(r'下跌家数\s*(\d+)', text)
    if m:
        result['下跌家数'] = int(m.group(1))
    m = re.search(r'总成交额\s*([\d.]+)\s*亿', text)
    if m:
        result['总成交额'] = m.group(1)

    return result


# 板块标题行: 【板块名】N只 X亿  或  【板块名】
_SECTOR_HEAD_RE = re.compile(r'【([^】]+)】')


def _iter_blocks(layout_details):
    """按原始顺序产出 (label, content) 块"""
    for page in layout_details:
        for item in page:
            yield item.get('label', ''), item.get('content', '')


# 分段参数
SEG_HEIGHT = 4000      # 每段高度(px) - 增大以减少分段数
SEG_OVERLAP = 500      # 段间重叠(px)，避免板块被腰斩
SEG_TRIGGER = 4500     # 超过此高度才分段，否则整图一次


def ensure_dependencies_extra():
    """确保 numpy 可用（红色检测需要）"""
    ensure_dependencies({'numpy': 'numpy>=1.24.0'})


# 板块标题单条识别 prompt
_TITLE_PROMPT = """这是从股票复盘图裁剪的一小条，最上方有一行红色文字的板块标题，格式为【板块名】N只 X亿。
请只输出最上方那一行红色板块标题中的板块名称，去掉【】符号。
只输出一个JSON对象，例如：{"板块":"半导体"}
如果看不到红色板块标题，输出：{}"""


def _find_red_bands(arr, threshold=5, gap=15, min_height=8):
    """
    检测图片中红色文字的 y 坐标区间（板块标题为红色）

    参数:
        arr: numpy RGB 数组
        threshold: 一行最少红色像素数
        gap: 区间合并间隔(px)
        min_height: 最小区间高度(px)，过滤噪点

    返回: [(y0, y1), ...]
    """
    import numpy as np
    R = arr[:, :, 0].astype(int)
    G = arr[:, :, 1].astype(int)
    B = arr[:, :, 2].astype(int)
    red_mask = (R > 140) & (G < 100) & (B < 100)
    rows = np.where(red_mask.sum(axis=1) >= threshold)[0]
    bands = []
    if len(rows) > 0:
        s = p = rows[0]
        for r in rows[1:]:
            if r - p > gap:
                if p - s >= min_height or True:  # 保留所有
                    bands.append((int(s), int(p)))
                s = r
            p = r
        bands.append((int(s), int(p)))
    return bands


def _call_title_vision(img_b64):
    """识别单个红色条的板块名，返回 板块名str|None"""
    api_key = get_api_key()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": get_vision_model(),
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            {"type": "text", "text": _TITLE_PROMPT},
        ]}],
        "temperature": 0.01,
        "max_tokens": 100,
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(GLM_CHAT_URL, headers=headers, json=payload, timeout=60)
            result = resp.json()
        except Exception:
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            return None
        try:
            content = result['choices'][0]['message']['content']
            if isinstance(content, list):
                content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
        except (KeyError, IndexError, TypeError):
            # 限速重试
            if result.get('code') in (1302, 429):
                time.sleep(30)
                continue
            return None
        # 解析 JSON 对象
        m = re.search(r'\{[^}]*\}', content)
        if m:
            try:
                obj = json.loads(m.group(0))
                name = str(obj.get('板块', '')).strip()
                return name if name else None
            except Exception:
                pass
        return None
    return None


def extract_sectors_by_red(image_path, layout_details, use_cache=True):
    """
    板块识别主方案（红色定位 + 单条识别 + y坐标匹配）

    流程:
        1. 红色检测定位所有板块标题的 y 坐标
        2. 裁剪每个红色条小图，Vision 识别板块名（小图准、省token）
        3. 用板块标题 y 坐标 与 OCR 股票 y 坐标匹配，分配板块归属
        4. 遇到"涨停炸板"标题后的股票全部丢弃

    返回: (sectors: list, error: str|None)
    """
    from PIL import Image
    import numpy as np
    import io

    cache_path = image_path + '.sectors.json'

    # 1. 红色检测板块标题 y 坐标 + 识别板块名（带缓存）
    sector_titles = None  # [(y, 板块名), ...]
    if use_cache and os.path.exists(cache_path):
        try:
            if os.path.getmtime(cache_path) >= os.path.getmtime(image_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    sector_titles = json.load(f)
        except Exception:
            sector_titles = None

    im = Image.open(image_path).convert('RGB')
    w, h = im.size

    if sector_titles is None:
        arr = np.array(im)
        bands = _find_red_bands(arr)
        print(f"    红色检测: {len(bands)} 个板块标题")
        sector_titles = []
        for (y0, y1) in bands:
            crop = im.crop((0, max(0, y0 - 5), w, min(h, y1 + 5)))
            buf = io.BytesIO()
            crop.save(buf, format='PNG')
            b64 = base64.b64encode(buf.getvalue()).decode()
            name = _call_title_vision(b64)
            if name:
                sector_titles.append([y0, name])
        # 写缓存
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(sector_titles, f, ensure_ascii=False)
        except Exception:
            pass

    if not sector_titles:
        return None, "未检测到板块标题"

    # 2. 提取所有股票（带 y 坐标）
    stocks = _extract_stocks_with_y(layout_details)
    if not stocks:
        return None, "未提取到股票"

    # 3. 找"涨停炸板"标题 y，作为有效数据截止线
    cutoff_y = h
    for y, name in sector_titles:
        if name in ('涨停炸板', '炸板'):
            cutoff_y = y
            break

    # 4. 按 y 坐标分配板块
    # 过滤掉非有效板块标题（图片标题等），保留在 cutoff 之前的
    valid_titles = [(y, name) for y, name in sector_titles if y < cutoff_y]
    valid_titles.sort(key=lambda x: x[0])

    results = []
    for s in stocks:
        sy = s['_y']
        if sy >= cutoff_y:
            continue  # 炸板数据，丢弃
        # 找该股票所属板块：最后一个 y <= sy 的板块标题
        sector = None
        for ty, tname in valid_titles:
            if ty <= sy:
                sector = tname
            else:
                break
        if sector is None or sector in EXCLUDED_SECTORS:
            continue
        results.append({
            '概念板块': sector,
            '代码': s['代码'],
            '名称': s['名称'],
            '末次时间': s['末次时间'],
            '连扳数': s['连扳数'],
            '原因': s['原因'],
        })

    return results, None


def _extract_stocks_with_y(layout_details):
    """提取所有股票并带上 y 坐标（含thead中被误判的股票），按y排序"""
    stocks = []
    for page in layout_details:
        for item in page:
            if item.get('label') != 'table':
                continue
            y_base = item.get('bbox_2d', [0, 0])[1]
            content = item.get('content', '')
            soup = BeautifulSoup(content, 'html.parser')
            for table in soup.find_all('table'):
                rows = table.find_all('tr')
                n_rows = len(rows)
                table_h = item.get('bbox_2d', [0, 0, 0, 0])
                th = (table_h[3] - table_h[1]) if len(table_h) >= 4 else 0
                for ri, row in enumerate(rows):
                    cells = [c.get_text(strip=True) for c in row.find_all(['th', 'td'])]
                    if not cells:
                        continue
                    first = cells[0].strip()
                    if first in ('代码', '名称', '板块'):
                        continue
                    full_text = ' '.join(cells)
                    if _SECTOR_HEAD_RE.search(full_text) and not re.match(r'^\d{6}$', first):
                        continue
                    if re.match(r'^\d{6}$', first) and len(cells) >= 4:
                        val4 = cells[3].strip()
                        # 估算该行的 y 坐标（表格内按行均分）
                        row_y = y_base + int(th * ri / max(n_rows, 1)) if th else y_base
                        stocks.append({
                            '代码': first,
                            '名称': cells[1].strip() if len(cells) > 1 else '',
                            '末次时间': cells[2].strip() if len(cells) > 2 else '',
                            '连扳数': val4,
                            '原因': cells[4].strip() if len(cells) > 4 else '',
                            '_y': row_y,
                        })
    stocks.sort(key=lambda s: s['_y'])
    return stocks


# ============== 找 03/04 图片 ==============

def classify_image(layout_details):
    """
    依据图片内容判断类型
    返回: '03' | '04' | None

    03图特征: 开头包含 "880005" 或 "涨跌家数"
    04图特征: 
        1. 包含 "湖南人涨停复盘" 标题
        2. 以板块结构 "【市场连板股】" 开头
        3. 开头表格含涨停股特征（代码|名称|末次时间|连板数|原因）
           （部分日期标题为图形未被OCR识别，需通过表格结构判断）
    """
    head = layout_to_text(layout_details, limit=200)
    full = layout_to_text(layout_details)

    # 03 优先（开头特征明确）
    if '880005' in head or '涨跌家数' in head:
        return '03'

    # 04: 标题命中
    if '湖南人涨停复盘' in full:
        return '04'
    # 04: 以"市场连板股"板块开头
    if '【市场连板股】' in head or head.lstrip().startswith('市场连板股'):
        return '04'

    # 04: 开头表格含涨停股特征（代码|名称|末次时间|连板数|原因）
    # 部分日期的标题是图形，OCR只能识别表格，需通过表格结构判断
    head_lower = head.lower()
    if ('连板数' in head or '连板' in head) and ('代码' in head or '名称' in head):
        # 检查是否包含典型的涨停股表格结构
        if re.search(r'代码.*名称.*连板', head, re.DOTALL):
            return '04'
        # 检查是否有6位股票代码开头（涨停股表格特征）
        if re.search(r'^\s*\d{6}\s+\S+\s+\d{1,2}:\d{2}\s+\d', head):
            return '04'

    # 04: 表格内容包含"涨停炸板"（04图底部特有）
    if '涨停炸板' in full:
        return '04'

    return None


def read_order(date_folder, base_dir='dataresource'):
    """读取 _order.txt，返回 {序号: 文件名}"""
    order_file = os.path.join(base_dir, date_folder, '_order.txt')
    order = {}
    if not os.path.exists(order_file):
        return order
    with open(order_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^(\d+)\.\s*(\S+\.png)$', line, re.I)
            if m:
                order[int(m.group(1))] = m.group(2)
    return order


def find_03_04(date_folder, base_dir='dataresource'):
    """
    在日期文件夹中用 GLM-OCR 找 03/04 图片
    策略: 先按 _order.txt 第3/4张验证，命中即用；否则轮询其余图片

    返回: {'image_03': path|None, 'image_04': path|None, 'date': date_folder}
    """
    result = {'image_03': None, 'image_04': None, 'date': date_folder}

    folder = os.path.join(base_dir, date_folder)
    if not os.path.isdir(folder):
        print(f"  文件夹不存在: {folder}")
        return result

    def classify_file(name):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            return None
        layout, err = glm_ocr(path)
        if err:
            print(f"  识别 {name} 失败: {err}")
            return None
        return classify_image(layout)

    order = read_order(date_folder, base_dir)
    checked = set()

    # 策略1: 先验证 order 的第3张(03) / 第4张(04)
    if 3 in order:
        name = order[3]
        checked.add(name)
        if classify_file(name) == '03':
            result['image_03'] = os.path.join(folder, name)
            print(f"  03图(order#3命中): {name}")
    if 4 in order:
        name = order[4]
        checked.add(name)
        if classify_file(name) == '04':
            result['image_04'] = os.path.join(folder, name)
            print(f"  04图(order#4命中): {name}")

    # 策略2: 仍缺则轮询其余图片
    if not (result['image_03'] and result['image_04']):
        images = sorted([f for f in os.listdir(folder) if f.lower().endswith('.png')])
        for name in images:
            if result['image_03'] and result['image_04']:
                break
            if name in checked:
                continue
            kind = classify_file(name)
            if kind == '03' and not result['image_03']:
                result['image_03'] = os.path.join(folder, name)
                print(f"  03图(轮询命中): {name}")
            elif kind == '04' and not result['image_04']:
                result['image_04'] = os.path.join(folder, name)
                print(f"  04图(轮询命中): {name}")

    return result


def validate_same_date(img_03, img_04):
    """校验03/04来自同一日期文件夹"""
    if not img_03 or not img_04:
        return False, "图片路径不能为空"
    d03 = os.path.basename(os.path.dirname(img_03))
    d04 = os.path.basename(os.path.dirname(img_04))
    if d03 != d04:
        return False, f"日期不一致: 03在{d03}, 04在{d04}"
    return True, f"校验通过，日期: {d03}"


# ============== 单日提取 ==============

def export_excel(date_folder, zhangdie, sectors, output_dir='excelDataSource'):
    """
    基于提取模板生成Excel
    - 03提取 sheet: 上涨家数 | 下跌家数 | 总成交额
    - 04提取 sheet: 概念板块 | 代码 | 名称 | 末次时间 | 连扳数 | 原因
    """
    os.makedirs(output_dir, exist_ok=True)

    # 以模板为基础（保留表头/样式）
    if os.path.exists(TEMPLATE_PATH):
        wb = openpyxl.load_workbook(TEMPLATE_PATH)
    else:
        wb = openpyxl.Workbook()
        wb.active.title = '03提取'
        wb['03提取'].append(['上涨家数', '下跌家数', '总成交额'])
        ws04 = wb.create_sheet('04提取')
        ws04.append(['概念板块', '代码', '名称', '末次时间', '连扳数', '原因'])

    # === 03提取 ===
    ws03 = wb['03提取']
    ws03.cell(row=2, column=1, value=zhangdie.get('上涨家数'))
    ws03.cell(row=2, column=2, value=zhangdie.get('下跌家数'))
    ws03.cell(row=2, column=3, value=zhangdie.get('总成交额'))

    # === 04提取 ===
    ws04 = wb['04提取']
    # 清除模板里第2行起的旧数据（保留表头）
    if ws04.max_row > 1:
        ws04.delete_rows(2, ws04.max_row)
    for s in sectors:
        ws04.append([
            s.get('概念板块', ''),
            s.get('代码', ''),
            s.get('名称', ''),
            s.get('末次时间', ''),
            s.get('连扳数', ''),
            s.get('原因', ''),
        ])

    out_path = os.path.join(output_dir, f'{date_folder}_涨停复盘.xlsx')
    wb.save(out_path)
    return out_path


# ============== 单日提取 ==============

def extract_date(date_folder, base_dir='dataresource', output_dir='excelDataSource'):
    """提取单日数据并生成Excel"""
    date_folder = date_folder.replace('-', '')
    print("=" * 60)
    print(f"提取日期: {date_folder}")
    print("=" * 60)

    # 1. 找03/04（先order，后轮询）
    print("[1/4] 查找 03/04 图片 (先order第3/4张，未命中再轮询)...")
    imgs = find_03_04(date_folder, base_dir)
    img_03, img_04 = imgs['image_03'], imgs['image_04']

    if not img_03:
        print("✗ 未找到03图片(开头含 880005/涨跌家数)")
        return False
    if not img_04:
        print("✗ 未找到04图片(湖南人涨停复盘/市场连板股)")
        return False

    # 2. 校验同日期
    valid, msg = validate_same_date(img_03, img_04)
    print(f"[2/4] {msg}")
    if not valid:
        return False

    # 3. 提取数据
    print("[3/4] 提取数据...")
    layout_03, err03 = glm_ocr(img_03)
    if err03:
        print(f"✗ 03图提取失败: {err03}")
        return False

    zhangdie = extract_zhangdie(layout_03)

    # 04图板块识别新方案(红色定位):
    #   1. OCR 提取所有股票（含 y 坐标）
    #   2. 红色检测定位板块标题 + 单条小图识别板块名（准、省token）
    #   3. 按 y 坐标匹配分配板块，涨停炸板之后丢弃
    layout_04, err04 = glm_ocr(img_04)
    if err04:
        print(f"✗ 04图OCR失败: {err04}")
        return False

    sectors, errsec = extract_sectors_by_red(img_04, layout_04)
    if errsec:
        print(f"✗ 04图板块识别失败: {errsec}")
        return False

    if not sectors:
        print(f"✗ 04图提取失败: 无有效数据")
        return False

    print(f"  涨跌(03): {zhangdie}")
    from collections import Counter
    dist = Counter(s['概念板块'] for s in sectors)
    print(f"  板块个股(04): {len(sectors)} 条，{len(dist)} 个板块")
    print(f"  板块分布: {dict(dist)}")

    # 4. 生成Excel
    print("[4/4] 生成Excel(按模板)...")
    excel_path = export_excel(date_folder, zhangdie, sectors, output_dir)
    print(f"✓ 已生成: {excel_path}")
    print("=" * 60)
    return True


def list_dates(base_dir='dataresource', prefix=None):
    """列出 dataresource 下的日期文件夹（可按前缀过滤），升序"""
    if not os.path.isdir(base_dir):
        return []
    dates = [d for d in os.listdir(base_dir)
             if os.path.isdir(os.path.join(base_dir, d)) and re.match(r'^\d{8}$', d)]
    if prefix:
        dates = [d for d in dates if d.startswith(prefix)]
    return sorted(dates)


def extract_batch(dates, base_dir='dataresource', output_dir='excelDataSource',
                  skip_existing=True):
    """批量提取多个日期"""
    ok, skipped, failed = [], [], []
    for i, d in enumerate(dates, 1):
        d = d.replace('-', '')
        out = os.path.join(output_dir, f'{d}_涨停复盘.xlsx')
        if skip_existing and os.path.exists(out):
            print(f"[{i}/{len(dates)}] {d} 已存在Excel，跳过")
            skipped.append(d)
            continue
        print(f"\n[{i}/{len(dates)}] 处理 {d}")
        try:
            if extract_date(d, base_dir, output_dir):
                ok.append(d)
            else:
                failed.append(d)
        except Exception as e:
            print(f"✗ {d} 异常: {e}")
            failed.append(d)

    print("\n" + "=" * 60)
    print(f"批量完成: 成功 {len(ok)} | 跳过 {len(skipped)} | 失败 {len(failed)}")
    if failed:
        print(f"失败日期: {failed}")
    print("=" * 60)
    return ok, skipped, failed


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法:")
        print("  py extract_glm.py <日期>            单日,如 20260601")
        print("  py extract_glm.py <日期> <日期> ... 多日")
        print("  py extract_glm.py month 202606      按月批量(自动跳过已导出)")
        print("  py extract_glm.py all               全部日期批量")
        sys.exit(1)

    if sys.argv[1] == 'month' and len(sys.argv) >= 3:
        dates = list_dates(prefix=sys.argv[2])
        print(f"按月 {sys.argv[2]}: 共 {len(dates)} 天")
        extract_batch(dates)
    elif sys.argv[1] == 'all':
        dates = list_dates()
        print(f"全部: 共 {len(dates)} 天")
        extract_batch(dates)
    else:
        for d in sys.argv[1:]:
            extract_date(d)
            print()
