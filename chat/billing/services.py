from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from uuid import uuid4

from ..choices import ProviderInterface
from .models import (
    INITIAL_BALANCE_MICRO_USD,
    UsageRequest,
    Wallet,
    WalletLedgerEntry,
)


class BillingError(Exception):
    pass


class InsufficientBalance(BillingError):
    pass


class InvalidRequestState(BillingError):
    pass


class IdempotencyConflict(BillingError):
    pass


def calculate_charge_micro_usd(input_tokens, output_tokens):
    if any(type(count) is not int or count < 0 for count in (input_tokens, output_tokens)):
        raise ValueError('Token counts must be non-negative integers.')
    return (input_tokens + output_tokens) * 2


def normalize_idempotency_key(idempotency_key):
    if not isinstance(idempotency_key, str):
        raise ValueError('The idempotency key must be a string.')
    idempotency_key = idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError('The idempotency key must contain 1 to 128 characters.')
    return idempotency_key


def provision_initial_wallet(user, *, using=None):
    wallet = Wallet.objects.db_manager(using).create(
        user=user,
        balance_micro_usd=INITIAL_BALANCE_MICRO_USD,
    )
    WalletLedgerEntry.objects.db_manager(using).create(
        wallet=wallet,
        entry_type=WalletLedgerEntry.EntryType.INITIAL_CREDIT,
        amount_micro_usd=INITIAL_BALANCE_MICRO_USD,
        idempotency_key='initial-credit',
    )


def create_usage_request(user, conversation, provider, idempotency_key, *, user_message=None):
    if not conversation.is_owned_by(user):
        raise PermissionError('The conversation does not belong to the usage owner.')
    if provider not in ProviderInterface.values:
        raise ValueError('Unsupported provider interface.')

    idempotency_key = normalize_idempotency_key(idempotency_key)

    request, created = UsageRequest.objects.get_or_create(
        user=user,
        idempotency_key=idempotency_key,
        defaults={
            'conversation': conversation,
            'provider': provider,
            'user_message': user_message,
        },
    )
    if not created and (
        request.conversation_id != conversation.pk
        or request.provider != provider
        or (
            user_message is not None
            and (
                request.user_message is None
                or request.user_message.content != user_message.content
            )
        )
    ):
        raise IdempotencyConflict('The idempotency key is already bound to another request.')
    return request, created


def reserve_usage(request, *, maximum_input_tokens, maximum_output_tokens):
    if (
        type(maximum_input_tokens) is not int
        or type(maximum_output_tokens) is not int
        or maximum_input_tokens < 0
        or maximum_output_tokens < 0
    ):
        raise ValueError('Token limits must be non-negative integers.')
    if maximum_output_tokens > settings.MAX_CHAT_OUTPUT_TOKENS:
        raise ValueError('The output token limit exceeds MAX_CHAT_OUTPUT_TOKENS.')
    maximum_total_tokens = maximum_input_tokens + maximum_output_tokens
    if maximum_total_tokens == 0:
        raise ValueError('The maximum total token count must be positive.')
    maximum_charge_micro_usd = calculate_charge_micro_usd(
        maximum_input_tokens,
        maximum_output_tokens,
    )

    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if locked_request.status != UsageRequest.Status.PENDING:
            raise InvalidRequestState('Only pending requests can be reserved.')

        wallet = Wallet.objects.select_for_update().get(user_id=locked_request.user_id)
        if wallet.balance_micro_usd < maximum_charge_micro_usd:
            raise InsufficientBalance('The wallet balance does not cover this reservation.')

        wallet.balance_micro_usd -= maximum_charge_micro_usd
        wallet.save(update_fields=('balance_micro_usd',))
        locked_request.reserved_input_tokens = maximum_input_tokens
        locked_request.reserved_output_tokens = maximum_output_tokens
        locked_request.reserved_total_tokens = maximum_total_tokens
        locked_request.reserved_micro_usd = maximum_charge_micro_usd
        locked_request.status = UsageRequest.Status.RESERVED
        locked_request.save(
            update_fields=(
                'reserved_input_tokens',
                'reserved_output_tokens',
                'reserved_total_tokens',
                'reserved_micro_usd',
                'status',
                'updated_at',
            )
        )
        WalletLedgerEntry.objects.create(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.RESERVATION,
            amount_micro_usd=maximum_charge_micro_usd,
            idempotency_key=f'usage-{locked_request.pk}-reservation',
            related_request=locked_request,
        )
        return locked_request


def _release_reservation(wallet, request, amount_micro_usd):
    if not amount_micro_usd:
        return
    wallet.balance_micro_usd += amount_micro_usd
    wallet.save(update_fields=('balance_micro_usd',))
    WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=WalletLedgerEntry.EntryType.RELEASE,
        amount_micro_usd=amount_micro_usd,
        idempotency_key=f'usage-{request.pk}-release',
        related_request=request,
    )


