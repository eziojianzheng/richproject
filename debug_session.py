#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
debug_session.py - 持久会话探针 (Playwright)

目的: 让 ZCode agent 能在一个常驻的 Chrome 里反复抓取当前状态,
      用户在浏览器里随意切换/操作, agent 随时发指令抓"当前这一帧",
      实现"并肩看一个浏览器"的调试体验。

架构 (文件指令队列, 零额外依赖):
  - serve   : 后台常驻。启动 Chrome 一直开着, 轮询 _session_cmd.json,
              收到指令执行, 写 _session_result.json
  - 客户端  : snapshot / switch / click / stop, 写指令、等结果

用法:
  # 1) 启动持久会话 (后台常驻, Chrome 一直开着)
  python debug_session.py serve            # 默认 /hot
  python debug_session.py serve //monitor  # 指定页面

  # 2) agent 随时发指令 (我通过 Bash 调用)
  python debug_session.py snapshot         # 抓当前帧 -> _debug_browser.json/.png
  python debug_session.py switch ths       # 切到同花顺视图后抓
  python debug_session.py click 计算       # 点"计算"按钮后抓
  python debug_session.py goto //monitor   # 导航到新页面
  python debug_session.py stop             # 关闭会话

  # 3) 关闭
  python debug_session.py stop

通信文件 (都在项目根目录):
  _session_cmd.json     指令队列 (客户端写, serve 读)
  _session_result.json  执行结果 (serve 写, 客户端读)
  _session_alive.flag   会话存活标记 (serve 存在表示在线)
  _debug_browser.png    最新截图
  _debug_browser.json   最新快照数据
"""

import sys
import os
import json
import time
import argparse
import subprocess
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE_URL = "http://127.0.0.1:5000"
CMD_FILE = "_session_cmd.json"
RESULT_FILE = "_session_result.json"
ALIVE_FLAG = "_session_alive.flag"
OUT_JSON = "_debug_browser.json"
OUT_PNG = "_debug_browser.png"

VIEW_IDS = ["view-hnr", "view-ths", "view-explosive", "view-heat",
            "view-ladder", "view-breakout", "view-caizhaomao"]
GLOBAL_VARS = ["DATA", "curSort", "curStockSort", "isDarkTheme",
               "LAZY_EXPANDS", "_curTab", "_conceptTrackData",
               "_conceptCharts", "_conceptTaskId", "_conceptPollTimer"]


def normalize_path(raw):
    """规范化页面路径: 支持 //hot (Git Bash 防转换), 完整 URL, /hot"""
    if raw.startswith("http"):
        return raw
    raw = raw.replace("\\", "/")
    if not raw.startswith("/"):
        raw = "/" + raw
    if raw.startswith("//"):
        raw = raw[1:]
    return BASE_URL + raw


def _get_content_frame(page):
    """若 page 是 nav.html 外壳(用 iframe 嵌套子页), 返回 iframe 的 content frame;
    否则返回 page 自身的主 frame。"""
    for f in page.frames:
        if f is page.main_frame:
            continue
        u = f.url or ""
        # 真实内容页: /hot /monitor /update 等 (非 about:blank, 非外壳 /#)
        if u and not u.startswith("about:blank") and "/#" not in u:
            return f
    # fallback: 如果有任意非主 frame, 取它
    for f in page.frames:
        if f is not page.main_frame:
            return f
    return page.main_frame


