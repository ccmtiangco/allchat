from django.db import transaction

from .models import Conversation, Message

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
