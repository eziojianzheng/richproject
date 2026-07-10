#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tqcenter 适配层 — 通达信官方量化接口 (TdxQuant/PYPlugins) 封装

数据源优先级: tqcenter (走自己账号, 不封IP, 异步推送) > mootdx (兜底)

特性:
  - initialize(): 一次性初始化, 线程安全(单例+锁)
  - snapshot(code): 完整快照(价/量/额/5档/均价/昨收)
  - batch_pricevol(codes): 批量价格(列表刷新用)
  - kline(code, count, period): K线(日/周/月/分钟)
  - stock_list(market): 股票列表(含名称)
  - subscribe(codes, callback): 异步行情推送(DLL回调, 零延迟)
  - index_snapshot(): 上证指数快照

注意:
  - 必须通达信量化版客户端开着并登录
  - Windows only (DLL)
  - subscribe 最多100只
  - 分时数据 tqcenter 盘后可用, 盘中用 mootdx minute
"""

import os
import sys
import json
import threading
import logging

try:
    import yaml
except Exception:
    yaml = None

_logger = logging.getLogger('tdx_source')
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(asctime)s] [TDXSRC] %(levelname)s %(message)s', datefmt='%H:%M:%S'))
    _logger.addHandler(_h)

# ============== tqcenter 路径配置 ==============
# 通达信量化版安装目录
#   优先级: config.yml 的 tdx.install_dir > 环境变量 TDX_INSTALL_DIR > 自动探测(注册表/常见路径) > 默认值
#   自动探测命中后会回写到 config.yml, 下次启动直接命中, 避免重复扫描

_CONFIG_PATH = 'config.yml'


def _load_tdx_config():
    """从 config.yml 读取 tdx 配置段。返回 (install_dir, local_port)。
    install_dir 为空串表示未配置(需自动探测); local_port 为 0 表示不尝试本地直连。"""
    install_dir = ''
    local_port = 0
    if yaml and os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            tdx = data.get('tdx') or {}
            install_dir = str(tdx.get('install_dir') or '').strip()
            try:
                local_port = int(tdx.get('local_port') or 0)
            except (TypeError, ValueError):
                local_port = 0
        except Exception:
            pass
    return install_dir, local_port


def _save_tdx_install_dir(install_dir):
    """把探测到的安装目录回写到 config.yml 的 tdx.install_dir, 保留其他配置段。"""
    if not yaml or not os.path.exists(_CONFIG_PATH):
        return
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        tdx = data.get('tdx')
        if not isinstance(tdx, dict):
            tdx = {}
            data['tdx'] = tdx
        if str(tdx.get('install_dir') or '').strip() == install_dir:
            return  # 已一致, 无需写
        tdx['install_dir'] = install_dir
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        _logger.info(f'已把通达信安装目录回写到 config.yml: {install_dir}')
    except Exception as e:
        _logger.warning(f'回写 config.yml 失败(不影响运行): {e}')


def _auto_detect_tdx_install_dir():
    """自动探测通达信量化版安装目录(不读 config/环境变量)。返回路径字符串或 None。"""
    # 1. 常见安装路径(按命中概率排序)
    candidates = [
        r'D:\game\tdx',          # 本机实际安装位置
        r'C:\new_tdx',
        r'D:\new_tdx',
        r'C:\通达信',
        r'D:\通达信',
        r'E:\new_tdx',
    ]
    for p in candidates:
        if os.path.isdir(os.path.join(p, 'PYPlugins')):
            return p
    # 2. 注册表探测(通达信量化版写入的卸载项)
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall') as base:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(base, i)
                            i += 1
                            with winreg.OpenKey(base, sub) as k:
                                name, _ = winreg.QueryValueEx(k, 'DisplayName')
                                if name and ('通达信' in name or 'TDX' in name.upper()):
                                    loc, _ = winreg.QueryValueEx(k, 'InstallLocation')
                                    if loc and os.path.isdir(os.path.join(loc, 'PYPlugins')):
                                        return loc
                        except OSError:
                            break
            except OSError:
                continue
    except Exception:
        pass
    return None


def _resolve_tdx_install_dir():
    """按优先级解析安装目录: config.yml > 环境变量 > 自动探测 > 回写config > 默认值。
    返回 (install_dir, source) where source in {'config','env','auto','default'}。"""
    # 1. config.yml 显式配置
    cfg_dir, _ = _load_tdx_config()
    if cfg_dir and os.path.isdir(os.path.join(cfg_dir, 'PYPlugins')):
        return cfg_dir, 'config'
    if cfg_dir:
        _logger.warning(f'config.yml 中 tdx.install_dir="{cfg_dir}" 无效(找不到 PYPlugins), 尝试自动探测')
    # 2. 环境变量
    env = os.environ.get('TDX_INSTALL_DIR', '').strip()
    if env and os.path.isdir(os.path.join(env, 'PYPlugins')):
        return env, 'env'
    # 3. 自动探测
    detected = _auto_detect_tdx_install_dir()
    if detected:
        # 探测命中 -> 回写到 config.yml, 下次直接命中
        _save_tdx_install_dir(detected)
        return detected, 'auto'
    # 4. 兜底
    return r'C:\new_tdx_mock', 'default'


TDX_INSTALL_DIR, TDX_INSTALL_SOURCE = _resolve_tdx_install_dir()
TDX_LOCAL_PORT = _load_tdx_config()[1]
PYPLUGINS_DIR = os.path.join(TDX_INSTALL_DIR, 'PYPlugins')
SYS_DIR = os.path.join(PYPLUGINS_DIR, 'sys')

_tq = None           # tqcenter.tq 模块引用
_tq_available = None  # None=未检测, True=可用, False=不可用
_tq_lock = threading.Lock()
# initialize 需要一个文件路径作为连接标识(run_id), 用本模块自身路径即可, 不依赖外部文件
_tq_init_path = os.path.abspath(__file__)

# 订阅状态
_subscribed_codes = set()       # 已订阅的代码集合 (XXXXXX.XX 格式)
_sub_callbacks = {}             # {code: callback_func} 每只股票的回调
_sub_lock = threading.Lock()


def _ensure_path():
    """把 PYPlugins/sys 加入 sys.path, 使 import tqcenter 可用"""
    for p in (SYS_DIR, PYPLUGINS_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)


def is_available():
    """检测 tqcenter 是否可用 (客户端是否开着)"""
    global _tq_available
    if _tq_available is not None:
        return _tq_available
    _ensure_path()
    try:
        from tqcenter import tq as _tq_mod
        _tq = _tq_mod
        _tq.initialize(_tq_init_path)
        # 测试一次快照确认连接正常
        snap = _tq.get_market_snapshot(stock_code='000001.SH', field_list=[])
        if snap and snap.get('ErrorId') == '0':
            _tq_available = True
            _logger.info('tqcenter 可用 (通达信量化版已连接)')
            return True
        else:
            _tq_available = False
            _logger.warning(f'tqcenter 快照测试失败: {snap}')
            return False
    except Exception as e:
        _tq_available = False
        _logger.warning(f'tqcenter 不可用 (通达信量化版未启动?): {e}')
        return False
    finally:
        # 检测时不要保持连接, 后续 initialize() 会重新建
        try:
            if _tq is not None:
                _tq.close()
        except Exception:
            pass


def initialize():
    """初始化 tqcenter 连接 (幂等, 线程安全)。返回 True/False。"""
    global _tq, _tq_available
    with _tq_lock:
        if _tq is not None and _tq_available:
            return True
        _ensure_path()
        try:
            from tqcenter import tq as _tq_mod
            _tq = _tq_mod
            _tq.initialize(_tq_init_path)
            _tq_available = True
            _logger.info('tqcenter 初始化成功')
            return True
        except Exception as e:
            _tq_available = False
            _logger.warning(f'tqcenter 初始化失败: {e}')
            return False


def _to_tq_code(code):
    """6位纯数字代码 -> tqcenter 格式 (XXXXXX.SH/SZ)。
    6开头=沪市SH, 0/3开头=深市SZ, 8开头=北交所BJ"""
    code = str(code).strip()
    if '.' in code:
        return code.upper()
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith(('60', '68', '9')):
        return f'{code}.SH'
    elif code.startswith(('0', '2', '3')):
        return f'{code}.SZ'
    elif code.startswith('8') or code.startswith('4'):
        return f'{code}.BJ'
    return f'{code}.SH'


def _to_plain_code(tq_code):
    """XXXXXX.SH/SZ -> 6位纯数字"""
    return str(tq_code).split('.')[0]


# ============== 快照 ==============

def snapshot(code):
    """获取单股完整快照。
    返回 dict: {code, name?, price, last_close, open, high, low, vol, amount,
                average, pct, buy_p[], buy_v[], sell_p[], sell_v[]} 或 None。
    code 支持 '300001' 或 '300001.SZ' 格式。"""
    if not initialize():
        return None
    tq_code = _to_tq_code(code)
    if not tq_code:
        return None
    try:
        with _tq_lock:
            raw = _tq.get_market_snapshot(stock_code=tq_code, field_list=[])
        if not raw or raw.get('ErrorId') != '0':
            return None
        last_close = float(raw.get('LastClose', 0) or 0)
        price = float(raw.get('Now', 0) or 0)
        if price <= 0:
            price = last_close
        pct = ((price - last_close) / last_close * 100) if last_close > 0 else 0
        return {
            'code': _to_plain_code(tq_code),
            'tq_code': tq_code,
            'price': round(price, 3),
            'last_close': round(last_close, 3),
            'open': float(raw.get('Open', 0) or 0),
            'high': float(raw.get('Max', 0) or 0),
            'low': float(raw.get('Min', 0) or 0),
            'vol': float(raw.get('Volume', 0) or 0),
            'amount': float(raw.get('Amount', 0) or 0),  # 万元
            'average': float(raw.get('Average', 0) or 0),
            'pct': round(pct, 2),
            'buy_p': raw.get('Buyp', []),
            'buy_v': raw.get('Buyv', []),
            'sell_p': raw.get('Sellp', []),
            'sell_v': raw.get('Sellv', []),
        }
    except Exception as e:
        _logger.debug(f'snapshot {code} 异常: {e}')
        return None


def index_snapshot(index_code='000001.SH'):
    """获取指数快照 (默认上证指数)。返回同 snapshot 格式但无5档。"""
    if not initialize():
        return None
    try:
        with _tq_lock:
            raw = _tq.get_market_snapshot(stock_code=index_code, field_list=[])
        if not raw or raw.get('ErrorId') != '0':
            return None
        last_close = float(raw.get('LastClose', 0) or 0)
        price = float(raw.get('Now', 0) or 0)
        if price <= 0:
            price = last_close
        pct = ((price - last_close) / last_close * 100) if last_close > 0 else 0
        return {
            'code': _to_plain_code(index_code),
            'tq_code': index_code,
            'price': round(price, 2),
            'last_close': round(last_close, 2),
            'open': float(raw.get('Open', 0) or 0),
            'high': float(raw.get('Max', 0) or 0),
            'low': float(raw.get('Min', 0) or 0),
            'vol': float(raw.get('Volume', 0) or 0),
            'amount': float(raw.get('Amount', 0) or 0),
            'average': float(raw.get('Average', 0) or 0),  # 领先指标均价
            'pct': round(pct, 2),
            'up_home': int(float(raw.get('UpHome', 0) or 0)),   # 上涨家数
            'down_home': int(float(raw.get('DownHome', 0) or 0)),  # 下跌家数
        }
    except Exception as e:
        _logger.debug(f'index_snapshot {index_code} 异常: {e}')
        return None


# ============== 批量价格 ==============

def batch_pricevol(codes):
    """批量获取价格和成交量。codes 为 6位纯数字代码列表。
    返回 {code: {price, last_close, vol, pct}} 或 None。"""
    if not initialize() or not codes:
        return None
    tq_codes = [_to_tq_code(c) for c in codes]
    tq_codes = [c for c in tq_codes if c]
    if not tq_codes:
        return None
    try:
        with _tq_lock:
            raw = _tq.get_pricevol(stock_list=tq_codes)
        if not raw:
            return None
        result = {}
        for tq_code, info in raw.items():
            plain = _to_plain_code(tq_code)
            last_close = float(info.get('LastClose', 0) or 0)
            price = float(info.get('Now', 0) or 0)
            if price <= 0:
                price = last_close
            pct = ((price - last_close) / last_close * 100) if last_close > 0 else 0
            result[plain] = {
                'price': round(price, 3),
                'last_close': round(last_close, 3),
                'vol': float(info.get('Volume', 0) or 0),
                'pct': round(pct, 2),
            }
        return result
    except Exception as e:
        _logger.debug(f'batch_pricevol 异常: {e}')
        return None


def batch_snapshots(codes):
    """批量获取完整快照 (逐个调 snapshot, 适合需要 amount/5档的场景)。
    返回 {code: snapshot_dict}。"""
    result = {}
    for c in codes:
        s = snapshot(c)
        if s:
            result[_to_plain_code(c)] = s
    return result if result else None


# ============== K线 ==============

# 已 refresh 过的 (code, period) 集合, 避免重复刷新(refresh 会触发客户端拉数据, 虽快但有开销)
_kline_refreshed = set()
_kline_refresh_lock = threading.Lock()


def kline(code, count=120, period='1d', dividend_type='none'):
    """获取K线数据。
    code: '300001' 或 '300001.SZ'
    count: K线根数
    period: '1d'日/'1w'周/'1mon'月/'1m'分/'5m'/'15m'/'30m'/'60m'
    dividend_type: 'none'不复权/'front'前复权/'back'后复权
    返回 [{date, open, high, low, close, vol, amount}] 或 None。

    注意: tqcenter 的 get_market_data 依赖客户端K线缓存, 首次取某只股票某周期时
    需先调 refresh_kline 触发客户端拉取(约0.3秒), 之后同进程内再取走缓存(0.03秒)。
    本函数自动处理 refresh, 调用方无感。"""
    if not initialize():
        return None
    tq_code = _to_tq_code(code)
    if not tq_code:
        return None
    try:
        # 首次取该股该周期: 先 refresh_kline 触发客户端拉缓存
        # refresh_kline 仅支持 1m/5m/1d 三种周期, 其他周期跳过(走 get_market_data 自带的拉取)
        refresh_key = (tq_code, period)
        need_refresh = False
        with _kline_refresh_lock:
            if refresh_key not in _kline_refreshed and period in ('1m', '5m', '1d'):
                need_refresh = True
                _kline_refreshed.add(refresh_key)
        if need_refresh:
            with _tq_lock:
                _tq.refresh_kline(stock_list=[tq_code], period=period)

        with _tq_lock:
            data = _tq.get_market_data(
                field_list=[],
                stock_list=[tq_code],
                start_time='', end_time='',
                count=count,
                dividend_type=dividend_type,
                period=period,
                fill_data=True
            )
        if not data or 'Close' not in data:
            return None
        close_df = data['Close']
        if close_df is None or close_df.empty:
            return None
        # 各字段 DataFrame, index=日期, columns=[tq_code]
        opens = data.get('Open')
        highs = data.get('High')
        lows = data.get('Low')
        vols = data.get('Volume')
        amts = data.get('Amount')
        bars = []
        for idx in close_df.index:
            d = str(idx)[:10]
            bars.append({
                'date': d,
                'open': round(float(opens.loc[idx, tq_code]) if opens is not None else 0, 3),
                'high': round(float(highs.loc[idx, tq_code]) if highs is not None else 0, 3),
                'low': round(float(lows.loc[idx, tq_code]) if lows is not None else 0, 3),
                'close': round(float(close_df.loc[idx, tq_code]), 3),
                'vol': float(vols.loc[idx, tq_code]) if vols is not None else 0,
                'amount': float(amts.loc[idx, tq_code]) if amts is not None else 0,
            })
        return bars
    except Exception as e:
        _logger.debug(f'kline {code} 异常: {e}')
        return None


def index_kline(index_code='000001.SH', count=120, period='1d'):
    """指数K线 (上证指数 000001.SH)。返回同 kline 格式。"""
    return kline(index_code, count=count, period=period, dividend_type='none')


# ============== 股票列表 ==============

def stock_list(market='5', with_name=True):
    """获取股票列表。
    market: '5'=全部A股 '7'=上证主板 '8'=深证主板 '51'=创业板 '52'=科创板 '50'=沪深A股
            '9'=重点指数 '0'=自选股 '1'=持仓股
    with_name: True返回[{code,name}] False返回[code]
    返回列表或 None。"""
    if not initialize():
        return None
    try:
        with _tq_lock:
            lst = _tq.get_stock_list(market, list_type=1 if with_name else 0)
        if not lst:
            return None
        if with_name:
            return [{'code': _to_plain_code(x['Code']), 'name': x.get('Name', '')} for x in lst]
        else:
            return [_to_plain_code(x) if isinstance(x, str) else _to_plain_code(x.get('Code', '')) for x in lst]
    except Exception as e:
        _logger.debug(f'stock_list 异常: {e}')
        return None


def segment_codes(segment):
    """获取板块代码+名称。segment='cyb'(创业板) 或 'kcb'(科创板)。
    返回 (codes_list, names_dict) 或 (None, None)。"""
    market = '51' if segment == 'cyb' else '52' if segment == 'kcb' else None
    if not market:
        return None, None
    lst = stock_list(market=market, with_name=True)
    if not lst:
        return None, None
    codes = [r['code'] for r in lst]
    names = {r['code']: r['name'] for r in lst}
    return codes, names


# ============== 异步订阅推送 ==============

def subscribe(codes, callback):
    """订阅股票行情推送 (DLL异步回调, 零延迟)。
    codes: 6位纯数字代码列表 (最多100只)
    callback: func(code_str)  code_str 为 'XXXXXX.XX' 格式, 收到推送时调用

    推送内容只有 Code, 需在回调中调 snapshot() 取最新数据。
    返回成功订阅的代码列表。"""
    if not initialize():
        return []
    tq_codes = [_to_tq_code(c) for c in codes]
    tq_codes = [c for c in tq_codes if c]
    if not tq_codes:
        return []

    with _sub_lock:
        # 记录每只股票的回调
        for tc in tq_codes:
            _sub_callbacks[tc] = callback
        # 只订阅尚未订阅的
        new_codes = [c for c in tq_codes if c not in _subscribed_codes]

    if not new_codes:
        return [_to_plain_code(c) for c in tq_codes]

    def _wrapper(data_str):
        """DLL 回调入口, 分发到用户回调"""
        try:
            j = json.loads(data_str)
            code = j.get('Code', '')
            if not code:
                return
            with _sub_lock:
                cb = _sub_callbacks.get(code)
            if cb:
                cb(code)
        except Exception as e:
            _logger.debug(f'subscribe 回调异常: {e}')

    try:
        with _tq_lock:
            _tq.subscribe_hq(stock_list=new_codes, callback=_wrapper)
        with _sub_lock:
            _subscribed_codes.update(new_codes)
        _logger.info(f'订阅成功: {new_codes} (共{len(_subscribed_codes)}只)')
        return [_to_plain_code(c) for c in tq_codes]
    except Exception as e:
        _logger.warning(f'subscribe 异常: {e}')
        return []


def unsubscribe(codes):
    """取消订阅。codes 为 6位纯数字代码列表。"""
    if not _tq or not codes:
        return
    tq_codes = [_to_tq_code(c) for c in codes]
    tq_codes = [c for c in tq_codes if c]
    try:
        with _tq_lock:
            _tq.unsubscribe_hq(stock_list=tq_codes)
        with _sub_lock:
            for tc in tq_codes:
                _subscribed_codes.discard(tc)
                _sub_callbacks.pop(tc, None)
        _logger.info(f'取消订阅: {tq_codes}')
    except Exception as e:
        _logger.debug(f'unsubscribe 异常: {e}')


def unsubscribe_all():
    """取消所有订阅"""
    with _sub_lock:
        all_codes = list(_subscribed_codes)
    if all_codes and _tq:
        try:
            with _tq_lock:
                _tq.unsubscribe_hq(stock_list=all_codes)
        except Exception:
            pass
    with _sub_lock:
        _subscribed_codes.clear()
        _sub_callbacks.clear()


def subscribed_codes_list():
    """返回当前已订阅的代码列表 (6位纯数字)"""
    with _sub_lock:
        return [_to_plain_code(c) for c in _subscribed_codes]


# ============== 交易日历 ==============

_trading_dates_cache = None

def trading_dates(market='SH', count=250):
    """获取交易日列表。返回 ['YYYYMMDD', ...] 或 None。"""
    global _trading_dates_cache
    if _trading_dates_cache:
        return _trading_dates_cache
    if not initialize():
        return None
    try:
        with _tq_lock:
            dates = _tq.get_trading_dates(market=market, start_time='', end_time='', count=count)
        if dates:
            result = [str(d).replace('-', '')[:8] for d in dates]
            _trading_dates_cache = result
            return result
    except Exception as e:
        _logger.debug(f'trading_dates 异常: {e}')
    return None


# ============== 自检 ==============

def selftest():
    """自检: 初始化 + 快照 + K线 + 列表。返回 True/False。"""
    print('=== tqcenter 自检 ===')
    ok = initialize()
    print(f'1. 初始化: {"OK" if ok else "FAIL"}')
    if not ok:
        return False
    s = snapshot('600519')
    print(f'2. 快照 600519: {"OK" if s else "FAIL"} {s}')
    k = kline('600519', count=3)
    print(f'3. K线 600519: {"OK" if k else "FAIL"} {k}')
    lst = stock_list('52', with_name=True)
    print(f'4. 科创板列表: {"OK" if lst else "FAIL"} 数量={len(lst) if lst else 0}')
    idx = index_snapshot()
    print(f'5. 上证指数快照: {"OK" if idx else "FAIL"} {idx}')
    return all([ok, s, k, lst, idx])


def get_tdx_info():
    """返回通达信安装/数据目录的诊断信息(供健康检查路由使用)。"""
    vipdoc = os.path.join(TDX_INSTALL_DIR, 'Vipdoc')
    sh_lday = os.path.join(vipdoc, 'sh', 'lday')
    sz_lday = os.path.join(vipdoc, 'sz', 'lday')
    sh_minline = os.path.join(vipdoc, 'sh', 'minline')
    sz_minline = os.path.join(vipdoc, 'sz', 'minline')
    # 统计 minline 是否有 .lc1 文件
    minline_count = 0
    for d in (sh_minline, sz_minline):
        if os.path.isdir(d):
            try:
                minline_count += len([f for f in os.listdir(d) if f.endswith('.lc1')])
            except Exception:
                pass
    return {
        'install_dir': TDX_INSTALL_DIR,
        'install_source': TDX_INSTALL_SOURCE,   # config / env / auto / default
        'local_port': TDX_LOCAL_PORT,
        'pyplugins_exists': os.path.isdir(PYPLUGINS_DIR),
        'vipdoc_exists': os.path.isdir(vipdoc),
        'sh_lday_exists': os.path.isdir(sh_lday),
        'sz_lday_exists': os.path.isdir(sz_lday),
        'minline_file_count': minline_count,
        'tqcenter_available': is_available(),
    }


if __name__ == '__main__':
    selftest()
