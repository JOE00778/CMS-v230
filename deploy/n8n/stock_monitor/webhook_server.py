"""容器内 HTTP webhook：触发 check_products 扫描并返回 JSON diff。

N8N Cron 节点 → HTTP Request → http://stock-monitor:8787/run
N8N 收到 JSON 后用原生 Lark 节点 append 到电子表格。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

PORT = int(os.environ.get("PORT", "8787"))

# 全局：同一时刻只允许一个扫描
_scan_lock = threading.Lock()


def _run_scan() -> dict:
    # 延迟 import 以保证 sys.path 生效
    from check_products import load_jan_list, load_prev_state, save_state, scan_all, diff_status, write_report
    from datetime import datetime

    items = load_jan_list()
    prev = load_prev_state()
    curr = scan_all(items, use_sd=False)
    save_state(curr)

    diff = diff_status(prev, curr)
    run_date = datetime.now().strftime("%Y%m%d")
    report = write_report(curr, diff, run_date)

    return {
        "ok": True,
        "run_date": run_date,
        "total": len(items),
        "report_path": str(report),
        "diff": diff,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", ""):
            return self._html_status()
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path.startswith("/run"):
            if not _scan_lock.acquire(blocking=False):
                return self._json(409, {"ok": False, "error": "scan already in progress"})
            try:
                result = _run_scan()
                return self._json(200, result)
            except Exception as e:
                return self._json(500, {
                    "ok": False,
                    "error": str(e),
                    "trace": traceback.format_exc(),
                })
            finally:
                _scan_lock.release()
        return self._json(404, {"ok": False, "error": "not found"})

    def _html_status(self):
        scan_busy = _scan_lock.locked()
        html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>停产监控 webhook</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#222;line-height:1.6}}
code{{background:#f4f4f4;padding:2px 6px;border-radius:4px;font-size:90%}}
.st{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:13px}}
.ok{{background:#e6f7ee;color:#04693b}}
.busy{{background:#fff3e0;color:#9a5300}}</style>
</head><body>
<h1>🏷 停产监控 webhook</h1>
<p>状态: <span class="st {'busy' if scan_busy else 'ok'}">{'扫描进行中' if scan_busy else '空闲'}</span></p>
<h2>端点</h2>
<ul>
  <li><code>GET /health</code> — 健康检查</li>
  <li><code>GET /run</code> — 触发一次全量扫描（2~3 小时，返回 JSON diff）</li>
</ul>
<h2>飞书表格</h2>
<p><a href="{os.environ.get('LARK_SHEET_URL','https://ra9v81dggsk.feishu.cn/sheets/WhdnsP15phaGBAtw0jvcH7bwn2f')}" target="_blank">商品停产監視</a></p>
<h2>由 N8N 触发</h2>
<p>工作流中的 HTTP Request URL 用 <code>http://stock-monitor:8787/run</code>（N8N 容器内部主机名）</p>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[webhook] {self.address_string()} - {fmt % args}\n")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[webhook] listening on :{PORT}", flush=True)
    srv.serve_forever()
