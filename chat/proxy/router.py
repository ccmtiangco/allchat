from dataclasses import dataclass
from contextlib import contextmanager
import json
from urllib.parse import quote, urlparse

import requests
from django.conf import settings

from ..choices import ProviderInterface

MAX_PROXY_RESPONSE_BYTES = 2 * 1024 * 1024


class ProxyError(RuntimeError):
    pass


class ProxyConfigurationError(ProxyError):
    pass


class ProxyProtocolError(ProxyError):
    pass


@dataclass(frozen=True)
class ProxyResponse:
    provider: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    completion_status: str
    upstream_request_id: str


@dataclass(frozen=True)
class ProxyStreamEvent:
    kind: str
    text: str = ''
    result: ProxyResponse | None = None


def _validate_input(messages, maximum_output_tokens):
    if not isinstance(messages, list) or not messages:
        raise ValueError('At least one conversation message is required.')
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get('role') not in {'user', 'assistant'}
            or not isinstance(message.get('content'), str)
            or not message['content'].strip()
        ):
            raise ValueError('Conversation context contains an unsupported message.')
    if type(maximum_output_tokens) is not int or not 0 < maximum_output_tokens <= settings.MAX_CHAT_OUTPUT_TOKENS:
        raise ValueError('The requested output token cap is invalid.')


def _request_json(path, api_key, headers, body):
    if not api_key:
        raise ProxyConfigurationError('The selected provider key is not configured.')
    if urlparse(settings.PROXY_BASE_URL).scheme != 'https':
        raise ProxyConfigurationError('The proxy base URL must use HTTPS.')

    request_headers = {'Content-Type': 'application/json', **headers}
    url = f"{settings.PROXY_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.post(
            url,
            headers=request_headers,
            json=body,
            timeout=(settings.PROXY_CONNECT_TIMEOUT, settings.PROXY_READ_TIMEOUT),
            allow_redirects=False,
        )
    except requests.Timeout as exc:
        raise ProxyError('The proxy request timed out; usage may be unknown.') from exc
    except requests.RequestException as exc:
        raise ProxyError('The proxy request failed; usage may be unknown.') from exc

    if not 200 <= response.status_code < 300:
        raise ProxyError(f'The proxy returned HTTP {response.status_code}; usage may be unknown.')

    content_length = response.headers.get('Content-Length')
    if content_length and content_length.isdigit() and int(content_length) > MAX_PROXY_RESPONSE_BYTES:
        raise ProxyProtocolError('The proxy response exceeded the configured size limit.')
    if len(response.content) > MAX_PROXY_RESPONSE_BYTES:
        raise ProxyProtocolError('The proxy response exceeded the configured size limit.')

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProxyProtocolError('The proxy returned invalid JSON; usage may be unknown.') from exc
    if not isinstance(payload, dict):
        raise ProxyProtocolError('The proxy returned an unsupported response.')
    return payload


@contextmanager
def _request_stream(path, api_key, headers, body):
    if not api_key:
        raise ProxyConfigurationError('The selected provider key is not configured.')
    if urlparse(settings.PROXY_BASE_URL).scheme != 'https':
        raise ProxyConfigurationError('The proxy base URL must use HTTPS.')

    request_headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream', **headers}
    url = f"{settings.PROXY_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.post(
            url,
            headers=request_headers,
            json=body,
            timeout=(settings.PROXY_CONNECT_TIMEOUT, settings.PROXY_READ_TIMEOUT),
            allow_redirects=False,
            stream=True,
        )
    except requests.Timeout as exc:
        raise ProxyError('The proxy stream timed out; usage may be unknown.') from exc
    except requests.RequestException as exc:
        raise ProxyError('The proxy stream failed; usage may be unknown.') from exc

    try:
        if not 200 <= response.status_code < 300:
            raise ProxyError(
                f'The proxy returned HTTP {response.status_code}; usage may be unknown.'
            )
        yield response
    finally:
        response.close()


