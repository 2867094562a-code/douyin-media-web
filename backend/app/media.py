"""No-disk media streams. Bounded chunks, original bytes, true progress."""
import asyncio
import json
import time
from urllib.parse import urlsplit
from .config import settings
from .security import SafeHTTP, UpstreamError, validate_url_security

FORMATS={'jpg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif','avif':'image/avif','mp4':'video/mp4'}
def sniff(data,kind):
    if kind=='video':
        if data[4:8]==b'ftyp' and any(b in data[8:32] for b in (b'isom',b'iso2',b'mp4',b'avc',b'dash')):return 'mp4'
        raise UpstreamError('源站未返回普通 MP4 文件，可能是验证页、HLS 或不支持的媒体')
    if data.startswith(b'\xff\xd8\xff'):return 'jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'png'
    if data.startswith((b'GIF87a',b'GIF89a')):return 'gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return 'webp'
    if data[4:8]==b'ftyp' and any(b in data[8:32] for b in (b'avif',b'avis')):return 'avif'
    raise UpstreamError('源站未返回受支持的图片文件，可能需要验证或资源已过期')

def make_assets(post):
    assets=[]
    def add(kind,label,urls):
        safe=[]
        for url in urls[:12]:
            try:validate_url_security(url,settings.ALLOWED_DOMAINS | settings.ALLOWED_CDN_DOMAINS)
            except ValueError:continue
            if url not in safe:safe.append(url)
        if safe:assets.append({'id':str(len(assets)),'kind':kind,'label':label,'urls':safe,'url':safe[0]})
    for i,urls in enumerate(post.get('gallery',[])[:settings.MAX_ASSETS_PER_POST],1):add('image',f'image_{i:02d}',urls)
    if post.get('video'):add('video','video',post['video'])
    covers=post.get('cover') or (post.get('gallery') or [[]])[0]
    if covers:add('cover','cover',covers)
    return assets

def metadata(post):
    return {k:post.get(k,'') for k in ('id','source_url','title','description','author','warning')} | {
        'image_count':sum(a['kind']=='image' for a in post['assets']),
        'has_video':any(a['kind']=='video' for a in post['assets']),
        'exported_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        'note':'仅含公开作品元数据，不保存 Cookie 或带签名的媒体直链。'}

async def asset_stream(http,asset,post,job):
    last='源资源不可访问'
    for url in asset['urls'][:8]:
        started=False
        try:
            async with http.open(url,{'Referer':post['source_url'],'User-Agent':'Mozilla/5.0'}) as r:
                if r.status!=200:raise UpstreamError(f'源站返回 HTTP {r.status}；请重新解析或使用源直链')
                if r.headers.get('Content-Encoding','identity') not in ('identity',''):raise UpstreamError('不支持压缩传输的媒体响应')
                expected=r.content_length
                if expected is not None and expected>settings.MAX_SINGLE_FILE_BYTES:raise UpstreamError('文件超过单文件大小限制')
                try:first=await r.content.readexactly(32)
                except asyncio.IncompleteReadError:raise UpstreamError('源文件过短或不完整') from None
                ext=sniff(first,asset['kind'])
                job['filename']=post['id']+'_'+asset['label']+'.'+ext
                job['media_type']=FORMATS[ext]
                job['expected_bytes']=expected
                received=0;started=True
                async def account(chunk):
                    nonlocal received
                    received+=len(chunk)
                    if received>settings.MAX_SINGLE_FILE_BYTES or job['source_bytes']+len(chunk)>settings.MAX_TASK_TOTAL_BYTES:
                        raise UpstreamError('传输超过文件或总流量限制')
                    job['source_bytes']+=len(chunk);job['updated']=time.monotonic()
                    return chunk
                yield await account(first)
                async for chunk in r.content.iter_chunked(65536):yield await account(chunk)
                if expected is not None and received!=expected:raise UpstreamError('源文件传输不完整')
                job['files_done']+=1
                return
        except (asyncio.CancelledError,GeneratorExit):raise
        except Exception as exc:
            if started:raise UpstreamError('媒体传输中断或超过安全限制，请重新解析后重试') from None
            last=str(exc) if isinstance(exc,UpstreamError) else f'源站连接失败（{type(exc).__name__}），请重新解析或检查后端网络'
    raise UpstreamError(last)


async def transfer_stream(job, semaphore):
    """A single original file, consumed entirely by one response task."""
    job['status'] = 'waiting'
    try:
        async with asyncio.timeout(settings.STREAM_TIMEOUT_SECONDS):
            async with semaphore:
                job['status'] = 'streaming'
                async with SafeHTTP() as http:
                    async for chunk in asset_stream(http, job['asset'], job['post'], job):
                        yield chunk
                job['status'] = 'completed'
    except (asyncio.CancelledError, GeneratorExit):
        job['status'] = 'cancelled'
        job['error'] = '浏览器已断开或传输已取消'
        raise
    except Exception as exc:
        job['status'] = 'failed'
        job['error'] = str(exc) if isinstance(exc, UpstreamError) else '传输超时或源站连接中断'
        raise UpstreamError(job['error']) from None
    finally:
        job['updated'] = time.monotonic()
