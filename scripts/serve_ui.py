#!/usr/bin/env python3
"""本地预览演示界面（离线开发用；正式演示走 CloudFront）。

正式路径是 `scripts/deploy_ui.py` —— CloudFront 把界面与 API 放在同一个源下，
浏览器发的是同源请求，不需要 CORS。本脚本只是本地改页面时的快速预览：
把同样的 `/` 与 `/v1/*` 路径在 localhost 上拼起来，避免每改一行都等
CloudFront 缓存失效。

**本进程不持有任何令牌。** 令牌由浏览器在页面里输入并随请求发出，
本进程只做透传。这一点和 CloudFront 的行为一致，所以本地看到的表现
与线上一致 —— 包括未输入令牌时的 401。

用法：
    python3 scripts/serve_ui.py                    # 自动从 CDE 栈取 API 地址
    python3 scripts/serve_ui.py --api-base https://xxx.execute-api.us-east-1.amazonaws.com
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_FILE = ROOT / "web" / "index.html"

UPSTREAM_TIMEOUT = 30
# 与 CloudFront 的行为对齐：只有 /v1/* 走 API 源，其余走静态页面。
API_PREFIX = "/v1/"
# 需要透传给上游的请求头。刻意用白名单：不把浏览器的全部头照搬过去。
FORWARD_HEADERS = ("authorization", "content-type", "idempotency-key")


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "YuetuUIPreview/2.0"
    api_base: str = ""

    def log_message(self, fmt: str, *args: object) -> None:
        # 只记方法、路径与状态码。绝不记请求头 —— Authorization 在其中。
        sys.stderr.write(f"[ui] {fmt % args}\n")

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 同源部署下浏览器不会发预检；本地预览也保持同源，因此直接 204。
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(API_PREFIX):
            self._proxy(method)
        elif method == "GET" and path in ("/", "/index.html"):
            self._serve_ui()
        elif method == "GET" and path == "/healthz":
            self._send_json(200, {"status": "ok", "upstream": self.api_base})
        else:
            # 与 CloudFront 的 CustomErrorResponses 对齐：未知路径回落首页。
            if method == "GET":
                self._serve_ui()
            else:
                self._send_json(404, {"message": "本地预览：仅提供 / 与 /v1/*"})

    # ---------------- 实现 ----------------

    def _serve_ui(self) -> None:
        try:
            body = UI_FILE.read_bytes()
        except OSError:
            self._send_json(500, {"message": f"界面文件缺失：{UI_FILE}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        target = f"{self.api_base}{parsed.path}"
        if parsed.query:
            target += f"?{parsed.query}"

        payload: bytes | None = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b"{}"

        headers = {"Content-Type": "application/json"}
        for name in FORWARD_HEADERS:
            value = self.headers.get(name)
            if value:
                headers[name] = value

        request = urllib.request.Request(target, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                self._relay(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 原样转发：界面要展示 401/403/404/422/409 的真实响应体。
            self._relay(exc.code, exc.read(), dict(exc.headers or {}))
        except Exception as exc:
            self._send_json(502, {"message": f"上游请求失败：{type(exc).__name__}"})

    def _relay(self, status: int, body: bytes, upstream_headers: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header(
            "Content-Type", upstream_headers.get("Content-Type", "application/json; charset=utf-8")
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if upstream_headers.get("Idempotency-Replayed"):
            self.send_header("Idempotency-Replayed", upstream_headers["Idempotency-Replayed"])
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def resolve_api_base(explicit: str | None, stack: str, region: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    try:
        import boto3

        client = boto3.client("cloudformation", region_name=region)
        for output in client.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs") or []:
            if output["OutputKey"] == "ApiBaseUrl":
                return str(output["OutputValue"]).rstrip("/")
    except Exception as exc:
        print(f"[WARN] 无法从栈 {stack} 读取 ApiBaseUrl：{type(exc).__name__}", file=sys.stderr)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="本地预览演示界面")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--stack", default="yuetu-trip-cde-demo")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    api_base = resolve_api_base(args.api_base, args.stack, args.region)
    if not api_base:
        print("[FAIL] 未能确定 API 基地址，请用 --api-base 显式指定。", file=sys.stderr)
        return 1

    PreviewHandler.api_base = api_base

    print("=" * 74)
    print("途悦旅行助手界面 —— 本地预览（正式演示请用 scripts/deploy_ui.py 走 CloudFront）")
    print("=" * 74)
    print(f"上游 API : {api_base}")
    print(f"本地地址 : http://localhost:{args.port}")
    print("令牌     : 本进程不持有令牌；在页面右上角输入后即可使用")
    print()
    print(f"从自己的电脑访问：ssh -L {args.port}:localhost:{args.port} ec2-user@<EC2 地址>")
    print("Ctrl+C 停止。")
    print("=" * 74)

    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] 已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
