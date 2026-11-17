"""独立安全验收：只验证需求约束，不测试实现内部算法。"""
import sys
from pathlib import Path
import pytest
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
from app.security import validate_url_security,SSRFSecurityError,is_ip_private_or_reserved,sanitize_url_for_logging
from app.config import settings

@pytest.mark.parametrize('url',[
 'https://@www.douyin.com/video/1234567890123456789',
 'https://www.douyin.com:bad/video/1234567890123456789',
 'https://www.douyin.com:99999/video/1234567890123456789',
 'https://www.dou\tyin.com/video/1234567890123456789',
 'https://user:secret@www.douyin.com/video/1234567890123456789',
 'https://douyin.com.evil.example/video/1234567890123456789',
])
def test_malformed_and_deceptive_urls_rejected(url):
 with pytest.raises(SSRFSecurityError):validate_url_security(url,settings.ALLOWED_DOMAINS)

@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','100.64.0.1','198.18.0.1','::1','::ffff:10.0.0.1','64:ff9b::a00:1','2002:a00:1::1'])
def test_private_and_transition_addresses_rejected(address):
 assert is_ip_private_or_reserved(address)

def test_logger_never_retains_credentials_or_query():
 value=sanitize_url_for_logging('https://user:secret@www.douyin.com/video/123?token=hidden#private')
 assert all(x not in value for x in ('user','secret','token','hidden','private'))
