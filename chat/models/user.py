from django.contrib.auth.models import AbstractUser
from django.db import transaction


class User(AbstractUser):
    def save(self, *args, **kwargs):
        is_new = self._state.adding
        using = kwargs.get('using')

        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            if is_new:
                from ..billing.models import INITIAL_BALANCE_MICRO_USD, Wallet, WalletLedgerEntry

                wallet = Wallet.objects.db_manager(using).create(
                    user=self,
                    balance_micro_usd=INITIAL_BALANCE_MICRO_USD,
                )
                WalletLedgerEntry.objects.db_manager(using).create(
                    wallet=wallet,
                    entry_type=WalletLedgerEntry.EntryType.INITIAL_CREDIT,
                    amount_micro_usd=INITIAL_BALANCE_MICRO_USD,
                )