def capture(page, context, label="capture"):
    """抓一次全套快照。若当前页是 nav 外壳, 自动穿透 iframe 抓真实内容。"""
    snap = {"label": label, "timestamp": datetime.now().isoformat(), "url": page.url}

    if page.url.startswith("about:blank") or page.url == "chrome://newtab/":
        snap["error"] = "页面未成功加载 (停在 about:blank)"
        snap["dom_text"] = None
        return snap

    # 检测是否为 nav 外壳 (含 iframe), 是则穿透
    cf = _get_content_frame(page)
    is_iframe = (cf != page.main_frame)
    if is_iframe:
        snap["iframe_url"] = cf.url
        snap["url"] = cf.url  # 用真实内容页 URL 覆盖

    # 抓取用的 frame (外壳则用 iframe content frame, 否则用主 frame)
    eval_frame = cf

    # 1) 可见 DOM 文本 (从内容 frame 抓)
    try:
        snap["dom_text"] = eval_frame.evaluate("""() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script,style,noscript').forEach(e => e.remove());
            return (clone.innerText || '').replace(/\\n{3,}/g, '\\n\\n').trim();
        }""")
    except Exception as e:
        snap["dom_text"] = f"<抓取失败: {e}>"
    snap["dom_text_len"] = len(snap.get("dom_text") or "")

    # 2) 关键全局变量 (从内容 frame 抓)
    globals_snap = {}
    for name in GLOBAL_VARS:
        try:
            val = eval_frame.evaluate(f"(typeof {name} === 'undefined') ? '__UNDEFINED__' : {name}")
            try:
                json.dumps(val)
            except Exception:
                val = f"<unserializable: {type(val).__name__}>"
            globals_snap[name] = val
        except Exception:
            globals_snap[name] = "__eval_error__"
    snap["globals"] = globals_snap

    # 3) 可见视图 (从内容 frame 抓)
    try:
        snap["visible_views"] = eval_frame.evaluate(f"""() => ({{
            {",".join(f'"{v}": !!document.getElementById("{v}") && document.getElementById("{v}").offsetParent !== null' for v in VIEW_IDS)}
        }})""")
    except Exception:
        snap["visible_views"] = {}

    # 4) 页面 console (从内容 frame 抓)
    try:
        snap["page_console_log"] = eval_frame.evaluate("""() => {
            const el = document.getElementById('consoleLog');
            return el ? (el.innerText || '').trim() : null;
        }""")
    except Exception:
        snap["page_console_log"] = None

    # 5) storage / cookies (localStorage 跟 frame 的 origin 走, 内容页同源所以一样)
    try:
        snap["localStorage"] = eval_frame.evaluate("() => ({...localStorage})")
    except Exception:
        snap["localStorage"] = {}
    try:
        snap["sessionStorage"] = eval_frame.evaluate("() => ({...sessionStorage})")
    except Exception:
        snap["sessionStorage"] = {}
    snap["cookies"] = context.cookies()

    # 6) 标题 / 视口
    snap["title"] = page.title()
    try:
        snap["viewport"] = eval_frame.evaluate("() => ({w: innerWidth, h: innerHeight, scrollX, scrollY})")
    except Exception:
        snap["viewport"] = None
    snap["is_iframe_content"] = is_iframe
    return snap


