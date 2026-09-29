from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from ..billing.services import create_usage_request, fail_before_upstream
from ..choices import ProviderInterface
from .models import (
    DEFAULT_CONVERSATION_TITLE,
    MAX_GENERATED_TITLE_LENGTH,
    Conversation,
    Message,
)
from .services import (
    ContextLimitExceeded,
    append_message,
    build_conversation_context,
    delete_conversation,
    delete_latest_user_message,
    edit_latest_user_message,
    get_owned_conversation,
    rename_conversation,
)

User = get_user_model()


class ConversationAndMessageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='conversation-user', password='password')

    def test_first_user_message_creates_owned_conversation_and_safe_title(self):
        conversation, message = Conversation.create_from_first_message(
            self.user,
            'A useful title\nAdditional prompt context',
        )

        self.assertEqual(conversation.owner, self.user)
        self.assertEqual(conversation.title, 'A useful title')
        self.assertEqual(message.conversation, conversation)
        self.assertEqual(message.role, Message.Role.USER)
        self.assertIsNone(message.provider)

        refreshed = Conversation.objects.get(pk=conversation.pk)
        self.assertGreaterEqual(refreshed.updated_at, conversation.created_at)

    def test_generated_title_is_limited_and_empty_first_message_is_rejected(self):
        conversation, _ = Conversation.create_from_first_message(
            self.user,
            'A' * (MAX_GENERATED_TITLE_LENGTH + 20),
        )
        self.assertEqual(len(conversation.title), MAX_GENERATED_TITLE_LENGTH)

        with self.assertRaises(ValidationError):
            Conversation.create_from_first_message(self.user, '  \n  ')

    def test_messages_are_ordered_and_provider_is_stored_per_assistant_turn(self):
        conversation, user_message = Conversation.create_from_first_message(self.user, 'First turn')
        openai_reply = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content='OpenAI-compatible answer',
            provider=ProviderInterface.OPENAI,
        )
        next_user_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content='Second turn',
        )
        anthropic_reply = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content='Anthropic-compatible answer',
            provider=ProviderInterface.ANTHROPIC,
        )

        messages = list(conversation.messages.all())
        self.assertEqual(
            [message.pk for message in messages],
            [user_message.pk, openai_reply.pk, next_user_message.pk, anthropic_reply.pk],
        )
        self.assertEqual(openai_reply.provider, ProviderInterface.OPENAI)
        self.assertEqual(anthropic_reply.provider, ProviderInterface.ANTHROPIC)

    def test_conversation_history_is_scoped_to_each_owner(self):
        first_conversation, _ = Conversation.create_from_first_message(self.user, 'Private one')
        other_user = User.objects.create_user(username='other-conversation-user', password='password')
        second_conversation, _ = Conversation.create_from_first_message(other_user, 'Private two')

        self.assertQuerySetEqual(
            Conversation.objects.filter(owner=self.user),
            [first_conversation],
        )
        self.assertQuerySetEqual(
            Conversation.objects.filter(owner=other_user),
            [second_conversation],
        )

    def test_database_rejects_role_provider_mismatch(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'A prompt')
        invalid_message = Message(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content='Missing provider',
        )

        with self.assertRaises(ValidationError):
            invalid_message.full_clean()

    def test_context_keeps_order_and_drops_old_complete_turns_to_fit(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'old user')
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content='old answer',
            provider=ProviderInterface.OPENAI,
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content='new prompt',
        )

        context = build_conversation_context(self.user, conversation.pk, max_characters=10)

        self.assertEqual(context, [{'role': 'user', 'content': 'new prompt'}])

    def test_context_rejects_a_newest_message_over_the_limit(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'A message that is too long')

        with self.assertRaises(ContextLimitExceeded):
            build_conversation_context(self.user, conversation.pk, max_characters=4)

    def test_reads_and_message_writes_are_scoped_to_the_owner(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'Private history')
        other_user = User.objects.create_user(username='intruder', password='password')

        with self.assertRaises(Conversation.DoesNotExist):
            get_owned_conversation(other_user, conversation.pk)
        with self.assertRaises(Conversation.DoesNotExist):
            build_conversation_context(other_user, conversation.pk)
        with self.assertRaises(Conversation.DoesNotExist):
            append_message(other_user, conversation.pk, Message.Role.USER, 'Injected message')

        self.assertEqual(conversation.messages.count(), 1)

    def test_conversation_edit_and_delete_are_owner_scoped(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'Private history')
        other_user = User.objects.create_user(username='conversation-intruder', password='password')

        with self.assertRaises(Conversation.DoesNotExist):
            rename_conversation(other_user, conversation.pk, 'Stolen title')
        with self.assertRaises(Conversation.DoesNotExist):
            delete_conversation(other_user, conversation.pk)

        self.assertEqual(Conversation.objects.get(pk=conversation.pk).title, 'Private history')
        rename_conversation(self.user, conversation.pk, 'Updated title')
        delete_conversation(self.user, conversation.pk)
        self.assertFalse(Conversation.objects.filter(pk=conversation.pk).exists())

    def test_failed_usage_record_is_preserved_when_deleting_conversation(self):
        conversation, _ = Conversation.create_from_first_message(self.user, 'Failed history')
        request, _ = create_usage_request(
            self.user,
            conversation,
            ProviderInterface.OPENAI,
            'delete-protection',
        )
        failed = fail_before_upstream(request)
        self.assertFalse(failed.ledger_entries.exists())

        with self.assertRaises(ValidationError):
            delete_conversation(self.user, conversation.pk)

        self.assertTrue(Conversation.objects.filter(pk=conversation.pk).exists())
        self.assertEqual(failed.status, 'failed_before_upstream')
        self.assertEqual(conversation.usage_requests.count(), 1)
        with self.assertRaises(ProtectedError):
            conversation.delete()
        with self.assertRaises(ProtectedError):
            Conversation.objects.filter(pk=conversation.pk).delete()

    def test_message_edit_and_delete_are_owner_scoped(self):
        conversation, message = Conversation.create_from_first_message(self.user, 'Original prompt')
        other_user = User.objects.create_user(username='message-intruder', password='password')

        with self.assertRaises(Conversation.DoesNotExist):
            edit_latest_user_message(other_user, conversation.pk, message.pk, 'Changed prompt')
        with self.assertRaises(Conversation.DoesNotExist):
            delete_latest_user_message(other_user, conversation.pk, message.pk)

        message.refresh_from_db()
        self.assertEqual(message.content, 'Original prompt')
        edited = edit_latest_user_message(self.user, conversation.pk, message.pk, 'Edited prompt')
        self.assertEqual(edited.content, 'Edited prompt')
        delete_latest_user_message(self.user, conversation.pk, message.pk)
        self.assertFalse(conversation.messages.exists())
        self.assertEqual(
            Conversation.objects.get(pk=conversation.pk).title,
            DEFAULT_CONVERSATION_TITLE,
        )


class ConversationModelDefaultsTests(TestCase):
    def test_conversation_uses_neutral_title_when_created_without_a_first_message(self):
        user = User.objects.create_user(username='untitled-user', password='password')

        conversation = Conversation.objects.create(owner=user)

        self.assertEqual(conversation.title, DEFAULT_CONVERSATION_TITLE)
