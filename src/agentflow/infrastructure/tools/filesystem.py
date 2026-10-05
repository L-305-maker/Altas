"""只读文件工具。根目录由部署者设定，模型仅能传入相对路径。"""

import asyncio
from pathlib import Path

from agentflow.application.tools.runtime import Tool


def read_text_tool(root: Path) -> Tool:
    root = root.resolve(strict=True)

    async def read(arguments: dict) -> dict:
        path = (root / arguments["path"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("path is outside the tool root")

        # 根目录不可由不可信进程并发改写；resolve 检查不是 OS 级文件沙箱。
        def load():
            with path.open("rb") as source:
                data = source.read(100_001)
            if len(data) > 100_000:
                raise ValueError("file exceeds 100 KB")
            return {"text": data.decode("utf-8")}

        return await asyncio.to_thread(load)

    return Tool(
        "read_text",
        "读取授权目录中的 UTF-8 文本",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 240}},
            "required": ["path"],
            "additionalProperties": False,
        },
        read,
    )
