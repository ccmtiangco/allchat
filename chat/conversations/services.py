from .models import Message

MAX_CONTEXT_CHARACTERS = 32_000


class ContextLimitExceeded(ValueError):
    pass


def build_conversation_context(conversation, *, max_characters=MAX_CONTEXT_CHARACTERS):
    if type(max_characters) is not int or max_characters <= 0:
        raise ValueError('The context character limit must be a positive integer.')

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