def settle_usage(
    request,
    input_tokens,
    output_tokens,
    *,
    assistant_message,
    upstream_request_id='',
    latency_ms=None,
):
    if assistant_message.pk is None:
        raise ValueError('A successful usage settlement requires a saved assistant message.')
    if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
        raise ValueError('Latency must be a non-negative integer number of milliseconds.')
    charge = calculate_charge_micro_usd(input_tokens, output_tokens)
    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if locked_request.status == UsageRequest.Status.SUCCEEDED:
            if (
                locked_request.input_tokens == input_tokens
                and locked_request.output_tokens == output_tokens
                and locked_request.assistant_message_id == getattr(assistant_message, 'pk', None)
            ):
                return locked_request
            raise InvalidRequestState('The request has already been settled with different usage.')
        if locked_request.status != UsageRequest.Status.RESERVED:
            raise InvalidRequestState('Only reserved requests can be settled.')

        locked_request.input_tokens = input_tokens
        locked_request.output_tokens = output_tokens
        locked_request.total_tokens = input_tokens + output_tokens
        locked_request.charge_micro_usd = charge
        locked_request.upstream_request_id = upstream_request_id
        locked_request.assistant_message = assistant_message
        locked_request.latency_ms = latency_ms
        locked_request.completed_at = timezone.now()

        if (
            input_tokens > locked_request.reserved_input_tokens
            or output_tokens > locked_request.reserved_output_tokens
            or charge > locked_request.reserved_micro_usd
        ):
            locked_request.status = UsageRequest.Status.RECONCILIATION_REQUIRED
            locked_request.reconciliation_reason = (
                UsageRequest.ReconciliationReason.RESERVATION_EXCEEDED
            )
            locked_request.save()
            return locked_request

        wallet = Wallet.objects.select_for_update().get(user_id=locked_request.user_id)
        if charge:
            WalletLedgerEntry.objects.create(
                wallet=wallet,
                entry_type=WalletLedgerEntry.EntryType.USAGE_DEBIT,
                amount_micro_usd=charge,
                idempotency_key=f'usage-{locked_request.pk}-debit',
                related_request=locked_request,
                metadata={'pricing_version': locked_request.pricing_version},
            )

        release_amount = locked_request.reserved_micro_usd - charge
        _release_reservation(wallet, locked_request, release_amount)

        locked_request.status = UsageRequest.Status.SUCCEEDED
        locked_request.reconciliation_reason = None
        locked_request.save()
        return locked_request


def fail_before_upstream(request):
    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if locked_request.status == UsageRequest.Status.FAILED_BEFORE_UPSTREAM:
            return locked_request
        if locked_request.status not in {
            UsageRequest.Status.PENDING,
            UsageRequest.Status.RESERVED,
        }:
            raise InvalidRequestState('This request cannot be marked as failed.')

        if locked_request.status == UsageRequest.Status.RESERVED:
            wallet = Wallet.objects.select_for_update().get(user_id=locked_request.user_id)
            _release_reservation(wallet, locked_request, locked_request.reserved_micro_usd)

        locked_request.status = UsageRequest.Status.FAILED_BEFORE_UPSTREAM
        locked_request.completed_at = timezone.now()
        locked_request.save(update_fields=('status', 'completed_at', 'updated_at'))
        return locked_request


def mark_usage_unknown(
    request,
    *,
    upstream_request_id='',
    assistant_message=None,
    latency_ms=None,
):
    if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
        raise ValueError('Latency must be a non-negative integer number of milliseconds.')
    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if (
            locked_request.status == UsageRequest.Status.RECONCILIATION_REQUIRED
            and locked_request.reconciliation_reason == UsageRequest.ReconciliationReason.USAGE_UNKNOWN
        ):
            return locked_request
        if locked_request.status != UsageRequest.Status.RESERVED:
            raise InvalidRequestState('Only reserved requests can have unknown usage.')

        locked_request.status = UsageRequest.Status.RECONCILIATION_REQUIRED
        locked_request.reconciliation_reason = UsageRequest.ReconciliationReason.USAGE_UNKNOWN
        locked_request.upstream_request_id = upstream_request_id
        locked_request.assistant_message = assistant_message
        locked_request.latency_ms = latency_ms
        locked_request.completed_at = timezone.now()
        locked_request.save(
            update_fields=(
                'status',
                'reconciliation_reason',
                'upstream_request_id',
                'assistant_message',
                'latency_ms',
                'completed_at',
                'updated_at',
            )
        )
        return locked_request


