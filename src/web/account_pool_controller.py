"""
账号池控制器
负责基于外部 CPA 账号池数量进行自动补量与本地账号健康清理。
"""

import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..config.constants import AccountStatus
from ..config.settings import get_settings
from ..core.upload.cpa_upload import batch_upload_to_cpa, get_cpa_pool_count
from ..database import crud
from ..database.models import Account, RegistrationTask
from ..database.session import get_db

logger = logging.getLogger(__name__)

_controller: Optional["AccountPoolController"] = None


def count_active_accounts() -> int:
    """统计本地 active 账号数量。"""
    with get_db() as db:
        return db.query(Account).filter(Account.status == AccountStatus.ACTIVE.value).count()


def list_active_account_ids(limit: int, offset: int) -> List[int]:
    """按稳定顺序分页取 active 账号 ID，用于轮询健康检查。"""
    if limit <= 0:
        return []

    with get_db() as db:
        query = (
            db.query(Account.id)
            .filter(Account.status == AccountStatus.ACTIVE.value)
            .order_by(Account.created_at.asc(), Account.id.asc())
        )
        return [row[0] for row in query.offset(max(offset, 0)).limit(limit).all()]


def validate_account_health(account_id: int) -> Tuple[bool, Optional[str]]:
    """复用现有账号校验逻辑。"""
    from .routes.accounts import do_validate

    return do_validate(account_id, None)


def delete_account_by_id(account_id: int) -> bool:
    """删除本地账号。"""
    with get_db() as db:
        return crud.delete_account(db, account_id)


def resolve_cpa_target_config() -> Optional[Dict[str, Any]]:
    """
    解析当前控制器应读写的 CPA 目标。

    优先级：
    1. 若设置了 account_pool_cpa_service_id，则使用该数据库服务
    2. 否则使用首个启用的数据库 CPA 服务
    3. 若无数据库服务且启用了全局 CPA 配置，则退回全局配置
    """
    settings = get_settings()
    preferred_service_id = max(int(getattr(settings, "account_pool_cpa_service_id", 0) or 0), 0)

    with get_db() as db:
        services = crud.get_cpa_services(db, enabled=True)
        selected = None
        if preferred_service_id:
            selected = next((svc for svc in services if svc.id == preferred_service_id), None)
        elif services:
            selected = services[0]

        if selected:
            return {
                "service_id": selected.id,
                "service_name": selected.name,
                "api_url": selected.api_url,
                "api_token": selected.api_token,
                "include_proxy_url": bool(selected.include_proxy_url),
                "source": "db",
            }

    if settings.cpa_enabled and settings.cpa_api_url and settings.cpa_api_token:
        return {
            "service_id": None,
            "service_name": "global",
            "api_url": settings.cpa_api_url,
            "api_token": settings.cpa_api_token.get_secret_value(),
            "include_proxy_url": False,
            "source": "global",
        }

    return None


async def trigger_registration_fill(
    count: int,
    email_service_type: str,
    interval_min: int,
    interval_max: int,
    concurrency: int,
    cpa_target: Dict[str, Any],
) -> Dict[str, Any]:
    """
    直接在进程内触发一轮补量注册，并在需要时补做全局 CPA 上传。
    """
    batch_id = str(uuid.uuid4())
    task_uuids: List[str] = []

    with get_db() as db:
        for _ in range(count):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(db, task_uuid=task_uuid, proxy=None)
            task_uuids.append(task_uuid)

    cpa_service_ids = [cpa_target["service_id"]] if cpa_target.get("service_id") else []
    from .routes.registration import run_batch_registration

    await run_batch_registration(
        batch_id,
        task_uuids,
        email_service_type,
        None,
        None,
        None,
        interval_min,
        interval_max,
        concurrency,
        mode="pipeline",
        auto_upload_cpa=True,
        cpa_service_ids=cpa_service_ids,
        auto_upload_sub2api=False,
        sub2api_service_ids=[],
        auto_upload_tm=False,
        tm_service_ids=[],
    )

    uploaded_account_ids: List[int] = []
    if cpa_target.get("source") == "global":
        with get_db() as db:
            emails: List[str] = []
            for task_uuid in task_uuids:
                task = crud.get_registration_task_by_uuid(db, task_uuid)
                if not task or task.status != "completed" or not isinstance(task.result, dict):
                    continue
                email = str(task.result.get("email") or "").strip()
                if email:
                    emails.append(email)

            if emails:
                accounts = db.query(Account).filter(Account.email.in_(emails)).all()
                uploaded_account_ids = [account.id for account in accounts if account.access_token]

        if uploaded_account_ids:
            batch_upload_to_cpa(
                uploaded_account_ids,
                api_url=cpa_target["api_url"],
                api_token=cpa_target["api_token"],
                include_proxy_url=bool(cpa_target.get("include_proxy_url")),
            )

    return {
        "batch_id": batch_id,
        "count": count,
        "task_uuids": task_uuids,
        "uploaded_account_ids": uploaded_account_ids,
    }


