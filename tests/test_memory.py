"""
Unit tests for memory.py module.

Tests cover:
- Pure functions (_cosine_similarity, _format_messages)
- init() with mocking
- retrieve() with mocking
- compress_async message filtering
- _call_compress_llm JSON parsing
"""

import json
import sys
import unittest
from unittest.mock import Mock, MagicMock, patch, mock_open
from io import BytesIO

# Add parent directory to path to import memory module
sys.path.insert(0, '/home/test/.openclaw/workspace/gogetajob/~/repos/forks/724-office')

import memory


class TestCosineSimilarity(unittest.TestCase):
    """Tests for _cosine_similarity pure function."""

    def test_identical_vectors(self):
        """Identical vectors should have similarity of 1.0"""
        vec = [1.0, 2.0, 3.0]
        similarity = memory._cosine_similarity(vec, vec)
        self.assertAlmostEqual(similarity, 1.0, places=5)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors should have similarity of 0.0"""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertAlmostEqual(similarity, 0.0, places=5)

    def test_opposite_vectors(self):
        """Opposite vectors should have similarity of -1.0"""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [-1.0, 0.0, 0.0]
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertAlmostEqual(similarity, -1.0, places=5)

    def test_zero_vector_a(self):
        """Zero vector should return 0.0 similarity"""
        vec_a = [0.0, 0.0, 0.0]
        vec_b = [1.0, 2.0, 3.0]
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertEqual(similarity, 0.0)

    def test_zero_vector_b(self):
        """Zero vector should return 0.0 similarity"""
        vec_a = [1.0, 2.0, 3.0]
        vec_b = [0.0, 0.0, 0.0]
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertEqual(similarity, 0.0)

    def test_both_zero_vectors(self):
        """Both zero vectors should return 0.0 similarity"""
        vec_a = [0.0, 0.0, 0.0]
        vec_b = [0.0, 0.0, 0.0]
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertEqual(similarity, 0.0)

    def test_general_case(self):
        """Test general case with known result"""
        vec_a = [1.0, 2.0, 3.0]
        vec_b = [4.0, 5.0, 6.0]
        # Manual calculation: dot=32, norm_a=sqrt(14), norm_b=sqrt(77)
        # similarity = 32 / (sqrt(14) * sqrt(77)) ≈ 0.9746
        similarity = memory._cosine_similarity(vec_a, vec_b)
        self.assertAlmostEqual(similarity, 0.9746, places=3)


class TestFormatMessages(unittest.TestCase):
    """Tests for _format_messages pure function."""

    def test_single_user_message(self):
        """Format single user message"""
        messages = [{"role": "user", "content": "Hello"}]
        result = memory._format_messages(messages)
        self.assertEqual(result, "User: Hello")

    def test_single_assistant_message(self):
        """Format single assistant message"""
        messages = [{"role": "assistant", "content": "Hi there"}]
        result = memory._format_messages(messages)
        self.assertEqual(result, "Assistant: Hi there")

    def test_conversation(self):
        """Format multi-turn conversation"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]
        result = memory._format_messages(messages)
        expected = "User: Hello\nAssistant: Hi there\nUser: How are you?"
        self.assertEqual(result, expected)

    def test_empty_content(self):
        """Skip messages with empty content"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "Anyone there?"},
        ]
        result = memory._format_messages(messages)
        expected = "User: Hello\nUser: Anyone there?"
        self.assertEqual(result, expected)

    def test_missing_content(self):
        """Skip messages without content field"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant"},
            {"role": "user", "content": "Test"},
        ]
        result = memory._format_messages(messages)
        expected = "User: Hello\nUser: Test"
        self.assertEqual(result, expected)

    def test_empty_messages(self):
        """Empty message list returns empty string"""
        result = memory._format_messages([])
        self.assertEqual(result, "")


