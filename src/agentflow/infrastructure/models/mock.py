"""确定性的离线替身，仅验证接口和业务流程，不模拟真实推理质量。"""

from copy import deepcopy


class MockProvider:
    def __init__(self, responses: list[dict] | None = None):
        self.responses = deepcopy(
            responses or [{"role": "assistant", "content": "离线测试输出"}]
        )
        self.calls = 0

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        if self.calls >= len(self.responses):
            raise RuntimeError("mock response sequence exhausted")
        response = deepcopy(self.responses[self.calls])
        self.calls += 1
        return response