def _iter_sse_events(response):
    event_name = ''
    data_lines = []
    received_bytes = 0
    try:
        for raw_line in response.iter_lines():
            if isinstance(raw_line, bytes):
                received_bytes += len(raw_line)
                if received_bytes > MAX_PROXY_RESPONSE_BYTES:
                    raise ProxyProtocolError('The proxy stream exceeded the configured size limit.')
                try:
                    line = raw_line.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise ProxyProtocolError('The proxy stream contained invalid UTF-8.') from exc
            else:
                line = str(raw_line)
                received_bytes += len(line.encode('utf-8'))
                if received_bytes > MAX_PROXY_RESPONSE_BYTES:
                    raise ProxyProtocolError('The proxy stream exceeded the configured size limit.')

            if not line:
                if data_lines:
                    yield event_name, '\n'.join(data_lines)
                event_name = ''
                data_lines = []
                continue
            if line.startswith(':'):
                continue

            field, separator, value = line.partition(':')
            if separator and value.startswith(' '):
                value = value[1:]
            if field == 'event':
                event_name = value
            elif field == 'data':
                data_lines.append(value)

        if data_lines:
            yield event_name, '\n'.join(data_lines)
    except requests.Timeout as exc:
        raise ProxyError('The proxy stream timed out; usage may be unknown.') from exc
    except requests.RequestException as exc:
        raise ProxyError('The proxy stream failed; usage may be unknown.') from exc


def _token_count(usage, key):
    count = usage.get(key) if isinstance(usage, dict) else None
    if type(count) is not int or count < 0:
        raise ProxyProtocolError('The proxy response did not include valid token usage.')
    return count


def _completion_status(provider, finish_reason):
    if provider == ProviderInterface.OPENAI:
        if finish_reason == 'stop':
            return 'complete'
        if finish_reason == 'length':
            return 'incomplete'
        if finish_reason == 'tool_calls':
            return 'tool_call'
        if finish_reason == 'content_filter':
            return 'filtered'
    elif provider == ProviderInterface.ANTHROPIC:
        if finish_reason == 'end_turn':
            return 'complete'
        if finish_reason == 'max_tokens':
            return 'incomplete'
        if finish_reason == 'tool_use':
            return 'tool_call'
    elif provider == ProviderInterface.GOOGLE:
        if finish_reason == 'STOP':
            return 'complete'
        if finish_reason == 'MAX_TOKENS':
            return 'incomplete'
        if finish_reason == 'SAFETY':
            return 'filtered'
    return 'other'


def _require_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ProxyProtocolError('The proxy response did not contain an assistant text answer.')
    return text


def _openai(messages, maximum_output_tokens):
    payload = _request_json(
        '/openai/v1/chat/completions',
        settings.OPENAI_PROXY_KEY,
        {'Authorization': f'Bearer {settings.OPENAI_PROXY_KEY}'},
        {
            'model': settings.OPENAI_PROXY_MODEL,
            'messages': messages,
            'max_tokens': maximum_output_tokens,
            'reasoning_effort': 'none',
        },
    )
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProxyProtocolError('The OpenAI-compatible response contained no choice.')
    choice = choices[0]
    finish_reason = choice.get('finish_reason') or 'unknown'
    completion_status = _completion_status(ProviderInterface.OPENAI, finish_reason)
    if completion_status == 'tool_call':
        raise ProxyProtocolError('Tool calls are not supported by this chat router.')
    message = choice.get('message')
    if not isinstance(message, dict):
        raise ProxyProtocolError('The OpenAI-compatible response contained no assistant message.')
    usage = payload.get('usage')
    return ProxyResponse(
        provider=ProviderInterface.OPENAI,
        model=settings.OPENAI_PROXY_MODEL,
        text=_require_text(message.get('content')),
        input_tokens=_token_count(usage, 'prompt_tokens'),
        output_tokens=_token_count(usage, 'completion_tokens'),
        finish_reason=finish_reason,
        completion_status=completion_status,
        upstream_request_id=str(payload.get('id') or ''),
    )


