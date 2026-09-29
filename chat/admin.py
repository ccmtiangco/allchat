from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.contrib.auth.admin import UserAdmin
from django.db import transaction

from .billing.models import UsageRequest, Wallet, WalletLedgerEntry
from .billing.services import (
    BillingError,
    admin_adjust_wallet,
    release_unknown_usage,
)
from .conversations.models import Conversation, Message
from .models import User


class WalletAdjustmentActionForm(ActionForm):
    amount_micro_usd = forms.IntegerField(
        required=False,
        label='Signed adjustment (micro-USD)',
        help_text='Positive values credit the wallet; negative values debit it.',
    )
    reason = forms.CharField(required=False, max_length=500)


class UsageResolutionActionForm(ActionForm):
    reason = forms.CharField(required=False, max_length=500)


admin.site.register(User, UserAdmin)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'formatted_balance', 'balance_micro_usd')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('user', 'balance_micro_usd')
    action_form = WalletAdjustmentActionForm
    actions = ('adjust_wallet_balances',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Apply a signed, reasoned wallet adjustment')
    def adjust_wallet_balances(self, request, queryset):
        try:
            amount = int(request.POST.get('amount_micro_usd', ''))
        except (TypeError, ValueError):
            self.message_user(
                request,
                'Enter a signed adjustment amount in micro-dollars.',
                level=messages.ERROR,
            )
            return

        reason = request.POST.get('reason', '').strip()
        if not reason:
            self.message_user(request, 'An adjustment reason is required.', level=messages.ERROR)
            return

        try:
            with transaction.atomic():
                for wallet in queryset:
                    admin_adjust_wallet(
                        wallet,
                        amount,
                        reason=reason,
                        actor=request.user,
                    )
        except (BillingError, PermissionError, ValueError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return

        self.message_user(
            request,
            f'Adjusted {queryset.count()} wallet(s); ledger entries include the supplied reason.',
            level=messages.SUCCESS,
        )


@admin.register(WalletLedgerEntry)
class WalletLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'wallet',
        'entry_type',
        'amount_micro_usd',
        'balance_delta_micro_usd',
        'related_request',
    )
    list_filter = ('entry_type', 'created_at')
    search_fields = ('wallet__user__username', 'idempotency_key')
    readonly_fields = tuple(field.name for field in WalletLedgerEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UsageRequest)
class UsageRequestAdmin(admin.ModelAdmin):
    list_display = (
        'created_at',
        'user',
        'provider',
        'status',
        'input_tokens',
        'output_tokens',
        'charge_micro_usd',
        'reconciliation_reason',
    )
    list_filter = ('status', 'provider', 'reconciliation_reason', 'created_at')
    search_fields = ('user__username', 'idempotency_key', 'upstream_request_id')
    readonly_fields = tuple(field.name for field in UsageRequest._meta.fields)
    action_form = UsageResolutionActionForm
    actions = ('release_unknown_reservations',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Release selected usage-unknown reservations (requires reason)')
    def release_unknown_reservations(self, request, queryset):
        reason = request.POST.get('reason', '').strip()
        if not reason:
            self.message_user(request, 'A reconciliation reason is required.', level=messages.ERROR)
            return

        released = 0
        skipped = 0
        try:
            with transaction.atomic():
                for usage_request in queryset:
                    if (
                        usage_request.status == UsageRequest.Status.RECONCILIATION_REQUIRED
                        and usage_request.reconciliation_reason
                        == UsageRequest.ReconciliationReason.USAGE_UNKNOWN
                    ):
                        release_unknown_usage(
                            usage_request,
                            reason=reason,
                            actor=request.user,
                        )
                        released += 1
                    else:
                        skipped += 1
        except (BillingError, PermissionError, ValueError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return

        self.message_user(
            request,
            f'Released {released} reservation(s); skipped {skipped} request(s).',
            level=messages.SUCCESS,
        )


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('title', 'owner', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('title', 'owner__username')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'role', 'provider', 'created_at')
    list_filter = ('role', 'provider', 'created_at')
    search_fields = ('content', 'conversation__title', 'conversation__owner__username')
    readonly_fields = tuple(field.name for field in Message._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
