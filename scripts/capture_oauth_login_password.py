import argparse
import json
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取真实 OAuth login_password 步骤请求")
    parser.add_argument("--url", required=True, help="真实 OAuth authorize 链接")
    parser.add_argument(
        "--output",
        default="build/oauth_login_capture.jsonl",
        help="抓包输出文件路径",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="等待手动完成登录流程的最长秒数",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="可选浏览器通道，例如 msedge 或 chrome",
    )
    return parser


def shorten(text: str | None, limit: int = 1500) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + " ...<truncated>"


def main() -> None:
    args = build_parser().parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")

    state = {"captured_password_step": False}
    lock = threading.Lock()

    def append_event(event: dict) -> None:
        with lock:
            with output_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    with sync_playwright() as p:
        launch_kwargs = {"headless": False}
        if args.channel:
            launch_kwargs["channel"] = args.channel
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        page = context.new_page()

        def on_request(request) -> None:
            url = request.url
            if (
                "auth.openai.com/api/accounts/" not in url
                and "sentinel.openai.com/backend-api/sentinel/req" not in url
            ):
                return

            post_data = request.post_data or ""
            append_event(
                {
                    "kind": "request",
                    "ts": time.time(),
                    "method": request.method,
                    "url": url,
                    "headers": request.headers,
                    "post_data": post_data,
                }
            )

            print(f"REQUEST {request.method} {url}")
            if post_data:
                print("REQUEST_BODY", shorten(post_data))

        def on_response(response) -> None:
            url = response.url
            if (
                "auth.openai.com/api/accounts/" not in url
                and "sentinel.openai.com/backend-api/sentinel/req" not in url
            ):
                return

            request = response.request
            try:
                body = response.text()
            except Exception as exc:  # pragma: no cover - 调试脚本容错
                body = f"<unreadable: {exc}>"

            append_event(
                {
                    "kind": "response",
                    "ts": time.time(),
                    "status": response.status,
                    "url": url,
                    "request_method": request.method,
                    "request_headers": request.headers,
                    "request_post_data": request.post_data or "",
                    "response_text": body,
                }
            )

            print(f"RESPONSE {response.status} {url}")
            if request.post_data:
                print("RESPONSE_FOR_REQUEST_BODY", shorten(request.post_data))
            print("RESPONSE_BODY", shorten(body))

            normalized_body = body.replace(" ", "").replace("\n", "")
            if "authorize/continue" in url and '"type":"login_password"' in normalized_body:
                print("DETECTED login_password page; continue until password is submitted.")

            req_body = request.post_data or ""
            if (
                "authorize/continue" in url
                and req_body
                and "screen_hint" not in req_body
                and "username" not in req_body
            ):
                print("DETECTED non-username authorize/continue POST. Password-step sample captured.")
                state["captured_password_step"] = True

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        print(f"Browser opened. Capture file: {output_path.resolve()}")

        deadline = time.time() + args.timeout_seconds
        while time.time() < deadline and not state["captured_password_step"]:
            page.wait_for_timeout(1000)

        if state["captured_password_step"]:
            page.wait_for_timeout(5000)
        else:
            print("Timed out without capturing a non-username authorize/continue POST.")

        browser.close()


if __name__ == "__main__":
    main()
