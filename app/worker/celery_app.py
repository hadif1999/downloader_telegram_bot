import logging

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger, worker_ready

from app.config import get_settings
from app.logging_config import redact_logs

after_setup_logger.connect(redact_logs, weak=False)
after_setup_task_logger.connect(redact_logs, weak=False)

s = get_settings()

celery = Celery("downloader", broker=s.redis_url, include=["app.worker.tasks"])
celery.conf.update(
    worker_concurrency=s.max_parallel_downloads,  # N downloads run in parallel, the rest wait in queue
    worker_prefetch_multiplier=1,  # don't let one worker hoard queued jobs
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_publish_retry=False,
    broker_connection_timeout=5,
    task_time_limit=s.task_time_limit,
    task_soft_time_limit=s.task_time_limit - 60,
    # must exceed the longest task, otherwise Redis redelivers running tasks
    broker_transport_options={"visibility_timeout": s.task_time_limit + 600},
    broker_connection_retry_on_startup=True,
)


@worker_ready.connect(weak=False)
def sweep_download_dir(**_):
    """Remove files left behind by killed workers and apply retention limits at startup."""
    from app.services import storage

    try:
        storage.prune(s.download_dir)
        logging.getLogger(__name__).info("media retention: %s", storage.usage(s.download_dir))
    except Exception:
        logging.getLogger(__name__).exception("download directory sweep failed")
    try:
        from app.services import health

        health.start_heartbeat("worker", disk=s.download_dir)  # for /admin -> System
    except Exception:
        logging.getLogger(__name__).exception("couldn't start the health heartbeat")
