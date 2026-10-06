"""命令行界面：扫描 / 拷贝 / 启动 Web 界面。

CLI 与 Web 后端共用同一套 ``picsaver.core`` 实现（见 Agent.md 第 4 节）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import typer

from .. import __version__
from ..core.events import PHASE_CANCELED, PHASE_COPYING, PHASE_FAILED, PHASE_FINISHED, ProgressEvent
from ..core.metrics import SpeedMeter
from ..core.models import DEFAULT_SUFFIXES, ConfigError, CopyConfig, human_bytes, normalize_suffixes
from ..core.service import CopyService

app = typer.Typer(add_completion=False, help="SD 卡照片按日期范围拷贝工具（jpg + raw）", no_args_is_help=True)


class ConsoleProgress:
    """把 core 进度事件渲染成 tqdm 进度条（真实进度：字节 / 文件数 / 速度 / ETA）。"""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.bar = None
        self.meter = SpeedMeter()
        self._last_bytes = 0

    def __call__(self, event: ProgressEvent) -> None:
        if event.phase == PHASE_COPYING and self.bar is None:
            from tqdm import tqdm

            self.bar = tqdm(
                total=max(event.total_bytes, 1),
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="预览" if self.dry_run else "复制",
                dynamic_ncols=True,
            )
        if self.bar is None:
            if event.message:
                typer.echo(f"· {event.message}")
            return
        delta = max(0, event.done_bytes - self._last_bytes)
        self._last_bytes = event.done_bytes
        if delta:
            self.bar.update(delta)
        self.meter.update(event.done_bytes)
        speed = self.meter.speed_bps()
        eta = self.meter.eta_seconds(max(0, event.total_bytes - event.done_bytes))
        postfix = f"{event.done_files}/{event.total_files} 个文件"
        if speed > 0:
            postfix += f" | {human_bytes(speed)}/s"
        if eta is not None:
            postfix += f" | 剩余 {int(eta // 60):02d}:{int(eta % 60):02d}"
        self.bar.set_postfix_str(postfix)
        if event.phase in (PHASE_FINISHED, PHASE_CANCELED, PHASE_FAILED):
            self.bar.close()
            self.bar = None


def _parse_suffixes(text: str) -> tuple[str, ...]:
    """解析 ``--suffixes jpg,arw`` 形式的参数。"""
    return normalize_suffixes([item for item in str(text).replace("，", ",").split(",") if item.strip()])


def _parse_date(text: str, field: str) -> date:
    """解析 ``YYYY-MM-DD`` 日期。"""
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise typer.BadParameter(f"{field} 需要 YYYY-MM-DD 格式，收到：{text}") from exc


@app.command("scan")
def scan_command(
    source: str = typer.Option(..., "--source", "-s", help="原始文件夹（SD 卡目录）"),
    suffixes: str = typer.Option(",".join(DEFAULT_SUFFIXES), "--suffixes", "-x", help="需要处理的后缀，逗号分隔"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="是否递归子目录"),
) -> None:
    """只扫描并统计，不复制任何文件。"""
    service = CopyService()
    try:
        wanted = _parse_suffixes(suffixes)
        result = service.scan(source, wanted, recursive=recursive)
    except (ConfigError, FileNotFoundError, NotADirectoryError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    data = result.to_dict()
    typer.echo(f"源目录          : {data['source_dir']}")
    typer.echo(f"配对照片数      : {data['groups']}")
    typer.echo(f"命中文件数      : {data['matched_files']}（{human_bytes(data['total_bytes'])}）")
    typer.echo(f"各后缀数量      : {data['suffix_counts']}")
    typer.echo(f"可确定时间      : {data['with_time']}")
    typer.echo(f"无时间将跳过    : {data['no_time']}")
    typer.echo(f"扫描耗时        : {data['elapsed_seconds']:.2f} 秒")
    if result.no_time_files:
        typer.secho("无 EXIF 拍摄时间的文件（最多显示 20 条）：", fg=typer.colors.YELLOW)
        for item in result.no_time_files[:20]:
            typer.echo(f"  - {item.path}")


@app.command("copy")
def copy_command(
    source: str = typer.Option(..., "--source", "-s", help="原始文件夹（SD 卡目录）"),
    date_from: str = typer.Option(..., "--date-from", help="日期范围第一天 YYYY-MM-DD（决定目标目录名）"),
    date_to: str = typer.Option(..., "--date-to", help="日期范围最后一天 YYYY-MM-DD"),
    target_parent: str = typer.Option(..., "--target-parent", "-t", help="目的文件夹的父文件夹"),
    place: str = typer.Option(..., "--place", "-p", prompt="请输入地点", help="地点（会写进目录名）"),
    note: str = typer.Option(..., "--note", "-n", prompt="请输入备注", help="备注（会写进目录名）"),
    suffixes: str = typer.Option(",".join(DEFAULT_SUFFIXES), "--suffixes", "-x", help="需要处理的后缀，逗号分隔"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="是否递归子目录"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览，不复制"),
) -> None:
    """按日期范围把照片复制到 ``[YYYYMMDD]地点-备注`` 目录（按后缀分子目录）。"""
    service = CopyService()
    try:
        config = CopyConfig(
            source_dir=Path(source),
            suffixes=_parse_suffixes(suffixes),
            date_from=_parse_date(date_from, "开始日期"),
            date_to=_parse_date(date_to, "结束日期"),
            target_parent=Path(target_parent),
            place=place,
            note=note,
            recursive=recursive,
        )
        config.validate()
    except (ConfigError, ValueError) as exc:
        typer.secho(f"[错误] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"目标目录        : {config.target_dir}")
    progress = ConsoleProgress(dry_run=dry_run)
    plan, report = service.run(config, progress=progress, dry_run=dry_run)
    if progress.bar is not None:
        progress.bar.close()
        progress.bar = None

    typer.echo("")
    typer.echo(report.to_text())
    typer.echo("")
    typer.echo(f"计划文件数      : {plan.planned_files}，后缀子目录：{', '.join(plan.suffix_dirs) or '无'}")
    raise typer.Exit(code=1 if report.failed_files else 0)


@app.command("web")
def web_command(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8765, "--port", help="监听端口"),
) -> None:
    """启动 Web 界面（前后端分离：FastAPI 后端 + 静态前端）。"""
    import uvicorn

    from ..api.app import app as fastapi_app

    typer.secho(f"pictureSaver2 v{__version__} —— 请在浏览器打开 http://{host}:{port}/", fg=typer.colors.GREEN)
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command("version")
def version_command() -> None:
    """显示版本号。"""
    typer.echo(f"pictureSaver2 {__version__}")


def main() -> None:
    """CLI 入口（供 ``python -m picsaver`` 调用）。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