class AccountPoolController:
    """应用内常驻账号池控制器。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()
        self._health_check_offset = 0
        self._consecutive_failure_count = 0
        self._next_retry_at: Optional[datetime] = None
        self._last_summary: Dict[str, Any] = {
            "running": False,
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "last_error": None,
            "external_available_count": None,
            "local_active_count": None,
            "deleted_account_ids": [],
            "transient_validation_failures": [],
            "triggered_registration_count": 0,
            "triggered_batch_id": None,
            "cpa_target": None,
            "consecutive_failure_count": 0,
            "next_retry_at": None,
        }

    def get_status(self) -> Dict[str, Any]:
        """获取控制器当前状态快照。"""
        snapshot = dict(self._last_summary)
        snapshot["running"] = self._task is not None and not self._task.done()
        snapshot["controller_enabled"] = bool(getattr(get_settings(), "account_pool_enabled", False))
        return snapshot

    async def start_if_enabled(self) -> bool:
        """若配置启用，则启动控制循环。"""
        settings = get_settings()
        if not getattr(settings, "account_pool_enabled", False):
            logger.info("账号池控制器未启用，跳过启动")
            return False
        if int(getattr(settings, "account_pool_target_count", 0) or 0) <= 0:
            logger.info("账号池目标数量未配置或为 0，跳过启动")
            return False
        return await self.start()

    async def start(self) -> bool:
        """启动控制循环。"""
        if self._task and not self._task.done():
            return False
        self._task = asyncio.create_task(self._run_loop(), name="account-pool-controller")
        self._last_summary["running"] = True
        logger.info("账号池控制器已启动")
        return True

    async def stop(self) -> bool:
        """停止控制循环。"""
        if not self._task or self._task.done():
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._last_summary["running"] = False
        logger.info("账号池控制器已停止")
        return True

    async def _run_loop(self) -> None:
        """常驻控制循环。"""
        while True:
            settings = get_settings()
            poll_interval = max(int(getattr(settings, "account_pool_poll_interval_seconds", 300) or 300), 5)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("账号池控制器循环异常: %s", exc)
                self._record_cycle_failure(str(exc), settings)
            await asyncio.sleep(poll_interval)

    async def run_once(self) -> Dict[str, Any]:
        """执行单轮巡检与补量。"""
        async with self._run_lock:
            settings = get_settings()
            now = datetime.now(UTC)

            summary = {
                "last_run_started_at": now.isoformat(),
                "last_run_finished_at": None,
                "last_error": None,
                "external_available_count": None,
                "local_active_count": count_active_accounts(),
                "deleted_account_ids": [],
                "transient_validation_failures": [],
                "triggered_registration_count": 0,
                "triggered_batch_id": None,
                "cpa_target": None,
                "consecutive_failure_count": self._consecutive_failure_count,
                "next_retry_at": self._next_retry_at.isoformat() if self._next_retry_at else None,
            }

            if self._next_retry_at and now < self._next_retry_at:
                summary["last_error"] = "控制器处于退避等待中"
                summary["last_run_finished_at"] = datetime.now(UTC).isoformat()
                self._last_summary.update(summary)
                return summary

            cpa_target = resolve_cpa_target_config()
            if not cpa_target:
                summary["last_error"] = "未找到可用的 CPA 配置"
                self._record_cycle_failure(summary["last_error"], settings, base_summary=summary)
                return self.get_status()

            summary["cpa_target"] = {
                "service_id": cpa_target.get("service_id"),
                "service_name": cpa_target.get("service_name"),
                "source": cpa_target.get("source"),
            }

            external_available_count = await self._fetch_external_available_count(cpa_target, settings)
            summary["external_available_count"] = external_available_count

            deleted_account_ids, transient_failures = self._run_health_check(settings)
            summary["deleted_account_ids"] = deleted_account_ids
            summary["transient_validation_failures"] = transient_failures
            summary["local_active_count"] = count_active_accounts()

            target_count = max(int(getattr(settings, "account_pool_target_count", 0) or 0), 0)
            max_registration_burst = max(int(getattr(settings, "account_pool_max_registration_burst", 1) or 1), 1)
            missing_count = max(target_count - external_available_count, 0)

            if missing_count > 0:
                fill_count = min(missing_count, max_registration_burst)
                fill_result = await self._trigger_fill(fill_count, cpa_target, settings)
                summary["triggered_registration_count"] = fill_count
                summary["triggered_batch_id"] = fill_result.get("batch_id")

            self._consecutive_failure_count = 0
            self._next_retry_at = None
            summary["consecutive_failure_count"] = 0
            summary["next_retry_at"] = None
            summary["last_run_finished_at"] = datetime.now(UTC).isoformat()
            self._last_summary.update(summary)
            return summary

    async def _fetch_external_available_count(self, cpa_target: Dict[str, Any], settings) -> int:
        """通过 CPA 查询外部可用账号池数量。"""

        def _fetch() -> int:
            success, count, error = get_cpa_pool_count(
                api_url=cpa_target["api_url"],
                api_token=cpa_target["api_token"],
            )
            if not success or count is None:
                raise RuntimeError(error or "查询 CPA 外部池数量失败")
            return int(count)

        retries = max(int(getattr(settings, "account_pool_retry_max_retries", 3) or 3), 0)
        base_delay = max(float(getattr(settings, "account_pool_retry_base_delay_seconds", 1) or 1), 0.1)
        max_delay = max(float(getattr(settings, "account_pool_retry_max_delay_seconds", 30) or 30), base_delay)

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                return _fetch()
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    break
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay *= 0.5 + random.random()
                logger.warning("查询 CPA 外部池数量失败 (attempt %s/%s): %s，%.2f 秒后重试", attempt + 1, retries + 1, exc, delay)
                await asyncio.sleep(delay)

        raise RuntimeError(str(last_error or "查询 CPA 外部池数量失败"))

    def _run_health_check(self, settings) -> Tuple[List[int], List[Dict[str, Any]]]:
        """对一批 active 账号做健康检查。"""
        batch_size = max(int(getattr(settings, "account_pool_health_check_batch_size", 20) or 20), 1)
        total_active = count_active_accounts()
        if total_active <= 0:
            self._health_check_offset = 0
            return [], []

        if self._health_check_offset >= total_active:
            self._health_check_offset = 0

        account_ids = list_active_account_ids(batch_size, self._health_check_offset)
        if not account_ids and self._health_check_offset:
            self._health_check_offset = 0
            account_ids = list_active_account_ids(batch_size, self._health_check_offset)

        deleted_account_ids: List[int] = []
        transient_failures: List[Dict[str, Any]] = []

        for account_id in account_ids:
            is_valid, error = validate_account_health(account_id)
            if is_valid:
                continue

            error_text = str(error or "")
            if error_text in {"Token 无效或已过期", "账号可能被封禁"}:
                if delete_account_by_id(account_id):
                    deleted_account_ids.append(account_id)
                continue

            transient_failures.append({"account_id": account_id, "error": error_text})

        self._health_check_offset = (self._health_check_offset + len(account_ids)) % max(total_active, 1)
        return deleted_account_ids, transient_failures

    async def _trigger_fill(self, fill_count: int, cpa_target: Dict[str, Any], settings) -> Dict[str, Any]:
        """触发补量注册。"""
        try:
            return await trigger_registration_fill(
                count=fill_count,
                email_service_type=str(getattr(settings, "account_pool_registration_email_service_type", "tempmail") or "tempmail"),
                interval_min=max(int(getattr(settings, "account_pool_registration_interval_min", 5) or 5), 0),
                interval_max=max(int(getattr(settings, "account_pool_registration_interval_max", 15) or 15), 0),
                concurrency=max(int(getattr(settings, "account_pool_registration_concurrency", 1) or 1), 1),
                cpa_target=cpa_target,
            )
        except Exception as exc:
            self._record_cycle_failure(f"补量注册失败: {exc}", settings)
            raise

    def _record_cycle_failure(
        self,
        error_message: str,
        settings,
        base_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录失败并进入指数退避。"""
        self._consecutive_failure_count += 1
        base_delay = max(float(getattr(settings, "account_pool_retry_base_delay_seconds", 1) or 1), 0.1)
        max_delay = max(float(getattr(settings, "account_pool_retry_max_delay_seconds", 30) or 30), base_delay)
        delay_seconds = min(base_delay * (2 ** max(self._consecutive_failure_count - 1, 0)), max_delay)
        self._next_retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)

        summary = dict(base_summary or {})
        summary.update(
            {
                "last_error": error_message,
                "consecutive_failure_count": self._consecutive_failure_count,
                "next_retry_at": self._next_retry_at.isoformat(),
                "last_run_finished_at": datetime.now(UTC).isoformat(),
            }
        )
        self._last_summary.update(summary)
        logger.warning("账号池控制器进入退避: %s，下一次重试时间 %s", error_message, self._next_retry_at.isoformat())


def get_account_pool_controller() -> AccountPoolController:
    """获取全局账号池控制器单例。"""
    global _controller
    if _controller is None:
        _controller = AccountPoolController()
    return _controller
