# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import os
import time
from functools import partial

from ori.network.events import ReasoningResult

logger = logging.getLogger(__name__)

try:
    from llama_cpp import Llama  # type: ignore[import-untyped]

    _LLAMA_AVAILABLE = True
except ImportError:
    _LLAMA_AVAILABLE = False


class ModelNotAvailableError(Exception):
    """Raised when the model file is missing or llama-cpp-python is not installed."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class LocalLLM:
    """Thin asyncio wrapper around a llama-cpp-python ``Llama`` instance.

    The model is loaded lazily on the first :meth:`reason` call so that startup
    time is not penalised when the model is ultimately not needed (e.g. when a
    rule engine match bypasses the LLM entirely).

    **Action tier authority:** :meth:`reason` always returns
    ``action_tier='A'``.  The Intelligence Elevator overwrites this with the
    tier declared in the skill YAML trigger.  The LLM never has authority over
    what physical action is taken — it only provides reasoning text and a
    confidence estimate.

    Args:
        model_path: Absolute path to a GGUF model file.
        context_window: Token context window passed to ``Llama(n_ctx=...)``.
    """

    def __init__(self, model_path: str, context_window: int = 2048) -> None:
        self._model_path = model_path
        self._context_window = context_window
        self._llm: object | None = None  # Llama instance, populated on first call

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """``True`` if llama-cpp-python is installed and the model file exists."""
        if not _LLAMA_AVAILABLE:
            return False
        return os.path.isfile(self._model_path)

    async def reason(self, prompt: str, max_tokens: int = 200) -> ReasoningResult:
        """Run inference and return a :class:`~ori.network.events.ReasoningResult`.

        Loads the model on the first call.  Subsequent calls reuse the loaded
        instance.

        The returned ``action_tier`` is always ``'A'`` — the elevator is
        responsible for setting the real tier from the skill configuration.

        Args:
            prompt: The formatted prompt string (built by the elevator).
            max_tokens: Maximum tokens to generate.

        Returns:
            :class:`~ori.network.events.ReasoningResult` with ``tier='local_slm'``
            and ``action_tier='A'``.

        Raises:
            :exc:`ModelNotAvailableError`: llama-cpp-python is not installed or
                the model file does not exist.
        """
        if not _LLAMA_AVAILABLE:
            raise ModelNotAvailableError(
                "LocalLLM: llama-cpp-python is not installed. "
                "Run: pip install llama-cpp-python"
            )
        if not os.path.isfile(self._model_path):
            raise ModelNotAvailableError(
                f"LocalLLM: model file not found: '{self._model_path}'"
            )

        await self._ensure_loaded()

        loop = asyncio.get_running_loop()
        start_ms = _now_ms()

        output = await loop.run_in_executor(
            None,
            partial(
                self._infer,
                prompt=prompt,
                max_tokens=max_tokens,
            ),
        )

        latency_ms = _now_ms() - start_ms
        text = output["choices"][0]["text"].strip()
        tokens_used = output["usage"]["completion_tokens"]

        # Confidence is not reliably extractable from a base LLM completion.
        # Default to 0.0; the elevator may override via post-processing.
        return ReasoningResult(
            text=text,
            tier="local_slm",
            model=os.path.basename(self._model_path),
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            confidence=0.0,
            action_tier="A",  # always — see class docstring
            proposed_action=None,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _ensure_loaded(self) -> None:
        """Load the model in a thread-pool executor if not already loaded."""
        if self._llm is not None:
            return
        loop = asyncio.get_running_loop()
        logger.info(
            "LocalLLM: loading model '%s' (n_ctx=%d) …",
            self._model_path,
            self._context_window,
        )
        self._llm = await loop.run_in_executor(None, self._load_model)
        logger.info("LocalLLM: model loaded")

    def _load_model(self) -> object:
        return Llama(
            model_path=self._model_path,
            n_ctx=self._context_window,
            n_threads=4,
            n_gpu_layers=0,
            verbose=False,
        )

    def _infer(self, prompt: str, max_tokens: int) -> dict:
        return self._llm(  # type: ignore[operator]
            prompt,
            max_tokens=max_tokens,
            temperature=0.1,
            stop=["\n\n"],
            echo=False,
        )
