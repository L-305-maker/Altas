"""资源受限的 Python 容器。Docker daemon 自身仍是受信任基础设施。

不挂载宿主目录、不注入环境密钥、不联网；镜像由部署者预先构建。
容器超时或调用取消时强制删除，避免仅杀 docker CLI 而留下后台计算。
"""

import asyncio
from contextlib import suppress
from uuid import uuid4


class DockerSandbox:
    def __init__(self, image: str = "agentflow-sandbox:1.0"):
        self.image = image

    def command(self, name: str) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--memory=128m",
            "--cpus=0.5",
            "--user=65534:65534",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "-i",
            self.image,
            "python",
            "-I",
            "-",
        ]

    async def execute(self, code: str, timeout: float = 10) -> dict:
        if len(code.encode()) > 100_000 or not 0 < timeout <= 60:
            raise ValueError("sandbox input or timeout exceeds limit")
        name = f"agentflow-{uuid4().hex}"
        process = await asyncio.create_subprocess_exec(
            *self.command(name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read(stream):
            chunks, size = [], 0
            while chunk := await stream.read(8192):
                size += len(chunk)
                if size > 100_000:
                    raise ValueError("sandbox output exceeds limit")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")

        try:
            async with asyncio.timeout(timeout):
                process.stdin.write(code.encode())
                await process.stdin.drain()
                process.stdin.close()
                async with asyncio.TaskGroup() as group:
                    stdout = group.create_task(read(process.stdout))
                    stderr = group.create_task(read(process.stderr))
                    group.create_task(process.wait())
                return {
                    "stdout": stdout.result(),
                    "stderr": stderr.result(),
                    "exit_code": process.returncode,
                }
        finally:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(cleanup.wait(), 5)
            if cleanup.returncode is None:
                cleanup.kill()
                await cleanup.wait()
            if process.returncode is None:
                process.kill()
            await process.wait()
