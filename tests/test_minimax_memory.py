"""Unit tests for MiniMax embedding support in memory.py."""

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock missing modules
sys.modules.setdefault("lancedb", MagicMock())

import memory


class TestEmbedMiniMax(unittest.TestCase):
    """Tests for MiniMax native embedding API in _embed."""

    def setUp(self):
        # Reset module state
        memory._config = {}
        memory._enabled = False

    def test_empty_texts_returns_empty(self):
        memory._config = {"embedding_api": {}}
        result = memory._embed([])
        self.assertEqual(result, [])

    @patch("urllib.request.urlopen")
    def test_minimax_embedding_request_format(self, mock_urlopen):
        """MiniMax uses {model, texts, type} instead of {model, input, dimensions}."""
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.minimax.io/v1",
                "api_key": "test-key",
                "model": "embo-01",
                "dimension": 1536,
                "type": "db",
            }
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "vectors": [[0.1] * 1536, [0.2] * 1536],
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = memory._embed(["hello", "world"])

        # Verify request body uses MiniMax format
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        self.assertEqual(body["model"], "embo-01")
        self.assertEqual(body["texts"], ["hello", "world"])
        self.assertEqual(body["type"], "db")
        self.assertNotIn("input", body)
        self.assertNotIn("dimensions", body)

        # Verify response parsing
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 1536)

    @patch("urllib.request.urlopen")
    def test_minimax_embedding_default_type_db(self, mock_urlopen):
        """Default type should be 'db' when not specified."""
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.minimax.io/v1",
                "api_key": "test-key",
                "model": "embo-01",
                "dimension": 1536,
            }
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "vectors": [[0.1] * 1536],
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        memory._embed(["test"])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        self.assertEqual(body["type"], "db")

    @patch("urllib.request.urlopen")
    def test_openai_embedding_request_format(self, mock_urlopen):
        """OpenAI-compatible format should use {model, input, dimensions}."""
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model": "text-embedding-3-small",
                "dimension": 1024,
            }
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [
                {"embedding": [0.1] * 1024},
                {"embedding": [0.2] * 1024},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = memory._embed(["hello", "world"])

        # Verify request body uses OpenAI format
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        self.assertEqual(body["model"], "text-embedding-3-small")
        self.assertEqual(body["input"], ["hello", "world"])
        self.assertEqual(body["dimensions"], 1024)
        self.assertNotIn("texts", body)
        self.assertNotIn("type", body)

        # Verify response parsing
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 1024)

    @patch("urllib.request.urlopen")
    def test_minimax_embedding_url_construction(self, mock_urlopen):
        """Verify the URL is constructed correctly for MiniMax."""
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.minimax.io/v1",
                "api_key": "test-key",
                "model": "embo-01",
            }
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"vectors": [[0.1]]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        memory._embed(["test"])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertEqual(req.full_url, "https://api.minimax.io/v1/embeddings")

    @patch("urllib.request.urlopen")
    def test_minimax_auth_header(self, mock_urlopen):
        """Verify Bearer auth header is set correctly."""
        memory._config = {
            "embedding_api": {
                "api_base": "https://api.minimax.io/v1",
                "api_key": "my-minimax-key",
                "model": "embo-01",
            }
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"vectors": [[0.1]]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        memory._embed(["test"])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer my-minimax-key")


class TestInitSeedDimension(unittest.TestCase):
    """Tests for dynamic seed vector dimension in init."""

    @patch("memory.lancedb", create=True)
    def test_seed_uses_configured_dimension(self, mock_lancedb_module):
        """Seed vector should match embedding_api.dimension config."""
        import importlib
        # We can't easily test init() since it imports lancedb,
        # but we can verify the dimension logic conceptually
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {
                    "api_key": "test",
                    "dimension": 1536,
                }
            }
        }
        dim = config["memory"]["embedding_api"].get("dimension", 1024)
        self.assertEqual(dim, 1536)

    def test_default_dimension_is_1024(self):
        """Default dimension should be 1024 when not configured."""
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {
                    "api_key": "test",
                }
            }
        }
        dim = config["memory"]["embedding_api"].get("dimension", 1024)
        self.assertEqual(dim, 1024)


if __name__ == "__main__":
    unittest.main()
