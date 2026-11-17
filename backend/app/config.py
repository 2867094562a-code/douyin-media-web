"""Environment-only server configuration. No user browser profile or media storage."""
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    VERSION: str = '3.0.0'
    PORT: int = Field(8000, ge=1, le=65535)
    CHROMIUM_CHANNEL: str | None = None
    DNS_RESOLVER: Literal['system', 'cloudflare', 'alidns'] = 'system'
    CORS_ORIGINS: list[str] = ['http://localhost:8080', 'http://127.0.0.1:8080']
    API_KEY: str = ''
    MAX_BATCH_URLS: int = Field(10, ge=1, le=20)
    MAX_IN_MEMORY_JOBS: int = Field(30, ge=2, le=200)
    MAX_CONCURRENT_DOWNLOADS: int = Field(1, ge=1, le=4)
    MAX_CONCURRENT_PARSES: int = Field(1, ge=1, le=2)
    MAX_ASSETS_PER_POST: int = Field(100, ge=1, le=200)
    MAX_SINGLE_FILE_BYTES: int = Field(150 * 1024**2, ge=1024, le=512*1024**2)
    MAX_TASK_TOTAL_BYTES: int = Field(400 * 1024**2, ge=1024, le=1024**3)
    MAX_PAGE_BYTES: int = Field(6_000_000, ge=1024, le=10_000_000)
    MAX_PAGE_TOTAL_BYTES: int = Field(35_000_000, ge=1024, le=60_000_000)
    PAGE_LOAD_TIMEOUT_SECONDS: float = Field(40, gt=0, le=90)
    REQUEST_TIMEOUT_SECONDS: float = Field(20, gt=0, le=60)
    STREAM_TIMEOUT_SECONDS: float = Field(300, gt=0, le=600)
    JOB_TTL_SECONDS: int = Field(1800, ge=1, le=7200)
    MAX_REDIRECT_HOPS: int = Field(5, ge=0, le=8)
    RATE_LIMIT_PARSE_PER_MINUTE: int = Field(10, ge=1, le=100)
    RATE_LIMIT_DOWNLOAD_PER_MINUTE: int = Field(30, ge=1, le=200)
    ALLOWED_DOMAINS: frozenset[str] = frozenset({'douyin.com','iesdouyin.com'})
    ALLOWED_CDN_DOMAINS: frozenset[str] = frozenset({'douyinvod.com','douyinpic.com','douyinstatic.com','bytednsdoc.com','byteimg.com','pstatp.com','douyincdn.com','amemv.com','zijieapi.com','snssdk.com','lf-security.bytegoofy.com','lf-security-backup.bytegoofy.com','lf-c-flwb.bytetos.com','lf-cdn-tos.bytescm.com'})
    ALLOWED_SCHEMES: frozenset[str] = frozenset({'https'})
    @field_validator('CORS_ORIGINS')
    @classmethod
    def exact_origins(cls, values):
        from urllib.parse import urlsplit
        for value in values:
            p=urlsplit(value)
            if '*' in value or p.scheme not in ('https','http') or not p.hostname or p.path or p.query or p.fragment or p.username is not None:
                raise ValueError('CORS_ORIGINS must contain exact origins without paths or wildcards')
        return values

settings = Settings()
