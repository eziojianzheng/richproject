# -*- coding: utf-8 -*-
"""server.bp_caizhaomao - 招财猫复盘扫描/标签/锁定/导入导出。

原 api_server.py 行 6436-6991。
依赖 common: _load_concept_members / _read_day_closes_batch /
            trading_days_in_range / _limit_threshold / _bar_date8
"""
import os
import re
import json
import time
import uuid
import threading
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, make_response

from .common import (
    ht,  # noqa: F401
    _load_concept_members,
    _read_day_closes_batch,
    trading_days_in_range,
    _limit_threshold,
    _bar_date8,
)

caizhaomao_bp = Blueprint('caizhaomao', __name__)

# 招财猫复盘扫描任务状态
caizhaomao_tasks = {}

# 16 种行为分类: 8涨(起涨/续涨) + 8跌(起跌/续跌)
CAIZHAOMAO_LABELS_UP = [
    '右上角回调后起涨', '右上角回调后续涨', '右上角不回调式起涨', '右上角不回调式续涨',
    '右下角回调后起涨', '右下角回调后续涨', '右下角不回调式起涨', '右下角不回调式续涨',
]
CAIZHAOMAO_LABELS_DN = [
    '右上角回调后起跌', '右上角回调后续跌', '右上角不回调式起跌', '右上角不回调式续跌',
    '右下角回调后起跌', '右下角回调后续跌', '右下角不回调式起跌', '右下角不回调式续跌',
]
CAIZHAOMAO_LABELS = CAIZHAOMAO_LABELS_UP + CAIZHAOMAO_LABELS_DN
_CAIZHAOMAO_LABELS_FILE = '.caizhaomao_labels.json'
_caizhaomao_labels = {}          # {"code_date_concept": {code,name,date,pct,type,label,labels:[...],ts}}
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
    返回 (pctile 0~100, level) - level in 低位/中位/高位; 数据不足返回 (None,'')。
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


@caizhaomao_bp.route('/api/hot/caizhaomao/scan', methods=['POST'])
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

    task_id = str(uuid.uuid4())[:8]
    caizhaomao_tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'processed': 0, 'total': 0,
        'start': start8, 'end': end8, 'result': None, 'error': None,
    }
    threading.Thread(target=caizhaomao_scan_task,
                     args=(task_id, start8, end8), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/caizhaomao/status/{task_id}'})


@caizhaomao_bp.route('/api/hot/caizhaomao/status/<task_id>', methods=['GET'])
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


@caizhaomao_bp.route('/api/hot/caizhaomao/cancel/<task_id>', methods=['POST'])
def api_caizhaomao_cancel(task_id):
    """请求中断扫描任务"""
    if task_id not in caizhaomao_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    caizhaomao_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})


@caizhaomao_bp.route('/api/hot/caizhaomao/label', methods=['POST'])
def api_caizhaomao_label():
    """保存/更新某个个股(某日,某概念)的行为分类(支持多选)。
    参数: code, date(YYYYMMDD), concept, type, pct, name
          labels: [行为分类名, ...] 多选; 为空数组则清除。
          label: 兼容旧格式(单选), 若提供则转为 [label]。
    行为分类按概念板块区分, 同一只票在不同概念下可分别归类。"""
    data = request.get_json() or {}
    code = str(data.get('code', '')).strip()
    date = str(data.get('date', '')).strip()
    concept = str(data.get('concept', '')).strip()
    if not re.match(r'^\d{6}$', code) or not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'code/date 格式错误'}), 400
    if not concept:
        return jsonify({'success': False, 'error': '缺少 concept'}), 400
    # 兼容: 旧格式单 label -> [label]; 新格式 labels 数组
    labels = data.get('labels')
    if labels is None:
        single = str(data.get('label', '')).strip()
        labels = [single] if single else []
    if not isinstance(labels, list):
        return jsonify({'success': False, 'error': 'labels 须为数组'}), 400
    labels = [str(x).strip() for x in labels if str(x).strip()]
    for lb in labels:
        if lb not in CAIZHAOMAO_LABELS:
            return jsonify({'success': False, 'error': f'未知分类标签: {lb}'}), 400
    key = f'{code}_{date}_{concept}'
    with _caizhaomao_labels_lock:
        if not labels:
            _caizhaomao_labels.pop(key, None)
        else:
            _caizhaomao_labels[key] = {
                'code': code, 'date': date, 'concept': concept,
                'label': labels[0],          # 兼容旧格式(首个)
                'labels': labels,            # 多选数组
                'name': str(data.get('name', '')), 'type': str(data.get('type', '')),
                'pct': data.get('pct'), 'ts': time.time(),
            }
    _save_caizhaomao_labels()
    return jsonify({'success': True})


@caizhaomao_bp.route('/api/hot/caizhaomao/labels', methods=['GET'])
def api_caizhaomao_labels():
    """返回所有已保存的行为分类标签(可选 label / concept 过滤)。
    兼容旧格式: 旧数据用labels(数组), 新数据用label(字符串), 返回时统一补全两者。
    labels_up/labels_dn: 上涨8项/下跌8项, 供前端按side渲染checkbox。"""
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
        items = [x for x in items if label in (x.get('labels') or []) or x.get('label') == label]
    if concept:
        items = [x for x in items if x.get('concept') == concept]
    items.sort(key=lambda x: (x.get('date', ''), x.get('concept', ''), x.get('code', '')))
    return jsonify({'success': True, 'labels': CAIZHAOMAO_LABELS,
                    'labels_up': CAIZHAOMAO_LABELS_UP, 'labels_dn': CAIZHAOMAO_LABELS_DN,
                    'items': items})


@caizhaomao_bp.route('/api/hot/caizhaomao/lock', methods=['POST'])
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


@caizhaomao_bp.route('/api/hot/caizhaomao/locks', methods=['GET'])
def api_caizhaomao_locks():
    """返回所有已保存的板块锁定标记。"""
    return jsonify({'success': True, 'locks': _caizhaomao_locks})


# 扫描结果服务端持久化(localStorage只有5MB, 扫描结果常超10MB会静默丢失)
_CAIZHAOMAO_RESULT_FILE = '.caizhaomao_result_server.json'
_caizhaomao_result_lock = threading.Lock()


@caizhaomao_bp.route('/api/hot/caizhaomao/save', methods=['POST'])
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


@caizhaomao_bp.route('/api/hot/caizhaomao/load', methods=['GET'])
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


@caizhaomao_bp.route('/api/hot/caizhaomao/export', methods=['GET'])
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


@caizhaomao_bp.route('/api/hot/caizhaomao/import', methods=['POST'])
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
