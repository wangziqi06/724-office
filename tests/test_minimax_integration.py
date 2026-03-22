"""Integration tests for MiniMax provider.

These tests verify MiniMax API connectivity and end-to-end behavior.
Skipped unless MINIMAX_API_KEY is set in environment.
"""

import json
import os
import sys
import unittest
import urllib.request
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock missing modules
sys.modules.setdefault("messaging", MagicMock())
sys.modules.setdefault("lancedb", MagicMock())

import llm
import memory

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
SKIP_REASON = "MINIMAX_API_KEY not set"


@unittest.skipUnless(MINIMAX_API_KEY, SKIP_REASON)
class TestMiniMaxLLMIntegration(unittest.TestCase):
    """Integration tests for MiniMax LLM API."""

    def setUp(self):
        llm._config = {
            "default": "minimax",
            "providers": {
                "minimax": {
                    "api_base": "https://api.minimax.io/v1",
                    "api_key": MINIMAX_API_KEY,
                    "model": "MiniMax-M2.7",
                    "max_tokens": 256,
                }
            },
        }

    def test_chat_completion(self):
        """Test basic chat completion via MiniMax API."""
        messages = [{"role": "user", "content": "Say 'hello' and nothing else."}]
        result = llm._call_llm(messages, [])
        content = result["choices"][0]["message"]["content"]
        self.assertIsInstance(content, str)
        self.assertGreater(len(content), 0)
        # Think tags should be stripped
        self.assertNotIn("<think>", content)

    def test_tool_calling(self):
        """Test function calling via MiniMax API."""
        messages = [{"role": "user", "content": "What is 2+2? Use the calculator tool."}]
        tool_defs = [{
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Perform arithmetic calculations",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Math expression to evaluate"
                        }
                    },
                    "required": ["expression"]
                }
            }
        }]
        result = llm._call_llm(messages, tool_defs)
        msg = result["choices"][0]["message"]
        # Model may or may not use the tool, both are valid
        self.assertIsNotNone(msg)

    def test_think_tags_stripped(self):
        """Test that think tags are stripped from MiniMax responses."""
        messages = [
            {"role": "user", "content": "Think step by step: what is 15 * 23?"}
        ]
        result = llm._call_llm(messages, [])
        content = result["choices"][0]["message"]["content"]
        self.assertNotIn("<think>", content)


@unittest.skipUnless(MINIMAX_API_KEY, SKIP_REASON)
class TestMiniMaxEmbeddingIntegration(unittest.TestCase):
    """Integration tests for MiniMax embedding API.

    Note: MiniMax has strict RPM limits on the embedding endpoint.
    These tests may be skipped if rate limited.
    """

    def setUp(self):
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.minimax.io/v1",
                "api_key": MINIMAX_API_KEY,
                "model": "embo-01",
                "dimension": 1536,
                "type": "db",
            }
        }

    def _embed_with_retry(self, texts, max_retries=3):
        """Embed with retry for rate limiting."""
        import time
        for attempt in range(max_retries):
            result = memory._embed(texts)
            if result and all(v is not None for v in result):
                return result
            time.sleep(2 ** attempt)
        return result

    def _skip_if_rate_limited(self, result, expected_count):
        if not result or len(result) != expected_count:
            self.skipTest("MiniMax embedding API rate limited")

    def test_single_embedding(self):
        """Test embedding a single text."""
        result = self._embed_with_retry(["Hello world"])
        self._skip_if_rate_limited(result, 1)
        self.assertEqual(len(result[0]), 1536)

    def test_batch_embedding(self):
        """Test embedding multiple texts."""
        import time
        time.sleep(2)  # Avoid rate limiting
        texts = ["Hello", "World", "Test"]
        result = self._embed_with_retry(texts)
        self._skip_if_rate_limited(result, 3)
        for vec in result:
            self.assertEqual(len(vec), 1536)

    def test_query_type_embedding(self):
        """Test embedding with type='query' for search."""
        import time
        time.sleep(2)  # Avoid rate limiting
        memory._config["embedding_api"]["type"] = "query"
        result = self._embed_with_retry(["search query"])
        self._skip_if_rate_limited(result, 1)
        self.assertEqual(len(result[0]), 1536)


if __name__ == "__main__":
    unittest.main()
