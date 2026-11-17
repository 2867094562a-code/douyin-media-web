import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import security
from app.proxy import BrowserTunnel

async def test_dns_mixed_public_private_fails_closed(monkeypatch):
    records=[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('8.8.8.8',443)),(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',AsyncMock(return_value=records))
    with pytest.raises(security.SSRFSecurityError):await security.resolve_and_verify_dns('www.douyin.com')

async def test_connector_resolver_returns_only_verified_literals(monkeypatch):
    lookup=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('8.8.8.8',443))])
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',lookup)
    records=await security.PublicResolver({'douyin.com'},mode='system').resolve('www.douyin.com')
    assert records[0]['host']=='8.8.8.8' and records[0]['flags']==socket.AI_NUMERICHOST
    assert lookup.await_count==1

async def test_redirect_revalidated_before_second_connection():
    response=SimpleNamespace(status=302,headers={'Location':'https://127.0.0.1/secret'},close=lambda:None)
    http=security.SafeHTTP();http.session=SimpleNamespace(get=AsyncMock(return_value=response))
    with pytest.raises(security.SSRFSecurityError):
        async with http.open('https://www.douyin.com/start'):pass
    assert http.session.get.await_count==1

async def test_tunnel_requires_capability_and_rejects_private_authority():
    async with BrowserTunnel() as tunnel:
        reader,writer=await asyncio.open_connection('127.0.0.1',tunnel.port)
        writer.write(b'CONNECT www.douyin.com:443 HTTP/1.1\r\n\r\n');await writer.drain()
        assert (await reader.read()).startswith(b'HTTP/1.1 407')
        writer.close();await writer.wait_closed()
        reader,writer=await asyncio.open_connection('127.0.0.1',tunnel.port)
        writer.write(f'CONNECT 127.0.0.1:443 HTTP/1.1\r\nProxy-Authorization: {tunnel.auth}\r\n\r\n'.encode());await writer.drain()
        assert await reader.read()==b''
        writer.close();await writer.wait_closed()

def test_rate_limiter_bounded_and_expired(monkeypatch):
    clock=[0];monkeypatch.setattr(security.time,'monotonic',lambda:clock[0])
    limiter=security.InMemoryRateLimiter(max_clients=4)
    for n in range(20):limiter.check_rate_limit(str(n),1)
    assert len(limiter._history)==4
    with pytest.raises(security.RateLimitExceededError):limiter.check_rate_limit('19',1)
    clock[0]=61;limiter.check_rate_limit('fresh',1)
    assert len(limiter._history)==1
