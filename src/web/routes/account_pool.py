"""
账号池控制器 API 路由
"""

from fastapi import APIRouter

from ...config.settings import update_settings
from ..account_pool_controller import get_account_pool_controller

router = APIRouter()


@router.get("/status")
async def get_account_pool_status():
    """获取账号池控制器状态。"""
    controller = get_account_pool_controller()
    return controller.get_status()


@router.post("/start")
async def start_account_pool_controller():
    """启动账号池控制器，并持久化 enabled=true。"""
    update_settings(account_pool_enabled=True)
    controller = get_account_pool_controller()
    started = await controller.start()
    return {"success": True, "started": started, "status": controller.get_status()}


@router.post("/stop")
async def stop_account_pool_controller():
    """停止账号池控制器，并持久化 enabled=false。"""
    update_settings(account_pool_enabled=False)
    controller = get_account_pool_controller()
    stopped = await controller.stop()
    return {"success": True, "stopped": stopped, "status": controller.get_status()}


@router.post("/run-once")
async def run_account_pool_controller_once():
    """手动执行一轮账号池巡检。"""
    controller = get_account_pool_controller()
    result = await controller.run_once()
    return {"success": True, "result": result}
