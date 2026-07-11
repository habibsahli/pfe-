from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        embedding_model: str | None = None,
        llm_model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.OLLAMA_HOST).rstrip("/")
        self.embedding_model = embedding_model or settings.OLLAMA_EMBEDDING_MODEL
        self.llm_model = llm_model or settings.OLLAMA_LLM_MODEL
        self.timeout = timeout or settings.OLLAMA_TIMEOUT

    def _request(self, method: str, path: str, payload: dict[str, Any], retries: int = 2, timeout: int | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        effective_timeout = timeout if timeout is not None else self.timeout
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=effective_timeout) as client:
                    response = client.request(method, url, json=payload)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
                    continue
        raise RuntimeError(f"Ollama request failed for {path}: {last_exc}")

    def health_check(self) -> bool:
        try:
            with httpx.Client(timeout=max(5, int(self.timeout / 4))) as client:
                response = client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            payload = self._request("GET", "/api/tags", {})
            models = payload.get("models", [])
            return [m.get("name", "") for m in models if m.get("name")]
        except Exception:
            return []

    def embed_text(self, text_value: str, model: str | None = None) -> list[float]:
        embed_payload = {
            "model": model or self.embedding_model,
            "prompt": text_value,
        }
        response = self._request(
            "POST",
            "/api/embed",
            {
                "model": embed_payload["model"],
                "input": text_value,
                "prompt": text_value,
            },
        )
        embedding = response.get("embedding")
        if embedding and isinstance(embedding, list):
            return [float(x) for x in embedding]

        embeddings = response.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            first = embeddings[0]
            if isinstance(first, list):
                return [float(x) for x in first]

        raise RuntimeError("Ollama embedding response did not contain an embedding vector")

    def embed_batch(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        return [self.embed_text(text_value=t, model=model) for t in texts]

    def generate_with_meta(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        timeout: int | None = None,
        retries: int = 2,
    ) -> tuple[str, dict[str, Any]]:
        """Generate text and also return Ollama's usage metadata.

        The metadata dict carries token accounting so callers can attach it to
        observability spans:
          - prompt_tokens     : tokens in system + prompt (Ollama prompt_eval_count)
          - completion_tokens : tokens generated (Ollama eval_count)
          - total_tokens      : prompt_tokens + completion_tokens
          - total_duration_ms : end-to-end server time for the call
        Missing counters (older Ollama builds) come back as None.
        """
        payload = {
            "model": model or self.llm_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt

        effective_timeout = timeout if timeout is not None else self.timeout
        response = self._request("POST", "/api/generate", payload, retries=retries, timeout=effective_timeout)
        text_out = response.get("response")
        if not (isinstance(text_out, str) and text_out.strip()):
            raise RuntimeError(f"Ollama returned an empty response for model '{model or self.llm_model}'.")

        prompt_tokens = response.get("prompt_eval_count")
        completion_tokens = response.get("eval_count")
        total_tokens = (
            (prompt_tokens or 0) + (completion_tokens or 0)
            if (prompt_tokens is not None or completion_tokens is not None)
            else None
        )
        total_duration = response.get("total_duration")  # nanoseconds
        meta = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "total_duration_ms": (total_duration / 1e6) if total_duration else None,
        }
        return text_out.strip(), meta

    def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        timeout: int | None = None,
        retries: int = 2,
    ) -> str:
        text_out, _ = self.generate_with_meta(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
        )
        return text_out

    def generate_strict(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        timeout: int | None = None,
        retries: int = 2,
    ) -> str:
        """Generate text and raise if the target model is unavailable."""
        return self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        timeout: int | None = None,
        retries: int = 2,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Chat completion with optional tool-calling via Ollama's /api/chat.

        This is the tool-calling counterpart to ``generate_with_meta`` and is the
        LLM backend for the agent runtime (``app.agents``). When ``tools`` is
        supplied, a tool-capable model (e.g. llama3.1) may return tool calls
        instead of text; the caller is responsible for the dispatch loop.

        Args:
            messages: chat history, each ``{"role": "system|user|assistant|tool",
                "content": str, ...}``. Assistant tool-call turns and ``tool``
                result turns are appended by the caller between iterations.
            tools: OpenAI-style function specs
                (``{"type": "function", "function": {...}}``); omit for plain chat.

        Returns:
            (message, meta) where ``message`` is the assistant message dict
            (``role``, ``content``, and optional ``tool_calls``) and ``meta``
            carries the same token/duration accounting as ``generate_with_meta``.
        """
        payload: dict[str, Any] = {
            "model": model or self.llm_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        effective_timeout = timeout if timeout is not None else self.timeout
        response = self._request("POST", "/api/chat", payload, retries=retries, timeout=effective_timeout)

        message = response.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"Ollama /api/chat returned no message for model '{model or self.llm_model}'.")

        prompt_tokens = response.get("prompt_eval_count")
        completion_tokens = response.get("eval_count")
        total_tokens = (
            (prompt_tokens or 0) + (completion_tokens or 0)
            if (prompt_tokens is not None or completion_tokens is not None)
            else None
        )
        total_duration = response.get("total_duration")  # nanoseconds
        meta = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "total_duration_ms": (total_duration / 1e6) if total_duration else None,
        }
        return message, meta


ollama_client = OllamaClient()
