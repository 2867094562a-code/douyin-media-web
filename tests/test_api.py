import asyncio
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from app import main, media
from app.config import settings

POST_ID = '1234567890123456789'
URL = 'https://www.douyin.com/note/' + POST_ID

@pytest.fixture
def png():
    out = io.BytesIO()
    Image.new('RGB', (4, 3), 'green').save(out, format='PNG')
    return out.getvalue()

@pytest.fixture(autouse=True)
def isolated(monkeypatch, png):
    main.store.jobs.clear()
    main.store.tasks.clear()
    main.rate_limiter._history.clear()
    monkeypatch.setattr(settings, 'API_KEY', '')

    async def parse(url):
        if '999999' in url:
            raise media.UpstreamError('测试作品不可访问')
        return {
            'id': POST_ID,
            'source_url': URL,
            'title': '测试作品',
            'description': '<script>不执行</script>\n测试文案',
            'author': '匿名作者',
            'gallery': [['https://p3.douyinpic.com/test.png']],
            'video': [],
            'cover': [],
            'warning': '不能保证无水印'
        }
    monkeypatch.setattr(main, 'parse_link', parse)

    class Content:
        def __init__(self, data):
            self.data = data
            self.pos = 0
        async def readexactly(self, n):
            self.pos = n
            return self.data[:n]
        async def iter_chunked(self, n):
            for start in range(self.pos, len(self.data), n):
                yield self.data[start:start+n]

    class HTTP:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @asynccontextmanager
        async def open(self, *args, **kwargs):
            yield SimpleNamespace(status=200, headers={}, content_length=len(png), content=Content(png))

    monkeypatch.setattr(media, 'SafeHTTP', HTTP)

@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as c:
        yield c

async def parsed(client, text=URL):
    response = await client.post('/api/parse', json={'text': text})
    assert response.status_code == 202, response.text
    key = response.json()['id']
    for _ in range(100):
        job = (await client.get('/api/jobs/' + key)).json()
        if job['status'] in ('completed', 'failed', 'partial'):
            return job
        await asyncio.sleep(.001)
    pytest.fail('test job did not finish')

