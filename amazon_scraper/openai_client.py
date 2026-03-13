from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List
from urllib import error, request


class OpenAIConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    embedding_model: str
    chat_model: str
    api_base: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "OpenAIConfig":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "").strip()
        chat_model = os.getenv("OPENAI_CHAT_MODEL", "").strip()
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
        return cls(api_key=api_key, embedding_model=embedding_model, chat_model=chat_model)


class OpenAIClient:
    def __init__(self, config: OpenAIConfig) -> None:
        self.config = config

    def create_embeddings(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        payload = {
            "model": self.config.embedding_model,
            "input": texts,
        }
        response = self._post_json("/embeddings", payload)
        data = response.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OpenAI embeddings response missing data array")
        return [item["embedding"] for item in data]

    def create_chat_completion(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.config.chat_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = self._post_json("/chat/completions", payload)
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI chat response missing message content") from exc

    def _post_json(self, path: str, payload: dict) -> dict:
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