class TestInit(unittest.TestCase):
    """Tests for init() function with mocking."""

    def setUp(self):
        """Reset module state before each test"""
        memory._enabled = False
        memory._config = {}
        memory._llm_config = {}
        memory._db = None
        memory._table = None

    def test_disabled_in_config(self):
        """init() with disabled config should not enable memory"""
        config = {"memory": {"enabled": False}}
        llm_config = {}

        with patch('memory.log') as mock_log:
            memory.init(config, llm_config, "/tmp/test.db")
            mock_log.info.assert_called_with("[memory] disabled in config")
            self.assertFalse(memory._enabled)

    def test_missing_api_key(self):
        """init() with missing API key should not enable memory"""
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {}
            }
        }
        llm_config = {}

        with patch('memory.log') as mock_log:
            memory.init(config, llm_config, "/tmp/test.db")
            mock_log.error.assert_called_with("[memory] no embedding API key, disabled")
            self.assertFalse(memory._enabled)

    def test_successful_init_existing_table(self):
        """init() with valid config and existing table should succeed"""
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {"api_key": "test-key"}
            }
        }
        llm_config = {}

        mock_table = Mock()
        mock_table.count_rows.return_value = 42

        mock_db = Mock()
        mock_db.open_table.return_value = mock_table

        mock_lancedb = Mock()
        mock_lancedb.connect.return_value = mock_db

        with patch.dict('sys.modules', {'lancedb': mock_lancedb}), \
             patch('memory.log') as mock_log:
            memory.init(config, llm_config, "/tmp/test.db")

            mock_lancedb.connect.assert_called_once_with("/tmp/test.db")
            mock_db.open_table.assert_called_once_with("memories")
            self.assertTrue(memory._enabled)
            self.assertEqual(memory._table, mock_table)
            mock_log.info.assert_any_call("[memory] opened table, 42 memories")

    def test_successful_init_new_table(self):
        """init() should create new table if it doesn't exist"""
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {"api_key": "test-key"}
            }
        }
        llm_config = {}

        mock_table = Mock()
        mock_db = Mock()
        mock_db.open_table.side_effect = Exception("Table not found")
        mock_db.create_table.return_value = mock_table

        mock_lancedb = Mock()
        mock_lancedb.connect.return_value = mock_db

        mock_np = Mock()
        mock_np.zeros.return_value.tolist.return_value = [0.0] * 1024

        with patch.dict('sys.modules', {'lancedb': mock_lancedb, 'numpy': mock_np}), \
             patch('memory.log') as mock_log:
            memory.init(config, llm_config, "/tmp/test.db")

            mock_db.create_table.assert_called_once()
            self.assertTrue(memory._enabled)
            mock_log.info.assert_any_call("[memory] created new table")

    def test_init_exception(self):
        """init() should handle exceptions gracefully"""
        config = {
            "memory": {
                "enabled": True,
                "embedding_api": {"api_key": "test-key"}
            }
        }
        llm_config = {}

        mock_lancedb = Mock()
        mock_lancedb.connect.side_effect = Exception("Connection failed")

        with patch.dict('sys.modules', {'lancedb': mock_lancedb}), \
             patch('memory.log') as mock_log:
            memory.init(config, llm_config, "/tmp/test.db")

            self.assertFalse(memory._enabled)
            mock_log.error.assert_called()


