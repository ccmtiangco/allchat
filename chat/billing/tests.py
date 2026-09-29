from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings

from ..choices import ProviderInterface
from ..conversations.models import Conversation, Message
from .models import (
    INITIAL_BALANCE_MICRO_USD,
    PRICING_VERSION,
    UsageRequest,
    WalletLedgerEntry,
)
from .services import (
    BillingError,
    IdempotencyConflict,
    InsufficientBalance,
    InvalidRequestState,
    admin_adjust_wallet,
    calculate_charge_micro_usd,
    create_usage_request,
    fail_before_upstream,
    mark_usage_unknown,
    refund_usage,
    release_unknown_usage,
    reserve_usage,
    settle_usage,
)

User = get_user_model()


class BillingModelAndServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='billing-user', password='password')
        self.conversation, _ = Conversation.create_from_first_message(
            self.user,
            'Calculate a token charge',
        )

    def make_request(self, *, key='request-1', provider=ProviderInterface.OPENAI):
        return create_usage_request(self.user, self.conversation, provider, key)[0]

    def make_assistant_message(self, content='Assistant response'):
        return Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content=content,
            provider=ProviderInterface.OPENAI,
        )

    def current_balance(self):
        wallet = self.user.wallet
        wallet.refresh_from_db()
        return wallet.balance_micro_usd

    def test_opening_credit_is_recorded_as_an_append_only_ledger_entry(self):
        wallet = self.user.wallet
        entry = wallet.ledger_entries.get()

        self.assertEqual(wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(wallet.formatted_balance, '$5.000000')
        self.assertEqual(entry.amount_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(entry.balance_delta_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(entry.entry_type, WalletLedgerEntry.EntryType.INITIAL_CREDIT)

        entry.metadata = {'reason': 'changed'}
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(TypeError):
            WalletLedgerEntry.objects.filter(pk=entry.pk).update(metadata={'reason': 'changed'})
        with self.assertRaises(TypeError):
            WalletLedgerEntry.objects.filter(pk=entry.pk).delete()

    def test_usage_records_with_ledger_entries_cannot_be_deleted(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=10, maximum_output_tokens=0)
        reservation = request.ledger_entries.get(
            entry_type=WalletLedgerEntry.EntryType.RESERVATION
        )

        with self.assertRaises(ValidationError):
            request.delete()

        reservation.refresh_from_db()
        self.assertEqual(reservation.related_request_id, request.pk)

    def test_flat_rate_uses_combined_tokens_and_exact_micro_dollars(self):
        self.assertEqual(calculate_charge_micro_usd(1, 0), 2)
        self.assertEqual(calculate_charge_micro_usd(1_000, 0), 2_000)
        self.assertEqual(calculate_charge_micro_usd(600, 400), 2_000)
        self.assertEqual(calculate_charge_micro_usd(0, 0), 0)
        with self.assertRaises(ValueError):
            calculate_charge_micro_usd(-1, 0)
        with self.assertRaises(ValueError):
            calculate_charge_micro_usd(True, 0)

    def test_usage_request_is_idempotent_per_user_and_provider_selection(self):
        first, created = create_usage_request(
            self.user,
            self.conversation,
            ProviderInterface.OPENAI,
            'same-key',
        )
        repeated, repeated_created = create_usage_request(
            self.user,
            self.conversation,
            ProviderInterface.OPENAI,
            'same-key',
        )

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(first.status, UsageRequest.Status.PENDING)
        self.assertIsNone(first.input_tokens)
        self.assertEqual(first.pricing_version, PRICING_VERSION)
        reserve_usage(first, maximum_input_tokens=10, maximum_output_tokens=0)
        with self.assertRaises(InvalidRequestState):
            reserve_usage(repeated, maximum_input_tokens=10, maximum_output_tokens=0)
        with self.assertRaises(IdempotencyConflict):
            create_usage_request(
                self.user,
                self.conversation,
                ProviderInterface.ANTHROPIC,
                'same-key',
            )

    def test_usage_creation_rejects_another_users_conversation(self):
        other_user = User.objects.create_user(username='other-billing-user', password='password')

        with self.assertRaises(PermissionError):
            create_usage_request(
                other_user,
                self.conversation,
                ProviderInterface.OPENAI,
                'foreign-conversation',
            )

    def test_failed_usage_records_cannot_be_deleted(self):
        request = self.make_request()
        failed = fail_before_upstream(request)

        with self.assertRaises(ValidationError):
            failed.delete()
        with self.assertRaises(TypeError):
            UsageRequest.objects.filter(pk=failed.pk).delete()

        self.assertTrue(UsageRequest.objects.filter(pk=failed.pk).exists())

    def test_usage_record_save_rejects_a_conversation_owned_by_another_user(self):
        other_user = User.objects.create_user(username='foreign-owner', password='password')
        other_conversation, _ = Conversation.create_from_first_message(other_user, 'Private')

        with self.assertRaises(ValidationError):
            UsageRequest.objects.create(
                user=self.user,
                conversation=other_conversation,
                provider=ProviderInterface.OPENAI,
                idempotency_key='mismatched-owner',
            )

    def test_reservation_and_settlement_release_unused_amount(self):
        request = self.make_request()
        reserved = reserve_usage(
            request,
            maximum_input_tokens=1_000,
            maximum_output_tokens=1_500,
        )
        self.assertEqual(reserved.status, UsageRequest.Status.RESERVED)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD - 5_000)

        assistant_message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content='The answer',
            provider=ProviderInterface.OPENAI,
        )
        settled = settle_usage(
            request,
            1_000,
            1_000,
            assistant_message=assistant_message,
            upstream_request_id='proxy-request-1',
            latency_ms=1_234,
        )

        self.assertEqual(settled.status, UsageRequest.Status.SUCCEEDED)
        self.assertEqual(settled.input_tokens, 1_000)
        self.assertEqual(settled.output_tokens, 1_000)
        self.assertEqual(settled.total_tokens, 2_000)
        self.assertEqual(settled.charge_micro_usd, 4_000)
        self.assertEqual(settled.upstream_request_id, 'proxy-request-1')
        self.assertEqual(settled.assistant_message_id, assistant_message.pk)
        self.assertEqual(settled.latency_ms, 1_234)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD - 4_000)
        wallet = self.user.wallet
        wallet.refresh_from_db()
        self.assertEqual(wallet.formatted_balance, '$4.996000')

        entries = list(self.user.wallet.ledger_entries.all())
        self.assertEqual(
            [entry.entry_type for entry in entries],
            [
                WalletLedgerEntry.EntryType.INITIAL_CREDIT,
                WalletLedgerEntry.EntryType.RESERVATION,
                WalletLedgerEntry.EntryType.USAGE_DEBIT,
                WalletLedgerEntry.EntryType.RELEASE,
            ],
        )
        self.assertEqual(sum(entry.balance_delta_micro_usd for entry in entries), self.current_balance())

    def test_zero_usage_settles_and_releases_the_entire_reservation(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=50, maximum_output_tokens=0)

        settled = settle_usage(
            request,
            0,
            0,
            assistant_message=self.make_assistant_message(),
        )

        self.assertEqual(settled.status, UsageRequest.Status.SUCCEEDED)
        self.assertEqual(settled.total_tokens, 0)
        self.assertEqual(settled.charge_micro_usd, 0)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD)
        self.assertFalse(
            self.user.wallet.ledger_entries.filter(
                entry_type=WalletLedgerEntry.EntryType.USAGE_DEBIT
            ).exists()
        )

    def test_insufficient_balance_does_not_reserve_or_change_the_wallet(self):
        request = self.make_request()

        with self.assertRaises(InsufficientBalance):
            reserve_usage(
                request,
                maximum_input_tokens=(INITIAL_BALANCE_MICRO_USD // 2) + 1,
                maximum_output_tokens=0,
            )

        request.refresh_from_db()
        self.user.wallet.refresh_from_db()
        self.assertEqual(request.status, UsageRequest.Status.PENDING)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD)

    @override_settings(MAX_CHAT_OUTPUT_TOKENS=16)
    def test_reservation_rejects_output_caps_above_configured_limit(self):
        request = self.make_request()

        with self.assertRaises(ValueError):
            reserve_usage(request, maximum_input_tokens=10, maximum_output_tokens=17)

        request.refresh_from_db()
        self.assertEqual(request.status, UsageRequest.Status.PENDING)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD)

    def test_usage_unknown_keeps_the_reservation_for_reconciliation(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=500, maximum_output_tokens=0)

        unknown = mark_usage_unknown(request, upstream_request_id='ambiguous-upstream-id')

        self.assertEqual(unknown.status, UsageRequest.Status.RECONCILIATION_REQUIRED)
        self.assertEqual(
            unknown.reconciliation_reason,
            UsageRequest.ReconciliationReason.USAGE_UNKNOWN,
        )
        self.assertIsNone(unknown.input_tokens)
        self.assertEqual(unknown.charge_micro_usd, 0)
        self.assertEqual(unknown.upstream_request_id, 'ambiguous-upstream-id')
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD - 1_000)

    def test_usage_unknown_can_retain_partial_response_and_elapsed_time(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=500, maximum_output_tokens=200)
        partial = self.make_assistant_message('An incomplete response')

        unknown = mark_usage_unknown(
            request,
            upstream_request_id='interrupted-stream-id',
            assistant_message=partial,
            latency_ms=2_450,
        )

        self.assertEqual(unknown.status, UsageRequest.Status.RECONCILIATION_REQUIRED)
        self.assertEqual(unknown.assistant_message_id, partial.pk)
        self.assertEqual(unknown.latency_ms, 2_450)
        self.assertIsNone(unknown.input_tokens)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD - 1_400)

    def test_charge_larger_than_reservation_requires_reconciliation(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=25, maximum_output_tokens=25)

        result = settle_usage(
            request,
            25,
            26,
            assistant_message=self.make_assistant_message(),
        )

        self.assertEqual(result.status, UsageRequest.Status.RECONCILIATION_REQUIRED)
        self.assertEqual(
            result.reconciliation_reason,
            UsageRequest.ReconciliationReason.RESERVATION_EXCEEDED,
        )
        self.assertEqual(result.charge_micro_usd, 102)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD - 100)

    def test_failure_before_upstream_releases_a_reservation(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=250, maximum_output_tokens=0)

        failed = fail_before_upstream(request)

        self.assertEqual(failed.status, UsageRequest.Status.FAILED_BEFORE_UPSTREAM)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(
            list(self.user.wallet.ledger_entries.values_list('entry_type', flat=True)),
            [
                WalletLedgerEntry.EntryType.INITIAL_CREDIT,
                WalletLedgerEntry.EntryType.RESERVATION,
                WalletLedgerEntry.EntryType.RELEASE,
            ],
        )

    def test_repeated_settlement_does_not_duplicate_ledger_entries(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=15, maximum_output_tokens=35)
        assistant_message = self.make_assistant_message()
        settled = settle_usage(request, 10, 10, assistant_message=assistant_message)
        settle_usage(request, 10, 10, assistant_message=assistant_message)

        self.assertEqual(self.user.wallet.ledger_entries.count(), 4)
        self.assertEqual(settled.charge_micro_usd, 40)

    def test_refunds_are_recorded_and_cannot_exceed_settled_charge(self):
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=500, maximum_output_tokens=750)
        settle_usage(
            request,
            500,
            500,
            assistant_message=self.make_assistant_message(),
        )
        first_refund = refund_usage(
            request,
            500,
            idempotency_key='support-refund-1',
            metadata={'reason': 'support adjustment'},
        )
        repeated_refund = refund_usage(
            request,
            500,
            idempotency_key='support-refund-1',
            metadata={'reason': 'support adjustment'},
        )

        self.assertEqual(first_refund.pk, repeated_refund.pk)
        self.assertEqual(
            self.current_balance(),
            INITIAL_BALANCE_MICRO_USD - 1_500,
        )
        self.assertEqual(
            self.user.wallet.ledger_entries.filter(
                entry_type=WalletLedgerEntry.EntryType.REFUND
            ).get().metadata,
            {'reason': 'support adjustment'},
        )
        with self.assertRaises(BillingError):
            refund_usage(request, 1_501, idempotency_key='support-refund-2')

    def test_admin_wallet_adjustments_are_reasoned_ledger_entries(self):
        admin_user = User.objects.create_superuser(
            username='wallet-admin',
            email='wallet-admin@example.com',
            password='A-strong-password-8675309',
        )
        credit = admin_adjust_wallet(
            self.user.wallet,
            1_200,
            reason='Promotional credit',
            actor=admin_user,
        )
        debit = admin_adjust_wallet(
            self.user.wallet,
            -200,
            reason='Correct duplicate credit',
            actor=admin_user,
        )

        self.assertEqual(credit.entry_type, WalletLedgerEntry.EntryType.ADMIN_CREDIT)
        self.assertEqual(credit.balance_delta_micro_usd, 1_200)
        self.assertEqual(credit.metadata['reason'], 'Promotional credit')
        self.assertEqual(credit.metadata['admin_user_id'], admin_user.pk)
        self.assertEqual(debit.entry_type, WalletLedgerEntry.EntryType.ADMIN_DEBIT)
        self.assertEqual(debit.balance_delta_micro_usd, -200)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD + 1_000)

    def test_admin_can_release_unknown_usage_with_an_audited_entry(self):
        admin_user = User.objects.create_superuser(
            username='reconciliation-admin',
            email='reconciliation-admin@example.com',
            password='A-strong-password-8675309',
        )
        request = self.make_request()
        reserve_usage(request, maximum_input_tokens=100, maximum_output_tokens=20)
        mark_usage_unknown(request, upstream_request_id='unknown-usage-id')

        resolved = release_unknown_usage(
            request,
            reason='Confirmed no upstream charge',
            actor=admin_user,
        )

        self.assertEqual(resolved.status, UsageRequest.Status.RECONCILED_RELEASED)
        self.assertEqual(self.current_balance(), INITIAL_BALANCE_MICRO_USD)
        entry = self.user.wallet.ledger_entries.get(
            entry_type=WalletLedgerEntry.EntryType.RELEASE,
            related_request=request,
        )
        self.assertEqual(entry.metadata['reason'], 'Confirmed no upstream charge')
        self.assertEqual(entry.metadata['admin_user_id'], admin_user.pk)