def _openai_stream(messages, maximum_output_tokens):
    text_parts = []
    usage = None
    finish_reason = None
    upstream_request_id = ''
    saw_done = False
    with _request_stream(
        '/openai/v1/chat/completions',
        settings.OPENAI_PROXY_KEY,
        {'Authorization': f'Bearer {settings.OPENAI_PROXY_KEY}'},
        {
            'model': settings.OPENAI_PROXY_MODEL,
            'messages': messages,
            'max_tokens': maximum_output_tokens,
            'reasoning_effort': 'none',
            'stream': True,
            'stream_options': {'include_usage': True},
        },
    ) as response:
        for event_name, data in _iter_sse_events(response):
            if data == '[DONE]':
                saw_done = True
                break
            try:
                payload = json.loads(data)
            except ValueError as exc:
                raise ProxyProtocolError('The OpenAI-compatible stream contained invalid JSON.') from exc
            if not isinstance(payload, dict) or event_name == 'error' or payload.get('error'):
                raise ProxyProtocolError('The OpenAI-compatible stream reported an error.')

            upstream_request_id = str(payload.get('id') or upstream_request_id)
            if isinstance(payload.get('usage'), dict):
                usage = payload['usage']
            choices = payload.get('choices', [])
            if not isinstance(choices, list):
                raise ProxyProtocolError('The OpenAI-compatible stream contained invalid choices.')
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ProxyProtocolError('The OpenAI-compatible stream contained an invalid choice.')
                delta = choice.get('delta') or {}
                if not isinstance(delta, dict):
                    raise ProxyProtocolError('The OpenAI-compatible stream contained an invalid delta.')
                text = delta.get('content')
                if text is not None:
                    if not isinstance(text, str):
                        raise ProxyProtocolError('The OpenAI-compatible stream contained non-text output.')
                    if text:
                        text_parts.append(text)
                        yield ProxyStreamEvent(kind='delta', text=text)
                if choice.get('finish_reason') is not None:
                    finish_reason = choice['finish_reason']

    if not saw_done:
        raise ProxyProtocolError('The OpenAI-compatible stream ended before [DONE].')
    if not finish_reason:
        raise ProxyProtocolError('The OpenAI-compatible stream contained no finish reason.')
    completion_status = _completion_status(ProviderInterface.OPENAI, finish_reason)
    if completion_status == 'tool_call':
        raise ProxyProtocolError('Tool calls are not supported by this chat router.')
    result = ProxyResponse(
        provider=ProviderInterface.OPENAI,
        model=settings.OPENAI_PROXY_MODEL,
        text=_require_text(''.join(text_parts)),
        input_tokens=_token_count(usage, 'prompt_tokens'),
        output_tokens=_token_count(usage, 'completion_tokens'),
        finish_reason=finish_reason,
        completion_status=completion_status,
        upstream_request_id=upstream_request_id,
    )
    yield ProxyStreamEvent(kind='complete', result=result)


def _anthropic(messages, maximum_output_tokens):
    payload = _request_json(
        '/anthropic/v1/messages',
        settings.ANTHROPIC_PROXY_KEY,
        {
            'x-api-key': settings.ANTHROPIC_PROXY_KEY,
            'anthropic-version': '2023-06-01',
        },
        {
            'model': settings.ANTHROPIC_PROXY_MODEL,
            'messages': messages,
            'max_tokens': maximum_output_tokens,
            'thinking': {'type': 'disabled'},
        },
    )
    blocks = payload.get('content')
    if not isinstance(blocks, list):
        raise ProxyProtocolError('The Anthropic-compatible response contained no content blocks.')
    text = '\n'.join(
        block['text']
        for block in blocks
        if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
    )
    finish_reason = payload.get('stop_reason') or 'unknown'
    completion_status = _completion_status(ProviderInterface.ANTHROPIC, finish_reason)
    if completion_status == 'tool_call':
        raise ProxyProtocolError('Tool calls are not supported by this chat router.')
    usage = payload.get('usage')
    return ProxyResponse(
        provider=ProviderInterface.ANTHROPIC,
        model=settings.ANTHROPIC_PROXY_MODEL,
        text=_require_text(text),
        input_tokens=_token_count(usage, 'input_tokens'),
        output_tokens=_token_count(usage, 'output_tokens'),
        finish_reason=finish_reason,
        completion_status=completion_status,
        upstream_request_id=str(payload.get('id') or ''),
    )


