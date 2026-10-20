"""配置只有环境变量入口；SecretStr 避免对象 repr 意外打印凭据。"""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentflow.application.ports.model_request import ModelRequestOptions
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.application.ports.provider_admission import (
    ProviderAdmissionPolicy,
    ProviderKey,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTFLOW_", extra="ignore")
    database_url: SecretStr = SecretStr("sqlite:///agentflow.db")
    api_token: SecretStr | None = None
    provider: Literal["mock", "deepseek"] = "mock"
    model: str = "deepseek-flash"
    # 不读取 .env 文件以避免隐式加载；部署工具负责把环境变量传给进程。
    deepseek_api_key: SecretStr | None = None
    model_base_url: str = "https://api.deepseek.com"
    model_provider_key: ProviderKey | None = None
    model_request: ModelRequestOptions = Field(default_factory=ModelRequestOptions)
    provider_admission: ProviderAdmissionPolicy | None = None
    observed_usage: ObservedUsagePolicy | None = None
    concurrency: int = Field(default=4, ge=1, le=64)
    lease_seconds: float = Field(default=30, ge=3, le=600)
    poll_seconds: float = Field(default=0.5, gt=0, le=30)
    telemetry: bool = False
    tool_root: str | None = None
    sandbox_enabled: bool = False
    sandbox_image: str = "agentflow-sandbox:1.0"
    mcp_command: str | None = None
    mcp_args: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
