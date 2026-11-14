"""Atomic Redis fixed-window throttling; production fails closed on Redis errors."""
import logging
from functools import lru_cache

from fastapi import HTTPException
from redis import Redis
from app.config import get_settings

log = logging.getLogger(__name__)
_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


@lru_cache
def redis_client():
    return Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2,
                          health_check_interval=30, decode_responses=True)


def check_rate_limit(identity: str, per_minute: int) -> None:
    from time import time
    if get_settings().app_env == 'test':
        return
    bucket = int(time()) // 60
    # The identity is server-derived after token authentication.
    try:
        used = redis_client().eval(_LUA, 1, f'rate:{identity}:{bucket}', 120)
    except Exception:
        log.exception('Rate limiter unavailable')
        raise HTTPException(503, 'Rate limiting temporarily unavailable') from None
    if used > per_minute:
        raise HTTPException(429, 'Too many requests. Retry in the next minute.', headers={'Retry-After': '60'})
