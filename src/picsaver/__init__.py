"""pictureSaver2 —— SD 卡照片按日期范围拷贝工具。

分层约定（见 Agent.md）：
    core  -> 纯业务内核，不依赖任何界面 / Web 框架
    api   -> FastAPI 后端，只调用 core
    cli   -> 命令行入口，只调用 core
    web   -> 纯静态前端，通过 HTTP 调用 api
"""

__version__ = "0.1.0"
