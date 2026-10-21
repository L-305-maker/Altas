"""Immutable request identity and trusted adapter preflight, independent of SDKs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from agentflow.application.ports.context_projection import (
    ContextProjectionManifest,
    Digest,
)
from agentflow.application.ports.provider_admission import ProviderKey

if TYPE_CHECKING:
    from agentflow.application.ports.model_provider import ModelResponse

Positive = Annotated[int, Field(ge=1, strict=True)]
Version = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
]
ModelName = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
]
Assurance = Literal["unknown", "exact", "upper_bound"]


class ModelRequestError(Exception):
    def __init__(self, code="ModelRequestContractInvalid"):
        self.code = code
        super().__init__(code)


def canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def request_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RequestParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    temperature: float | None = Field(
        default=None, ge=0, le=2, allow_inf_nan=False, strict=True
    )
    top_p: float | None = Field(
        default=None, gt=0, le=1, allow_inf_nan=False, strict=True
    )
    response_format: Literal["text", "json_object"] = "text"


class ModelRequestOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_output_tokens: Positive = 4096
    # This v1 response interface consumes complete JSON responses, not SSE.
    stream: Literal[False] = False
    tools_enabled: bool = Field(default=True, strict=True)
    strict_token_window: bool = Field(default=False, strict=True)
    parameters: RequestParameters = Field(default_factory=RequestParameters)


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_key: ProviderKey
    model: ModelName
    capability_version: Version
    supports_tools: bool = Field(strict=True)
    supports_streaming: bool = Field(default=False, strict=True)
    supports_json_output: bool = Field(default=False, strict=True)
    max_output_tokens: Positive
    supported_parameters: tuple[Literal["temperature", "top_p"], ...] = ()
    context_window_tokens: Positive | None = None
    token_counting_method: Version | None = None
    token_counting_assurance: Assurance = "unknown"

    @model_validator(mode="after")
    def counter_identity(self):
        if (self.token_counting_method is None) != (
            self.token_counting_assurance == "unknown"
        ):
            raise ValueError("token counting assurance requires a trusted method")
        return self


class TokenCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: Annotated[int, Field(ge=0, strict=True)] | None = None
    assurance: Assurance = "unknown"
    method: Version | None = None

    @model_validator(mode="after")
    def known_measurement(self):
        if self.assurance == "unknown":
            if self.input_tokens is not None or self.method is not None:
                raise ValueError("unknown tokens cannot invent a count")
        elif self.input_tokens is None or self.method is None:
            raise ValueError("trusted token count requires method and count")
        return self


class TokenCounter(Protocol):
    def count(self, request_json: str) -> TokenCount:
        """Count framed INPUT for this model using the full effective request.

        Trusted implementation must account for tools, options and chat framing;
        it must not mistake output reservation or JSON length for input tokens.
        """
        ...


def check_capabilities(capabilities, options, tools, messages):
    if tools and not options.tools_enabled:
        raise ModelRequestError("ModelCapabilityUnsupported")
    if (
        tools or any(m.get("role") == "tool" or m.get("tool_calls") for m in messages)
    ) and not capabilities.supports_tools:
        raise ModelRequestError("ModelCapabilityUnsupported")
    if options.max_output_tokens > capabilities.max_output_tokens:
        raise ModelRequestError("ModelOutputLimit")
    if (
        options.parameters.response_format == "json_object"
        and not capabilities.supports_json_output
    ):
        raise ModelRequestError("ModelCapabilityUnsupported")
    if any(
        getattr(options.parameters, name) is not None
        and name not in capabilities.supported_parameters
        for name in ("temperature", "top_p")
    ):
        raise ModelRequestError("ModelCapabilityUnsupported")


def check_token_window(capabilities, options, count):
    if count.assurance != "unknown" and (
        count.method != capabilities.token_counting_method
        or count.assurance != capabilities.token_counting_assurance
    ):
        raise ModelRequestError("ModelTokenCounterInvalid")
    if options.strict_token_window:
        if count.assurance == "unknown":
            raise ModelRequestError("ModelTokenCountUnavailable")
        if capabilities.context_window_tokens is None:
            raise ModelRequestError("ModelTokenWindowUnavailable")
    if (
        count.input_tokens is not None
        and capabilities.context_window_tokens is not None
    ):
        if (
            count.input_tokens + options.max_output_tokens
            > capabilities.context_window_tokens
        ):
            raise ModelRequestError("ModelTokenWindowExceeded")


class ModelRequestContract(ModelRequestOptions):
    provider_key: ProviderKey
    model: ModelName
    adapter_version: Version
    request_contract_version: Positive = 1
    capabilities: ProviderCapabilities
    endpoint_fingerprint: Digest | None = None
    context_projection_digest: Digest
    tool_schema_digest: Digest
    token_count: TokenCount = Field(default_factory=TokenCount)
    context_egress_digest: Digest | None = None
    request_contract_digest: Digest

    @model_serializer(mode="wrap")
    def historical_contract(self, handler):
        data = handler(self)
        if self.context_egress_digest is None:
            data.pop("context_egress_digest", None)
        return data

    @model_validator(mode="after")
    def valid_identity(self):
        if (self.provider_key, self.model) != (
            self.capabilities.provider_key,
            self.capabilities.model,
        ):
            raise ValueError("request capability identity differs")
        try:
            check_capabilities(self.capabilities, self, [], [])
            check_token_window(self.capabilities, self, self.token_count)
        except ModelRequestError:
            raise ValueError("request exceeds declared capabilities") from None
        if (
            request_digest(
                self.model_dump(mode="json", exclude={"request_contract_digest"})
            )
            != self.request_contract_digest
        ):
            raise ValueError("request contract digest differs")
        return self

    def profile_digest(self) -> str:
        return request_digest(
            self.model_dump(
                mode="json",
                exclude={
                    "context_projection_digest",
                    "tool_schema_digest",
                    "token_count",
                    "request_contract_digest",
                    "context_egress_digest",
                },
            )
        )

    def verify_projection(self, projection: ContextProjectionManifest):
        if (
            self.request_contract_version != 1
            or self.context_projection_digest != projection.projected_context_digest
            or self.tool_schema_digest != projection.tool_schema_digest
            or self.context_egress_digest
            != (
                request_digest(projection.egress.model_dump(mode="json"))
                if projection.egress
                else None
            )
        ):
            raise ModelRequestError()

    def verify_context(self, messages, tools):
        context = {"messages": messages, **({"tools": tools} if tools else {})}
        if (
            request_digest(context) != self.context_projection_digest
            or request_digest(tools) != self.tool_schema_digest
        ):
            raise ModelRequestError()
        check_capabilities(self.capabilities, self, tools, messages)


def check_request_continuity(attempts, contract, *, retry):
    """Bind the session from its first recorded profile; never backfill old identity."""
    previous = attempts[-1].request_contract if attempts else None
    if retry and previous is None:
        raise ModelRequestError("ModelRequestContractUnavailable")
    if retry and previous != contract:
        raise ModelRequestError("ModelRequestContractChanged")
    first = next(
        (a.request_contract for a in attempts if a.request_contract is not None), None
    )
    if first is not None and first.profile_digest() != contract.profile_digest():
        raise ModelRequestError("ModelSessionConfigurationChanged")


@dataclass(frozen=True)
class ModelRequest:
    contract: ModelRequestContract
    context_json: str = field(repr=False)

    def payload(self) -> dict:
        context = json.loads(self.context_json)
        self.contract.verify_context(context["messages"], context.get("tools", []))
        return serialize_request(context, self.contract.model, self.contract)


def serialize_request(context, model, options):
    """Pure shared wire options; callers validate trusted capabilities first."""
    if set(context) - {"messages", "tools"}:
        raise ModelRequestError()
    payload = {
        "messages": context["messages"],
        **({"tools": context["tools"]} if context.get("tools") else {}),
        "model": model,
        "max_tokens": options.max_output_tokens,
        "stream": options.stream,
    }
    parameters = options.parameters
    if parameters.temperature is not None:
        payload["temperature"] = parameters.temperature
    if parameters.top_p is not None:
        payload["top_p"] = parameters.top_p
    payload["response_format"] = {"type": parameters.response_format}
    return payload


@dataclass(frozen=True)
class PreparedModelRequest:
    request: ModelRequest
    execute: Callable[[], Awaitable[ModelResponse]] = field(repr=False, compare=False)


def build_request(
    messages,
    tools,
    projection,
    *,
    capabilities,
    options,
    adapter_version,
    endpoint_fingerprint=None,
    token_counter=None,
):
    """Pure preflight; the counter is a trusted dependency, never model output."""
    check_capabilities(capabilities, options, tools, messages)
    context = {"messages": messages, **({"tools": tools} if tools else {})}
    try:
        context_json = canonical_json(context)
        count = TokenCount()
        if token_counter is not None:
            count = token_counter.count(
                canonical_json(serialize_request(context, capabilities.model, options))
            )
            if not isinstance(count, TokenCount):
                raise ValueError
            count = TokenCount.model_validate(count.model_dump())
    except Exception:
        raise ModelRequestError("ModelTokenCounterInvalid") from None
    check_token_window(capabilities, options, count)
    values = {
        **options.model_dump(mode="json"),
        "provider_key": capabilities.provider_key,
        "model": capabilities.model,
        "adapter_version": adapter_version,
        "request_contract_version": 1,
        "capabilities": capabilities.model_dump(mode="json"),
        "endpoint_fingerprint": endpoint_fingerprint,
        "context_projection_digest": projection.projected_context_digest,
        "tool_schema_digest": projection.tool_schema_digest,
        "token_count": count.model_dump(mode="json"),
    }
    if projection.egress is not None:
        values["context_egress_digest"] = request_digest(
            projection.egress.model_dump(mode="json")
        )
    contract = ModelRequestContract(
        **values, request_contract_digest=request_digest(values)
    )
    contract.verify_projection(projection)
    request = ModelRequest(contract, context_json)
    request.payload()
    return request
