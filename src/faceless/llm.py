"""Local model access via Ollama.

Everything the pipeline asks a model for is small and structured - a search
query, a one-line visual description, a choice between candidates - so this
stays a single schema-constrained call with no streaming and no SDK.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemma4:12b"
DEFAULT_HOST = "http://localhost:11434"
# Warm calls answer in about a second, but the first one has to page a ~8GB
# model in from disk, which can take minutes on a cold cache.
DEFAULT_TIMEOUT = 900
# Hold the model resident between calls; a harvest makes one per clip and
# reloading between them would dominate the runtime.
KEEP_ALIVE = "30m"


class LLMError(RuntimeError):
    """Raised when the local model is unreachable or answers unusably."""


def host() -> str:
    return os.environ.get("FACELESS_OLLAMA_HOST", DEFAULT_HOST).rstrip("/")


def default_model() -> str:
    return os.environ.get("FACELESS_OLLAMA_MODEL", DEFAULT_MODEL)


def _request(path: str, payload: dict | None, timeout: int) -> dict:
    # urllib sends a GET when data is None, which is what /api/tags expects.
    request = urllib.request.Request(
        f"{host()}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise LLMError(f"ollama returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"cannot reach ollama at {host()} ({exc.reason}). Is `ollama serve` running?"
        ) from exc
    except TimeoutError as exc:
        raise LLMError(f"ollama timed out after {timeout}s") from exc


def available_models() -> list[str]:
    return [model.get("name", "") for model in _request("/api/tags", None, 10).get("models", [])]


def warm(model: str | None = None) -> None:
    """Load the model before batch work starts.

    Without this the first real call absorbs the whole cold-load wait, which
    looks like a hang rather than a one-off cost.
    """
    model = model or default_model()
    installed = available_models()
    if model not in installed:
        raise LLMError(
            f"model {model!r} is not installed. Available: {', '.join(installed) or 'none'}. "
            f"Pull it with `ollama pull {model}`."
        )
    _request("/api/generate", {"model": model, "keep_alive": KEEP_ALIVE}, DEFAULT_TIMEOUT)


def generate_json(
    prompt: str,
    schema: dict,
    *,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    temperature: float = 0.2,
    attempts: int = 3,
) -> dict:
    """Prompt the model and get back JSON matching `schema`.

    Ollama constrains decoding to the schema, but constrained decoding still
    occasionally stops mid-string and yields truncated JSON - rare per call, and
    a certainty across the hundreds of calls a harvest makes, so a failed parse
    is retried rather than raised. A model can also answer with the right shape
    and the wrong content, which is why callers validate the values they use.
    """
    model = model or default_model()
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_predict": 512},
    }
    last_raw = ""
    for attempt in range(attempts):
        body = _request("/api/generate", payload, timeout)
        last_raw = (body.get("response") or "").strip()
        if last_raw:
            try:
                return json.loads(last_raw)
            except json.JSONDecodeError:
                pass
        # Nudge sampling off the path that derailed, rather than replaying it.
        payload["options"] = {**payload["options"], "temperature": temperature + 0.15 * (attempt + 1)}
    raise LLMError(f"{model} returned unparseable JSON after {attempts} attempts: {last_raw[:200]}")
