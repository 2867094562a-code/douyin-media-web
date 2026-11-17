"""Single-worker bounded in-memory jobs. Media bytes never touch server disk."""
import asyncio
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
import anyio
from pydantic import BaseModel, Field, ConfigDict
from .config import settings
from .security import rate_limiter, RateLimitExceededError, UpstreamError, validate_url_security
from .extraction import extract_share_links
from .browser import parse_link
from .media import make_assets, metadata, transfer_stream

class JobStore:
    def __init__(self):self.jobs={};self.tasks={}
    def cleanup(self):
        now=time.monotonic()
        for key,job in list(self.jobs.items()):
            if job['status'] not in ('running','streaming','waiting') and now-job['updated']>settings.JOB_TTL_SECONDS:
                self.jobs.pop(key,None)
                task=self.tasks.pop(key,None)
                if task and not task.done():task.cancel()
    def create(self,kind):
        self.cleanup()
        if len(self.jobs)>=settings.MAX_IN_MEMORY_JOBS:
            terminal_jobs = [k for k, j in self.jobs.items() if j['kind'] == 'transfer' and j['status'] in ('completed', 'failed', 'cancelled')]
            for k in terminal_jobs:
                self.jobs.pop(k, None)
                if len(self.jobs) < settings.MAX_IN_MEMORY_JOBS:
                    break
        if len(self.jobs)>=settings.MAX_IN_MEMORY_JOBS:raise HTTPException(429,'当前任务数已达上限，请稍后重试')
        key=secrets.token_urlsafe(24);now=time.monotonic()
        job={'id':key,'kind':kind,'status':'queued','created':now,'updated':now,'bytes':0,'source_bytes':0,'expected_bytes':None,'files_done':0,'error':None}
        self.jobs[key]=job;return job
    def get(self,key):
        self.cleanup()
        if key not in self.jobs:raise HTTPException(404,'任务不存在或已过期，请重新解析')
        return self.jobs[key]

store=JobStore();parse_slots=asyncio.Semaphore(settings.MAX_CONCURRENT_PARSES);stream_slots=asyncio.Semaphore(settings.MAX_CONCURRENT_DOWNLOADS)

@asynccontextmanager
async def lifespan(app):
    async def sweep():
        while True:await asyncio.sleep(30);store.cleanup()
    cleaner=asyncio.create_task(sweep())
    yield
    cleaner.cancel()
    for task in store.tasks.values():task.cancel()
    await asyncio.gather(cleaner,*store.tasks.values(),return_exceptions=True)

app=FastAPI(title='抖音媒体整理器 API',version=settings.VERSION,lifespan=lifespan)

@app.middleware('http')
async def protection(request,call_next):
    length=request.headers.get('content-length','0')
    if not length.isdigit():return JSONResponse({'detail':'请求体长度无效'},400)
    if len(length)>10 or int(length)>65536:return JSONResponse({'detail':'请求体过大'},413)
    if request.method=='POST':
        body=bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>65536:return JSONResponse({'detail':'请求体过大'},413)
        except TimeoutError:return JSONResponse({'detail':'请求体接收超时'},408)
        request._body=bytes(body)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    return response

def limit(request,bucket,amount):
    try:rate_limiter.check_rate_limit(f'{request.client.host if request.client else "unknown"}:{bucket}',amount)
    except RateLimitExceededError as exc:raise HTTPException(429,str(exc),headers={'Retry-After':'60'}) from None

async def authorized(request:Request):
    origin=request.headers.get('origin')
    if origin and origin not in settings.CORS_ORIGINS:raise HTTPException(403,'此网页来源不在后端 CORS 配置中')
    if settings.API_KEY and not hmac.compare_digest(request.headers.get('authorization','').encode(),f'Bearer {settings.API_KEY}'.encode()):
        raise HTTPException(401,'后端访问密钥缺失或不正确')
    limit(request,'api',240)

AUTH=[Depends(authorized)]
class ParseRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=20000)
class AssetSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    post_id:str=Field(max_length=25)
    asset_id:str=Field(max_length=5)
class DownloadRequest(AssetSelection):
    parse_job_id: str = Field(max_length=64)

def public_job(job):
    return {k:job[k] for k in ('id','kind','status','bytes','expected_bytes','files_done','error')} | {
        k:job[k] for k in ('total','completed','items','errors','total_files') if k in job}

async def run_parse(job,links):
    try:
        async with parse_slots:
            job['status']='running'
            for i,link in enumerate(links):
                try:
                    post=await parse_link(link)
                    if len(post.get('gallery',[]))>settings.MAX_ASSETS_PER_POST:raise UpstreamError('图集数量超过安全上限，请单独处理较小作品')
                    post['assets']=make_assets(post)
                    for k in ('gallery','video','cover'):post.pop(k,None)
                    if not post['assets']:raise UpstreamError('所有媒体地址均被安全策略拒绝或已失效')
                    size=len(__import__('json').dumps(post,ensure_ascii=False).encode())
                    if size>262144 or job.get('result_bytes',0)+size>1048576:raise UpstreamError('作品解析数据超过内存安全限制，请减小批次')
                    if not any(p['id']==post['id'] for p in job['items']):
                        job['items'].append(post);job['result_bytes']=job.get('result_bytes',0)+size
                except Exception as exc:
                    reason=str(exc) if isinstance(exc,UpstreamError) else '连接失败、解析超时或安全校验拒绝；请重试或正常访问作品确认权限'
                    job['errors'].append({'index':i+1,'reason':reason})
                job['completed']=i+1;job['updated']=time.monotonic()
            job['status']='partial' if job['items'] and job['errors'] else 'completed' if job['items'] else 'failed'
    except asyncio.CancelledError:job['status']='cancelled';raise
    finally:job['updated']=time.monotonic()