class TestRetrieve(unittest.TestCase):
    """Tests for retrieve() function with mocking."""

    def setUp(self):
        """Reset module state before each test"""
        memory._enabled = False
        memory._config = {}
        memory._table = None

    def test_not_enabled(self):
        """retrieve() should return empty string when not enabled"""
        memory._enabled = False
        result = memory.retrieve("test query", "session1")
        self.assertEqual(result, "")

    def test_no_table(self):
        """retrieve() should return empty string when table is None"""
        memory._enabled = True
        memory._table = None
        result = memory.retrieve("test query", "session1")
        self.assertEqual(result, "")

    @patch('memory._embed')
    def test_embedding_fails(self, mock_embed):
        """retrieve() should return empty string when embedding fails"""
        memory._enabled = True
        memory._table = Mock()
        memory._config = {"retrieve_top_k": 5}
        mock_embed.return_value = []

        result = memory.retrieve("test query", "session1")
        self.assertEqual(result, "")

    @patch('memory._embed')
    def test_no_results(self, mock_embed):
        """retrieve() should return empty string when no results"""
        memory._enabled = True
        memory._config = {"retrieve_top_k": 5}

        mock_search = Mock()
        mock_search.limit.return_value.to_list.return_value = []
        mock_table = Mock()
        mock_table.search.return_value = mock_search
        memory._table = mock_table

        mock_embed.return_value = [[0.1] * 1024]

        result = memory.retrieve("test query", "session1")
        self.assertEqual(result, "")

    @patch('memory._embed')
    def test_filter_seed_data(self, mock_embed):
        """retrieve() should filter out seed data"""
        memory._enabled = True
        memory._config = {"retrieve_top_k": 5}

        mock_search = Mock()
        mock_search.limit.return_value.to_list.return_value = [
            {"id": "seed", "fact": "System initialized"},
            {"id": "real", "fact": "Real memory"},
        ]
        mock_table = Mock()
        mock_table.search.return_value = mock_search
        memory._table = mock_table

        mock_embed.return_value = [[0.1] * 1024]

        result = memory.retrieve("test query", "session1")
        self.assertIn("[Relevant Memories]", result)
        self.assertIn("Real memory", result)
        self.assertNotIn("System initialized", result)

    @patch('memory._embed')
    def test_format_output_with_timestamp(self, mock_embed):
        """retrieve() should format output with timestamps"""
        memory._enabled = True
        memory._config = {"retrieve_top_k": 5}

        mock_search = Mock()
        mock_search.limit.return_value.to_list.return_value = [
            {"id": "1", "fact": "User likes Python", "timestamp": "2024-01-15"},
            {"id": "2", "fact": "Meeting scheduled", "timestamp": ""},
        ]
        mock_table = Mock()
        mock_table.search.return_value = mock_search
        memory._table = mock_table

        mock_embed.return_value = [[0.1] * 1024]

        result = memory.retrieve("test query", "session1")
        self.assertIn("[Relevant Memories]", result)
        self.assertIn("- User likes Python (2024-01-15)", result)
        self.assertIn("- Meeting scheduled", result)
        self.assertNotIn("Meeting scheduled ()", result)

    @patch('memory._embed')
    def test_custom_top_k(self, mock_embed):
        """retrieve() should use custom top_k parameter"""
        memory._enabled = True
        memory._config = {"retrieve_top_k": 5}

        mock_search = Mock()
        mock_limit = Mock()
        mock_limit.to_list.return_value = []
        mock_search.limit.return_value = mock_limit
        mock_table = Mock()
        mock_table.search.return_value = mock_search
        memory._table = mock_table

        mock_embed.return_value = [[0.1] * 1024]

        memory.retrieve("test query", "session1", top_k=10)
        mock_search.limit.assert_called_once_with(10)

    @patch('memory._embed')
    def test_retrieve_exception(self, mock_embed):
        """retrieve() should handle exceptions gracefully"""
        memory._enabled = True
        memory._table = Mock()
        mock_embed.side_effect = Exception("Embedding error")

        with patch('memory.log') as mock_log:
            result = memory.retrieve("test query", "session1")
            self.assertEqual(result, "")
            mock_log.error.assert_called()