# ============================================================
#  服务端: 常驻 Chrome, 轮询指令
# ============================================================
def serve(path, headless=False, width=1600, height=1000, wait=2.0):
    from playwright.sync_api import sync_playwright

    url = normalize_path(path)
    print(f"[session] serve 启动, 初始页面 {url}")
    print(f"[session] Chrome 常驻, 监控当前浏览器 (你在哪个页面我抓哪个)")
    print(f"[session] 发 stop 关闭")

    # 写存活标记
    with open(ALIVE_FLAG, "w") as f:
        f.write(url)

    console_msgs = []
    api_log = []
    # 跟踪当前活跃 page (用户切到哪个 tab 就是哪个)
    active_page = [None]  # 用 list 包一层便于闭包修改

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=headless,
                                     args=["--start-maximized"])
        context = browser.new_context(
            viewport={"width": width, "height": height},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36",
        )

        def _attach_page(p):
            """给一个 page 挂上网络/console 监听"""
            def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    body = None
                    if "json" in ct or "text" in ct:
                        try:
                            body = resp.text()
                            if len(body) > 8000:
                                body = body[:8000] + f"\n...[截断, 全长 {len(body)}]"
                        except Exception:
                            body = "<binary>"
                    api_log.append({"url": resp.url, "status": resp.status,
                                    "method": resp.request.method, "content_type": ct, "body": body})
                except Exception:
                    pass
            p.on("response", on_response)
            p.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
            p.on("pageerror", lambda e: console_msgs.append(f"[pageerror] {e}"))
            # 用户切到这个 tab 时, 标记为活跃
            p.on("load", lambda *a: active_page.__setitem__(0, p))

        # 首次打开初始页面
        page = context.new_page()
        active_page[0] = page
        _attach_page(page)
        # 以后用户新开的 tab 也自动挂监听
        context.on("page", lambda p: (_attach_page(p), active_page.__setitem__(0, p)))

        print(f"[session] 打开 {url}")
        try:
            # domcontentloaded 而非 networkidle: 避免长连接页面(WebSocket)永不 idle
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[session] ⚠ 首次导航异常: {e}")
        page.wait_for_timeout(int(wait * 1000))

        def get_active_page():
            """获取当前活跃 page。
            优先取 active_page[0]; 若它的 URL 是首页外壳(/#/) 则找真实内容页。"""
            p = active_page[0]
            # 若活跃页是首页外壳(用 iframe 嵌套), 优先找真实内容页
            if p and not p.is_closed() and "/#" in p.url:
                for x in context.pages:
                    if not x.is_closed() and "/#" not in x.url and not x.url.startswith("about:blank"):
                        return x
            if p is None or p.is_closed():
                pages = [x for x in context.pages if not x.is_closed()]
                p = pages[-1] if pages else None
                active_page[0] = p
            return p

        # 指令循环
        while True:
            # 读指令
            cmd = None
            if os.path.exists(CMD_FILE):
                try:
                    with open(CMD_FILE, "r", encoding="utf-8") as f:
                        cmd = json.load(f)
                    os.remove(CMD_FILE)  # 消费掉
                except Exception:
                    cmd = None

            if cmd:
                action = cmd.get("action")
                print(f"[session] 收到指令: {action} {cmd.get('arg','')}")
                result = {"action": action, "timestamp": datetime.now().isoformat()}

                # 始终操作当前活跃的 page (用户在看哪个就抓哪个)
                ap = get_active_page()

                if action == "stop":
                    result["status"] = "stopped"
                    _write_result(result)
                    print("[session] 收到 stop, 关闭中...")
                    break

                elif action == "snapshot":
                    cur_url = ap.url if ap else "about:blank"
                    snap = capture(ap, context, "manual")
                    ap.screenshot(path=OUT_PNG, full_page=cmd.get("full", True))
                    snap["browser_console"] = console_msgs[-200:]
                    snap["api_responses"] = api_log[-50:]
                    _write_snapshot(snap, cur_url)
                    result["status"] = "ok"
                    result["url"] = cur_url
                    result["dom_text_len"] = snap.get("dom_text_len")
                    result["visible_views"] = snap.get("visible_views")

                elif action == "switch":
                    target = cmd.get("arg", "")
                    clicked = ap.evaluate(f"""(t) => {{
                        if (typeof switchTab === 'function') {{ switchTab(t); return t; }}
                        return null;
                    }}""", target)
                    ap.wait_for_timeout(int(wait * 1000))
                    snap = capture(ap, context, f"switch:{target}")
                    ap.screenshot(path=OUT_PNG, full_page=cmd.get("full", True))
                    snap["browser_console"] = console_msgs[-200:]
                    _write_snapshot(snap, ap.url)
                    result["status"] = "ok"
                    result["clicked"] = clicked
                    result["visible_views"] = snap.get("visible_views")

                elif action == "click":
                    text = cmd.get("arg", "")
                    try:
                        ap.get_by_text(text, exact=False).first.click(timeout=5000)
                        ap.wait_for_timeout(int(wait * 1000))
                    except Exception as e:
                        result["click_error"] = str(e)
                    snap = capture(ap, context, f"click:{text}")
                    ap.screenshot(path=OUT_PNG, full_page=cmd.get("full", True))
                    snap["browser_console"] = console_msgs[-200:]
                    _write_snapshot(snap, ap.url)
                    result["status"] = "ok"
                    result["visible_views"] = snap.get("visible_views")

                elif action == "goto":
                    newurl = normalize_path(cmd.get("arg", "/"))
                    try:
                        # 不用 networkidle: monitor 页有 WebSocket 长连接, 永远不 idle
                        ap.goto(newurl, wait_until="domcontentloaded", timeout=30000)
                        ap.wait_for_timeout(int(wait * 1000))
                    except Exception as e:
                        result["goto_error"] = str(e)
                    snap = capture(ap, context, f"goto:{newurl}")
                    ap.screenshot(path=OUT_PNG, full_page=cmd.get("full", True))
                    snap["browser_console"] = console_msgs[-200:]
                    _write_snapshot(snap, newurl)
                    result["status"] = "ok"
                    result["url"] = ap.url

                elif action == "list_tabs":
                    # 列出所有打开的 tab
                    tabs = []
                    for i, p in enumerate(context.pages):
                        if not p.is_closed():
                            tabs.append({"index": i, "url": p.url, "title": p.title()})
                    result["status"] = "ok"
                    result["tabs"] = tabs

                elif action == "debug_frames":
                    # 诊断: 列出当前 page 的所有 frame
                    frs = []
                    for f in ap.frames:
                        frs.append({"url": f.url, "name": f.name, "is_main": f == ap.main_frame})
                    result["status"] = "ok"
                    result["frames"] = frs

                elif action == "eval":
                    # 执行任意 JS 并返回结果
                    code = cmd.get("arg", "")
                    try:
                        val = ap.evaluate(code)
                        result["status"] = "ok"
                        result["value"] = val
                    except Exception as e:
                        result["status"] = "error"
                        result["error"] = str(e)

                else:
                    result["status"] = "unknown_action"

                _write_result(result)
                print(f"[session] 指令完成: {action} -> {result.get('status')}")

            time.sleep(0.5)  # 轮询间隔

        browser.close()

    # 清理
    for f in [ALIVE_FLAG]:
        if os.path.exists(f):
            try: os.remove(f)
            except Exception: pass
    print("[session] 已关闭")


