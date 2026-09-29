from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import DEFAULT_CONVERSATION_TITLE, Conversation, Message

MAX_CONTEXT_CHARACTERS = 32_000


class ContextLimitExceeded(ValueError):
    pass


def get_owned_conversation(owner, conversation_id):
    return Conversation.objects.get(pk=conversation_id, owner=owner)


def append_message(owner, conversation_id, role, content, *, provider=None):
    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().get(
            pk=conversation_id,
            owner=owner,
        )
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

    conversation = Conversation.objects.get(pk=conversation_id, owner=owner)
    conversation.title = title
    conversation.save(update_fields=('title', 'updated_at'))
    return conversation


def delete_conversation(owner, conversation_id):
    conversation = Conversation.objects.get(pk=conversation_id, owner=owner)
    conversation.delete()


def edit_latest_user_message(owner, conversation_id, message_id, content):
    if not content.strip():
        raise ValidationError('A message cannot be empty.')

    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().get(
            pk=conversation_id,
            owner=owner,
        )
        if conversation.usage_requests.exists():
            raise ValidationError('Messages cannot be edited after a usage request exists.')
        message = conversation.messages.select_for_update().get(
            pk=message_id,
            role=Message.Role.USER,
        )
        latest_message = conversation.messages.order_by('-created_at', '-id').first()
        if latest_message.pk != message.pk:
            raise ValidationError('Only the latest user message can be edited.')
        message.content = content
        message.save(update_fields=('content',))
        return message


def delete_latest_user_message(owner, conversation_id, message_id):
    with transaction.atomic():
        conversation = Conversation.objects.select_for_update().get(
            pk=conversation_id,
            owner=owner,
        )
        if conversation.usage_requests.exists():
            raise ValidationError('Messages cannot be deleted after a usage request exists.')
        message = conversation.messages.select_for_update().get(
            pk=message_id,
            role=Message.Role.USER,
        )
        latest_message = conversation.messages.order_by('-created_at', '-id').first()
        if latest_message.pk != message.pk:
            raise ValidationError('Only the latest user message can be deleted.')
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