class TestCompressAsync(unittest.TestCase):
    """Tests for compress_async message filtering."""

    def setUp(self):
        """Reset module state before each test"""
        memory._enabled = True

    def test_not_enabled(self):
        """compress_async should return early if not enabled"""
        memory._enabled = False
        messages = [{"role": "user", "content": "test"}]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")
            mock_thread.assert_not_called()

    def test_skip_system_messages(self):
        """compress_async should skip non-user/assistant messages"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")

            # Get the messages passed to the worker
            call_args = mock_thread.call_args
            worker_messages = call_args[1]['args'][0]

            self.assertEqual(len(worker_messages), 2)
            self.assertEqual(worker_messages[0]["role"], "user")
            self.assertEqual(worker_messages[1]["role"], "assistant")

    def test_skip_tool_calls(self):
        """compress_async should skip assistant messages with tool_calls"""
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "weather"}]},
            {"role": "assistant", "content": "It's sunny"},
        ]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")

            call_args = mock_thread.call_args
            worker_messages = call_args[1]['args'][0]

            self.assertEqual(len(worker_messages), 2)
            self.assertEqual(worker_messages[0]["content"], "What's the weather?")
            self.assertEqual(worker_messages[1]["content"], "It's sunny")

    def test_skip_empty_content(self):
        """compress_async should skip messages with empty content"""
        messages = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": "Hi"},
        ]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")

            call_args = mock_thread.call_args
            worker_messages = call_args[1]['args'][0]

            self.assertEqual(len(worker_messages), 2)
            self.assertEqual(worker_messages[0]["content"], "Hello")
            self.assertEqual(worker_messages[1]["content"], "Hi")

    def test_skip_non_string_content(self):
        """compress_async should skip messages with non-string content"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": ["list", "content"]},
            {"role": "user", "content": {"type": "dict"}},
            {"role": "assistant", "content": "Hi"},
        ]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")

            call_args = mock_thread.call_args
            worker_messages = call_args[1]['args'][0]

            self.assertEqual(len(worker_messages), 2)
            self.assertEqual(worker_messages[0]["content"], "Hello")
            self.assertEqual(worker_messages[1]["content"], "Hi")

    def test_too_few_messages(self):
        """compress_async should skip if less than 2 messages after filtering"""
        messages = [
            {"role": "user", "content": "Hello"},
        ]

        with patch('memory.threading.Thread') as mock_thread:
            memory.compress_async(messages, "session1")
            mock_thread.assert_not_called()

    def test_start_thread(self):
        """compress_async should start background thread with valid messages"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]

        mock_thread_instance = Mock()
        with patch('memory.threading.Thread', return_value=mock_thread_instance) as mock_thread, \
             patch('memory.log'):
            memory.compress_async(messages, "session1")

            mock_thread.assert_called_once()
            mock_thread_instance.start.assert_called_once()

            # Verify daemon=True
            call_kwargs = mock_thread.call_args[1]
            self.assertTrue(call_kwargs['daemon'])


class TestCallCompressLLM(unittest.TestCase):
    """Tests for _call_compress_llm JSON parsing."""

    def setUp(self):
        """Set up test config"""
        memory._llm_config = {
            "providers": {
                "deepseek-chat": {
                    "api_base": "https://api.example.com",
                    "model": "deepseek-chat",
                    "api_key": "test-key",
                    "timeout": 120,
                }
            },
            "default": "deepseek-chat"
        }

    def _mock_urlopen(self, response_data):
        """Helper to create properly mocked urlopen response"""
        mock_response = Mock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        return mock_response

    def test_clean_json_response(self):
        """_call_compress_llm should parse clean JSON array"""
        response_data = {
            "choices": [{
                "message": {
                    "content": '[{"fact": "User likes Python", "keywords": ["python"], "persons": [], "timestamp": null, "topic": "preferences"}]'
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["fact"], "User likes Python")
            self.assertEqual(result[0]["keywords"], ["python"])

    def test_json_wrapped_in_code_blocks(self):
        """_call_compress_llm should extract JSON from markdown code blocks"""
        response_data = {
            "choices": [{
                "message": {
                    "content": '```json\n[{"fact": "Meeting at 3pm", "keywords": ["meeting"], "persons": [], "timestamp": "2024-01-15 15:00", "topic": "schedule"}]\n```'
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["fact"], "Meeting at 3pm")

    def test_json_with_extra_text(self):
        """_call_compress_llm should extract JSON array from text with extra content"""
        response_data = {
            "choices": [{
                "message": {
                    "content": 'Here are the extracted memories:\n[{"fact": "User prefers dark mode", "keywords": ["ui", "preferences"], "persons": [], "timestamp": null, "topic": "preferences"}]\nTotal: 1 memory'
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["fact"], "User prefers dark mode")

    def test_malformed_json(self):
        """_call_compress_llm should return empty list for malformed JSON"""
        response_data = {
            "choices": [{
                "message": {
                    "content": 'This is not valid JSON at all'
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response), \
             patch('memory.log') as mock_log:
            result = memory._call_compress_llm("test prompt")

            self.assertEqual(result, [])
            mock_log.warning.assert_called()

    def test_empty_content(self):
        """_call_compress_llm should return empty list for empty content"""
        response_data = {
            "choices": [{
                "message": {
                    "content": ""
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")
            self.assertEqual(result, [])

    def test_missing_content(self):
        """_call_compress_llm should return empty list when content is missing"""
        response_data = {
            "choices": [{
                "message": {}
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")
            self.assertEqual(result, [])

    def test_empty_array_response(self):
        """_call_compress_llm should handle empty array (nothing to remember)"""
        response_data = {
            "choices": [{
                "message": {
                    "content": "[]"
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response):
            result = memory._call_compress_llm("test prompt")
            self.assertEqual(result, [])

    def test_no_provider_configured(self):
        """_call_compress_llm should return empty list when no provider configured"""
        memory._llm_config = {"providers": {}}

        with patch('memory.log') as mock_log:
            result = memory._call_compress_llm("test prompt")
            self.assertEqual(result, [])
            mock_log.error.assert_called_with("[memory] no LLM provider for compress")

    def test_json_not_array(self):
        """_call_compress_llm should handle non-array JSON (return empty list)"""
        response_data = {
            "choices": [{
                "message": {
                    "content": '{"fact": "This is an object, not an array"}'
                }
            }]
        }

        mock_response = self._mock_urlopen(response_data)

        with patch('memory.urllib.request.urlopen', return_value=mock_response), \
             patch('memory.log') as mock_log:
            result = memory._call_compress_llm("test prompt")

            self.assertEqual(result, [])
            mock_log.warning.assert_called()


if __name__ == '__main__':
    unittest.main()