def _anthropic_stream(messages, maximum_output_tokens):
    text_parts = []
    input_tokens = None
    output_tokens = None
    finish_reason = None
    upstream_request_id = ''
    saw_message_stop = False
    with _request_stream(
        '/anthropic/v1/messages',
        settings.ANTHROPIC_PROXY_KEY,
        {
            'x-api-key': settings.ANTHROPIC_PROXY_KEY,
            'anthropic-version': '2023-06-01',
        },
        {
            'model': settings.ANTHROPIC_PROXY_MODEL,
            'messages': messages,
            'max_tokens': maximum_output_tokens,
            'thinking': {'type': 'disabled'},
            'stream': True,
        },
    ) as response:
        for event_name, data in _iter_sse_events(response):
            try:
                payload = json.loads(data)
            except ValueError as exc:
                raise ProxyProtocolError('The Anthropic-compatible stream contained invalid JSON.') from exc
            if not isinstance(payload, dict) or event_name == 'error' or payload.get('type') == 'error':
                raise ProxyError('The Anthropic-compatible stream reported an upstream error.')

            if payload.get('type') == 'message_start':
                message = payload.get('message')
                if not isinstance(message, dict):
                    raise ProxyProtocolError('The Anthropic-compatible stream omitted its message header.')
                upstream_request_id = str(message.get('id') or '')
                usage = message.get('usage')
                if isinstance(usage, dict):
                    input_tokens = usage.get('input_tokens', input_tokens)
            elif payload.get('type') == 'content_block_delta':
                delta = payload.get('delta')
                if not isinstance(delta, dict):
                    raise ProxyProtocolError('The Anthropic-compatible stream contained an invalid delta.')
                if delta.get('type') == 'text_delta':
                    text = delta.get('text')
                    if not isinstance(text, str):
                        raise ProxyProtocolError('The Anthropic-compatible stream contained invalid text.')
                    if text:
                        text_parts.append(text)
                        yield ProxyStreamEvent(kind='delta', text=text)
            elif payload.get('type') == 'message_delta':
                delta = payload.get('delta')
                usage = payload.get('usage')
                if isinstance(delta, dict):
                    finish_reason = delta.get('stop_reason', finish_reason)
                if isinstance(usage, dict):
                    output_tokens = usage.get('output_tokens', output_tokens)
            elif payload.get('type') == 'message_stop':
                saw_message_stop = True

    if not saw_message_stop:
        raise ProxyProtocolError('The Anthropic-compatible stream ended before message_stop.')
    if not finish_reason:
        raise ProxyProtocolError('The Anthropic-compatible stream contained no stop reason.')
    completion_status = _completion_status(ProviderInterface.ANTHROPIC, finish_reason)
    if completion_status == 'tool_call':
        raise ProxyProtocolError('Tool calls are not supported by this chat router.')
    result = ProxyResponse(
        provider=ProviderInterface.ANTHROPIC,
        model=settings.ANTHROPIC_PROXY_MODEL,
        text=_require_text(''.join(text_parts)),
        input_tokens=_token_count({'input_tokens': input_tokens}, 'input_tokens'),
        output_tokens=_token_count({'output_tokens': output_tokens}, 'output_tokens'),
        finish_reason=finish_reason,
        completion_status=completion_status,
        upstream_request_id=upstream_request_id,
    )
    yield ProxyStreamEvent(kind='complete', result=result)


def _google(messages, maximum_output_tokens):
    contents = [
        {
            'role': 'user' if message['role'] == 'user' else 'model',
            'parts': [{'text': message['content']}],
        }
        for message in messages
    ]
    model = quote(settings.GOOGLE_PROXY_MODEL, safe='')
    payload = _request_json(
        f'/google/v1beta/models/{model}:generateContent',
        settings.GOOGLE_PROXY_KEY,
        {'x-goog-api-key': settings.GOOGLE_PROXY_KEY},
        {
            'contents': contents,
            'generationConfig': {
                'maxOutputTokens': maximum_output_tokens,
                'thinkingConfig': {'thinkingBudget': 0},
            },
        },
    )
    candidates = payload.get('candidates')
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ProxyProtocolError('The Google-compatible response contained no candidate.')
    candidate = candidates[0]
    content = candidate.get('content')
    parts = content.get('parts') if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ProxyProtocolError('The Google-compatible response contained no content parts.')
    text = '\n'.join(
        part['text']
        for part in parts
        if isinstance(part, dict) and isinstance(part.get('text'), str)
    )
    finish_reason = candidate.get('finishReason') or 'unknown'
    completion_status = _completion_status(ProviderInterface.GOOGLE, finish_reason)
    if completion_status == 'tool_call':
        raise ProxyProtocolError('Function calls are not supported by this chat router.')
    usage = payload.get('usageMetadata')
    return ProxyResponse(
        provider=ProviderInterface.GOOGLE,
        model=settings.GOOGLE_PROXY_MODEL,
        text=_require_text(text),
        input_tokens=_token_count(usage, 'promptTokenCount'),
        output_tokens=_token_count(usage, 'candidatesTokenCount'),
        finish_reason=finish_reason,
        completion_status=completion_status,
        upstream_request_id=str(payload.get('responseId') or ''),
    )


