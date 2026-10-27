"""DeepSeek API 客户端：只做「理解与排序」，不产生任何地理事实。

约束：
* 输入为结构化 JSON（候选地点 + 真实路线数据 + 用户偏好）；
* 输出必须能通过 Pydantic 校验，且 poi_id 必须来自候选集合（由调用方二次校验）；
* 不把 Key 写入日志；统计 token 用量；
* 提供零 token 成本的健康探测（/models + /user/balance）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from models.schemas import AIResponse, ServiceName, ServiceState
from services.errors import DeepSeekError, NotConfiguredError, classify_http_status
from services.http_client import HttpTransport, redact

__all__ = ["DeepSeekService", "LLMResult", "LLMUsage", "CacheProtocol"]


class CacheProtocol(Protocol):
    def get_json(self, key: str, ttl_s: float | None = None) -> Any | None: ...
    def put_json(self, key: str, value: Any) -> None: ...


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, usage: dict[str, Any]) -> None:
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.total_tokens += int(usage.get("total_tokens", 0) or 0)
        self.calls += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass
class LLMResult:
    content: str
    data: dict[str, Any] | None = None
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    parsed: AIResponse | None = None


class DeepSeekService:
    def __init__(
        self,
        api_key: str,
        transport: HttpTransport,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-pro",
        cache: CacheProtocol | None = None,
        llm_timeout: float = 90.0,
        effort: str = "low",
    ) -> None:
        self._key = api_key
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._model = model
        self._cache = cache
        self._llm_timeout = max(15.0, float(llm_timeout))
        self._effort = (effort or "").strip()
        self.usage = LLMUsage()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @property
    def transport(self) -> HttpTransport:
        """底层连接池（诊断层用它统计真实 HTTP 次数，含重试）。"""
        return self._transport

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _require_key(self) -> None:
        if not self._key:
            raise NotConfiguredError(ServiceName.DEEPSEEK, "DeepSeek API Key 未配置")

    def _validate(self, payload: dict[str, Any], status: int) -> None:
        if 200 <= status < 300:
            if not isinstance(payload.get("choices"), list):
                raise DeepSeekError(
                    "DeepSeek 响应缺少 choices",
                    state=ServiceState.SERVICE_ERROR,
                    http_status=status,
                )
            return
        state, retryable = classify_http_status(status)
        err = payload.get("error") if isinstance(payload, dict) else None
        message = ""
        code = ""
        if isinstance(err, dict):
            message = str(err.get("message", ""))[:200]
            code = str(err.get("code") or err.get("type") or "")
        raise DeepSeekError(
            message or f"DeepSeek HTTP {status}",
            state=state,
            http_status=status,
            biz_code=code,
            retryable=retryable,
        )

    # ------------------------------------------------------------------ #
    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.25,
        max_tokens: int = 4000,
        model: str | None = None,
        timeout_s: float | None = None,
        cache_key: str | None = None,
        cache_ttl_s: float = 1800.0,
    ) -> LLMResult:
        """调用 /chat/completions 并强制 JSON 输出。

        推理模型会消耗大量 reasoning token，因此默认给足 max_tokens，
        并在支持时使用较低的 reasoning_effort（可用 MPAI_LLM_EFFORT 调整）。
        """
        self._require_key()
        use_model = model or self._model
        if cache_key and self._cache is not None:
            cached = self._cache.get_json(cache_key, cache_ttl_s)
            if cached:
                return LLMResult(
                    content=cached.get("content", ""),
                    data=cached.get("data"),
                    latency_ms=0,
                    model=cached.get("model", use_model),
                    usage={"cached": 1},
                )

        body: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self._effort:
            body["reasoning_effort"] = self._effort
        started = time.perf_counter()
        try:
            payload, status, latency = await self._transport.post_json(
                "deepseek",
                f"{self._base}/chat/completions",
                json_body=body,
                headers=self._headers(),
                max_retries=1,
                validate=self._validate,
                timeout=timeout_s or self._llm_timeout,
            )
        except DeepSeekError as exc:
            # 部分模型/账户不支持 reasoning_effort（HTTP 400）：去掉参数重试一次，
            # 并记住这个账户不支持，避免每次调用都先白付一次 400。
            if exc.http_status == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort", None)
                payload, status, latency = await self._transport.post_json(
                    "deepseek",
                    f"{self._base}/chat/completions",
                    json_body=body,
                    headers=self._headers(),
                    max_retries=0,
                    validate=self._validate,
                    timeout=timeout_s or self._llm_timeout,
                )
                self._effort = ""
            else:
                raise
        except Exception as exc:  # noqa: BLE001
            from services.errors import ServiceError

            if isinstance(exc, ServiceError):
                raise DeepSeekError(
                    exc.message,
                    state=exc.state,
                    http_status=exc.http_status,
                    biz_code=exc.biz_code,
                    retryable=exc.retryable,
                    detail=exc.detail,
                ) from exc
            raise DeepSeekError(
                f"DeepSeek 请求失败：{redact(str(exc))[:180]}",
                state=ServiceState.SERVICE_ERROR,
            ) from exc

        latency = latency or int((time.perf_counter() - started) * 1000)
        choices = payload.get("choices") or []
        if not choices:
            raise DeepSeekError("DeepSeek 返回空 choices", state=ServiceState.SERVICE_ERROR, http_status=status)
        message = choices[0].get("message") or {}
        content = str(message.get("content", "") or "")
        usage = payload.get("usage") or {}
        self.usage.add(usage if isinstance(usage, dict) else {})

        data: dict[str, Any] | None = None
        if content.strip():
            try:
                parsed_json = json.loads(content)
                if isinstance(parsed_json, dict):
                    data = parsed_json
            except json.JSONDecodeError:
                data = self._extract_json(content)

        if data is None:
            raise DeepSeekError(
                "DeepSeek 返回内容不是合法 JSON 对象",
                state=ServiceState.SERVICE_ERROR,
                http_status=status,
            )

        result = LLMResult(
            content=content,
            data=data,
            latency_ms=latency,
            model=str(payload.get("model", use_model)),
            usage=usage if isinstance(usage, dict) else {},
        )
        if cache_key and self._cache is not None and result.data is not None:
            self._cache.put_json(
                cache_key,
                {"content": result.content, "data": result.data, "model": result.model},
            )
        return result

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """容错：从可能带 markdown 围栏的文本里截取 JSON 对象。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:]
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return DeepSeekService._salvage_items(stripped)
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _salvage_items(text: str) -> dict[str, Any] | None:
        """输出被 max_tokens 截断时，尽量抢救已完整输出的 items。"""
        import re

        pattern = re.compile(r'\{\s*"poi_id"\s*:\s*"([^"]+)"[^{}]*\}', re.DOTALL)
        items: list[dict[str, Any]] = []
        for match in pattern.finditer(text):
            snippet = match.group(0)
            try:
                obj = json.loads(snippet)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("poi_id"):
                items.append(obj)
        if not items:
            return None
        summary = ""
        summary_match = re.search(r'"summary"\s*:\s*"([^"]*)"', text)
        if summary_match:
            summary = summary_match.group(1)
        return {"summary": summary, "items": items, "_salvaged": True}

    # ------------------------------------------------------------------ #
    async def health_probe(self) -> dict[str, Any]:
        """零 token 健康探测：先 /models（鉴权+连通），再 /user/balance（余额）。"""
        if not self.configured:
            return {
                "state": ServiceState.NOT_CONFIGURED,
                "latency_ms": 0,
                "detail": "未配置 DeepSeek API Key",
            }
        started = time.perf_counter()
        try:
            payload, status, latency = await self._transport.get_json(
                "deepseek",
                f"{self._base}/models",
                None,
                headers=self._headers(),
                max_retries=0,
            )
            if status >= 400:
                state, _ = classify_http_status(status)
                return {
                    "state": state,
                    "latency_ms": latency,
                    "detail": f"HTTP {status}",
                    "http_status": status,
                    "error_type": "HttpError",
                }
            models = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
            detail = f"可用模型 {len(models)} 个"
            balance_text = ""
            balance_state: ServiceState | None = None
            try:
                bal_payload, bal_status, bal_latency = await self._transport.get_json(
                    "deepseek",
                    f"{self._base}/user/balance",
                    None,
                    headers=self._headers(),
                    max_retries=0,
                )
                latency = max(latency, bal_latency)
                if bal_status < 400:
                    infos = bal_payload.get("balance_infos") or []
                    if infos:
                        amount = infos[0].get("total_balance", "0")
                        currency = infos[0].get("currency", "CNY")
                        balance_text = f"余额 {amount} {currency}"
                        try:
                            if float(amount) <= 0:
                                balance_state = ServiceState.QUOTA_LIMITED
                                balance_text += "（余额不足）"
                        except (TypeError, ValueError):
                            pass
                    if bal_payload.get("is_available") is False:
                        balance_state = ServiceState.QUOTA_LIMITED
                elif bal_status in (401, 403):
                    balance_state = ServiceState.AUTH_ERROR
            except Exception:  # noqa: BLE001 - 余额接口失败不致命
                pass
            final_state = balance_state or ServiceState.OK
            return {
                "state": final_state,
                "latency_ms": latency,
                "detail": "，".join(x for x in (detail, balance_text) if x),
                "http_status": status,
                "error_type": "",
                "models": models,
            }
        except Exception as exc:  # noqa: BLE001
            latency = int((time.perf_counter() - started) * 1000)
            from services.errors import ServiceError, classify_exception

            if isinstance(exc, ServiceError):
                return {
                    "state": exc.state,
                    "latency_ms": latency,
                    "detail": exc.message,
                    "http_status": exc.http_status,
                    "error_type": type(exc).__name__,
                }
            state, err_type, _ = classify_exception(exc)
            return {
                "state": state,
                "latency_ms": latency,
                "detail": redact(str(exc))[:160],
                "http_status": None,
                "error_type": err_type,
            }
