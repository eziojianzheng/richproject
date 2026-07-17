#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
热门股追踪 API 服务

启动: py hot_track_api.py
页面: http://127.0.0.1:5001/
接口: GET /api/hot/track?start=20260601&end=20260605&sort=stock_count&price=1
"""

from bootstrap import ensure_dependencies
ensure_dependencies({
    'flask': 'flask>=3.0.0',
    'openpyxl': 'openpyxl>=3.1.0',
})

import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import re
from flask import Flask, request, jsonify, render_template

import hot_track as ht

app = Flask(__name__)


def _valid_date(s):
    return bool(s and re.match(r'^\d{8}$', s))


@app.route('/')
def index():
    return render_template('hot_track.html')


@app.route('/api/hot/track')
def api_track():
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    sort = request.args.get('sort', 'stock_count')
    with_price = request.args.get('price', '1') not in ('0', 'false', 'no')

    if not _valid_date(start) or not _valid_date(end):
        return jsonify({'success': False,
                        'error': '日期格式错误, 请用 YYYYMMDD'}), 400
    if start > end:
        start, end = end, start
    if sort not in ('stock_count', 'days'):
        sort = 'stock_count'

    try:
        data = ht.track_hot_stocks(start, end, sort=sort, with_price=with_price)
    except Exception as e:
        return jsonify({'success': False, 'error': f'统计失败: {e}'}), 500

    return jsonify({'success': True, **data})


@app.route('/api/hot/dates')
def api_dates():
    """返回当前已有数据的所有日期(供前端日期选择)"""
    dates = sorted(ht.list_excel_dates().keys())
    return jsonify({'success': True, 'dates': dates})


if __name__ == '__main__':
    print("=" * 60)
    print("热门股追踪 API 服务")
    print("=" * 60)
    print("页面: http://127.0.0.1:5001/")
    print("接口: /api/hot/track?start=20260601&end=20260605&sort=stock_count")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5001, debug=True)
