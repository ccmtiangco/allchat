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
from ..proxy.router import ProxyConfigurationError, ProxyError, route_request
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


def _maximum_input_tokens(messages):
    return 16 + sum(len(message['content'].encode('utf-8')) + 8 for message in messages)


def submit_message(*, user, content, provider, idempotency_key, conversation_id=None):
    if not isinstance(content, str) or not content.strip():
        raise ValidationError({'content': 'A message cannot be empty.'})
    if len(content) > MAX_CONTEXT_CHARACTERS:
        raise ValidationError({'content': 'The message exceeds the context character limit.'})
    if provider not in ProviderInterface.values:
        raise ValueError('Unsupported provider interface.')
    idempotency_key = normalize_idempotency_key(idempotency_key)

    existing = _existing_turn(user, provider, idempotency_key, conversation_id, content)
    if existing:
        return existing

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
            return ChatTurnResult(
                conversation=request.conversation,
                usage_request=request,
                user_message=request.user_message,
                assistant_message=request.assistant_message,
                duplicate=True,
            )
        messages = build_conversation_context(user, conversation.pk)
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
        mark_usage_unknown(request)
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
    )
    logger.info(
        'Proxy request settled',
        extra={
            'request_id': settled_request.pk,
            'provider': provider,
            'status': settled_request.status,
            'latency_ms': round((monotonic() - request_started) * 1_000),
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
