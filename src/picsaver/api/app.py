"""FastAPI 后端：静态前端 + REST 接口 + SSE 真实进度。

本模块只做三件事：参数校验、任务编排、进度推送；所有文件系统逻辑都在 ``picsaver.core``。
"""

from __future__ import annotations

import asyncio
import json
import queue
import string
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .. import __version__
from ..core.events import PHASE_FINISHED
from ..core.models import DEFAULT_SUFFIXES, ConfigError
from ..core.service import CopyService
from .jobs import END_MARKER, JobManager
from .schemas import CopyRequest, JobCreateRequest, ScanRequest

#: 前端静态资源目录
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

service = CopyService()
manager = JobManager(service=service)


def _http_error(exc: Exception) -> HTTPException:
    """把 core 的异常映射成合适的 HTTP 状态码。"""
    if isinstance(exc, (ConfigError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, PermissionError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def create_app() -> FastAPI:
    """构建 FastAPI 应用（便于测试时单独创建实例）。"""
    application = FastAPI(
        title="pictureSaver2",
        version=__version__,
        description="按日期范围把 SD 卡中的 jpg / raw 照片拷贝到 [日期]地点-备注 目录",
    )
    # 只允许本机来源，避免其它网页跨站调用本地接口
    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    def index() -> Any:
        """返回前端页面。"""
        page = WEB_DIR / "index.html"
        if not page.exists():
            return JSONResponse(
                status_code=503,
                content={"detail": "前端页面尚未就绪（缺少 src/picsaver/web/index.html）"},
            )
        return FileResponse(str(page))

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        """健康检查。"""
        return {"status": "ok", "version": __version__}

    @application.get("/api/defaults")
    def defaults() -> dict[str, Any]:
        """界面默认值。"""
        from datetime import date as _date

        return {
            "source_dir": "",
            "target_parent": "",
            "suffixes": list(DEFAULT_SUFFIXES),
            "today": _date.today().isoformat(),
        }

    @application.get("/api/browse")
    def browse(path: str | None = Query(default=None, description="要浏览的绝对路径，省略时返回盘符")) -> dict[str, Any]:
        """目录浏览（给界面上的"浏览…"按钮用）。"""
        if not path:
            drives: list[dict[str, str]] = []
            for letter in string.ascii_uppercase:
                probe = f"{letter}:\\"
                try:
                    if Path(probe).exists():
                        drives.append({"name": f"{letter}:", "path": probe})
                except OSError:
                    # 光驱 / 读卡器未就绪等情况下 exists() 会抛错，直接跳过该盘符
                    continue
            return {"path": "", "parent": None, "dirs": drives, "error": None}
        base = Path(path)
        if not base.exists() or not base.is_dir():
            return {"path": str(path), "parent": None, "dirs": [], "error": "目录不存在或不可访问"}
        dirs: list[dict[str, str]] = []
        error: str | None = None
        try:
            for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
                if entry.is_dir() and not entry.name.startswith("$"):
                    dirs.append({"name": entry.name, "path": str(entry)})
        except OSError as exc:
            error = f"读取目录失败：{exc}"
        parent = str(base.parent) if base.parent != base else None
        return {"path": str(base), "parent": parent, "dirs": dirs, "error": error}

    @application.post("/api/scan")
    def scan(request: ScanRequest) -> dict[str, Any]:
        """扫描源目录：返回配对照片数、命中文件数、各后缀数量与无 EXIF 时间清单。"""
        try:
            result = service.scan(request.source_dir, request.suffixes, recursive=request.recursive)
        except Exception as exc:  # noqa: BLE001 - 统一转成 HTTP 错误
            raise _http_error(exc) from exc
        return result.to_dict()

    @application.post("/api/preview")
    def preview(request: CopyRequest) -> dict[str, Any]:
        """生成拷贝计划（dry-run，不落盘）。"""
        try:
            plan = service.preview(request.to_config())
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return plan.to_dict()

    @application.post("/api/jobs")
    def create_job(request: JobCreateRequest) -> dict[str, Any]:
        """创建后台拷贝任务。"""
        try:
            config = request.config.to_config()
            config.validate()
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        job = manager.submit(config, dry_run=request.dry_run)
        return {"job_id": job.id, "status": job.status}

    @application.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        """最近的任务列表。"""
        return {"jobs": manager.list_jobs()}

    @application.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        """任务状态 / 进度 / 报告。"""
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return manager.payload(job)

    @application.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        """请求取消任务（尽力而为，已复制的文件保留）。"""
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        manager.cancel(job_id)
        return {"job_id": job_id, "canceled": True, "status": job.status}

    @application.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str) -> EventSourceResponse:
        """SSE 实时进度流。

        注意：事件队列是单消费者的，同一任务请只保持一个 SSE 连接
        （前端在断线时降级为轮询 ``GET /api/jobs/{job_id}``）。
        """
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        async def event_stream():
            if job.progress:
                yield {"data": json.dumps(job.progress, ensure_ascii=False)}
            while True:
                try:
                    item = await asyncio.to_thread(job.events.get, True, 1.0)
                except queue.Empty:
                    if job.status != "running":
                        break
                    continue
                if END_MARKER in item:
                    break
                yield {"data": json.dumps(item, ensure_ascii=False)}
            final = dict(job.progress or {})
            final.setdefault("phase", PHASE_FINISHED)
            yield {
                "data": json.dumps(
                    {
                        "phase": final.get("phase"),
                        "status": job.status,
                        "job_id": job.id,
                        "report": job.report,
                        "error": job.error,
                    },
                    ensure_ascii=False,
                )
            }

        return EventSourceResponse(event_stream(), ping=15)

    return application


app = create_app()
