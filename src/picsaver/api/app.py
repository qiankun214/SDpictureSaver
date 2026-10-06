"""FastAPI 后端：静态前端 + REST 接口 + SSE 真实进度。

本模块只做三件事：参数校验、任务编排、进度推送；所有文件系统逻辑都在 ``picsaver.core``。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import string
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .. import __version__
from ..core.events import PHASE_CANCELED, PHASE_FAILED, PHASE_FINISHED
from ..core.models import DEFAULT_SUFFIXES, ConfigError
from ..core.service import CopyService
from .jobs import END_MARKER, JobManager, JobSpec
from .schemas import (
    CopyRequest,
    JobCreateRequest,
    MkdirRequest,
    ScanRequest,
    validate_folder_name,
)

#: 前端静态资源目录
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

#: 目录浏览最多返回的子目录数量（超出时 truncated=true）
MAX_BROWSE_DIRS = 2000

service = CopyService()
manager = JobManager(service=service)


def _list_subdirs(base: Path) -> tuple[list[dict[str, str]], int, str | None]:
    """只列出子目录（不 stat 普通文件）。

    :return: ``(最多 MAX_BROWSE_DIRS 个目录项, 目录总数, 错误信息)``
    """
    found: list[tuple[str, str]] = []
    error: str | None = None
    try:
        with os.scandir(base) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("$"):
                        continue
                    found.append((entry.name, entry.path))
                except OSError:
                    continue
    except OSError as exc:
        error = f"读取目录失败：{exc}"
    found.sort(key=lambda item: item[0].lower())
    total = len(found)
    trimmed = found[:MAX_BROWSE_DIRS]
    return [{"name": name, "path": path} for name, path in trimmed], total, error


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
        """目录浏览（给界面上的"浏览…"按钮用）。

        **只列文件夹**：用 :func:`os.scandir` 的目录项缓存判断类型，不对普通文件做 stat，
        因此即使目录里有几万个文件也能秒开（实测 20000 个文件从 ~1435 ms 降到 ~15 ms）。
        """
        started = time.perf_counter()

        def _elapsed() -> float:
            return round((time.perf_counter() - started) * 1000, 1)

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
            return {
                "path": "",
                "parent": None,
                "dirs": drives,
                "truncated": False,
                "total_dirs": len(drives),
                "elapsed_ms": _elapsed(),
                "error": None,
            }
        base = Path(path)
        if not base.exists() or not base.is_dir():
            return {
                "path": str(path),
                "parent": None,
                "dirs": [],
                "truncated": False,
                "total_dirs": 0,
                "elapsed_ms": _elapsed(),
                "error": "目录不存在或不可访问",
            }
        dirs, total, error = _list_subdirs(base)
        parent = str(base.parent) if base.parent != base else None
        return {
            "path": str(base),
            "parent": parent,
            "dirs": dirs,
            "truncated": total > len(dirs),
            "total_dirs": total,
            "elapsed_ms": _elapsed(),
            "error": error,
        }

    @application.post("/api/mkdir")
    def make_directory(request: MkdirRequest) -> dict[str, Any]:
        """在指定父目录下新建文件夹（给界面上的"新建文件夹"用）。"""
        raw_parent = (request.parent or "").strip().strip('"')
        if not raw_parent:
            raise HTTPException(status_code=400, detail="父目录不能为空")
        parent = Path(raw_parent)
        if not parent.exists() or not parent.is_dir():
            raise HTTPException(status_code=400, detail=f"父目录不存在或不是目录：{parent}")
        try:
            name = validate_folder_name(request.name)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = parent / name
        if target.exists():
            raise HTTPException(status_code=400, detail=f"已存在同名文件或文件夹：{target}")
        try:
            target.mkdir()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"创建文件夹失败：{exc}") from exc
        return {"path": str(target), "name": name, "created": True}

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
        """创建后台任务（mode: scan / preview / copy）。"""
        mode = request.resolved_mode
        source = Path(request.config.source_dir)
        suffixes = tuple(request.config.suffixes)
        if mode == "scan":
            # 扫描任务只要求源目录与后缀有效
            if not source.exists() or not source.is_dir():
                raise HTTPException(status_code=400, detail=f"原始文件夹不存在或不是目录：{source}")
            spec = JobSpec(
                mode="scan",
                source_dir=source,
                suffixes=suffixes,
                recursive=request.config.recursive,
            )
        else:
            try:
                config = request.config.to_config()
                config.validate()
            except Exception as exc:  # noqa: BLE001
                raise _http_error(exc) from exc
            spec = JobSpec(
                mode=mode,
                source_dir=source,
                suffixes=suffixes,
                recursive=config.recursive,
                config=config,
                dry_run=bool(request.dry_run),
            )
        job = manager.submit(spec)
        return {"job_id": job.id, "status": job.status, "mode": spec.mode}

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
            # 终局事件必须带终局阶段，覆盖上一次进度事件里的 scanning/copying
            terminal_phase = {
                "canceled": PHASE_CANCELED,
                "failed": PHASE_FAILED,
            }.get(job.status, PHASE_FINISHED)
            yield {
                "data": json.dumps(
                    {
                        "phase": terminal_phase,
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
