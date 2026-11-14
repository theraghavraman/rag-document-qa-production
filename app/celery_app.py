"""Idempotent, late-acked ingestion and periodic DB-driven recovery."""
from celery import Celery
from app.config import get_settings

s = get_settings()
celery_app = Celery('rag_platform', broker=s.redis_url, include=['app.tasks'])
celery_app.conf.update(
    task_serializer='json', accept_content=['json'], task_ignore_result=True,
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True, task_track_started=True,
    task_time_limit=1800, task_soft_time_limit=1500, timezone='UTC',
    broker_transport_options={'visibility_timeout': 3600},
    beat_schedule={
        'dispatch-outbox': {'task': 'app.tasks.dispatch_pending', 'schedule': 45},
        'garbage-collect': {'task': 'app.tasks.garbage_collect', 'schedule': 3600},
    },
)
