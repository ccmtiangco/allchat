from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..billing.models import WalletLedgerEntry
from .models import DEFAULT_CONVERSATION_TITLE, Conversation, Message

MAX_CONTEXT_CHARACTERS = 32_000


class ContextLimitExceeded(ValueError):
    pass


def _get_owned_conversation(owner, conversation_id, *, lock=False):
    conversations = Conversation.objects.select_for_update() if lock else Conversation.objects
    return conversations.get(pk=conversation_id, owner=owner)


def get_owned_conversation(owner, conversation_id):
    return _get_owned_conversation(owner, conversation_id)


def append_message(owner, conversation_id, role, content, *, provider=None):
    with transaction.atomic():
        conversation = _get_owned_conversation(owner, conversation_id, lock=True)
        return Message.objects.create(
            conversation=conversation,
            role=role,
            content=content,
            provider=provider,
        )


def rename_conversation(owner, conversation_id, title):
    title = title.strip()
    if not title or len(title) > 160:
        raise ValidationError('Conversation titles must contain 1 to 160 characters.')

    with transaction.atomic():
        conversation = _get_owned_conversation(owner, conversation_id, lock=True)
        conversation.title = title
        conversation.save(update_fields=('title', 'updated_at'))
        return conversation


def delete_conversation(owner, conversation_id):
    with transaction.atomic():
        conversation = _get_owned_conversation(owner, conversation_id, lock=True)
        usage_requests = list(conversation.usage_requests.select_for_update())
        if WalletLedgerEntry.objects.filter(related_request__in=usage_requests).exists():
            raise ValidationError('Conversations with wallet ledger entries cannot be deleted.')
        conversation.delete()


def _get_latest_unbilled_user_message(owner, conversation_id, message_id):
    conversation = _get_owned_conversation(owner, conversation_id, lock=True)
    if conversation.usage_requests.exists():
        raise ValidationError('Messages cannot be changed after a usage request exists.')
    message = conversation.messages.select_for_update().get(
        pk=message_id,
        role=Message.Role.USER,
    )
    latest_message = conversation.messages.order_by('-created_at', '-id').first()
    if latest_message.pk != message.pk:
        raise ValidationError('Only the latest user message can be changed.')
    return conversation, message


def edit_latest_user_message(owner, conversation_id, message_id, content):
    if not content.strip():
        raise ValidationError('A message cannot be empty.')

    with transaction.atomic():
        _, message = _get_latest_unbilled_user_message(owner, conversation_id, message_id)
        message.content = content
        message.save(update_fields=('content',))
        return message


def delete_latest_user_message(owner, conversation_id, message_id):
    with transaction.atomic():
        conversation, message = _get_latest_unbilled_user_message(
            owner,
            conversation_id,
            message_id,
        )
        message.delete()
        conversation.updated_at = timezone.now()
        fields = ['updated_at']
        if not conversation.messages.exists():
            conversation.title = DEFAULT_CONVERSATION_TITLE
            fields.append('title')
        conversation.save(update_fields=fields)


def build_conversation_context(
    owner,
    conversation_id,
    *,
    max_characters=MAX_CONTEXT_CHARACTERS,
):
    if type(max_characters) is not int or max_characters <= 0:
        raise ValueError('The context character limit must be a positive integer.')

    conversation = get_owned_conversation(owner, conversation_id)
    messages = list(conversation.messages.all())
    if not messages:
        return []
    if len(messages[-1].content) > max_characters:
        raise ContextLimitExceeded('The newest message exceeds the context character limit.')

    total_characters = sum(len(message.content) for message in messages)
    while total_characters > max_characters:
        oldest = messages.pop(0)
        total_characters -= len(oldest.content)
        if (
            oldest.role == Message.Role.USER
            and messages
            and messages[0].role == Message.Role.ASSISTANT
        ):
            total_characters -= len(messages.pop(0).content)

    return [{'role': message.role, 'content': message.content} for message in messages]