def _write_result(result):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


def _write_snapshot(snap, url):
    """写完整快照到 OUT_JSON"""
    out = {"url": url, "captured_at": datetime.now().isoformat(),
           "snapshots": [snap]}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


# ============================================================
#  客户端: 发指令, 等结果
# ============================================================
def send_cmd(action, arg="", full=True, timeout=30):
    """发指令并等结果, 返回 result dict。"""
    # 检查会话是否存活
    if not os.path.exists(ALIVE_FLAG):
        print(f"[client] ✗ 会话未运行! 请先启动: python debug_session.py serve", file=sys.stderr)
        sys.exit(2)

    # 写指令
    cmd = {"action": action, "arg": arg, "full": full, "ts": time.time()}
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False)

    # 等结果 (轮询 RESULT_FILE 的 mtime 变化)
    start = time.time()
    old_mtime = os.path.getmtime(RESULT_FILE) if os.path.exists(RESULT_FILE) else 0
    while time.time() - start < timeout:
        time.sleep(0.4)
        if os.path.exists(RESULT_FILE) and os.path.getmtime(RESULT_FILE) > old_mtime:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    print(f"[client] ✗ 指令超时 ({action})", file=sys.stderr)
    sys.exit(3)


def cmd_snapshot(full=True):
    r = send_cmd("snapshot", full=full)
    print(f"[client] snapshot: {r.get('status')} | url={r.get('url')} | DOM {r.get('dom_text_len')}字 | views={r.get('visible_views')}")
    print(f"  截图: {OUT_PNG}  数据: {OUT_JSON}")


def cmd_switch(target, full=True):
    r = send_cmd("switch", arg=target, full=full)
    print(f"[client] switch->{target}: {r.get('status')} | clicked={r.get('clicked')} | views={r.get('visible_views')}")
    print(f"  截图: {OUT_PNG}  数据: {OUT_JSON}")


def cmd_click(text, full=True):
    r = send_cmd("click", arg=text, full=full)
    print(f"[client] click「{text}」: {r.get('status')} | {r.get('click_error','')}")
    print(f"  截图: {OUT_PNG}  数据: {OUT_JSON}")


def cmd_goto(path, full=True):
    r = send_cmd("goto", arg=path, full=full)
    print(f"[client] goto {path}: {r.get('status')} | url={r.get('url')}")
    print(f"  截图: {OUT_PNG}  数据: {OUT_JSON}")


def cmd_stop():
    r = send_cmd("stop", timeout=5)
    print(f"[client] stop: {r.get('status')}")


def cmd_list_tabs():
    r = send_cmd("list_tabs", timeout=10)
    print(f"[client] tabs: {r.get('status')}")
    for t in r.get("tabs", []):
        print(f"  [{t['index']}] {t.get('title','')}  {t.get('url','')}")


# ============================================================
#  入口
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="持久会话探针")
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动常驻 Chrome 会话")
    p_serve.add_argument("path", nargs="?", default="/hot")
    p_serve.add_argument("--headless", action="store_true")
    p_serve.add_argument("--width", type=int, default=1600)
    p_serve.add_argument("--height", type=int, default=1000)
    p_serve.add_argument("--wait", type=float, default=2.0)

    sub.add_parser("snapshot", help="抓当前帧")
    p_sw = sub.add_parser("switch", help="切换视图后抓")
    p_sw.add_argument("target")
    p_cl = sub.add_parser("click", help="点击按钮后抓")
    p_cl.add_argument("text")
    p_go = sub.add_parser("goto", help="导航到新页面")
    p_go.add_argument("path")
    sub.add_parser("stop", help="关闭会话")
    sub.add_parser("status", help="查看会话状态")
    sub.add_parser("tabs", help="列出所有打开的标签页")

    args = ap.parse_args()

    if args.cmd == "serve":
        serve(args.path, args.headless, args.width, args.height, args.wait)
    elif args.cmd == "snapshot":
        cmd_snapshot()
    elif args.cmd == "switch":
        cmd_switch(args.target)
    elif args.cmd == "click":
        cmd_click(args.text)
    elif args.cmd == "goto":
        cmd_goto(args.path)
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "tabs":
        cmd_list_tabs()
    elif args.cmd == "status":
        if os.path.exists(ALIVE_FLAG):
            url = open(ALIVE_FLAG).read()
            print(f"[status] 会话在线, 页面: {url}")
        else:
            print("[status] 会话未运行")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