@skipUnless(connection.features.has_select_for_update, 'Database does not support row-level locks.')
class WalletReservationConcurrencyTests(TransactionTestCase):
    def test_concurrent_reservations_cannot_spend_the_same_balance(self):
        user = User.objects.create_user(username='concurrent-user', password='password')
        wallet = user.wallet
        wallet.balance_micro_usd = 100
        wallet.save(update_fields=('balance_micro_usd',))
        WalletLedgerEntry.objects.create(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.RESERVATION,
            amount_micro_usd=INITIAL_BALANCE_MICRO_USD - 100,
            idempotency_key='existing-hold',
        )
        conversation, _ = Conversation.create_from_first_message(user, 'Concurrent requests')
        requests = [
            create_usage_request(
                user,
                conversation,
                ProviderInterface.OPENAI,
                f'parallel-{number}',
            )[0]
            for number in range(2)
        ]
        barrier = Barrier(2)

        def attempt_reservation(request_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                reserve_usage(
                    UsageRequest(pk=request_id),
                    maximum_input_tokens=37,
                    maximum_output_tokens=0,
                )
                return 'reserved'
            except InsufficientBalance:
                return 'insufficient'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt_reservation, [item.pk for item in requests]))

        self.assertCountEqual(results, ['reserved', 'insufficient'])
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance_micro_usd, 26)
        self.assertEqual(
            wallet.ledger_entries.filter(
                entry_type=WalletLedgerEntry.EntryType.RESERVATION
            ).count(),
            2,
        )

    def test_concurrent_settlement_charges_the_request_only_once(self):
        user = User.objects.create_user(username='concurrent-settlement-user', password='password')
        conversation, _ = Conversation.create_from_first_message(user, 'Concurrent settlement')
        request, _ = create_usage_request(
            user,
            conversation,
            ProviderInterface.OPENAI,
            'parallel-settlement',
        )
        reserve_usage(request, maximum_input_tokens=100, maximum_output_tokens=100)
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content='Settled response',
            provider=ProviderInterface.OPENAI,
        )
        barrier = Barrier(2)

        def settle(request_id, assistant_message_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return settle_usage(
                    UsageRequest(pk=request_id),
                    50,
                    50,
                    assistant_message=Message(pk=assistant_message_id),
                ).status
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    settle,
                    [request.pk, request.pk],
                    [assistant_message.pk, assistant_message.pk],
                )
            )

        self.assertEqual(statuses, [UsageRequest.Status.SUCCEEDED] * 2)
        user.wallet.refresh_from_db()
        self.assertEqual(
            user.wallet.balance_micro_usd,
            INITIAL_BALANCE_MICRO_USD - 200,
        )
        self.assertEqual(
            user.wallet.ledger_entries.filter(
                entry_type=WalletLedgerEntry.EntryType.USAGE_DEBIT
            ).count(),
            1,
        )
        self.assertEqual(
            user.wallet.ledger_entries.filter(
                entry_type=WalletLedgerEntry.EntryType.RELEASE
            ).count(),
            1,
        )
