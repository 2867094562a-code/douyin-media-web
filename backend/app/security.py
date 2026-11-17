"""Strict outbound HTTPS policy. DNS is checked inside the actual connector resolver.

The connector receives only verified literal public addresses; there is no second
hostname lookup between validation and connection. TLS retains the original SNI.
"""
import asyncio
import ipaddress
import json
import re
import socket
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit, urljoin
from contextlib import asynccontextmanager
import aiohttp
from aiohttp.abc import AbstractResolver
from .config import settings

class SSRFSecurityError(ValueError): pass
class UpstreamError(ValueError): pass
class RateLimitExceededError(ValueError): pass

TRANSITION = tuple(ipaddress.ip_network(n) for n in ('64:ff9b::/96','64:ff9b:1::/48','2002::/16','2001::/32'))
def is_ip_private_or_reserved(value):
    try:
        ip=ipaddress.ip_address(value)
        if isinstance(ip,ipaddress.IPv6Address):
            if ip.ipv4_mapped: return is_ip_private_or_reserved(str(ip.ipv4_mapped))
            if any(ip in net for net in TRANSITION): return True
        return not ip.is_global or ip.is_multicast
    except ValueError: return True

def validate_host_domain(host, domains):
    if not host or not re.fullmatch(r'[a-zA-Z0-9.-]+',host): return False
    try: ipaddress.ip_address(host); return False
    except ValueError: pass
    return any(host.lower()==d or host.lower().endswith('.'+d) for d in domains)

def validate_url_security(url, allowed_domains, allowed_schemes=None):
    if not isinstance(url,str) or len(url)>8192 or re.search(r'[\x00-\x20\x7f\\]',url):
        raise SSRFSecurityError('地址含非法字符或过长')
    try:
        p=urlsplit(url)
        port=p.port
        if p.scheme!='https' or port not in (None,443) or p.username is not None or p.password is not None:
            raise SSRFSecurityError('仅允许无凭据的标准 HTTPS 地址')
        if not validate_host_domain(p.hostname,allowed_domains):
            raise SSRFSecurityError('地址不在受信域名白名单内')
        return p
    except (ValueError,TypeError) as exc:
        if isinstance(exc,SSRFSecurityError): raise
        raise SSRFSecurityError('地址格式无效') from None

def sanitize_url_for_logging(url):
    try:
        p=urlsplit(url)
        return f'{p.scheme}://{p.hostname or ""}{p.path}'
    except ValueError:return '[invalid URL]'

async def resolve_and_verify_dns(hostname,port=443):
    try:
        records=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(hostname,port,type=socket.SOCK_STREAM),5)
        addresses=list(dict.fromkeys(r[4][0] for r in records))
    except (OSError,asyncio.TimeoutError): raise SSRFSecurityError('DNS 解析失败或超时') from None
    if not addresses or any(is_ip_private_or_reserved(ip) for ip in addresses):
        raise SSRFSecurityError('DNS 返回非公网地址；如使用 TUN Fake-IP，请配置可信 DNS 解析器')
    return addresses

class PublicResolver(AbstractResolver):
    def __init__(self,domains,mode=None):
        self.domains=domains;self.mode=mode or settings.DNS_RESOLVER
        self.cache=OrderedDict()
    async def resolve(self,host,port=443,family=socket.AF_INET):
        if port!=443 or not validate_host_domain(host,self.domains):raise SSRFSecurityError('出站连接被安全策略拒绝')
        if self.mode in ('cloudflare','alidns'):
            cached=self.cache.get(host)
            if cached and cached[0]>time.monotonic():addresses=cached[1]
            else:
                # Fixed trusted numeric DoH endpoint; never controlled by an API caller.
                # HTTPS certificate verification remains on, and redirects are forbidden.
                timeout=aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout,trust_env=False) as dns:
                    endpoint='https://1.1.1.1/dns-query' if self.mode=='cloudflare' else 'https://223.5.5.5/resolve'
                    async with dns.get(endpoint,params={'name':host,'type':'A'},headers={'Accept':'application/dns-json'},allow_redirects=False) as r:
                        if r.status!=200:raise SSRFSecurityError('可信 DNS 服务暂不可用')
                        raw=bytearray()
                        async for part in r.content.iter_chunked(4096):
                            raw.extend(part)
                            if len(raw)>65536:raise SSRFSecurityError('DNS 响应过大')
                        try:data=json.loads(raw)
                        except ValueError:raise SSRFSecurityError('DNS 响应格式错误') from None
                addresses=list(dict.fromkeys(a['data'] for a in data.get('Answer',[]) if a.get('type')==1 and isinstance(a.get('data'),str)))
                if data.get('Status')!=0 or not addresses or any(is_ip_private_or_reserved(a) for a in addresses):raise SSRFSecurityError('DNS 未返回合法公网地址')
                self.cache[host]=(time.monotonic()+60,addresses)
                while len(self.cache)>256:self.cache.popitem(last=False)
        else:addresses=await resolve_and_verify_dns(host,port)
        return [{'hostname':host,'host':ip,'port':port,'family':socket.AF_INET6 if ':' in ip else socket.AF_INET,'proto':socket.IPPROTO_TCP,'flags':socket.AI_NUMERICHOST} for ip in addresses]
    async def close(self):self.cache.clear()

