#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
产业链映射工具: 读取 ths_concept_chains.json, 提供
  - concept -> chain(产业链) 映射
  - chain -> 有序概念列表(按上游->下游分层)
  - 噪音标签黑名单(资金/风格/区域/政策等, 不参与热力/盯盘)
供 复盘概念热力图 与 盯盘产业链 两处复用。
"""
import os
import json

_CHAINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ths_concept_chains.json')

_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    with open(_CHAINS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    chains = data.get('industrial_chains', {})
    companies = data.get('company_chains', {}) or {}
    themes = data.get('themes_no_chain', {}) or {}

    # concept -> {chain_id, chain_name, tier}
    concept2chain = {}
    # chain_id -> {name, order:[(tier, [concepts...])], concepts:[...]}
    chain_info = {}
    for cid, c in chains.items():
        name = c.get('name', cid)
        tiers = c.get('tier_order') or list((c.get('tiers') or {}).keys())
        ordered = []
        flat = []
        for tier in (c.get('tiers') or {}):
            lst = c['tiers'][tier] or []
            ordered.append({'tier': tier, 'concepts': lst})
            for con in lst:
                flat.append(con)
                concept2chain.setdefault(con, {'chain_id': cid, 'chain_name': name, 'tier': tier})
        chain_info[cid] = {'name': name, 'tiers': ordered, 'concepts': flat}

    # 公司链(单列)
    company_map = {}
    for cname, cons in companies.items():
        if cname.startswith('_'):
            continue
        company_map[cname] = cons
        for con in cons:
            concept2chain.setdefault(con, {'chain_id': 'company:' + cname,
                                           'chain_name': '公司链·' + cname, 'tier': ''})

    # 噪音黑名单: 资金/风格/区域/政策/金融/事件 等无法反映"在炒什么"的标签
    blocklist = set()
    for k, v in themes.items():
        if k.startswith('_') or not isinstance(v, list):
            continue
        for con in v:
            blocklist.add(con)

    _cache = {
        'concept2chain': concept2chain,
        'chain_info': chain_info,
        'company_map': company_map,
        'blocklist': blocklist,
        'industrial_concepts': [c for ci in chain_info.values() for c in ci['concepts']],
    }
    return _cache


def concept_to_chain(concept):
    """返回 {chain_id, chain_name, tier} 或 None"""
    return _load()['concept2chain'].get(concept)


def is_blocked(concept):
    return concept in _load()['blocklist']


def chain_info():
    """chain_id -> {name, tiers:[{tier,concepts}], concepts:[]}"""
    return _load()['chain_info']


def meaningful_concepts():
    """产业链内的全部概念(去重, 保持链内顺序)"""
    seen = set()
    out = []
    for con in _load()['industrial_concepts']:
        if con not in seen:
            seen.add(con)
            out.append(con)
    return out

def company_chains():
    """公司链: {company_name: [concept, ...]} (如 华为链 -> [华为概念, 华为昇腾, ...])"""
    return _load()['company_map']


def reload_chains():
    global _cache
    _cache = None
    return _load()
