"""Provider-agnostic LLM client (Fireworks by default).

Two deliberate choices:

1. **We validate every response ourselves.** Fireworks accepts a json_schema
   response_format, but it is documented to silently downgrade strict schema
   enforcement to free-form JSON mode in some configurations. A silently
   unenforced schema is worse than none, because you stop checking. So the
   schema is sent *and* the result is parsed through pydantic locally.

2. **A bad response is discarded, never repaired.** One retry, then the event
   is dropped and logged. Coaxing a malformed answer into shape is how a
   language model ends up deciding something it was never asked to decide.

The provider is swappable by editing config/default.yaml — the base_url and
model ids are the only provider-specific values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from glassbox.config import require_env

T = TypeVar("T", bound=BaseModel)


class LlmUnavailableError(Exception):
    """Provider call failed. The caller drops the event; it never guesses."""


class LlmSchemaError(Exception):
    """Response did not satisfy the schema after a retry."""


@dataclass
class LlmClient:
    base_url: str
    api_key: str
    timeout: float = 20.0
    _client: object | None = None

    @classmethod
    def from_config(cls, cfg) -> LlmClient:
        return cls(
            base_url=cfg.llm.base_url,
            api_key=require_env("FIREWORKS_API_KEY"),
            timeout=cfg.llm.timeout_seconds,
        )

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self.base_url, api_key=self.api_key, timeout=self.timeout
            )
        return self._client

    def extract(
        self,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> T:
        """Return a validated instance of `schema`, or raise. Never returns
        partial or coerced data."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response_format = {
            "type": "json_object",
            "schema": schema.model_json_schema(),
        }

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format=response_format,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                # equivalent here: we drop the event rather than guess at it.
                raise LlmUnavailableError(f"{type(e).__name__}: {e}") from e

            raw = completion.choices[0].message.content or ""
            try:
                return schema.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as e:
                last_error = e
                if attempt == 0:
                    # Show the model its own failure rather than re-asking blindly.
                    messages.append({"role": "assistant", "content": raw[:500]})
                    messages.append(
                        {
                            "role": "user",
                            "content": "That did not match the required schema. "
                            "Return only valid JSON matching it exactly.",
                        }
                    )

        raise LlmSchemaError(f"schema not satisfied after retry: {last_error}")

    def extract_text(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> str:
        """Free-form prose. Used only for the nightly narrative — never in the
        trading path, where every model output is schema-validated."""
        try:
            completion = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            raise LlmUnavailableError(f"{type(e).__name__}: {e}") from e
        text = (completion.choices[0].message.content or "").strip()
        if not text:
            # Some reasoning models return their answer on a separate channel and
            # leave content empty. Treat that as unavailable rather than writing
            # an empty section — the caller degrades to numbers only.
            raise LlmUnavailableError(f"{model} returned empty content")
        return text

    def list_models(self) -> list[str]:
        """Model ids available to this account — model names change, so we look
        them up rather than trusting a hardcoded string."""
        return sorted(m.id for m in self.client.models.list().data)
