"""Normal Playwright navigation through an authenticated, public-IP-only tunnel."""
import asyncio
import json
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
from .config import settings
from .security import UpstreamError, validate_url_security
from .proxy import BrowserTunnel
from .extraction import get_post_id, extract_post, parse_html_states

def score(post):
    return len(post.get('gallery',[]))*20 + bool(post.get('video'))*30 + bool(post.get('description'))*6 + bool(post.get('author'))*3

def categorize_title(title: str) -> str:
    """安全分类页面标题类别（严格脱敏，绝不输出具体文案或参数）。"""
    t = (title or "").lower().strip()
    if not t:
        return "empty"
    if any(w in t for w in ("验证码", "安全验证", "captcha", "verify", "robot", "滑动", "环境异常", "访问验证")):
        return "security_challenge"
    if any(w in t for w in ("登录", "login", "passport", "sign in")):
        return "login_required"
    if any(w in t for w in ("不存在", "已删除", "404", "not found", "页面不见", "内容已被删除", "作品已被删除")):
        return "not_found"
    if "抖音" in t or "douyin" in t:
        return "douyin_normal"
    return "other_normal"

async def _parse_once(link):
    validate_url_security(link, settings.ALLOWED_DOMAINS)
    states = []
    pending = set()
    matched = asyncio.Event()
    target = get_post_id(link)
    navigation_error = False

    # 结构化安全诊断字典（使用主文档响应，严格有界、不含隐私与 query）
    diag = {
        "http_status": None,
        "nav_stage": "started",
        "title_cat": "unknown",
        "final_id": None,
        "id_in_html": False,
        "states_count": 0,
    }

    async with asyncio.timeout(settings.PAGE_LOAD_TIMEOUT_SECONDS + 15):
        async with BrowserTunnel() as tunnel:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    channel=settings.CHROMIUM_CHANNEL,
                    proxy=tunnel.browser_proxy,
                    args=[
                        '--disable-quic',
                        '--disable-background-networking',
                        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                        '--disk-cache-size=0',
                        '--media-cache-size=0'
                    ]
                )
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale='zh-CN',
                        service_workers='block',
                        accept_downloads=False
                    )

                    async def close_socket(ws):
                        await ws.close()
                    await context.route_web_socket('**/*', close_socket)

                    async def check(route):
                        request = route.request
                        try:
                            validate_url_security(request.url, settings.ALLOWED_DOMAINS | settings.ALLOWED_CDN_DOMAINS)
                            if request.method != 'GET' or request.resource_type in ('image', 'media', 'font'):
                                await route.abort()
                                return
                            await route.continue_()
                        except Exception:
                            try:
                                await route.abort()
                            except Exception:
                                pass
                    await context.route('**/*', check)

                    page = await context.new_page()

                    async def capture(response):
                        try:
                            if len(states) >= 40:
                                return
                            ctype = response.headers.get('content-type', '')
                            if 'json' not in ctype or not any(s in response.url.lower() for s in ('aweme', 'detail', 'note', 'iteminfo')):
                                return
                            length = int(response.headers.get('content-length') or 0)
                            if length > settings.MAX_PAGE_BYTES:
                                return
                            raw = await response.body()
                            if len(raw) > settings.MAX_PAGE_BYTES:
                                return
                            data = json.loads(raw)
                            if isinstance(data, (dict, list)):
                                states.append(data)
                                current = get_post_id(page.url) or target
                                post = extract_post(data, current) if current else None
                                if post and (post.get('gallery') or post.get('video')):
                                    matched.set()
                        except Exception:
                            pass

                    def receive(response):
                        task = asyncio.create_task(capture(response))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    page.on('response', receive)

                    try:
                        # 主文档最终响应
                        main_response = await page.goto(
                            link,
                            wait_until='domcontentloaded',
                            timeout=int(settings.PAGE_LOAD_TIMEOUT_SECONDS * 1000)
                        )
                        if main_response:
                            diag["http_status"] = main_response.status
                        diag["nav_stage"] = "domcontentloaded"
                        try:
                            await asyncio.wait_for(matched.wait(), 8)
                        except TimeoutError:
                            pass
                    except Exception:
                        navigation_error = True
                        diag["nav_stage"] = "navigation_error"

                    validate_url_security(page.url, settings.ALLOWED_DOMAINS)
                    final_id = get_post_id(page.url)
                    diag["final_id"] = final_id

                    raw_title = ""
                    try:
                        raw_title = await page.title()
                    except Exception:
                        pass
                    diag["title_cat"] = categorize_title(raw_title)

                    if navigation_error and not final_id:
                        raise UpstreamError("页面跳转或加载超时，无法连接作品页面")

                    if not final_id:
                        if diag["title_cat"] == "security_challenge":
                            raise UpstreamError("触发平台安全验证，本开源服务不破解验证；请自建或本地运行")
                        if diag["title_cat"] == "login_required":
                            raise UpstreamError("页面跳转至登录限制页；本服务不提取或存储任何用户 Cookie/凭据")
                        raise UpstreamError(f"未跳转到有效作品 ID（页面标题类: {diag['title_cat']}，HTTP: {diag['http_status'] or '未知'}）")

                    if target and final_id != target:
                        raise UpstreamError('跳转作品 ID 与输入不一致，已拒绝解析')

                    html = await page.content()
                    if len(html.encode()) > settings.MAX_PAGE_BYTES:
                        raise UpstreamError('页面数据超过安全限制')

                    diag["id_in_html"] = final_id in html
                    parsed_states = parse_html_states(html)
                    states.extend(parsed_states)
                    diag["states_count"] = len(states)

                    if pending:
                        await asyncio.wait(list(pending), timeout=2)

                    candidates = [post for data in states if (post := extract_post(data, final_id))]
                    if not candidates:
                        if tunnel.limited:
                            raise UpstreamError('页面流量超过安全限制')
                        if diag["title_cat"] == "security_challenge":
                            raise UpstreamError("触发平台安全验证，本开源服务不破解人机验证；请在本地自建运行")
                        if diag["title_cat"] == "login_required":
                            raise UpstreamError("页面提示需要登录才能访问；本服务不提取或存储用户 Cookie/凭据")
                        if diag["title_cat"] == "not_found":
                            raise UpstreamError("页面提示作品不存在或已被作者删除")

                        # 严格有界的脱敏诊断信息：仅报告 HTTP 状态、标题类别、状态数与是否包含 ID，绝无 query/Cookie/正文
                        raise UpstreamError(f"未匹配到作品数据（HTTP: {diag['http_status'] or '未知'}，标题类: {diag['title_cat']}，状态数: {diag['states_count']}，HTML含ID: {diag['id_in_html']}）")

                    post = max(candidates, key=score)
                    if not post.get('gallery') and not post.get('video'):
                        raise UpstreamError('页面未提供图片或普通 MP4 播放资源')

                    kind = 'note' if '/note/' in urlsplit(page.url).path else 'video'
                    post.update(
                        id=final_id,
                        source_url=f'https://www.douyin.com/{kind}/{final_id}',
                        warning='原图与普通播放资源优先；无法保证去除平台嵌入的水印。直链可能过期。'
                    )
                    return post
                finally:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*list(pending), return_exceptions=True)
                    await browser.close()


async def parse_link(link):
    """One normal retry for transient loading failures, never solve verification."""
    for attempt in range(2):
        try:
            post = await _parse_once(link)
            post['parse_attempts'] = attempt + 1
            return post
        except UpstreamError as error:
            msg = str(error)
            if any(k in msg for k in ("安全验证", "人机验证", "登录", "不存在", "已被删除")):
                raise
            if attempt or not msg.startswith('未匹配到作品数据'):
                raise
        except (TimeoutError, OSError):
            if attempt:
                raise UpstreamError('作品加载超时或连接失败，请稍后重试') from None
        await asyncio.sleep(1)
