"""Loopback-only authenticated CONNECT tunnel for ordinary Chromium HTTPS.

Only allowlisted hosts on port 443 are reachable. DNS is resolved once to verified
public literals before connect; Chromium retains TLS certificate checking/SNI.
This avoids changing the website's normal TLS/session behavior with route.fulfill.
"""
import asyncio
import base64
import contextlib
import hmac
import secrets
from .config import settings
from .security import PublicResolver, validate_url_security

class BrowserTunnel:
    def __init__(self):
        self.password=secrets.token_urlsafe(24)
        self.auth='Basic '+base64.b64encode(('browser:'+self.password).encode()).decode()
        self.domains=settings.ALLOWED_DOMAINS | settings.ALLOWED_CDN_DOMAINS
        self.resolver=PublicResolver(self.domains)
        self.used=0;self.limited=False;self.writers=set();self.tasks=set()
        self.slots=asyncio.Semaphore(16)
    async def __aenter__(self):
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0,limit=8192)
        self.port=self.server.sockets[0].getsockname()[1]
        return self
    @property
    def browser_proxy(self):
        return {'server':f'http://127.0.0.1:{self.port}','username':'browser','password':self.password,'bypass':'<-loopback>'}
    async def __aexit__(self,*args):
        self.server.close();await self.server.wait_closed()
        for writer in list(self.writers):writer.close()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)
        await self.resolver.close()
    async def handle(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task);self.writers.add(writer);remote=None
        try:
            async with asyncio.timeout(settings.PAGE_LOAD_TIMEOUT_SECONDS+15):
                header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
                if len(header)>8192:return
                lines=header.decode('ascii').split('\r\n');parts=lines[0].split(' ')
                headers={}
                for line in lines[1:]:
                    if ':' in line:
                        k,v=line.split(':',1);headers[k.lower()]=v.strip()
                if not hmac.compare_digest(headers.get('proxy-authorization','').encode(),self.auth.encode()):
                    writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="browser"\r\nContent-Length: 0\r\n\r\n');await writer.drain();return
                if len(parts)!=3 or parts[0]!='CONNECT' or parts[2] not in ('HTTP/1.0','HTTP/1.1'):return
                p=validate_url_security('https://'+parts[1],self.domains)
                # No path/query tricks in the CONNECT authority.
                if p.path or p.query or p.fragment or not parts[1].endswith(':443'):return
                records=await self.resolver.resolve(p.hostname,443)
                async with self.slots:
                    last=None
                    for record in records[:3]:
                        try:
                            upstream,remote=await asyncio.wait_for(asyncio.open_connection(record['host'],443,family=record['family']),5)
                            break
                        except OSError as exc:last=exc
                    else:return
                    self.writers.add(remote)
                    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
                    async def pump(source,destination):
                        while chunk:=await source.read(65536):
                            self.used+=len(chunk)
                            if self.used>settings.MAX_PAGE_TOTAL_BYTES:
                                self.limited=True;raise ValueError('browser network budget exhausted')
                            destination.write(chunk);await destination.drain()
                    pumps=[asyncio.create_task(pump(reader,remote)),asyncio.create_task(pump(upstream,writer))]
                    try:
                        done,pending=await asyncio.wait(pumps,return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for pump_task in pumps:pump_task.cancel()
                        await asyncio.gather(*pumps,return_exceptions=True)
        except (Exception,asyncio.CancelledError):pass
        finally:
            for connection in (writer,remote):
                if connection:
                    connection.close();self.writers.discard(connection)
                    with contextlib.suppress(Exception,asyncio.CancelledError):await connection.wait_closed()
            self.tasks.discard(task)
