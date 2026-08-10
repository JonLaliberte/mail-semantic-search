"""Local-cache-first loading for Hugging Face models.

Sentence-transformers revalidates every model file against huggingface.co on each
load, so a cached model still depends on the network at import time. When the Hub
is unreachable that costs minutes of hangs, and on a connection error
`huggingface_hub` closes its shared httpx client before retrying with it, turning a
transient network blip into `RuntimeError: Cannot send a request, as the client has
been closed.` Loading from the local cache first keeps routine runs entirely
offline; only a genuinely missing model reaches the network.
"""

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def load_model_local_first(loader: Callable[..., Any], model_name: str, **kwargs: Any) -> Any:
    """Load a model from the local cache, downloading it only if it isn't there."""
    try:
        return loader(model_name, local_files_only=True, **kwargs)
    except Exception as exc:  # not cached, or cache incomplete
        logger.info(
            "Model %s not available in local cache (%s); fetching from Hugging Face",
            model_name,
            exc,
        )
        return loader(model_name, **kwargs)
