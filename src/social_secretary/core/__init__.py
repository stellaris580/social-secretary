"""核心层：存储、去重、采集流水线、摘要、ServiceAPI 门面。

对外主要入口：

- :class:`SocialSecretaryService`（实现 ``contracts.ServiceAPI``，UI 只依赖它）
- :class:`Storage`（SQLite 仓储）
- :func:`social_secretary.core.summary.generate_daily_summary`
- :class:`ScanPipeline`（采集编排，通过注入的 ``source_factory`` 解耦采集器）
"""

from .dedupe import (
    MessageDeduplicator,
    compute_content_hash,
    content_hash_for,
    timestamp_bucket,
)
from .demo import (
    DEMO_ACCOUNT_IDS,
    clear_demo_data,
    is_demo_active,
    load_demo_data,
)
from .collector_worker import (
    CollectorWorkerError,
    CollectorWorkerProtocolError,
    CollectorWorkerTimeout,
    WorkerQQMessageSource,
    run_worker_task,
)
from .drafts import ExternalDraftAdapter, TemplateDraftProvider
from .mail_state import StorageSyncStateStore
from .pipeline import (
    NullClassifier,
    NullTodoExtractor,
    ScanPipeline,
    SourceFactory,
)
from .service import (
    DEFAULT_DRAFT_STYLES,
    NotFoundError,
    ServiceError,
    SocialSecretaryService,
    default_source_factory,
    set_default_source_factory,
)
from .settings_store import SettingsStore
from .storage import SCHEMA_VERSION, Storage, local_day_of, now_local, to_iso
from .summary import (
    generate_daily_summary,
    get_cached_summary,
    summary_from_dict,
    summary_to_dict,
)

__all__ = [
    "DEFAULT_DRAFT_STYLES",
    "DEMO_ACCOUNT_IDS",
    "ExternalDraftAdapter",
    "CollectorWorkerError",
    "CollectorWorkerProtocolError",
    "CollectorWorkerTimeout",
    "MessageDeduplicator",
    "NotFoundError",
    "NullClassifier",
    "NullTodoExtractor",
    "SCHEMA_VERSION",
    "ScanPipeline",
    "ServiceError",
    "SettingsStore",
    "SocialSecretaryService",
    "SourceFactory",
    "Storage",
    "StorageSyncStateStore",
    "TemplateDraftProvider",
    "WorkerQQMessageSource",
    "clear_demo_data",
    "compute_content_hash",
    "content_hash_for",
    "default_source_factory",
    "generate_daily_summary",
    "get_cached_summary",
    "is_demo_active",
    "load_demo_data",
    "local_day_of",
    "now_local",
    "run_worker_task",
    "set_default_source_factory",
    "summary_from_dict",
    "summary_to_dict",
    "timestamp_bucket",
    "to_iso",
]
