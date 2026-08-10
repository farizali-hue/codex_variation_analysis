from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib import error, request

from .project_env import load_project_dotenv

LOGGER = logging.getLogger(__name__)

# Gallery image downloads hit Amazon's CDN, which resets connections now and then.
_IMAGE_DOWNLOAD_ATTEMPTS = 3
_IMAGE_DOWNLOAD_BACKOFF_SECONDS = 0.5


@dataclass(frozen=True)
class ChatModelPricing:
    input_cost_per_1m_tokens: float
    output_cost_per_1m_tokens: float


# Verified against official OpenAI model docs on 2026-04-04.
_KNOWN_EMBEDDING_MODEL_PRICES: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}

# Verified against official OpenAI model docs on 2026-04-04.
_KNOWN_CHAT_MODEL_PRICES: dict[str, ChatModelPricing] = {
    "gpt-5.4": ChatModelPricing(input_cost_per_1m_tokens=2.50, output_cost_per_1m_tokens=15.00),
    "gpt-5.4-mini": ChatModelPricing(input_cost_per_1m_tokens=0.75, output_cost_per_1m_tokens=4.50),
    "gpt-5.4-nano": ChatModelPricing(input_cost_per_1m_tokens=0.20, output_cost_per_1m_tokens=1.25),
    "gpt-5": ChatModelPricing(input_cost_per_1m_tokens=1.25, output_cost_per_1m_tokens=10.00),
    "gpt-5-mini": ChatModelPricing(input_cost_per_1m_tokens=0.25, output_cost_per_1m_tokens=2.00),
    "gpt-5-nano": ChatModelPricing(input_cost_per_1m_tokens=0.05, output_cost_per_1m_tokens=0.40),
    "gpt-4.1": ChatModelPricing(input_cost_per_1m_tokens=2.00, output_cost_per_1m_tokens=8.00),
    "gpt-4.1-mini": ChatModelPricing(input_cost_per_1m_tokens=0.40, output_cost_per_1m_tokens=1.60),
    "gpt-4.1-nano": ChatModelPricing(input_cost_per_1m_tokens=0.10, output_cost_per_1m_tokens=0.40),
    "gpt-4o": ChatModelPricing(input_cost_per_1m_tokens=2.50, output_cost_per_1m_tokens=10.00),
    "gpt-4o-mini": ChatModelPricing(input_cost_per_1m_tokens=0.15, output_cost_per_1m_tokens=0.60),
    "gpt-4": ChatModelPricing(input_cost_per_1m_tokens=30.00, output_cost_per_1m_tokens=60.00),
}


class OpenAIConfigError(RuntimeError):
    pass


class OpenAIBudgetExceededError(RuntimeError):
    def __init__(self, max_cost_usd: float, estimated_cost_usd: float) -> None:
        self.max_cost_usd = max_cost_usd
        self.estimated_cost_usd = estimated_cost_usd
        super().__init__(
            f"Estimated OpenAI cost limit reached: ${estimated_cost_usd:.6f} >= ${max_cost_usd:.6f}"
        )


class OpenAIRunCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    embedding_model: str
    chat_model: str
    vision_model: str = ""
    canonicalization_model: str = ""
    api_base: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60
    allow_remote_image_urls: bool = False
    max_cost_usd: float | None = None
    embedding_cost_per_1m_tokens: float | None = None
    chat_input_cost_per_1m_tokens: float | None = None
    chat_output_cost_per_1m_tokens: float | None = None

    @classmethod
    def from_env(cls) -> "OpenAIConfig":
        try:
            load_project_dotenv(require_package=True)
        except ImportError as exc:
            raise OpenAIConfigError(
                "python-dotenv is required to load environment variables for this project. "
                "Run `pip install -r requirements.txt`."
            ) from exc

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "").strip()
        chat_model = os.getenv("OPENAI_CHAT_MODEL", "").strip()
        vision_model = os.getenv("OPENAI_VISION_MODEL", "").strip()
        canonicalization_model = os.getenv("OPENAI_CANONICALIZATION_MODEL", "").strip()
        allow_remote_image_urls = _read_optional_bool_env("OPENAI_VISION_REMOTE_URLS", default=False)
        missing = [
            name
            for name, value in (
                ("OPENAI_API_KEY", api_key),
                ("OPENAI_EMBEDDING_MODEL", embedding_model),
                ("OPENAI_CHAT_MODEL", chat_model),
            )
            if not value
        ]
        if missing:
            raise OpenAIConfigError(f"Missing required OpenAI configuration: {', '.join(missing)}")
        config = cls(
            api_key=api_key,
            embedding_model=embedding_model,
            chat_model=chat_model,
            vision_model=vision_model,
            canonicalization_model=canonicalization_model,
            allow_remote_image_urls=allow_remote_image_urls,
            max_cost_usd=_read_optional_float_env("OPENAI_MAX_COST_USD"),
            embedding_cost_per_1m_tokens=_resolve_embedding_model_price(
                embedding_model,
                _read_optional_float_env("OPENAI_EMBEDDING_COST_PER_1M_TOKENS"),
            ),
            chat_input_cost_per_1m_tokens=_resolve_chat_input_model_price(
                chat_model,
                _read_optional_float_env("OPENAI_CHAT_INPUT_COST_PER_1M_TOKENS"),
            ),
            chat_output_cost_per_1m_tokens=_resolve_chat_output_model_price(
                chat_model,
                _read_optional_float_env("OPENAI_CHAT_OUTPUT_COST_PER_1M_TOKENS"),
            ),
        )
        config.validate_cost_controls()
        return config

    def has_cost_estimation(self) -> bool:
        return all(
            value is not None
            for value in (
                self.embedding_cost_per_1m_tokens,
                self.chat_input_cost_per_1m_tokens,
                self.chat_output_cost_per_1m_tokens,
            )
        )

    def validate_cost_controls(self) -> None:
        price_fields = (
            ("embedding_cost_per_1m_tokens", self.embedding_cost_per_1m_tokens),
            ("chat_input_cost_per_1m_tokens", self.chat_input_cost_per_1m_tokens),
            ("chat_output_cost_per_1m_tokens", self.chat_output_cost_per_1m_tokens),
        )
        provided_prices = any(value is not None for _, value in price_fields)
        missing_prices = [name for name, value in price_fields if value is None]
        if provided_prices and missing_prices:
            raise OpenAIConfigError(
                "Cost estimation requires all pricing values: "
                "embedding_cost_per_1m_tokens, chat_input_cost_per_1m_tokens, "
                "chat_output_cost_per_1m_tokens. If your configured model is not in the built-in "
                "pricing catalog, set the missing prices manually."
            )
        if self.max_cost_usd is not None and missing_prices:
            raise OpenAIConfigError(
                "A max cost limit requires all pricing values: "
                "embedding_cost_per_1m_tokens, chat_input_cost_per_1m_tokens, "
                "chat_output_cost_per_1m_tokens. If your configured model is not in the built-in "
                "pricing catalog, set the missing prices manually."
            )
        normalized_chat_model = _normalize_model_name(self.chat_model)
        for env_name, override_model in (
            ("OPENAI_VISION_MODEL", self.resolved_vision_model()),
            ("OPENAI_CANONICALIZATION_MODEL", self.resolved_canonicalization_model()),
        ):
            if not override_model or _normalize_model_name(override_model) == normalized_chat_model:
                continue
            input_price = _resolve_chat_input_model_price(override_model, None)
            output_price = _resolve_chat_output_model_price(override_model, None)
            if self.max_cost_usd is not None and (input_price is None or output_price is None):
                raise OpenAIConfigError(
                    f"A max cost limit with {env_name} requires built-in pricing for the "
                    "configured model or matching OPENAI_CHAT_MODEL pricing."
                )

    def resolved_vision_model(self) -> str:
        return self.vision_model.strip() or self.chat_model

    def resolved_canonicalization_model(self) -> str:
        """Ingredient matching is the hardest judgment here — allow a stronger model."""
        return self.canonicalization_model.strip() or self.chat_model


@dataclass
class ApiUsageSummary:
    embedding_calls: int = 0
    embedding_tokens: int = 0
    embedding_cost_usd: float = 0.0
    chat_calls: int = 0
    chat_prompt_tokens: int = 0
    chat_prompt_cost_usd: float = 0.0
    chat_completion_tokens: int = 0
    chat_completion_cost_usd: float = 0.0
    chat_total_tokens: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        return self.embedding_cost_usd + self.chat_prompt_cost_usd + self.chat_completion_cost_usd

    def add_embedding(self, total_tokens: int, cost_usd: float) -> None:
        self.embedding_calls += 1
        self.embedding_tokens += total_tokens
        self.embedding_cost_usd += cost_usd

    def add_chat(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        prompt_cost_usd: float,
        completion_cost_usd: float,
    ) -> None:
        self.chat_calls += 1
        self.chat_prompt_tokens += prompt_tokens
        self.chat_prompt_cost_usd += prompt_cost_usd
        self.chat_completion_tokens += completion_tokens
        self.chat_completion_cost_usd += completion_cost_usd
        self.chat_total_tokens += total_tokens

    def copy(self) -> "ApiUsageSummary":
        return ApiUsageSummary(
            embedding_calls=self.embedding_calls,
            embedding_tokens=self.embedding_tokens,
            embedding_cost_usd=self.embedding_cost_usd,
            chat_calls=self.chat_calls,
            chat_prompt_tokens=self.chat_prompt_tokens,
            chat_prompt_cost_usd=self.chat_prompt_cost_usd,
            chat_completion_tokens=self.chat_completion_tokens,
            chat_completion_cost_usd=self.chat_completion_cost_usd,
            chat_total_tokens=self.chat_total_tokens,
        )


