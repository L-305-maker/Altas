"""可用于本地协议测试的 MCP server，不访问文件、网络或数据库。"""

import hashlib

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentflow-evidence")


@mcp.tool()
def fingerprint(text: str) -> dict:
    """返回文本的 SHA-256，便于证据版本比对。"""
    if len(text) > 100_000:
        raise ValueError("text is too large")
    return {"sha256": hashlib.sha256(text.encode()).hexdigest()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
