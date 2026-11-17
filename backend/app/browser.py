"""Normal Playwright navigation through an authenticated, public-IP-only tunnel."""
import asyncio
import json
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
from .config import settings
from .security import UpstreamError,validate_url_security
from .proxy import BrowserTunnel
from .extraction import get_post_id,extract_post,parse_html_states

def score(post):return len(post.get('gallery',[]))*20+bool(post.get('video'))*30+bool(post.get('description'))*6+bool(post.get('author'))*3

async def _parse_once(link):
    validate_url_security(link,settings.ALLOWED_DOMAINS)
    states=[];pending=set();matched=asyncio.Event();target=get_post_id(link);navigation_error=False
    async with asyncio.timeout(settings.PAGE_LOAD_TIMEOUT_SECONDS+15):
        async with BrowserTunnel() as tunnel:
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,channel=settings.CHROMIUM_CHANNEL,proxy=tunnel.browser_proxy,
                    args=['--disable-quic','--disable-background-networking','--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--disk-cache-size=0','--media-cache-size=0'])
                try:
                    context=await browser.new_context(locale='zh-CN',service_workers='block',accept_downloads=False)
                    async def close_socket(ws):await ws.close()
                    await context.route_web_socket('**/*',close_socket)
                    async def check(route):
                        request=route.request
                        try:
                            validate_url_security(request.url,settings.ALLOWED_DOMAINS | settings.ALLOWED_CDN_DOMAINS)
                            if request.method!='GET' or request.resource_type in ('image','media','font'):
                                await route.abort();return
                            # Every redirect is a new browser request; the tunnel also rechecks every CONNECT.
                            await route.continue_()
                        except Exception:
                            try:await route.abort()
                            except Exception:pass
                    await context.route('**/*',check)
                    page=await context.new_page()
                    async def capture(response):
                        try:
                            if len(states)>=40:return
                            ctype=response.headers.get('content-type','')
                            if 'json' not in ctype or not any(s in response.url.lower() for s in ('aweme','detail','note','iteminfo')):return
                            length=int(response.headers.get('content-length') or 0)
                            if length>settings.MAX_PAGE_BYTES:return
                            raw=await response.body()
                            if len(raw)>settings.MAX_PAGE_BYTES:return
                            data=json.loads(raw)
                            if isinstance(data,(dict,list)):
                                states.append(data)
                                current=get_post_id(page.url) or target
                                post=extract_post(data,current) if current else None
                                if post and (post.get('gallery') or post.get('video')):matched.set()
                        except Exception:pass
                    def receive(response):
                        task=asyncio.create_task(capture(response));pending.add(task);task.add_done_callback(pending.discard)
                    page.on('response',receive)
                    try:
                        await page.goto(link,wait_until='domcontentloaded',timeout=int(settings.PAGE_LOAD_TIMEOUT_SECONDS*1000))
                        try:await asyncio.wait_for(matched.wait(),8)
                        except TimeoutError:pass
                    except Exception:navigation_error=True
                    validate_url_security(page.url,settings.ALLOWED_DOMAINS)
                    final_id=get_post_id(page.url)
                    if not final_id:raise UpstreamError('未跳转到明确作品；可能需要登录、验证或作品不可访问')
                    if target and final_id!=target:raise UpstreamError('跳转作品 ID 与输入不一致，已拒绝解析')
                    html=await page.content()
                    if len(html.encode())>settings.MAX_PAGE_BYTES:raise UpstreamError('页面数据超过安全限制')
                    states.extend(parse_html_states(html))
                    if pending:await asyncio.wait(list(pending),timeout=2)
                    candidates=[post for data in states if (post:=extract_post(data,final_id))]
                    if not candidates:
                        if tunnel.limited:raise UpstreamError('页面流量超过安全限制')
                        raise UpstreamError('未找到精确作品数据；可能需要正常登录/验证、资源不可访问或页面结构变化')
                    post=max(candidates,key=score)
                    if not post.get('gallery') and not post.get('video'):raise UpstreamError('页面未提供图片或普通 MP4 播放资源')
                    kind='note' if '/note/' in urlsplit(page.url).path else 'video'
                    post.update(id=final_id,source_url=f'https://www.douyin.com/{kind}/{final_id}',warning='原图与普通播放资源优先；无法保证去除平台嵌入的水印。直链可能过期。')
                    return post
                finally:
                    for task in pending:task.cancel()
                    await asyncio.gather(*list(pending),return_exceptions=True)
                    await browser.close()


async def parse_link(link):
    """One normal retry for transient loading failures, never solve verification."""
    for attempt in range(2):
        try:
            post = await _parse_once(link)
            post['parse_attempts'] = attempt + 1
            return post
        except UpstreamError as error:
            if attempt or not str(error).startswith('未找到精确作品数据'):
                raise
        except (TimeoutError, OSError):
            if attempt:
                raise UpstreamError('作品加载超时或连接失败，请稍后重试') from None
        await asyncio.sleep(1)
