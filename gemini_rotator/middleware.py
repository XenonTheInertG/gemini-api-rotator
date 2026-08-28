"""
gemini_rotator.middleware — high-level async execute() wrapper.

This is the preferred interface for most users:

    result = await rotator.execute("your prompt", model="gemini-2.5-flash")

Handles key selection, acquire/release, timing, error classification,
retry with backoff, and stat recording — automatically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, List, Optional, Union

from gemini_rotator.models import ErrorType, classify_error

logger = logging.getLogger(__name__)


class RotatorMiddleware:
    """
    Mixin / standalone wrapper that adds async execute() to a GeminiAPIRotator.

    GeminiAPIRotator inherits from this — you do not instantiate it directly.
    """

    # These are satisfied by GeminiAPIRotator
    _keys:    list

    def get_next_working_key(self) -> Optional[str]: ...
    def acquire(self, key: str) -> None: ...
    def release(self, key: str) -> None: ...
    def record_outcome(self, key, exc=None, latency=None, model=None) -> None: ...

    # ── execute() ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        prompt:        Union[str, list],
        *,
        model:         str                 = "gemini-2.5-flash",
        max_tokens:    int                 = 1024,
        temperature:   Optional[float]     = None,
        system:        Optional[str]       = None,
        extra_config:  Optional[dict]      = None,
        priority:      int                 = 0,          # reserved for queue integration
    ) -> Any:
        """
        Execute a Gemini request with automatic key rotation, retry, and stat recording.

        Parameters
        ----------
        prompt : str | list
            The prompt string, or a list of content parts accepted by google-genai.
        model : str
            Gemini model string. Default: "gemini-2.5-flash".
        max_tokens : int
            Maximum tokens in the response. Default: 1024.
        temperature : float | None
            Sampling temperature. None = model default.
        system : str | None
            Optional system instruction.
        extra_config : dict | None
            Additional kwargs passed to GenerateContentConfig.

        Returns
        -------
        google.genai.types.GenerateContentResponse
            Raw response object. Access .text for the string output.

        Raises
        ------
        RuntimeError
            If all keys are suspended and no key is available.
        Exception
            Re-raises the last exception after all retries are exhausted.
        """
        try:
            import google.genai as genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError(
                "google-genai is required to use execute(). "
                "Install it: pip install google-genai"
            )

        cfg        = self.config
        last_exc   = None

        for attempt in range(1, cfg.max_retries + 1):
            key = self.get_next_working_key()
            if key is None:
                raise RuntimeError(
                    "No API keys available — all keys are suspended. "
                    "Check rotator.status() for details."
                )

            self.acquire(key)
            t_start = time.perf_counter()
            try:
                # Build GenerateContentConfig
                gc_kwargs: dict = {"max_output_tokens": max_tokens}
                if temperature is not None:
                    gc_kwargs["temperature"] = temperature
                if system is not None:
                    gc_kwargs["system_instruction"] = system
                if extra_config:
                    gc_kwargs.update(extra_config)

                gen_config = genai_types.GenerateContentConfig(**gc_kwargs)
                client     = genai.Client(api_key=key)

                # Run blocking call in executor so we don't block the event loop
                loop     = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model    = model,
                        contents = prompt if isinstance(prompt, list) else [prompt],
                        config   = gen_config,
                    ),
                )

                latency = time.perf_counter() - t_start
                self.record_outcome(key, exc=None, latency=latency, model=model)
                return response

            except Exception as exc:
                latency   = time.perf_counter() - t_start
                etype     = classify_error(exc)
                last_exc  = exc

                self.record_outcome(key, exc=exc, latency=None, model=model)

                if etype == ErrorType.SUSPENDED:
                    # Key is permanently broken — no point retrying it, try next
                    logger.warning(
                        "[Middleware] %s suspended on attempt %d/%d — switching key.",
                        key[:4] + "...", attempt, cfg.max_retries,
                    )
                    continue

                if etype == ErrorType.RATE_LIMIT:
                    if attempt < cfg.max_retries:
                        await asyncio.sleep(cfg.retry_delay_seconds * attempt)
                    continue

                if etype == ErrorType.TRANSIENT:
                    if attempt < cfg.max_retries:
                        await asyncio.sleep(cfg.retry_delay_seconds * attempt)
                    continue

                # Unknown error — re-raise immediately
                raise

            finally:
                self.release(key)

        raise last_exc  # type: ignore[misc]

    async def execute_batch(
        self,
        prompts:      List[Union[str, list]],
        *,
        model:        str               = "gemini-2.5-flash",
        max_tokens:   int               = 1024,
        concurrency:  int               = 5,
        **kwargs,
    ) -> List[Any]:
        """
        Execute multiple prompts concurrently with a semaphore cap.

        Parameters
        ----------
        prompts : list
            List of prompt strings or content-part lists.
        concurrency : int
            Maximum simultaneous requests in flight. Default: 5.

        Returns
        -------
        list
            Responses in the same order as prompts.
            Failed items are Exception instances rather than raising.
        """
        sem     = asyncio.Semaphore(concurrency)
        results = [None] * len(prompts)

        async def _one(idx: int, prompt) -> None:
            async with sem:
                try:
                    results[idx] = await self.execute(
                        prompt,
                        model=model,
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                except Exception as exc:
                    results[idx] = exc

        await asyncio.gather(*(_one(i, p) for i, p in enumerate(prompts)))
        return results