async def test_parse_and_metadata_and_serial_download_no_disk(client, png, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = await parsed(client)
    assert job['status'] == 'completed'
    post = job['items'][0]
    assert post['author'] == '匿名作者'
    assert post['assets'][0]['url'].endswith('test.png')

    # 测试单独获取 JSON 元数据 (不含敏感 cookie 或签名直链)
    meta_resp = await client.get(f'/api/jobs/{job["id"]}/posts/{POST_ID}/metadata')
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta['author'] == '匿名作者'
    assert 'urls' not in meta
    assert meta['id'] == POST_ID
    assert 'exported_at' in meta

    # 测试单资源串行下载 (无 ZIP 纯流式转发)
    r = await client.post('/api/downloads', json={
        'parse_job_id': job['id'],
        'post_id': POST_ID,
        'asset_id': '0'
    })
    assert r.status_code == 201, r.text
    transfer = r.json()
    assert transfer['status'] == 'ready'

    # 获取流式文件
    file = await client.get(transfer['download_path'])
    assert file.status_code == 200
    assert file.content == png
    assert file.headers['content-type'].startswith('image/png')
    assert 'image_01.png' in file.headers['content-disposition']

    # 验证传输完成状态与字节记录
    progress = (await client.get('/api/jobs/' + transfer['id'])).json()
    assert progress['status'] == 'completed'
    assert progress['bytes'] == len(png)
    assert progress['files_done'] == 1

    # 验证一次性下载 Capability 机制：再次请求应返回 409 已使用
    assert (await client.get(transfer['download_path'])).status_code == 409

    # 关键安全约束：验证服务端磁盘零媒体落盘
    assert list(tmp_path.iterdir()) == []

async def test_single_download_original_bytes_and_extension(client, png):
    job = await parsed(client)
    r = await client.post('/api/downloads', json={
        'parse_job_id': job['id'],
        'post_id': POST_ID,
        'asset_id': '0'
    })
    assert r.status_code == 201
    file = await client.get(r.json()['download_path'])
    assert file.status_code == 200
    assert file.content == png
    assert file.headers['content-type'].startswith('image/png')
    assert 'image_01.png' in file.headers['content-disposition']
    state = (await client.get('/api/jobs/' + r.json()['id'])).json()
    assert state['bytes'] == len(png)
    assert state['expected_bytes'] == len(png)

async def test_partial_batch_and_failed_item(client):
    job = await parsed(client, URL + '\nhttps://www.douyin.com/video/9999999999999999999')
    assert job['status'] == 'partial' and len(job['items']) == 1
    assert job['errors'] == [{'index': 2, 'reason': '测试作品不可访问'}]

async def test_invalid_input_and_no_arbitrary_download_url(client):
    assert (await client.post('/api/parse', json={'text': 'hello'})).status_code == 400
    assert (await client.post('/api/parse', json={'text': 'https://u:p@www.douyin.com/video/123'})).status_code == 400
    assert (await client.post('/api/downloads', json={'parse_job_id': 'x', 'url': 'http://127.0.0.1/secret'})).status_code == 422
    assert (await client.post('/api/parse', content='x' * 65537)).status_code == 413
    assert (await client.get('/api/jobs/not-a-real-capability')).status_code == 404

async def test_key_and_cors(client, monkeypatch):
    monkeypatch.setattr(settings, 'API_KEY', 'synthetic-test-key')
    assert (await client.post('/api/parse', json={'text': URL})).status_code == 401
    assert (await client.post('/api/parse', json={'text': URL}, headers={'Authorization': 'Bearer synthetic-test-key', 'Origin': 'https://evil.example'})).status_code == 403
    response = await client.options('/api/parse', headers={'Origin': 'http://localhost:8080', 'Access-Control-Request-Method': 'POST'})
    assert response.headers['access-control-allow-origin'] == 'http://localhost:8080'
    response = await client.options('/api/parse', headers={'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'POST'})
    assert 'access-control-allow-origin' not in response.headers

async def test_ttl_and_limits(client, monkeypatch):
    job = await parsed(client)
    main.store.jobs[job['id']]['updated'] -= settings.JOB_TTL_SECONDS + 1
    assert (await client.get('/api/jobs/' + job['id'])).status_code == 404
    monkeypatch.setattr(settings, 'RATE_LIMIT_PARSE_PER_MINUTE', 1)
    assert (await client.post('/api/parse', json={'text': URL})).status_code == 429 # prior parse already consumed this window

async def test_size_limit_never_reports_success(client, monkeypatch):
    job = await parsed(client)
    monkeypatch.setattr(settings, 'MAX_SINGLE_FILE_BYTES', 1)
    r = await client.post('/api/downloads', json={
        'parse_job_id': job['id'],
        'post_id': POST_ID,
        'asset_id': '0'
    })
    assert r.status_code == 201
    file = await client.get(r.json()['download_path'])
    assert file.status_code == 502
    state = (await client.get('/api/jobs/' + r.json()['id'])).json()
    assert state['status'] == 'failed'
    assert state['bytes'] == 0

async def test_job_capacity_and_unknown_asset(client, monkeypatch):
    job = await parsed(client)
    assert (await client.post('/api/downloads', json={
        'parse_job_id': job['id'],
        'post_id': POST_ID,
        'asset_id': '9'
    })).status_code == 400
    monkeypatch.setattr(settings, 'MAX_IN_MEMORY_JOBS', 1)
    assert (await client.post('/api/parse', json={'text': URL})).status_code == 429

async def test_transfer_response_midstream_failure_never_reports_success(client, monkeypatch, png):
    job = await parsed(client)
    # 模拟在首个分块后上游突发网络异常或文件损坏
    class BrokenContent:
        def __init__(self, data):
            self.data = data
            self.pos = 0
        async def readexactly(self, n):
            self.pos = n
            return self.data[:n]
        async def iter_chunked(self, n):
            yield self.data[self.pos:self.pos+n]
            raise media.UpstreamError('模拟上游连接突发中断')

    class BrokenHTTP:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @asynccontextmanager
        async def open(self, *args, **kwargs):
            yield SimpleNamespace(status=200, headers={}, content_length=len(png)*2, content=BrokenContent(png))

    monkeypatch.setattr(media, 'SafeHTTP', BrokenHTTP)
    r = await client.post('/api/downloads', json={
        'parse_job_id': job['id'],
        'post_id': POST_ID,
        'asset_id': '0'
    })
    assert r.status_code == 201
    transfer = r.json()
    # 客户端在读取流时遭遇连接中断
    with pytest.raises(Exception):
        resp = await client.get(transfer['download_path'])
        # 消费流以触发 midstream 异常
        _ = resp.content

    # 严格检验：状态必须是 failed，绝不能假宣称 completed
    state = (await client.get('/api/jobs/' + transfer['id'])).json()
    assert state['status'] == 'failed'