class SafeHTTP:
    def __init__(self, domains=None, resolver=None):
        self.domains=domains or settings.ALLOWED_DOMAINS | settings.ALLOWED_CDN_DOMAINS
        self.resolver=resolver or PublicResolver(self.domains)
        self.session=None
    async def __aenter__(self):
        connector=aiohttp.TCPConnector(resolver=self.resolver,use_dns_cache=False,limit=8,limit_per_host=4)
        self.session=aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=settings.STREAM_TIMEOUT_SECONDS,connect=settings.REQUEST_TIMEOUT_SECONDS,sock_read=settings.REQUEST_TIMEOUT_SECONDS),trust_env=False,auto_decompress=False,cookie_jar=aiohttp.DummyCookieJar(),headers={'User-Agent':'Mozilla/5.0'})
        return self
    async def __aexit__(self,*args):
        await self.session.close();await self.resolver.close()
    @asynccontextmanager
    async def open(self,url,headers=None):
        current=url
        # Only explicit harmless headers may leave the service; no browser Cookie/Token.
        allowed={'user-agent','accept','referer','range'}
        clean={k:v for k,v in (headers or {}).items() if k.lower() in allowed}
        clean['Accept-Encoding']='identity'
        for hop in range(settings.MAX_REDIRECT_HOPS+1):
            validate_url_security(current,self.domains)
            response=await self.session.get(current,headers=clean,allow_redirects=False)
            if response.status in (301,302,303,307,308):
                location=response.headers.get('Location');response.close()
                if not location or hop==settings.MAX_REDIRECT_HOPS:raise UpstreamError('重定向无效或次数过多')
                next_url=urljoin(current,location)
                if urlsplit(next_url).hostname!=urlsplit(current).hostname:
                    clean={k:v for k,v in clean.items() if k.lower()!='cookie'}
                current=next_url;continue
            try:yield response
            finally:response.close()
            return
    async def read(self,url,limit,headers=None):
        async with self.open(url,headers) as r:
            if r.status!=200:raise UpstreamError(f'源站返回 HTTP {r.status}')
            if r.content_length is not None and r.content_length>limit:raise UpstreamError('源响应超过大小限制')
            body=bytearray()
            async for chunk in r.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body)>limit:raise UpstreamError('源响应超过大小限制')
            return str(r.url),r.headers,bytes(body)

class InMemoryRateLimiter:
    def __init__(self,max_clients=2048):self._history=OrderedDict();self.max_clients=max_clients
    def check_rate_limit(self,key,limit_per_minute):
        now=time.monotonic()
        while self._history:
            first,(_,last)=next(iter(self._history.items()))
            if now-last<60:break
            self._history.pop(first)
        q,_=self._history.pop(key,(deque(),now))
        while q and q[0]<=now-60:q.popleft()
        self._history[key]=(q,now)
        while len(self._history)>self.max_clients:self._history.popitem(last=False)
        if len(q)>=limit_per_minute:raise RateLimitExceededError('请求过于频繁，请一分钟后重试')
        q.append(now)

rate_limiter=InMemoryRateLimiter()
