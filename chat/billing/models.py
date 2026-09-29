from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from ..choices import ProviderInterface

INITIAL_BALANCE_MICRO_USD = 5_000_000
PRICING_VERSION = 'flat-2-micro-usd-per-token-v1'


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet',
    )
    balance_micro_usd = models.PositiveBigIntegerField(default=0)

    @property
    def formatted_balance(self):
        dollars, micro_dollars = divmod(self.balance_micro_usd, 1_000_000)
        return f'${dollars}.{micro_dollars:06d}'

    def __str__(self):
        return f'{self.user} wallet ({self.formatted_balance})'


class WalletLedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        INITIAL_CREDIT = 'initial_credit', 'Initial credit'
        RESERVATION = 'reservation', 'Usage reservation'
        USAGE_DEBIT = 'usage_debit', 'Settled usage'
        RELEASE = 'release', 'Reservation release'
        REFUND = 'refund', 'Refund'

    class AppendOnlyQuerySet(models.QuerySet):
        def update(self, **kwargs):
            raise TypeError('Wallet ledger entries cannot be updated.')

        def delete(self):
            raise TypeError('Wallet ledger entries cannot be deleted.')

    objects = AppendOnlyQuerySet.as_manager()

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name='ledger_entries',
    )
    entry_type = models.CharField(max_length=32, choices=EntryType.choices)
    amount_micro_usd = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=128, blank=True, default='')
    related_request = models.ForeignKey(
        'chat.UsageRequest',
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='ledger_entries',
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_micro_usd__gt=0),
                name='wallet_ledger_amount_positive',
            ),
            models.CheckConstraint(
                condition=Q(
                    entry_type__in=(
                        'initial_credit',
                        'reservation',
                        'usage_debit',
                        'release',
                        'refund',
                    )
                ),
                name='wallet_ledger_entry_type_supported',
            ),
            models.UniqueConstraint(
                fields=('wallet',),
                condition=Q(entry_type='initial_credit'),
                name='one_wallet_initial_credit',
            ),
            models.UniqueConstraint(
                fields=('wallet', 'idempotency_key'),
                condition=~Q(idempotency_key=''),
                name='unique_wallet_ledger_idempotency',
            ),
        ]
        ordering = ('created_at', 'id')

    @property
    def balance_delta_micro_usd(self):
        if self.entry_type in {
            self.EntryType.INITIAL_CREDIT,
            self.EntryType.RELEASE,
            self.EntryType.REFUND,
        }:
            return self.amount_micro_usd
        if self.entry_type == self.EntryType.RESERVATION:
            return -self.amount_micro_usd
        return 0

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Wallet ledger entries are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Wallet ledger entries are append-only.')

    def __str__(self):
        return f'{self.get_entry_type_display()}: {self.amount_micro_usd} micro-USD'


class UsageRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RESERVED = 'reserved', 'Reserved'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED_BEFORE_UPSTREAM = 'failed_before_upstream', 'Failed before upstream'
        RECONCILIATION_REQUIRED = 'reconciliation_required', 'Reconciliation required'

    class ReconciliationReason(models.TextChoices):
        USAGE_UNKNOWN = 'usage_unknown', 'Usage unknown'
        RESERVATION_EXCEEDED = 'reservation_exceeded', 'Reservation exceeded'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='usage_requests',
    )
    conversation = models.ForeignKey(
        'chat.Conversation',
        on_delete=models.CASCADE,
        related_name='usage_requests',
    )
    assistant_message = models.OneToOneField(
        'chat.Message',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='usage_request',
    )
    provider = models.CharField(max_length=16, choices=ProviderInterface.choices)
    idempotency_key = models.CharField(max_length=128)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_micro_usd = models.PositiveBigIntegerField(default=0)
    charge_micro_usd = models.PositiveBigIntegerField(default=0)
    pricing_version = models.CharField(max_length=64, default=PRICING_VERSION)
    upstream_request_id = models.CharField(max_length=128, blank=True)
    reconciliation_reason = models.CharField(
        max_length=32,
        choices=ReconciliationReason.choices,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(idempotency_key=''),
                name='usage_idempotency_key_nonempty',
            ),
            models.CheckConstraint(
                condition=Q(provider__in=ProviderInterface.values),
                name='usage_provider_is_supported',
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        'pending',
                        'reserved',
                        'succeeded',
                        'failed_before_upstream',
                        'reconciliation_required',
                    )
                ),
                name='usage_status_supported',
            ),
            models.CheckConstraint(
                condition=(
                    Q(reconciliation_reason__isnull=True)
                    | Q(reconciliation_reason__in=('usage_unknown', 'reservation_exceeded'))
                ),
                name='usage_reconciliation_reason_supported',
            ),
            models.UniqueConstraint(
                fields=('user', 'idempotency_key'),
                name='unique_usage_idempotency_per_user',
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        input_tokens__isnull=True,
                        output_tokens__isnull=True,
                        total_tokens__isnull=True,
                    )
                    | Q(
                        input_tokens__isnull=False,
                        output_tokens__isnull=False,
                        total_tokens=F('input_tokens') + F('output_tokens'),
                    )
                ),
                name='usage_token_counts_consistent',
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        reserved_input_tokens__isnull=True,
                        reserved_output_tokens__isnull=True,
                        reserved_total_tokens__isnull=True,
                    )
                    | Q(
                        reserved_input_tokens__isnull=False,
                        reserved_output_tokens__isnull=False,
                        reserved_total_tokens=F('reserved_input_tokens')
                        + F('reserved_output_tokens'),
                    )
                ),
                name='reserved_token_caps_consistent',
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=('reserved', 'succeeded', 'reconciliation_required'))
                    | Q(
                        reserved_input_tokens__isnull=False,
                        reserved_output_tokens__isnull=False,
                        reserved_total_tokens__isnull=False,
                    )
                ),
                name='reserved_usage_has_token_caps',
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status='succeeded')
                    | Q(
                        input_tokens__isnull=False,
                        output_tokens__isnull=False,
                        total_tokens__isnull=False,
                    )
                ),
                name='successful_usage_has_token_counts',
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status='reconciliation_required',
                        reconciliation_reason__isnull=False,
                    )
                    | (
                        ~Q(status='reconciliation_required')
                        & Q(reconciliation_reason__isnull=True)
                    )
                ),
                name='usage_reconciliation_reason_matches_status',
            ),
        ]
        ordering = ('-created_at', '-id')

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.user_id and self.conversation_id:
            if not self.conversation.is_owned_by(self.user):
                errors['conversation'] = 'The conversation must belong to the usage owner.'
        if self.assistant_message_id:
            if self.assistant_message.conversation_id != self.conversation_id:
                errors['assistant_message'] = 'The assistant message must belong to this conversation.'
            if self.assistant_message.role != 'assistant':
                errors['assistant_message'] = 'Usage can only be linked to an assistant message.'
            if self.assistant_message.provider != self.provider:
                errors['assistant_message'] = 'The assistant message provider must match the request.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.provider} usage ({self.status}) for {self.user}'