def refund_usage(request, amount_micro_usd, *, idempotency_key, metadata=None):
    if type(amount_micro_usd) is not int or amount_micro_usd <= 0:
        raise ValueError('A refund must be a positive integer amount in micro-dollars.')
    idempotency_key = normalize_idempotency_key(idempotency_key)

    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if locked_request.status != UsageRequest.Status.SUCCEEDED:
            raise InvalidRequestState('Only settled usage can be refunded.')
        locked_wallet = Wallet.objects.select_for_update().get(user_id=locked_request.user_id)
        existing_entry = WalletLedgerEntry.objects.filter(
            wallet=locked_wallet,
            idempotency_key=idempotency_key,
        ).first()
        if existing_entry:
            if (
                existing_entry.related_request_id == locked_request.pk
                and existing_entry.entry_type == WalletLedgerEntry.EntryType.REFUND
                and existing_entry.amount_micro_usd == amount_micro_usd
            ):
                return existing_entry
            raise IdempotencyConflict('The refund idempotency key is already in use.')

        refunded = (
            WalletLedgerEntry.objects.filter(
                related_request=locked_request,
                entry_type=WalletLedgerEntry.EntryType.REFUND,
            ).aggregate(total=Sum('amount_micro_usd'))['total']
            or 0
        )
        if refunded + amount_micro_usd > locked_request.charge_micro_usd:
            raise BillingError('Refunds cannot exceed the settled usage charge.')

        locked_wallet.balance_micro_usd += amount_micro_usd
        locked_wallet.save(update_fields=('balance_micro_usd',))
        return WalletLedgerEntry.objects.create(
            wallet=locked_wallet,
            entry_type=WalletLedgerEntry.EntryType.REFUND,
            amount_micro_usd=amount_micro_usd,
            idempotency_key=idempotency_key,
            related_request=locked_request,
            metadata=metadata or {},
        )


def admin_adjust_wallet(wallet, signed_amount_micro_usd, *, reason, actor):
    if type(signed_amount_micro_usd) is not int or signed_amount_micro_usd == 0:
        raise ValueError('The adjustment must be a non-zero integer amount in micro-dollars.')
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValueError('An adjustment reason of 1 to 500 characters is required.')
    if not actor.is_staff:
        raise PermissionError('Only staff users can adjust a wallet.')

    amount = abs(signed_amount_micro_usd)
    entry_type = (
        WalletLedgerEntry.EntryType.ADMIN_CREDIT
        if signed_amount_micro_usd > 0
        else WalletLedgerEntry.EntryType.ADMIN_DEBIT
    )
    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if signed_amount_micro_usd < 0 and locked_wallet.balance_micro_usd < amount:
            raise InsufficientBalance('The wallet cannot be adjusted below zero.')
        locked_wallet.balance_micro_usd += signed_amount_micro_usd
        locked_wallet.save(update_fields=('balance_micro_usd',))
        return WalletLedgerEntry.objects.create(
            wallet=locked_wallet,
            entry_type=entry_type,
            amount_micro_usd=amount,
            idempotency_key=f'admin-adjustment-{uuid4().hex}',
            metadata={'reason': reason.strip(), 'admin_user_id': actor.pk},
        )


def release_unknown_usage(request, *, reason, actor):
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValueError('A reconciliation reason of 1 to 500 characters is required.')
    if not actor.is_staff:
        raise PermissionError('Only staff users can reconcile usage.')

    with transaction.atomic():
        locked_request = UsageRequest.objects.select_for_update().get(pk=request.pk)
        if locked_request.status == UsageRequest.Status.RECONCILED_RELEASED:
            return locked_request
        if (
            locked_request.status != UsageRequest.Status.RECONCILIATION_REQUIRED
            or locked_request.reconciliation_reason != UsageRequest.ReconciliationReason.USAGE_UNKNOWN
        ):
            raise InvalidRequestState('Only unknown usage requests can be released this way.')

        amount = locked_request.reserved_micro_usd
        wallet = Wallet.objects.select_for_update().get(user_id=locked_request.user_id)
        wallet.balance_micro_usd += amount
        wallet.save(update_fields=('balance_micro_usd',))
        WalletLedgerEntry.objects.create(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.RELEASE,
            amount_micro_usd=amount,
            idempotency_key=f'usage-{locked_request.pk}-reconciliation-release',
            related_request=locked_request,
            metadata={
                'reason': reason.strip(),
                'admin_user_id': actor.pk,
                'resolution': 'release_unknown_usage',
            },
        )
        locked_request.status = UsageRequest.Status.RECONCILED_RELEASED
        locked_request.reconciliation_reason = None
        locked_request.completed_at = timezone.now()
        locked_request.save()
        return locked_request
