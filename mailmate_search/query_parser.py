"""Local natural-language query parser for metadata filters."""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mailmate_search.config import config

logger = logging.getLogger(__name__)


@dataclass
class ParsedQuery:
    """Structured query plan extracted from natural language."""

    semantic_query: str
    from_addr: Optional[str] = None
    to_addr: Optional[str] = None
    subject: Optional[str] = None
    subject_like: Optional[str] = None
    date_after: Optional[str] = None
    date_before: Optional[str] = None
    has_attachments: Optional[bool] = None
    attachment_type: Optional[str] = None
    attachment_name: Optional[str] = None


class LocalQueryParser:
    """Parses natural-language query text into structured filters via local LLM."""

    def __init__(self) -> None:
        self.endpoint = config.query_parser_endpoint
        self.model = config.query_parser_model
        self.timeout_seconds = config.query_parser_timeout_seconds

    def parse(self, query: str) -> Optional[ParsedQuery]:
        """Return parsed query plan, or None if unavailable/invalid."""
        if not query.strip():
            return None

        try:
            raw_text = self._call_ollama_generate(query)
            json_payload = self._extract_json_object(raw_text)
            if not json_payload:
                return None
            return self._coerce_parsed_query(json_payload, original_query=query)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.debug(f"Query parser endpoint unavailable: {e}")
            return None
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            logger.debug(f"Query parser response invalid: {e}")
            return None

    def _call_ollama_generate(self, query: str) -> str:
        """Call a local Ollama-compatible generate endpoint."""
        prompt = self._build_prompt(query)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0,
            },
        }
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            content = resp.read().decode("utf-8")

        data = json.loads(content)
        response_text = data.get("response", "")
        if not isinstance(response_text, str):
            raise ValueError("Expected string response from parser model")
        return response_text

    def _build_prompt(self, query: str) -> str:
        """Build a strict JSON-output prompt."""
        return f"""
You are a query planner for email search.
Convert the user query into a STRICT JSON object with these keys only:
- semantic_query (string)
- from_addr (string or null)
- to_addr (string or null)
- subject (string or null)
- subject_like (string or null)
- date_after (YYYY-MM-DD string or null)
- date_before (YYYY-MM-DD string or null)
- has_attachments (true, false, or null)
- attachment_type (string like "pdf" without dot, or null)
- attachment_name (string or null)

Rules:
- Output JSON only, no markdown, no explanations.
- Keep semantic_query concise and focused on meaning.
- Use null for unknown fields.
- If query asks for attached files, set has_attachments=true.
- If query asks for no attachments, set has_attachments=false.
- For relative dates (e.g. "last month"), infer concrete YYYY-MM-DD using current local date context.
- Prefer subject_like instead of subject unless exact subject is explicitly requested.

User query: {query}
""".strip()

    def _extract_json_object(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """Extract first top-level JSON object from model output."""
        if not raw_text:
            return None
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = raw_text[start : end + 1]
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _coerce_parsed_query(
        self, payload: Dict[str, Any], original_query: str
    ) -> ParsedQuery:
        """Normalize arbitrary JSON payload into ParsedQuery."""

        def text_or_none(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                cleaned = value.strip()
                return cleaned or None
            return None

        def bool_or_none(value: Any) -> Optional[bool]:
            if isinstance(value, bool):
                return value
            if value is None:
                return None
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "yes"}:
                    return True
                if lowered in {"false", "no"}:
                    return False
            return None

        semantic_query = text_or_none(payload.get("semantic_query")) or original_query
        attachment_type = text_or_none(payload.get("attachment_type"))
        if attachment_type:
            attachment_type = attachment_type.lstrip(".").lower()

        return ParsedQuery(
            semantic_query=semantic_query,
            from_addr=text_or_none(payload.get("from_addr")),
            to_addr=text_or_none(payload.get("to_addr")),
            subject=text_or_none(payload.get("subject")),
            subject_like=text_or_none(payload.get("subject_like")),
            date_after=text_or_none(payload.get("date_after")),
            date_before=text_or_none(payload.get("date_before")),
            has_attachments=bool_or_none(payload.get("has_attachments")),
            attachment_type=attachment_type,
            attachment_name=text_or_none(payload.get("attachment_name")),
        )
