import json
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from ..choices import ProviderInterface
from .router import (
    MAX_PROXY_RESPONSE_BYTES,
    ProxyConfigurationError,
    ProxyError,
    ProxyProtocolError,
    route_request,
    route_stream,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode('utf-8')

    def json(self):
        return self.payload


class FakeStreamingResponse:
    status_code = 200
    headers = {}

    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def iter_lines(self, chunk_size=512):
        yield from self.lines

    def close(self):
        self.closed = True


@override_settings(
    PROXY_BASE_URL='https://proxy.test',
    OPENAI_PROXY_KEY='test-openai-key',
    ANTHROPIC_PROXY_KEY='test-anthropic-key',
    GOOGLE_PROXY_KEY='test-google-key',
    OPENAI_PROXY_MODEL='openai-test-model',
    ANTHROPIC_PROXY_MODEL='anthropic-test-model',
    GOOGLE_PROXY_MODEL='google-test-model',
    PROXY_CONNECT_TIMEOUT=3,
    PROXY_READ_TIMEOUT=17,
    MAX_CHAT_OUTPUT_TOKENS=128,
)
class ProxyAdapterTests(SimpleTestCase):
    messages = [
        {'role': 'user', 'content': 'Question'},
        {'role': 'assistant', 'content': 'Previous answer'},
    ]

    def test_openai_compatible_route_normalizes_answer_and_usage(self):
        response = FakeResponse(
            {
                'id': 'openai-request-1',
                'choices': [
                    {
                        'message': {'content': 'OpenAI answer'},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 5},
            }
        )
        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            result = route_request(ProviderInterface.OPENAI, self.messages, 64)

        self.assertEqual(result.text, 'OpenAI answer')
        self.assertEqual(result.input_tokens, 12)
        self.assertEqual(result.output_tokens, 5)
        self.assertEqual(result.provider, ProviderInterface.OPENAI)
        self.assertEqual(result.model, 'openai-test-model')
        self.assertEqual(result.finish_reason, 'stop')
        self.assertEqual(result.completion_status, 'complete')
        self.assertEqual(result.upstream_request_id, 'openai-request-1')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://proxy.test/openai/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-openai-key')
        self.assertEqual(kwargs['json']['max_tokens'], 64)
        self.assertEqual(kwargs['timeout'], (3, 17))
        self.assertFalse(kwargs['allow_redirects'])

    def test_anthropic_compatible_route_normalizes_text_blocks_and_usage(self):
        response = FakeResponse(
            {
                'id': 'anthropic-request-1',
                'content': [{'type': 'text', 'text': 'Anthropic answer'}],
                'stop_reason': 'end_turn',
                'usage': {'input_tokens': 9, 'output_tokens': 4},
            }
        )
        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            result = route_request(ProviderInterface.ANTHROPIC, self.messages, 64)

        self.assertEqual(result.text, 'Anthropic answer')
        self.assertEqual(result.input_tokens, 9)
        self.assertEqual(result.output_tokens, 4)
        self.assertEqual(result.model, 'anthropic-test-model')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://proxy.test/anthropic/v1/messages')
        self.assertEqual(kwargs['headers']['x-api-key'], 'test-anthropic-key')
        self.assertEqual(kwargs['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(kwargs['json']['max_tokens'], 64)

    def test_google_compatible_route_maps_roles_and_normalizes_usage(self):
        response = FakeResponse(
            {
                'responseId': 'google-request-1',
                'candidates': [
                    {
                        'content': {'parts': [{'text': 'Google answer'}]},
                        'finishReason': 'STOP',
                    }
                ],
                'usageMetadata': {
                    'promptTokenCount': 15,
                    'candidatesTokenCount': 6,
                },
            }
        )
        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            result = route_request(ProviderInterface.GOOGLE, self.messages, 64)

        self.assertEqual(result.text, 'Google answer')
        self.assertEqual(result.input_tokens, 15)
        self.assertEqual(result.output_tokens, 6)
        self.assertEqual(result.model, 'google-test-model')
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            'https://proxy.test/google/v1beta/models/google-test-model:generateContent',
        )
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'test-google-key')
        self.assertEqual(kwargs['json']['contents'][1]['role'], 'model')
        self.assertEqual(kwargs['json']['generationConfig']['maxOutputTokens'], 64)

    def test_truncation_is_normalized_as_incomplete(self):
        response = FakeResponse(
            {
                'choices': [
                    {
                        'message': {'content': 'Partial answer'},
                        'finish_reason': 'length',
                    }
                ],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 64},
            }
        )
        with patch('chat.proxy.router.requests.post', return_value=response):
            result = route_request(ProviderInterface.OPENAI, self.messages, 64)

        self.assertEqual(result.completion_status, 'incomplete')
        self.assertEqual(result.finish_reason, 'length')

    def test_missing_usage_and_tool_calls_are_protocol_errors(self):
        missing_usage = FakeResponse(
            {'choices': [{'message': {'content': 'Answer'}, 'finish_reason': 'stop'}]}
        )
        with patch('chat.proxy.router.requests.post', return_value=missing_usage):
            with self.assertRaises(ProxyProtocolError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)

        tool_call = FakeResponse(
            {
                'choices': [
                    {
                        'message': {'content': None, 'tool_calls': [{'id': 'tool-1'}]},
                        'finish_reason': 'tool_calls',
                    }
                ],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 3},
            }
        )
        with patch('chat.proxy.router.requests.post', return_value=tool_call):
            with self.assertRaises(ProxyProtocolError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)

    def test_timeout_is_not_retried(self):
        with patch(
            'chat.proxy.router.requests.post',
            side_effect=requests.Timeout('timeout'),
        ) as post:
            with self.assertRaises(ProxyError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)
        post.assert_called_once()

    def test_http_errors_are_not_retried(self):
        response = FakeResponse({'error': 'rate limited'})
        response.status_code = 429
        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            with self.assertRaises(ProxyError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)
        post.assert_called_once()

    def test_bad_provider_missing_key_and_oversized_response_are_rejected(self):
        with self.assertRaises(ValueError):
            route_request('arbitrary-url', self.messages, 64)

        with override_settings(OPENAI_PROXY_KEY=''):
            with self.assertRaises(ProxyConfigurationError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)

        response = FakeResponse({}, headers={'Content-Length': str(MAX_PROXY_RESPONSE_BYTES + 1)})
        with patch('chat.proxy.router.requests.post', return_value=response):
            with self.assertRaises(ProxyProtocolError):
                route_request(ProviderInterface.OPENAI, self.messages, 64)

    def test_openai_stream_normalizes_deltas_and_terminal_usage(self):
        lines = [
            b'data: {"id":"chat-stream-1","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            b'',
            b'data: {"id":"chat-stream-1","choices":[{"delta":{"content":" there"},"finish_reason":null}]}',
            b'',
            b'data: {"id":"chat-stream-1","choices":[{"delta":{},"finish_reason":"stop"}]}',
            b'',
            b'data: {"id":"chat-stream-1","choices":[],"usage":{"prompt_tokens":13,"completion_tokens":2}}',
            b'',
            b'data: [DONE]',
            b'',
        ]
        response = FakeStreamingResponse(lines)

        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            events = list(route_stream(ProviderInterface.OPENAI, self.messages, 64))

        self.assertEqual([event.text for event in events if event.kind == 'delta'], ['Hello', ' there'])
        final = events[-1]
        self.assertEqual(final.kind, 'complete')
        self.assertEqual(final.result.text, 'Hello there')
        self.assertEqual(final.result.input_tokens, 13)
        self.assertEqual(final.result.output_tokens, 2)
        self.assertEqual(final.result.upstream_request_id, 'chat-stream-1')
        self.assertTrue(response.closed)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://proxy.test/openai/v1/chat/completions')
        self.assertTrue(kwargs['stream'])
        self.assertTrue(kwargs['json']['stream'])
        self.assertTrue(kwargs['json']['stream_options']['include_usage'])

    def test_anthropic_stream_normalizes_text_and_message_usage(self):
        lines = [
            b'event: message_start',
            b'data: {"type":"message_start","message":{"id":"msg-stream-1","usage":{"input_tokens":9}}}',
            b'',
            b'event: content_block_delta',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Anthropic"}}',
            b'',
            b'event: content_block_delta',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" answer"}}',
            b'',
            b'event: message_delta',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}',
            b'',
            b'event: message_stop',
            b'data: {"type":"message_stop"}',
            b'',
        ]
        response = FakeStreamingResponse(lines)

        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            events = list(route_stream(ProviderInterface.ANTHROPIC, self.messages, 64))

        self.assertEqual(
            [event.text for event in events if event.kind == 'delta'],
            ['Anthropic', ' answer'],
        )
        final = events[-1]
        self.assertEqual(final.result.text, 'Anthropic answer')
        self.assertEqual(final.result.input_tokens, 9)
        self.assertEqual(final.result.output_tokens, 4)
        self.assertEqual(final.result.upstream_request_id, 'msg-stream-1')
        self.assertTrue(response.closed)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://proxy.test/anthropic/v1/messages')
        self.assertTrue(kwargs['json']['stream'])

    def test_google_stream_normalizes_candidate_text_and_usage_only_events(self):
        lines = [
            b'data: {"responseId":"google-stream-1","candidates":[{"content":{"parts":[{"text":"Google"}]}}]}',
            b'',
            b'data: {"usageMetadata":{"promptTokenCount":15,"candidatesTokenCount":6}}',
            b'',
            b'data: {"candidates":[{"finishReason":"STOP"}]}',
            b'',
        ]
        response = FakeStreamingResponse(lines)

        with patch('chat.proxy.router.requests.post', return_value=response) as post:
            events = list(route_stream(ProviderInterface.GOOGLE, self.messages, 64))

        self.assertEqual([event.text for event in events if event.kind == 'delta'], ['Google'])
        final = events[-1]
        self.assertEqual(final.result.text, 'Google')
        self.assertEqual(final.result.input_tokens, 15)
        self.assertEqual(final.result.output_tokens, 6)
        self.assertEqual(final.result.upstream_request_id, 'google-stream-1')
        self.assertTrue(response.closed)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            'https://proxy.test/google/v1beta/models/google-test-model:streamGenerateContent?alt=sse',
        )
        self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'test-google-key')

    def test_stream_requires_terminal_usage_and_closes_response_on_protocol_error(self):
        response = FakeStreamingResponse(
            [
                b'data: {"choices":[{"delta":{"content":"Partial"},"finish_reason":"stop"}]}',
                b'',
                b'data: [DONE]',
                b'',
            ]
        )

        with patch('chat.proxy.router.requests.post', return_value=response):
            with self.assertRaises(ProxyProtocolError):
                list(route_stream(ProviderInterface.OPENAI, self.messages, 64))

        self.assertTrue(response.closed)
