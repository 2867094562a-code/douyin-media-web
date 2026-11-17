# -*- coding: utf-8 -*-
"""前端受控 UI 夹具与边缘行为测试 (Controlled UI Mock Fixture Tests)
本测试仅用于在 CI 环境中复现与验证前端边界逻辑（通过路由与浏览器受控模拟）：
1. requires_api_key 为 True 且无 key 时保持未连接并提示输入密钥。
2. 异步 config.json 读取延迟时，用户手动连接不被覆盖的竞态测试。
3. 默认后端旧 health 慢响应（无论是 200 成功还是 500 失败）不会覆盖用户手动连接的新状态。
4. 直连图片已知 Content-Length 预检超限时直接中止并报错（严禁偷偷降级中继）。
5. 直连图片无 Content-Length 分块流式传输累积超限时主动中止并报错（严禁偷偷降级中继）。
6. 直连下载过程中用户点击停止按钮，立即中止并不发起后续文件请求。
7. 直连图片响应超时被中止，界面明确提示超时，且绝不降级中继。
8. 直连图片遇到 CORS 跨域限制时平滑降级到服务端兼容流式中继。
9. 取消解析竞态场景：涵盖 pending POST 期间取消、DELETE 失败如实提示不假确认、在途 GET 返回不覆盖取消状态。
【注意：本文件使用受控夹具验证前端边界与竞态防御，绝非真实网络媒体下载成功的证明】。
"""
import asyncio
import json
import socket
from pathlib import Path
import pytest
from playwright.async_api import async_playwright

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

def get_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