def _google_stream(messages, maximum_output_tokens):
    contents = [
        {
            'role': 'user' if message['role'] == 'user' else 'model',
            'parts': [{'text': message['content']}],
        }
        for message in messages
    ]
    model = quote(settings.GOOGLE_PROXY_MODEL, safe='')
    text_parts = []
    usage = None
    finish_reason = None
    upstream_request_id = ''
    with _request_stream(
        f'/google/v1beta/models/{model}:streamGenerateContent?alt=sse',
        settings.GOOGLE_PROXY_KEY,
        {'x-goog-api-key': settings.GOOGLE_PROXY_KEY},
        {
            'contents': contents,
            'generationConfig': {
                'maxOutputTokens': maximum_output_tokens,
                'thinkingConfig': {'thinkingBudget': 0},
            },
        },
    ) as response:
        for event_name, data in _iter_sse_events(response):
            try:
                payload = json.loads(data)
            except ValueError as exc:
                raise ProxyProtocolError('The Google-compatible stream contained invalid JSON.') from exc
            if not isinstance(payload, dict) or event_name == 'error' or payload.get('error'):
                raise ProxyError('The Google-compatible stream reported an upstream error.')

            upstream_request_id = str(payload.get('responseId') or upstream_request_id)
            if isinstance(payload.get('usageMetadata'), dict):
                usage = payload['usageMetadata']
            candidates = payload.get('candidates')
            if candidates is None:
                continue
            if not isinstance(candidates, list):
                raise ProxyProtocolError('The Google-compatible stream contained invalid candidates.')
            if not candidates:
                continue
            candidate = candidates[0]
            if not isinstance(candidate, dict):
                raise ProxyProtocolError('The Google-compatible stream contained an invalid candidate.')
            if candidate.get('finishReason') is not None:
                finish_reason = candidate['finishReason']
            content = candidate.get('content')
            parts = content.get('parts', []) if isinstance(content, dict) else []
            if not isinstance(parts, list):
                raise ProxyProtocolError('The Google-compatible stream contained invalid content parts.')
            for part in parts:
                if not isinstance(part, dict):
                    raise ProxyProtocolError('The Google-compatible stream contained an invalid content part.')
                text = part.get('text')
                if text is not None:
                    if not isinstance(text, str):
                        raise ProxyProtocolError('The Google-compatible stream contained invalid text.')
                    if text:
                        text_parts.append(text)
                        yield ProxyStreamEvent(kind='delta', text=text)

    if not finish_reason:
        raise ProxyProtocolError('The Google-compatible stream contained no finish reason.')
    result = ProxyResponse(
        provider=ProviderInterface.GOOGLE,
        model=settings.GOOGLE_PROXY_MODEL,
        text=_require_text(''.join(text_parts)),
        input_tokens=_token_count(usage, 'promptTokenCount'),
        output_tokens=_token_count(usage, 'candidatesTokenCount'),
        finish_reason=finish_reason,
        completion_status=_completion_status(ProviderInterface.GOOGLE, finish_reason),
        upstream_request_id=upstream_request_id,
    )
    yield ProxyStreamEvent(kind='complete', result=result)


_ADAPTERS = {
    ProviderInterface.OPENAI: _openai,
    ProviderInterface.ANTHROPIC: _anthropic,
    ProviderInterface.GOOGLE: _google,
}


def route_request(provider, messages, maximum_output_tokens):
    _validate_input(messages, maximum_output_tokens)
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError('Unsupported provider interface.')
    return adapter(messages, maximum_output_tokens)


_STREAM_ADAPTERS = {
    ProviderInterface.OPENAI: _openai_stream,
    ProviderInterface.ANTHROPIC: _anthropic_stream,
    ProviderInterface.GOOGLE: _google_stream,
}


def route_stream(provider, messages, maximum_output_tokens):
    _validate_input(messages, maximum_output_tokens)
    adapter = _STREAM_ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError('Unsupported provider interface.')
    return adapter(messages, maximum_output_tokens)
