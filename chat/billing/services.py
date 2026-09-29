from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

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


def _normalize_idempotency_key(idempotency_key):
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


def create_usage_request(user, conversation, provider, idempotency_key):
    if conversation.owner_id != user.pk:
        raise PermissionError('The conversation does not belong to the usage owner.')
    if provider not in ProviderInterface.values:
        raise ValueError('Unsupported provider interface.')

    idempotency_key = _normalize_idempotency_key(idempotency_key)

    request, created = UsageRequest.objects.get_or_create(
        user=user,
        idempotency_key=idempotency_key,
        defaults={'conversation': conversation, 'provider': provider},
    )
    if not created and (
        request.conversation_id != conversation.pk or request.provider != provider
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
    assistant_message=None,
    upstream_request_id='',
):
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


def mark_usage_unknown(request, *, upstream_request_id=''):
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
        locked_request.completed_at = timezone.now()
        locked_request.save(
            update_fields=(
                'status',
                'reconciliation_reason',
                'upstream_request_id',
                'completed_at',
                'updated_at',
            )
        )
        return locked_request


def refund_usage(request, amount_micro_usd, *, idempotency_key, metadata=None):
    if type(amount_micro_usd) is not int or amount_micro_usd <= 0:
        raise ValueError('A refund must be a positive integer amount in micro-dollars.')
    idempotency_key = _normalize_idempotency_key(idempotency_key)

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
