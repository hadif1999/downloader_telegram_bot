from celery import Celery

from app.config import get_settings

s = get_settings()

celery = Celery("downloader", broker=s.redis_url, include=["app.worker.tasks"])
celery.conf.update(
    worker_concurrency=s.max_parallel_downloads,  # N downloads run in parallel, the rest wait in queue
    worker_prefetch_multiplier=1,  # don't let one worker hoard queued jobs
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_time_limit=s.task_time_limit,
    task_soft_time_limit=s.task_time_limit - 60,
    # must exceed the longest task, otherwise Redis redelivers running tasks
    broker_transport_options={"visibility_timeout": s.task_time_limit + 600},
    broker_connection_retry_on_startup=True,
)
