from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.db.models import Q

INITIAL_BALANCE_MICRO_USD = 5_000_000


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

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name='ledger_entries',
    )
    entry_type = models.CharField(max_length=32, choices=EntryType.choices)
    amount_micro_usd = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_micro_usd__gt=0),
                name='wallet_ledger_amount_positive',
            ),
            models.UniqueConstraint(
                fields=('wallet',),
                condition=Q(entry_type='initial_credit'),
                name='one_wallet_initial_credit',
            ),
        ]

    def __str__(self):
        return f'{self.get_entry_type_display()}: {self.amount_micro_usd} micro-USD'


class User(AbstractUser):
    def save(self, *args, **kwargs):
        is_new = self._state.adding
        using = kwargs.get('using')

        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            if is_new:
                wallet = Wallet.objects.db_manager(using).create(
                    user=self,
                    balance_micro_usd=INITIAL_BALANCE_MICRO_USD,
                )
                WalletLedgerEntry.objects.db_manager(using).create(
                    wallet=wallet,
                    entry_type=WalletLedgerEntry.EntryType.INITIAL_CREDIT,
                    amount_micro_usd=INITIAL_BALANCE_MICRO_USD,
                )