@app.get('/api/health')
async def health():
    return {'service':'douyin-media','version':settings.VERSION,'requires_api_key':bool(settings.API_KEY),'limits':{'batch':settings.MAX_BATCH_URLS,'file_bytes':settings.MAX_SINGLE_FILE_BYTES,'total_bytes':settings.MAX_TASK_TOTAL_BYTES},'storage':'memory-metadata-only'}

@app.post('/api/parse',status_code=202,dependencies=AUTH)
async def start_parse(body:ParseRequest,request:Request):
    limit(request,'parse',settings.RATE_LIMIT_PARSE_PER_MINUTE)
    links=extract_share_links(body.text.replace('https\\://','https://'))
    if not links:raise HTTPException(400,'未找到有效抖音作品链接，请粘贴完整分享内容或 HTTPS 链接')
    if len(links)>settings.MAX_BATCH_URLS:raise HTTPException(400,f'每批最多 {settings.MAX_BATCH_URLS} 个链接')
    for url in links:
        try:validate_url_security(url,settings.ALLOWED_DOMAINS)
        except ValueError:raise HTTPException(400,'仅接受白名单内、不含凭据的 HTTPS 抖音链接') from None
    job=store.create('parse');job.update(total=len(links),completed=0,items=[],errors=[])
    store.tasks[job['id']]=asyncio.create_task(run_parse(job,links))
    return public_job(job)

@app.get('/api/jobs/{key}',dependencies=AUTH)
async def get_job(key:str):return public_job(store.get(key))

@app.delete('/api/jobs/{key}',dependencies=AUTH)
async def cancel_job(key:str):
    job=store.get(key)
    if job['kind']!='parse':
        if job['status'] in ('completed','failed','cancelled','ready'):
            store.jobs.pop(key,None)
            return {'id':key,'status':'removed'}
        raise HTTPException(409,'请先停止浏览器中的文件传输')
    task=store.tasks.get(key)
    if task and not task.done():task.cancel()
    job['status']='cancelled';return public_job(job)

@app.get('/api/jobs/{key}/posts/{post_id}/metadata',dependencies=AUTH)
async def get_metadata(key:str,post_id:str):
    job=store.get(key)
    post=next((p for p in job.get('items',[]) if p['id']==post_id),None)
    if not post:raise HTTPException(404,'作品不存在')
    return metadata(post)

@app.post('/api/downloads',status_code=201,dependencies=AUTH)
async def start_download(body:DownloadRequest,request:Request):
    limit(request,'download',settings.RATE_LIMIT_DOWNLOAD_PER_MINUTE)
    parse=store.get(body.parse_job_id)
    if parse['kind']!='parse' or parse['status'] not in ('completed','partial'):raise HTTPException(409,'请等待作品解析结束')
    post = next((p for p in parse['items'] if p['id'] == body.post_id), None)
    asset = next((a for a in post['assets'] if a['id'] == body.asset_id), None) if post else None
    if not asset:
        raise HTTPException(400, '资源不存在，请重新解析')
    job = store.create('transfer')
    job.update(post=post, asset=asset, total_files=1, status='ready')
    return public_job(job) | {'download_path': f'/api/transfers/{job["id"]}/file'}


class TransferResponse(Response):
    """Preflight and consume upstream in the same task, including cancellation.

    A preflight generator resumed by another ASGI task can misdirect upstream
    timeout/cancellation scopes. Keep all context managers in one streaming task.
    """
    def __init__(self, job):
        super().__init__(content=None)
        self.job = job

    async def __call__(self, scope, receive, send):
        async def stream_body():
            job = self.job
            stream = transfer_stream(job, stream_slots)
            started = False
            try:
                first = await anext(stream)
                headers = {
                    'Content-Disposition': f'attachment; filename="{job["filename"]}"',
                    'Content-Type': job['media_type'],
                    'Cache-Control': 'no-store',
                    'X-Content-Type-Options': 'nosniff',
                }
                if job['expected_bytes'] is not None:
                    headers['Content-Length'] = str(job['expected_bytes'])
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
                started = True
                job['bytes'] += len(first)
                await send({'type': 'http.response.body', 'body': first, 'more_body': True})
                async for chunk in stream:
                    job['bytes'] += len(chunk)
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            except UpstreamError:
                if not started:
                    await JSONResponse({'detail': job['error'] or '源资源不可用'}, status_code=502)(scope, receive, send)
                else:
                    raise
            finally:
                await stream.aclose()

        async with anyio.create_task_group() as group:
            async def drive():
                try:
                    await stream_body()
                finally:
                    group.cancel_scope.cancel()
            group.start_soon(drive)
            while True:
                if (await receive())['type'] == 'http.disconnect':
                    group.cancel_scope.cancel()
                    break


@app.get('/api/transfers/{key}/file')
async def get_file(key: str, request: Request):
    # Single-use random capability; API keys and user cookies never enter URLs.
    limit(request, 'file', settings.RATE_LIMIT_DOWNLOAD_PER_MINUTE)
    job = store.get(key)
    if job['kind'] != 'transfer' or job['status'] != 'ready':
        raise HTTPException(409, '下载任务已使用、不可用或过期，请重新创建')
    if time.monotonic() - job['created'] > 120:
        raise HTTPException(410, '下载入口已过期，请重新点击下载')
    job['status'] = 'waiting'
    return TransferResponse(job)

# CORS wraps error responses from the body-limit middleware too.
app.add_middleware(CORSMiddleware,allow_origins=settings.CORS_ORIGINS,allow_credentials=False,allow_methods=['GET','POST','DELETE'],allow_headers=['Authorization','Content-Type'],expose_headers=['Content-Length','Content-Disposition'])
