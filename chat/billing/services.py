from .models import INITIAL_BALANCE_MICRO_USD, Wallet, WalletLedgerEntry


def provision_initial_wallet(user, *, using=None):
    wallet = Wallet.objects.db_manager(using).create(
        user=user,
        balance_micro_usd=INITIAL_BALANCE_MICRO_USD,
    )
    WalletLedgerEntry.objects.db_manager(using).create(
        wallet=wallet,
        entry_type=WalletLedgerEntry.EntryType.INITIAL_CREDIT,
        amount_micro_usd=INITIAL_BALANCE_MICRO_USD,
    )
