from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse

from ..billing.models import INITIAL_BALANCE_MICRO_USD, UsageRequest
from ..billing.services import InsufficientBalance, create_usage_request, fail_before_upstream
from ..choices import ProviderInterface
from ..proxy.router import ProxyConfigurationError, ProxyError, ProxyResponse
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
from .orchestration import submit_message

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


class ChatOrchestrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='orchestrator-user', password='password')

    def response(self, provider, *, text='Provider answer', input_tokens=12, output_tokens=5):
        return ProxyResponse(
            provider=provider,
            model='configured-model',
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason='stop',
            completion_status='complete',
            upstream_request_id='upstream-test-id',
        )

    def test_new_message_routes_once_and_settles_exact_usage(self):
        with patch(
            'chat.conversations.orchestration.route_request',
            return_value=self.response(ProviderInterface.OPENAI),
        ) as route:
            result = submit_message(
                user=self.user,
                content='Explain this request',
                provider=ProviderInterface.OPENAI,
                idempotency_key='turn-1',
            )

            retry = submit_message(
                user=self.user,
                content='Explain this request',
                provider=ProviderInterface.OPENAI,
                idempotency_key='turn-1',
            )

        self.assertEqual(route.call_count, 1)
        self.assertFalse(result.duplicate)
        self.assertTrue(retry.duplicate)
        self.assertEqual(result.conversation.pk, retry.conversation.pk)
        self.assertEqual(result.usage_request.status, UsageRequest.Status.SUCCEEDED)
        self.assertEqual(result.usage_request.total_tokens, 17)
        self.assertEqual(result.usage_request.charge_micro_usd, 34)
        self.assertEqual(result.usage_request.user_message.content, 'Explain this request')
        self.assertEqual(result.assistant_message.provider, ProviderInterface.OPENAI)
        self.assertEqual(result.assistant_message.content, 'Provider answer')
        self.assertEqual(result.finish_reason, 'stop')
        self.assertEqual(result.completion_status, 'complete')
        self.assertEqual(result.conversation.messages.count(), 2)
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD - 34)

    def test_server_rendered_chat_form_posts_and_redirects_to_history(self):
        self.client.force_login(self.user)
        home = self.client.get(reverse('chat:home'))
        self.assertContains(home, '$5.000000')
        self.assertContains(home, 'OpenAI-compatible')
        self.assertContains(home, 'Anthropic-compatible')
        self.assertContains(home, 'Google-compatible')
        self.assertContains(home, 'csrfmiddlewaretoken')

        with patch(
            'chat.conversations.orchestration.route_request',
            return_value=self.response(ProviderInterface.GOOGLE, text='Rendered answer'),
        ) as route:
            response = self.client.post(
                reverse('chat:send_message'),
                {
                    'content': 'View test prompt',
                    'provider': ProviderInterface.GOOGLE,
                    'conversation_id': '',
                    'idempotency_key': 'view-test-turn',
                },
            )

        route.assert_called_once()
        request = UsageRequest.objects.get(idempotency_key='view-test-turn')
        self.assertRedirects(
            response,
            reverse('chat:conversation', kwargs={'conversation_id': request.conversation_id}),
        )
        detail = self.client.get(response.url)
        self.assertContains(detail, 'View test prompt')
        self.assertContains(detail, 'Rendered answer')
        self.assertContains(detail, 'Google-compatible')
        self.assertContains(detail, '$0.000034')
        self.assertContains(detail, '$4.999966')
        self.assertContains(detail, 'View test prompt')

    def test_provider_can_change_between_turns(self):
        with patch(
            'chat.conversations.orchestration.route_request',
            side_effect=[
                self.response(ProviderInterface.ANTHROPIC, text='First provider'),
                self.response(ProviderInterface.GOOGLE, text='Second provider'),
            ],
        ) as route:
            first = submit_message(
                user=self.user,
                content='First turn',
                provider=ProviderInterface.ANTHROPIC,
                idempotency_key='provider-turn-1',
            )
            second = submit_message(
                user=self.user,
                content='Second turn',
                provider=ProviderInterface.GOOGLE,
                idempotency_key='provider-turn-2',
                conversation_id=first.conversation.pk,
            )

        self.assertEqual(route.call_count, 2)
        self.assertEqual(first.assistant_message.provider, ProviderInterface.ANTHROPIC)
        self.assertEqual(second.assistant_message.provider, ProviderInterface.GOOGLE)
        self.assertEqual(
            list(first.conversation.messages.values_list('role', 'provider')),
            [
                ('user', None),
                ('assistant', ProviderInterface.ANTHROPIC),
                ('user', None),
                ('assistant', ProviderInterface.GOOGLE),
            ],
        )

    def test_insufficient_balance_does_not_call_the_proxy(self):
        wallet = self.user.wallet
        wallet.balance_micro_usd = 0
        wallet.save(update_fields=('balance_micro_usd',))

        with patch('chat.conversations.orchestration.route_request') as route:
            with self.assertRaises(InsufficientBalance):
                submit_message(
                    user=self.user,
                    content='Too expensive',
                    provider=ProviderInterface.OPENAI,
                    idempotency_key='no-balance',
                )

        route.assert_not_called()
        request = UsageRequest.objects.get(idempotency_key='no-balance')
        self.assertEqual(request.status, UsageRequest.Status.FAILED_BEFORE_UPSTREAM)
        self.assertEqual(request.conversation.messages.filter(role=Message.Role.USER).count(), 1)

    def test_ambiguous_proxy_failure_keeps_reservation_and_duplicate_does_not_retry(self):
        with patch(
            'chat.conversations.orchestration.route_request',
            side_effect=ProxyError('temporary upstream failure'),
        ) as route:
            with self.assertRaises(ProxyError):
                submit_message(
                    user=self.user,
                    content='Could not complete',
                    provider=ProviderInterface.OPENAI,
                    idempotency_key='ambiguous-turn',
                )

            duplicate = submit_message(
                user=self.user,
                content='Could not complete',
                provider=ProviderInterface.OPENAI,
                idempotency_key='ambiguous-turn',
            )

        self.assertEqual(route.call_count, 1)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            duplicate.usage_request.status,
            UsageRequest.Status.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(
            duplicate.usage_request.reconciliation_reason,
            UsageRequest.ReconciliationReason.USAGE_UNKNOWN,
        )
        self.assertIsNone(duplicate.usage_request.input_tokens)
        self.assertGreater(duplicate.usage_request.reserved_micro_usd, 0)

    def test_missing_proxy_configuration_releases_reservation_before_upstream(self):
        with patch(
            'chat.conversations.orchestration.route_request',
            side_effect=ProxyConfigurationError('provider key is missing'),
        ) as route:
            with self.assertRaises(ProxyConfigurationError):
                submit_message(
                    user=self.user,
                    content='Configuration failure',
                    provider=ProviderInterface.OPENAI,
                    idempotency_key='missing-key',
                )

        route.assert_called_once()
        request = UsageRequest.objects.get(idempotency_key='missing-key')
        self.assertEqual(request.status, UsageRequest.Status.FAILED_BEFORE_UPSTREAM)
        self.assertTrue(
            request.ledger_entries.filter(
                entry_type='release',
            ).exists()
        )
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD)

    def test_foreign_conversation_is_not_sent_to_the_proxy(self):
        other_user = User.objects.create_user(username='other-chat-user', password='password')
        conversation, _ = Conversation.create_from_first_message(other_user, 'Private')

        with patch('chat.conversations.orchestration.route_request') as route:
            with self.assertRaises(Conversation.DoesNotExist):
                submit_message(
                    user=self.user,
                    content='Attempted access',
                    provider=ProviderInterface.OPENAI,
                    idempotency_key='foreign-turn',
                    conversation_id=conversation.pk,
                )

        route.assert_not_called()

    def test_home_renders_balance_and_provider_selection(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('chat:home'))

        self.assertTemplateUsed(response, 'chat/home.html')
        self.assertContains(response, '$5.000000')
        self.assertContains(response, 'OpenAI-compatible')
        self.assertContains(response, 'Anthropic-compatible')
        self.assertContains(response, 'Google-compatible')
        self.assertContains(response, 'Conversations')

    def test_conversation_page_renders_roles_usage_and_exact_debit(self):
        self.client.force_login(self.user)
        with patch(
            'chat.conversations.orchestration.route_request',
            return_value=self.response(ProviderInterface.OPENAI),
        ):
            result = submit_message(
                user=self.user,
                content='Render this answer',
                provider=ProviderInterface.OPENAI,
                idempotency_key='template-turn',
            )

        response = self.client.get(
            reverse('chat:conversation', kwargs={'conversation_id': result.conversation.pk})
        )

        self.assertTemplateUsed(response, 'chat/conversation.html')
        self.assertContains(response, 'message-card--user')
        self.assertContains(response, 'message-card--assistant')
        self.assertContains(response, 'OpenAI-compatible')
        self.assertContains(response, '12 input')
        self.assertContains(response, '5 output')
        self.assertContains(response, '17 total tokens')
        self.assertContains(response, '$0.000034')
        self.assertContains(response, '$4.999966')

    def test_message_post_redirects_to_the_created_conversation(self):
        self.client.force_login(self.user)
        with patch(
            'chat.conversations.orchestration.route_request',
            return_value=self.response(ProviderInterface.GOOGLE),
        ):
            response = self.client.post(
                reverse('chat:send_message'),
                {
                    'content': 'Submitted through HTML form',
                    'provider': ProviderInterface.GOOGLE,
                    'conversation_id': '',
                    'idempotency_key': 'html-form-turn',
                },
            )

        request = UsageRequest.objects.get(idempotency_key='html-form-turn')
        self.assertRedirects(
            response,
            reverse('chat:conversation', kwargs={'conversation_id': request.conversation_id}),
        )