class OpenAIClient:
    def __init__(self, config: OpenAIConfig, cancel_event: threading.Event | None = None) -> None:
        self.config = config
        self.cancel_event = cancel_event
        self._job_usage = ApiUsageSummary()
        self._batch_usage = ApiUsageSummary()
        self._image_data_url_cache: dict[str, str] = {}

    def reset_job_usage(self) -> None:
        self._job_usage = ApiUsageSummary()

    def reset_batch_usage(self) -> None:
        self._batch_usage = ApiUsageSummary()

    def get_job_usage(self) -> ApiUsageSummary:
        return self._job_usage.copy()

    def get_batch_usage(self) -> ApiUsageSummary:
        return self._batch_usage.copy()

    def create_embeddings(self, texts: list[str], cache_hits: int = 0) -> list[list[float]]:
        if not texts:
            return []
        self._raise_if_cancelled()
        payload = {
            "model": self.config.embedding_model,
            "input": texts,
        }
        response = self._post_json("/embeddings", payload)
        total_tokens = _coerce_usage_value(response.get("usage"), "total_tokens")
        estimated_cost_usd = _estimate_cost_usd(total_tokens, self.config.embedding_cost_per_1m_tokens)
        self._job_usage.add_embedding(total_tokens, estimated_cost_usd)
        self._batch_usage.add_embedding(total_tokens, estimated_cost_usd)
        LOGGER.debug(
            "api_usage call_type=embedding model=%s input_count=%d total_tokens=%d cache_hits=%d estimated_cost_usd=%.6f batch_estimated_cost_usd=%.6f max_cost_usd=%s",
            self.config.embedding_model,
            len(texts),
            total_tokens,
            cache_hits,
            estimated_cost_usd,
            self._batch_usage.estimated_cost_usd,
            _format_optional_usd(self.config.max_cost_usd),
        )
        self._enforce_cost_limit()

        data = response.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OpenAI embeddings response missing data array")
        return [item["embedding"] for item in data]

    def create_chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        asin: str,
        field: str,
        retry_attempt: int = 0,
        model: str | None = None,
        image_inputs: list[dict[str, str]] | None = None,
    ) -> str:
        self._raise_if_cancelled()
        return self._create_chat_completion_internal(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            asin=asin,
            field=field,
            retry_attempt=retry_attempt,
            model=model,
            image_inputs=image_inputs,
            allow_inline_image_retry=True,
        )

    def _create_chat_completion_internal(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        asin: str,
        field: str,
        retry_attempt: int,
        model: str | None,
        image_inputs: list[dict[str, str]] | None,
        allow_inline_image_retry: bool,
    ) -> str:
        model_name = model or self.config.chat_model
        user_content: str | list[dict[str, object]] = user_prompt
        if image_inputs:
            prepared_image_inputs = self._prepare_image_inputs_for_request(image_inputs)
            user_content = [{"type": "text", "text": user_prompt}]
            for image_input in prepared_image_inputs:
                image_url = str(image_input.get("url", "") or "").strip()
                if not image_url:
                    continue
                detail = str(image_input.get("detail", "auto") or "auto").strip()
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": detail or "auto",
                        },
                    }
                )
        payload = {
            "model": model_name,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "developer", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        try:
            response = self._post_json("/chat/completions", payload)
        except RuntimeError as exc:
            if (
                allow_inline_image_retry
                and self.config.allow_remote_image_urls
                and image_inputs
                and self._should_retry_with_inline_images(exc)
            ):
                LOGGER.warning(
                    "Retrying chat completion with inline image data asin=%s field=%s model=%s because remote image fetch failed",
                    asin,
                    field,
                    model_name,
                )
                inline_image_inputs = self._convert_image_inputs_to_data_urls(image_inputs)
                return self._create_chat_completion_internal(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    asin=asin,
                    field=field,
                    retry_attempt=retry_attempt + 1,
                    model=model_name,
                    image_inputs=inline_image_inputs,
                    allow_inline_image_retry=False,
                )
            raise
        prompt_tokens = _coerce_usage_value(response.get("usage"), "prompt_tokens")
        completion_tokens = _coerce_usage_value(response.get("usage"), "completion_tokens")
        total_tokens = _coerce_usage_value(response.get("usage"), "total_tokens")
        prompt_cost_usd = _estimate_cost_usd(prompt_tokens, self._resolve_chat_input_price(model_name))
        completion_cost_usd = _estimate_cost_usd(completion_tokens, self._resolve_chat_output_price(model_name))
        estimated_cost_usd = prompt_cost_usd + completion_cost_usd
        self._job_usage.add_chat(
            prompt_tokens,
            completion_tokens,
            total_tokens,
            prompt_cost_usd,
            completion_cost_usd,
        )
        self._batch_usage.add_chat(
            prompt_tokens,
            completion_tokens,
            total_tokens,
            prompt_cost_usd,
            completion_cost_usd,
        )
        LOGGER.debug(
            "api_usage call_type=chat model=%s asin=%s field=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d retry_attempt=%d prompt_cost_usd=%.6f completion_cost_usd=%.6f estimated_cost_usd=%.6f batch_estimated_cost_usd=%.6f max_cost_usd=%s",
            model_name,
            asin,
            field,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            retry_attempt,
            prompt_cost_usd,
            completion_cost_usd,
            estimated_cost_usd,
            self._batch_usage.estimated_cost_usd,
            _format_optional_usd(self.config.max_cost_usd),
        )
        self._enforce_cost_limit()

        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI chat response missing message content") from exc

    def _enforce_cost_limit(self) -> None:
        if self.config.max_cost_usd is None:
            return
        estimated_cost_usd = self._batch_usage.estimated_cost_usd
        if estimated_cost_usd >= self.config.max_cost_usd:
            raise OpenAIBudgetExceededError(
                max_cost_usd=self.config.max_cost_usd,
                estimated_cost_usd=estimated_cost_usd,
            )

    def _resolve_chat_input_price(self, model_name: str) -> float | None:
        if _normalize_model_name(model_name) == _normalize_model_name(self.config.chat_model):
            return self.config.chat_input_cost_per_1m_tokens
        return _resolve_chat_input_model_price(model_name, None)

    def _resolve_chat_output_price(self, model_name: str) -> float | None:
        if _normalize_model_name(model_name) == _normalize_model_name(self.config.chat_model):
            return self.config.chat_output_cost_per_1m_tokens
        return _resolve_chat_output_model_price(model_name, None)

    def _post_json(self, path: str, payload: dict) -> dict:
        self._raise_if_cancelled()
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.config.api_base}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI request failed status={exc.code} body={details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"OpenAI request failed: {exc.reason}") from exc

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OpenAIRunCancelledError("OpenAI operation cancelled")

    def _should_retry_with_inline_images(self, exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "invalid_image_url" in message or "timeout while downloading" in message

    def _convert_image_inputs_to_data_urls(self, image_inputs: list[dict[str, str]]) -> list[dict[str, str]]:
        """Force inline data URLs, ignoring ``allow_remote_image_urls``.

        Used by the retry path after OpenAI failed to fetch a remote image itself.
        """
        return [
            prepared
            for _, prepared in self.prepare_reachable_image_inputs(image_inputs, force_inline=True)
        ]

    def prepare_reachable_image_inputs(
        self, image_inputs: list[dict[str, str]], *, force_inline: bool = False
    ) -> list[tuple[int, dict[str, str]]]:
        """Resolve inputs up front as ``(original_index, prepared_input)`` pairs.

        Callers whose prompt text is positional ("classify these in the same order")
        MUST use this before building that text and keep only the returned indices.
        Dropping an unreachable image later would leave more prompt slots than images
        and shift every description onto the wrong picture.
        """
        prepared: list[tuple[int, dict[str, str]]] = []
        failures: list[str] = []
        for index, image_input in enumerate(image_inputs):
            image_url = str(image_input.get("url", "") or "").strip()
            if not image_url:
                continue
            detail = str(image_input.get("detail", "auto") or "auto").strip() or "auto"
            if (self.config.allow_remote_image_urls and not force_inline) or image_url.startswith("data:"):
                prepared.append((index, {"url": image_url, "detail": detail}))
                continue
            try:
                data_url = self._download_image_data_url(image_url)
            except OpenAIRunCancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Skipping unreachable gallery image url=%s reason=%s", image_url, exc)
                failures.append(image_url)
                continue
            prepared.append((index, {"url": data_url, "detail": detail}))
        if failures and not prepared:
            raise RuntimeError(f"All {len(failures)} image download(s) failed; first url={failures[0]}")
        if failures:
            LOGGER.info(
                "Continuing with %d of %d images (%d unreachable)",
                len(prepared), len(prepared) + len(failures), len(failures),
            )
        return prepared

    def _prepare_image_inputs_for_request(self, image_inputs: list[dict[str, str]]) -> list[dict[str, str]]:
        return [prepared for _, prepared in self.prepare_reachable_image_inputs(image_inputs)]

    def _download_image_data_url(self, image_url: str) -> str:
        cached = self._image_data_url_cache.get(image_url)
        if cached is not None:
            return cached

        self._raise_if_cancelled()
        req = request.Request(
            url=image_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            method="GET",
        )
        # Amazon's CDN resets connections occasionally (WinError 10054 mid-handshake).
        # That is transient, so retry before giving up; 4xx responses are not.
        image_bytes = b""
        content_type = ""
        for attempt in range(_IMAGE_DOWNLOAD_ATTEMPTS):
            self._raise_if_cancelled()
            try:
                with request.urlopen(req, timeout=min(self.config.timeout_seconds, 30)) as response:
                    image_bytes = response.read()
                    content_type = str(response.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
                break
            except error.HTTPError as exc:
                if exc.code < 500 or attempt == _IMAGE_DOWNLOAD_ATTEMPTS - 1:
                    details = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Image download failed status={exc.code} url={image_url} body={details}"
                    ) from exc
                last_reason: object = f"status={exc.code}"
            except error.URLError as exc:
                if attempt == _IMAGE_DOWNLOAD_ATTEMPTS - 1:
                    raise RuntimeError(f"Image download failed url={image_url} reason={exc.reason}") from exc
                last_reason = exc.reason
            LOGGER.debug(
                "Retrying image download attempt=%d/%d url=%s reason=%s",
                attempt + 2, _IMAGE_DOWNLOAD_ATTEMPTS, image_url, last_reason,
            )
            time.sleep(_IMAGE_DOWNLOAD_BACKOFF_SECONDS * (2 ** attempt))

        if not image_bytes:
            raise RuntimeError(f"Image download returned empty body url={image_url}")

        if not content_type.startswith("image/"):
            guessed_type, _ = mimetypes.guess_type(image_url)
            content_type = guessed_type or "image/jpeg"
        data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        self._image_data_url_cache[image_url] = data_url
        return data_url


def _coerce_usage_value(usage: object, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _estimate_cost_usd(tokens: int, rate_per_1m_tokens: float | None) -> float:
    if rate_per_1m_tokens is None:
        return 0.0
    return (tokens / 1_000_000) * rate_per_1m_tokens


def _format_optional_usd(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.6f}"


def _resolve_embedding_model_price(model_name: str, explicit_price: float | None) -> float | None:
    if explicit_price is not None:
        return explicit_price
    normalized_model_name = _normalize_model_name(model_name)
    return _KNOWN_EMBEDDING_MODEL_PRICES.get(normalized_model_name)


def _resolve_chat_input_model_price(model_name: str, explicit_price: float | None) -> float | None:
    if explicit_price is not None:
        return explicit_price
    pricing = _KNOWN_CHAT_MODEL_PRICES.get(_normalize_model_name(model_name))
    return pricing.input_cost_per_1m_tokens if pricing is not None else None


def _resolve_chat_output_model_price(model_name: str, explicit_price: float | None) -> float | None:
    if explicit_price is not None:
        return explicit_price
    pricing = _KNOWN_CHAT_MODEL_PRICES.get(_normalize_model_name(model_name))
    return pricing.output_cost_per_1m_tokens if pricing is not None else None


def _normalize_model_name(model_name: str) -> str:
    normalized = (model_name or "").strip().lower()
    if normalized in _KNOWN_EMBEDDING_MODEL_PRICES or normalized in _KNOWN_CHAT_MODEL_PRICES:
        return normalized

    normalized = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", normalized)
    if normalized.endswith("-latest"):
        normalized = normalized[: -len("-latest")]
    return normalized


def _read_optional_float_env(name: str) -> float | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise OpenAIConfigError(f"{name} must be a valid number") from exc
    if value < 0:
        raise OpenAIConfigError(f"{name} must be >= 0")
    return value


def _read_optional_bool_env(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise OpenAIConfigError(f"{name} must be a boolean value like true/false")
