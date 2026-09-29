from dataclasses import dataclass
import logging
from time import monotonic

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from ..billing.models import UsageRequest
from ..billing.services import (
    IdempotencyConflict,
    InsufficientBalance,
    create_usage_request,
    fail_before_upstream,
    mark_usage_unknown,
    normalize_idempotency_key,
    reserve_usage,
    settle_usage,
)
from ..choices import ProviderInterface
from ..proxy.router import (
    ProxyConfigurationError,
    ProxyError,
    ProxyProtocolError,
    route_request,
    route_stream,
)
from .models import Conversation, Message
from .services import (
    MAX_CONTEXT_CHARACTERS,
    append_message,
    build_conversation_context,
    get_owned_conversation,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTurnResult:
    conversation: Conversation
    usage_request: UsageRequest
    user_message: Message
    assistant_message: Message | None
    duplicate: bool
    finish_reason: str | None = None
    completion_status: str | None = None


@dataclass(frozen=True)
class PreparedChatTurn:
    conversation: Conversation
    usage_request: UsageRequest
    user_message: Message
    messages: list[dict] | None
    duplicate: bool
    assistant_message: Message | None = None


@dataclass(frozen=True)
class ChatStreamEvent:
    kind: str
    payload: dict


def _existing_turn(user, provider, idempotency_key, conversation_id, content):
    request = (
        UsageRequest.objects.select_related('conversation', 'user_message', 'assistant_message')
        .filter(user=user, idempotency_key=idempotency_key)
        .first()
    )
    if request is None:
        return None
    if (
        request.provider != provider
        or (conversation_id is not None and request.conversation_id != conversation_id)
        or request.user_message is None
        or request.user_message.content != content
    ):
        raise IdempotencyConflict('The idempotency key is already bound to another message.')
    return ChatTurnResult(
        conversation=request.conversation,
        usage_request=request,
        user_message=request.user_message,
        assistant_message=request.assistant_message,
        duplicate=True,
    )


def _validate_submission(content, provider, idempotency_key):
    if not isinstance(content, str) or not content.strip():
        raise ValidationError({'content': 'A message cannot be empty.'})
    if len(content) > MAX_CONTEXT_CHARACTERS:
        raise ValidationError({'content': 'The message exceeds the context character limit.'})
    if provider not in ProviderInterface.values:
        raise ValueError('Unsupported provider interface.')
    return normalize_idempotency_key(idempotency_key)


def _prepare_chat_turn(user, content, provider, idempotency_key, conversation_id):
    existing = _existing_turn(user, provider, idempotency_key, conversation_id, content)
    if existing:
        return PreparedChatTurn(
            conversation=existing.conversation,
            usage_request=existing.usage_request,
            user_message=existing.user_message,
            messages=None,
            duplicate=True,
            assistant_message=existing.assistant_message,
        )

    with transaction.atomic():
        if conversation_id is None:
            conversation, user_message = Conversation.create_from_first_message(user, content)
        else:
            conversation = get_owned_conversation(user, conversation_id, lock=True)
            user_message = append_message(
                user,
                conversation_id,
                Message.Role.USER,
                content,
            )

        request, created = create_usage_request(
            user,
            conversation,
            provider,
            idempotency_key,
            user_message=user_message,
        )
        if not created:
            user_message.delete()
            return PreparedChatTurn(
                conversation=request.conversation,
                usage_request=request,
                user_message=request.user_message,
                messages=None,
                duplicate=True,
                assistant_message=request.assistant_message,
            )
        messages = build_conversation_context(user, conversation.pk)

    return PreparedChatTurn(
        conversation=conversation,
        usage_request=request,
        user_message=user_message,
        messages=messages,
        duplicate=False,
    )


def _maximum_input_tokens(messages):
    return 16 + sum(len(message['content'].encode('utf-8')) + 8 for message in messages)


def submit_message(*, user, content, provider, idempotency_key, conversation_id=None):
    idempotency_key = _validate_submission(content, provider, idempotency_key)
    prepared = _prepare_chat_turn(user, content, provider, idempotency_key, conversation_id)
    if prepared.duplicate:
        return ChatTurnResult(
            conversation=prepared.conversation,
            usage_request=prepared.usage_request,
            user_message=prepared.user_message,
            assistant_message=prepared.assistant_message,
            duplicate=True,
        )

    request = prepared.usage_request
    conversation = prepared.conversation
    user_message = prepared.user_message
    messages = prepared.messages
    maximum_input_tokens = _maximum_input_tokens(messages)

    try:
        request = reserve_usage(
            request,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=settings.MAX_CHAT_OUTPUT_TOKENS,
        )
    except InsufficientBalance:
        fail_before_upstream(request)
        raise

    request_started = monotonic()
    try:
        response = route_request(
            provider,
            messages,
            request.reserved_output_tokens,
        )
    except ProxyConfigurationError:
        fail_before_upstream(request)
        logger.warning(
            'Proxy configuration unavailable',
            extra={
                'request_id': request.pk,
                'provider': provider,
                'status': UsageRequest.Status.FAILED_BEFORE_UPSTREAM,
            },
        )
        raise
    except ProxyError:
        latency_ms = round((monotonic() - request_started) * 1_000)
        mark_usage_unknown(request, latency_ms=latency_ms)
        logger.warning(
            'Proxy request outcome requires reconciliation',
            extra={
                'request_id': request.pk,
                'provider': provider,
                'status': UsageRequest.Status.RECONCILIATION_REQUIRED,
            },
        )
        raise

    assistant_message = append_message(
        user,
        conversation.pk,
        Message.Role.ASSISTANT,
        response.text,
        provider=provider,
    )
    settled_request = settle_usage(
        request,
        response.input_tokens,
        response.output_tokens,
        assistant_message=assistant_message,
        upstream_request_id=response.upstream_request_id,
        latency_ms=round((monotonic() - request_started) * 1_000),
    )
    settled_request.refresh_from_db()
    logger.info(
        'Proxy request settled',
        extra={
            'request_id': settled_request.pk,
            'provider': provider,
            'status': settled_request.status,
            'latency_ms': settled_request.latency_ms,
        },
    )
    return ChatTurnResult(
        conversation=conversation,
        usage_request=settled_request,
        user_message=user_message,
        assistant_message=assistant_message,
        duplicate=False,
        finish_reason=response.finish_reason,
        completion_status=response.completion_status,
    )


def _stream_started_event(prepared):
    return ChatStreamEvent(
        kind='started',
        payload={
            'conversation_id': prepared.conversation.pk,
            'conversation_title': prepared.conversation.title,
            'user_message_id': prepared.user_message.pk,
            'user_text': prepared.user_message.content,
            'usage_request_id': prepared.usage_request.pk,
            'duplicate': prepared.duplicate,
        },
    )


def _existing_stream_events(prepared):
    request = prepared.usage_request
    assistant_message = prepared.assistant_message
    yield ChatStreamEvent(
        kind='already_processed',
        payload={
            'conversation_id': prepared.conversation.pk,
            'conversation_title': prepared.conversation.title,
            'user_message_id': prepared.user_message.pk,
            'usage_request_id': request.pk,
            'status': request.status,
            'assistant_message_id': assistant_message.pk if assistant_message else None,
            'assistant_text': assistant_message.content if assistant_message else '',
            'provider': request.provider,
            'latency_ms': request.latency_ms,
            'input_tokens': request.input_tokens,
            'output_tokens': request.output_tokens,
            'total_tokens': request.total_tokens,
            'formatted_charge': request.formatted_charge,
            'reconciliation_reason': request.reconciliation_reason,
        },
    )


def _record_unknown_stream(prepared, provider, text, latency_ms):
    assistant_message = None
    if text:
        assistant_message = append_message(
            prepared.usage_request.user,
            prepared.conversation.pk,
            Message.Role.ASSISTANT,
            text,
            provider=provider,
        )
    return mark_usage_unknown(
        prepared.usage_request,
        assistant_message=assistant_message,
        latency_ms=latency_ms,
    )


def stream_message_turn(*, user, content, provider, idempotency_key, conversation_id=None):
    idempotency_key = _validate_submission(content, provider, idempotency_key)
    prepared = _prepare_chat_turn(user, content, provider, idempotency_key, conversation_id)
    yield _stream_started_event(prepared)
    if prepared.duplicate:
        yield from _existing_stream_events(prepared)
        return

    request = prepared.usage_request
    try:
        request = reserve_usage(
            request,
            maximum_input_tokens=_maximum_input_tokens(prepared.messages),
            maximum_output_tokens=settings.MAX_CHAT_OUTPUT_TOKENS,
        )
    except InsufficientBalance:
        failed = fail_before_upstream(request)
        yield ChatStreamEvent(
            kind='failed_before_upstream',
            payload={
                'conversation_id': prepared.conversation.pk,
                'usage_request_id': failed.pk,
                'message': 'Your wallet balance is too low for this request.',
            },
        )
        return

    request_started = monotonic()
    text_parts = []
    stream = route_stream(provider, prepared.messages, request.reserved_output_tokens)
    result = None
    try:
        for proxy_event in stream:
            if proxy_event.kind == 'delta':
                text_parts.append(proxy_event.text)
                yield ChatStreamEvent(kind='delta', payload={'text': proxy_event.text})
            elif proxy_event.kind == 'complete':
                result = proxy_event.result

        if result is None:
            raise ProxyProtocolError('The provider stream ended without a completion event.')
    except GeneratorExit:
        latency_ms = round((monotonic() - request_started) * 1_000)
        _record_unknown_stream(prepared, provider, ''.join(text_parts), latency_ms)
        logger.warning(
            'Browser disconnected during proxy stream; usage requires reconciliation',
            extra={
                'request_id': request.pk,
                'provider': provider,
                'status': UsageRequest.Status.RECONCILIATION_REQUIRED,
            },
        )
        raise
    except ProxyConfigurationError:
        failed = fail_before_upstream(request)
        yield ChatStreamEvent(
            kind='failed_before_upstream',
            payload={
                'conversation_id': prepared.conversation.pk,
                'usage_request_id': failed.pk,
                'message': 'The selected provider is not configured. No provider request was sent.',
            },
        )
        return
    except ProxyError:
        latency_ms = round((monotonic() - request_started) * 1_000)
        unknown = _record_unknown_stream(prepared, provider, ''.join(text_parts), latency_ms)
        logger.warning(
            'Proxy stream outcome requires reconciliation',
            extra={
                'request_id': unknown.pk,
                'provider': provider,
                'status': unknown.status,
            },
        )
        yield ChatStreamEvent(
            kind='reconciliation_required',
            payload={
                'conversation_id': prepared.conversation.pk,
                'usage_request_id': unknown.pk,
                'assistant_message_id': unknown.assistant_message_id,
                'assistant_text': ''.join(text_parts),
                'latency_ms': unknown.latency_ms,
                'reconciliation_reason': unknown.reconciliation_reason,
                'message': 'The response or its usage could not be confirmed. Funds remain reserved for reconciliation; the request was not retried.',
            },
        )
        return
    finally:
        close = getattr(stream, 'close', None)
        if close:
            close()

    latency_ms = round((monotonic() - request_started) * 1_000)
    assistant_message = append_message(
        user,
        prepared.conversation.pk,
        Message.Role.ASSISTANT,
        result.text,
        provider=provider,
    )
    settled = settle_usage(
        request,
        result.input_tokens,
        result.output_tokens,
        assistant_message=assistant_message,
        upstream_request_id=result.upstream_request_id,
        latency_ms=latency_ms,
    )
    settled.refresh_from_db()
    payload = {
        'conversation_id': prepared.conversation.pk,
        'usage_request_id': settled.pk,
        'assistant_message_id': assistant_message.pk,
        'assistant_text': result.text,
        'provider': provider,
        'latency_ms': settled.latency_ms,
        'input_tokens': settled.input_tokens,
        'output_tokens': settled.output_tokens,
        'total_tokens': settled.total_tokens,
        'formatted_charge': settled.formatted_charge,
        'completion_status': result.completion_status,
    }
    if settled.status == UsageRequest.Status.RECONCILIATION_REQUIRED:
        payload.update(
            {
                'reconciliation_reason': settled.reconciliation_reason,
                'message': 'Usage exceeded its reservation and is awaiting reconciliation.',
            }
        )
        yield ChatStreamEvent(kind='reconciliation_required', payload=payload)
    else:
        yield ChatStreamEvent(kind='completed', payload=payload)