@pytest.fixture(scope="module")
def static_server():
    import http.server
    import threading

    port = get_free_port()
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(*args, directory=str(frontend_dir), **kwargs)
    httpd = http.server.HTTPServer(('127.0.0.1', port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.mark.asyncio
async def test_init_backend_requires_api_key(static_server):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("**/config.json", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"apiBaseUrl": "http://127.0.0.1:8999"})
        ))
        await context.route("**/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "service": "douyin-media", "version": "3.0.0",
                "requires_api_key": True,
                "limits": {"batch": 10, "file_bytes": 157286400, "total_bytes": 419430400}
            })
        ))
        page = await context.new_page()
        await page.goto(static_server)
        await page.wait_for_timeout(300)
        conn_text = await page.locator("#connectionText").text_content()
        assert conn_text == "未连接后端"
        notice = await page.locator("#notice").text_content()
        assert "需要访问密钥" in notice
        assert await page.locator("#parseButton").is_disabled()
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_init_backend_async_race_before_config(static_server):
    """测试 config.json 延迟响应时，用户手动连接新地址，晚到的 config 不会覆盖新连接"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        async def slow_config(route):
            await asyncio.sleep(1.0)
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"apiBaseUrl": "http://127.0.0.1:8998"}))
        await context.route("**/config.json", slow_config)

        await context.route("http://127.0.0.1:8997/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "service": "douyin-media", "version": "3.0.0",
                "requires_api_key": False,
                "limits": {"batch": 10, "file_bytes": 157286400, "total_bytes": 419430400}
            })
        ))

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#settingsButton").click()
        await page.locator("#settingsDialog").wait_for(state="visible")
        await page.locator("#apiUrl").fill("http://127.0.0.1:8997")
        await page.locator("#connectButton").click()
        await page.locator("#settingsDialog").wait_for(state="hidden")
        await page.locator("#connectionDot.online").wait_for(state="visible")
        assert await page.locator("#connectionText").text_content() == "后端已连接"

        # 等待 config.json 慢响应返回并处理
        await page.wait_for_timeout(1500)
        assert await page.locator("#connectionText").text_content() == "后端已连接"
        assert await page.locator("#connectionDot.online").is_visible()
        await context.close()
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 500])
async def test_init_backend_old_health_race_condition(static_server, status_code):
    """测试默认后端旧 health 慢响应（无论是 200 成功还是 500 失败）不会冲掉用户手动连接的新后端成功状态"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("**/config.json", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"apiBaseUrl": "http://127.0.0.1:8990"})
        ))
        async def slow_health(route):
            await asyncio.sleep(1.0)
            if status_code == 200:
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                    "limits": {"batch": 10, "file_bytes": 157286400, "total_bytes": 419430400}
                }))
            else:
                await route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "error"}))
        await context.route("http://127.0.0.1:8990/api/health", slow_health)

        await context.route("http://127.0.0.1:8991/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                "limits": {"batch": 10, "file_bytes": 157286400, "total_bytes": 419430400}
            })
        ))

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#settingsButton").click()
        await page.locator("#settingsDialog").wait_for(state="visible")
        await page.locator("#apiUrl").fill("http://127.0.0.1:8991")
        await page.locator("#connectButton").click()
        await page.locator("#settingsDialog").wait_for(state="hidden")
        await page.locator("#connectionDot.online").wait_for(state="visible")
        assert await page.locator("#connectionText").text_content() == "后端已连接"

        # 等待旧 health 慢响应返回（200 或 500）
        await page.wait_for_timeout(1500)
        assert await page.locator("#connectionText").text_content() == "后端已连接"
        assert await page.locator("#connectionDot.online").is_visible()
        # 打开设置窗口，确认当前连接依然是用户手动连接的 8991，没有被旧 200 成功响应篡改
        await page.locator("#settingsButton").click()
        await page.locator("#settingsDialog").wait_for(state="visible")
        assert await page.locator("#apiUrl").input_value() == "http://127.0.0.1:8991"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_direct_image_oversized_no_relay_fallback(static_server):
    """测试已知 Content-Length 直连图片预检超限时直接报错终止，绝不能降级走服务端流式中继"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8996')")
        await context.route("http://127.0.0.1:8996/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 1000, "total_bytes": 2000}})
        ))
        await context.route("http://127.0.0.1:8996/api/parse", lambda r: r.fulfill(
            status=202, content_type="application/json",
            body=json.dumps({"id": "job_oversize", "kind": "parse", "status": "completed", "total": 1, "completed": 1, "items": [], "errors": []})
        ))
        await context.route("http://127.0.0.1:8996/api/jobs/job_oversize", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "id": "job_oversize", "status": "completed", "total": 1, "completed": 1, "errors": [],
                "items": [{
                    "id": "post_oversize", "author": "测试", "title": "超大图片测试",
                    "assets": [{"id": "0", "kind": "image", "label": "image_01", "url": "https://cdn.example.com/oversized.webp"}]
                }]
            })
        ))
        await context.route("https://cdn.example.com/oversized.webp", lambda r: r.fulfill(
            status=200, headers={"Content-Length": "5000", "Content-Type": "image/webp", "Access-Control-Allow-Origin": "*"},
            body=b"RIFF" + b"\x00" * 4996
        ))
        downloads_called = False
        async def catch_downloads(route):
            nonlocal downloads_called
            downloads_called = True
            await route.abort()
        await context.route("http://127.0.0.1:8996/api/downloads", catch_downloads)

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/1111111111111111111")
        await page.locator("#parseButton").click()
        await page.wait_for_selector("article.post")
        await page.locator("article.post .primary").click()
        await page.wait_for_timeout(500)
        notice = await page.locator("#notice").text_content()
        assert "超过安全限制" in notice or "未完成" in notice or "错误" in notice
        assert not downloads_called, "超限必须直接终止，绝不能降级走服务端流式中继！"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_direct_image_unknown_length_stream_oversized(static_server):
    """测试明确无 Content-Length（受控 ReadableStream 分块流）直连图片在累积读取字节超过限制时主动中止并报错，绝不降级中继"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        # 注入针对 chunk_over.webp 的无 Content-Length 受控流式 Response
        await context.add_init_script("""
            const _origFetch = window.fetch;
            window.fetch = async (url, options) => {
                if (typeof url === 'string' && url.includes('chunk_over.webp')) {
                    let chunkCount = 0;
                    const stream = new ReadableStream({
                        pull(controller) {
                            chunkCount++;
                            if (chunkCount === 1) {
                                const b = new Uint8Array(600);
                                b[0] = 0x52; b[1] = 0x49; b[2] = 0x46; b[3] = 0x46;
                                b[8] = 0x57; b[9] = 0x45; b[10] = 0x42; b[11] = 0x50;
                                controller.enqueue(b);
                            } else if (chunkCount === 2) {
                                controller.enqueue(new Uint8Array(600));
                            } else {
                                controller.close();
                            }
                        }
                    });
                    window.__CHUNK_STREAM_TRIGGERED = true;
                    return new Response(stream, {
                        status: 200,
                        headers: new Headers({'Content-Type': 'image/webp'})
                    });
                }
                return _origFetch(url, options);
            };
        """)
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8988')")
        await context.route("http://127.0.0.1:8988/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 1000, "total_bytes": 2000}})
        ))
        await context.route("http://127.0.0.1:8988/api/parse", lambda r: r.fulfill(
            status=202, content_type="application/json",
            body=json.dumps({"id": "job_chunk_over", "kind": "parse", "status": "completed", "total": 1, "completed": 1, "items": [], "errors": []})
        ))
        await context.route("http://127.0.0.1:8988/api/jobs/job_chunk_over", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "id": "job_chunk_over", "status": "completed", "total": 1, "completed": 1, "errors": [],
                "items": [{
                    "id": "post_chunk_over", "author": "测试", "title": "分块超限测试",
                    "assets": [{"id": "0", "kind": "image", "label": "image_01", "url": "https://cdn.example.com/chunk_over.webp"}]
                }]
            })
        ))
        downloads_called = False
        async def catch_downloads(route):
            nonlocal downloads_called
            downloads_called = True
            await route.abort()
        await context.route("http://127.0.0.1:8988/api/downloads", catch_downloads)

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/3333333333333333333")
        await page.locator("#parseButton").click()
        await page.wait_for_selector("article.post")
        await page.locator("article.post .primary").click()
        await page.wait_for_timeout(500)

        assert await page.evaluate("window.__CHUNK_STREAM_TRIGGERED === true")
        notice = await page.locator("#notice").text_content()
        assert "超过安全限制" in notice or "未完成" in notice or "错误" in notice
        assert not downloads_called, "分块读取累加超限必须直接终止，绝不能降级走服务端流式中继！"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_direct_image_stopped_by_user(static_server):
    """测试用户在直连下载过程中点击停止按钮，立即中止当前请求并停止后续排队文件"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8987')")
        await context.route("http://127.0.0.1:8987/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 100000, "total_bytes": 200000}})
        ))
        await context.route("http://127.0.0.1:8987/api/parse", lambda r: r.fulfill(
            status=202, content_type="application/json",
            body=json.dumps({"id": "job_stop", "kind": "parse", "status": "completed", "total": 1, "completed": 1, "items": [], "errors": []})
        ))
        await context.route("http://127.0.0.1:8987/api/jobs/job_stop", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "id": "job_stop", "status": "completed", "total": 1, "completed": 1, "errors": [],
                "items": [{
                    "id": "post_stop", "author": "测试", "title": "停止下载测试",
                    "assets": [
                        {"id": "0", "kind": "image", "label": "image_01", "url": "https://cdn.example.com/slow_file.webp"},
                        {"id": "1", "kind": "image", "label": "image_02", "url": "https://cdn.example.com/file2.webp"}
                    ]
                }]
            })
        ))
        async def slow_stream(route):
            await asyncio.sleep(0.8)
            try:
                await route.fulfill(
                    status=200, headers={"Content-Type": "image/webp", "Content-Length": "200", "Access-Control-Allow-Origin": "*"},
                    body=b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 188
                )
            except Exception:
                pass
        await context.route("https://cdn.example.com/slow_file.webp", slow_stream)

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/4444444444444444444")
        await page.locator("#parseButton").click()
        await page.wait_for_selector("article.post")
        await page.wait_for_timeout(300)

        second_file_download_requested = False
        async def catch_file2_download(route):
            nonlocal second_file_download_requested
            second_file_download_requested = True
            await route.abort()
        await context.route("https://cdn.example.com/file2.webp", catch_file2_download)

        await page.locator("article.post .primary").click()

        stop_btn = page.locator("#stopDownload")
        await stop_btn.wait_for(state="visible")
        await stop_btn.click()
        await page.wait_for_timeout(1000)

        notice = await page.locator("#notice").text_content()
        assert "已停止" in notice or "已取消" in notice or "未开始" in notice
        assert not second_file_download_requested, "停止后后续文件绝对不能再发起下载请求！"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_direct_image_timeout_aborted(static_server):
    """测试直连下载超过时限被 abort，页面明确提示超时并中止，绝不降级中继"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        await context.add_init_script("""
            const _origSetTimeout = window.setTimeout;
            window.setTimeout = (fn, delay, ...args) => {
                if (delay === 20000) return _origSetTimeout(fn, 50, ...args);
                return _origSetTimeout(fn, delay, ...args);
            };
        """)
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8986')")
        await context.route("http://127.0.0.1:8986/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 100000, "total_bytes": 200000}})
        ))
        await context.route("http://127.0.0.1:8986/api/parse", lambda r: r.fulfill(
            status=202, content_type="application/json",
            body=json.dumps({"id": "job_timeout", "kind": "parse", "status": "completed", "total": 1, "completed": 1, "items": [], "errors": []})
        ))
        await context.route("http://127.0.0.1:8986/api/jobs/job_timeout", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "id": "job_timeout", "status": "completed", "total": 1, "completed": 1, "errors": [],
                "items": [{
                    "id": "post_timeout", "author": "测试", "title": "超时测试",
                    "assets": [{"id": "0", "kind": "image", "label": "image_01", "url": "https://cdn.example.com/hanging.webp"}]
                }]
            })
        ))
        async def hanging_response(route):
            await asyncio.sleep(0.5)
            try:
                await route.fulfill(status=200, headers={"Content-Type": "image/webp"}, body=b"")
            except Exception:
                pass
        await context.route("https://cdn.example.com/hanging.webp", hanging_response)

        downloads_called = False
        async def catch_downloads(route):
            nonlocal downloads_called
            downloads_called = True
            await route.abort()
        await context.route("http://127.0.0.1:8986/api/downloads", catch_downloads)

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/5555555555555555555")
        await page.locator("#parseButton").click()
        await page.wait_for_selector("article.post")
        await page.locator("article.post .primary").click()
        await page.wait_for_timeout(600)

        meta = await page.locator(".transfer-meta").text_content()
        assert "超时" in meta, f"应展示超时提示，实际文本为: {meta}"
        assert not downloads_called, "超时必须直接中止，绝不能降级走服务端流式中继！"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
async def test_direct_image_cors_fallback_to_relay(static_server):
    """测试直连图片遇到 CORS 跨域错误时，平滑降级为兼容流式中继保存"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8995')")
        await context.route("http://127.0.0.1:8995/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 100000, "total_bytes": 200000}})
        ))
        await context.route("http://127.0.0.1:8995/api/parse", lambda r: r.fulfill(
            status=202, content_type="application/json",
            body=json.dumps({"id": "job_cors", "kind": "parse", "status": "completed", "total": 1, "completed": 1, "items": [], "errors": []})
        ))
        await context.route("http://127.0.0.1:8995/api/jobs/job_cors", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "id": "job_cors", "status": "completed", "total": 1, "completed": 1, "errors": [],
                "items": [{
                    "id": "post_cors", "author": "测试", "title": "CORS降级测试",
                    "assets": [{"id": "0", "kind": "image", "label": "image_01", "url": "https://cdn.example.com/blocked_cors.webp"}]
                }]
            })
        ))
        await context.route("https://cdn.example.com/blocked_cors.webp", lambda r: r.fulfill(
            status=200, headers={"Content-Type": "image/webp"}, body=b"RIFF" + b"\x00" * 28
        ))
        relay_called = False
        async def mock_relay(route):
            nonlocal relay_called
            relay_called = True
            await route.fulfill(status=201, content_type="application/json",
                                body=json.dumps({"id": "transfer_cors", "status": "ready", "download_path": "/api/transfers/cors/file"}))
        await context.route("http://127.0.0.1:8995/api/downloads", mock_relay)
        await context.route("http://127.0.0.1:8995/api/transfers/cors/file", lambda r: r.fulfill(
            status=200, headers={"Content-Type": "image/webp", "Content-Disposition": 'attachment; filename="cors.webp"'},
            body=b"RIFF\x20\x00\x00\x00WEBPVP8 \x14\x00\x00\x00" + b"\x00"*20
        ))
        await context.route("http://127.0.0.1:8995/api/jobs/transfer_cors", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"id": "transfer_cors", "status": "completed", "bytes": 40})
        ))

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/2222222222222222222")
        await page.locator("#parseButton").click()
        await page.wait_for_selector("article.post")
        await page.locator("article.post .primary").click()
        await page.wait_for_timeout(600)
        assert relay_called, "CORS 限制时必须平滑降级（fallback）调用服务端兼容中继！"
        await context.close()
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["pending_post_delete_success", "pending_post_delete_fail", "in_flight_get_cancel"])
async def test_cancel_parse_scenarios(static_server, scenario):
    """测试取消解析的三种关键竞态：
    1. pending_post_delete_success: 慢速 POST /api/parse 期间取消，DELETE 成功，展示'解析已取消'。
    2. pending_post_delete_fail: 慢速 POST 期间取消，DELETE 失败，展示'已停止等待'，不假确认已取消。
    3. in_flight_get_cancel: 慢速 GET /api/jobs 期间取消，DELETE 成功，慢速 GET 返回后不覆盖已取消状态，不添加卡片。
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_init_script("localStorage.setItem('shiying-api-url', 'http://127.0.0.1:8985')")
        await context.route("http://127.0.0.1:8985/api/health", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"service": "douyin-media", "version": "3.0.0", "requires_api_key": False,
                             "limits": {"batch": 10, "file_bytes": 100000, "total_bytes": 200000}})
        ))

        delete_called = False
        async def mock_delete(route):
            nonlocal delete_called
            delete_called = True
            if scenario == "pending_post_delete_fail":
                await route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "DELETE failed"}))
            else:
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "cancelled"}))

        await context.route("http://127.0.0.1:8985/api/jobs/job_cancel_test", mock_delete)

        if scenario in ("pending_post_delete_success", "pending_post_delete_fail"):
            # 慢速 POST 延迟 800ms 返回
            async def slow_post(route):
                await asyncio.sleep(0.8)
                await route.fulfill(status=202, content_type="application/json", body=json.dumps({"id": "job_cancel_test", "status": "queued"}))
            await context.route("http://127.0.0.1:8985/api/parse", slow_post)
        else:
            # in_flight_get_cancel: POST 快速返回，但 GET 轮询延迟 800ms
            await context.route("http://127.0.0.1:8985/api/parse", lambda r: r.fulfill(
                status=202, content_type="application/json", body=json.dumps({"id": "job_cancel_test", "status": "running"})
            ))
            async def slow_get(route):
                await asyncio.sleep(0.8)
                # 慢速 GET 返回时带有一个作品（如果未被拦截，正常会添加卡片）
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "id": "job_cancel_test", "status": "running", "total": 1, "completed": 1, "errors": [],
                    "items": [{
                        "id": "post_in_flight", "author": "测试", "title": "在途卡片",
                        "assets": [{"id": "0", "kind": "video", "label": "video", "url": "https://cdn.example.com/v.mp4"}]
                    }]
                }))
            await context.route("http://127.0.0.1:8985/api/jobs/job_cancel_test", lambda r: mock_delete(r) if r.request.method == "DELETE" else slow_get(r))

        page = await context.new_page()
        await page.goto(static_server)
        await page.locator("#connectionDot.online").wait_for(state="visible")
        await page.locator("#shareInput").fill("https://www.douyin.com/note/6666666666666666666")
        await page.locator("#parseButton").click()

        # 等待取消按钮可用并点击取消
        cancel_btn = page.locator("#cancelParse")
        await cancel_btn.wait_for(state="visible")
        assert not await cancel_btn.is_disabled()
        await cancel_btn.click()

        # 等待慢速请求和取消逻辑完成
        await page.wait_for_timeout(1400)

        parse_state = await page.locator("#parseState").text_content()
        notice = await page.locator("#notice").text_content()

        if scenario == "pending_post_delete_success":
            assert delete_called, "pending POST 期间取消，必须在新 ID 返回后立即调用 DELETE！"
            assert parse_state == "解析已取消"
            assert "解析已取消" in notice
        elif scenario == "pending_post_delete_fail":
            assert delete_called
            assert parse_state == "已停止等待"
            assert "未能确认" in notice, f"DELETE 失败绝不能声称已取消，实际提示: {notice}"
        elif scenario == "in_flight_get_cancel":
            assert delete_called
            assert parse_state == "解析已取消"
            # 关键验证：在途慢速 GET 返回后，绝对不能把卡片加到界面上！
            cards_count = await page.locator("article.post").count()
            assert cards_count == 0, f"在途 GET 返回时不应添加卡片，当前卡片数: {cards_count}"

        await context.close()
        await browser.close()
