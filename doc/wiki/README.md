# AllChat

AllChat is a server-rendered Django chat application. Authenticated users can
start conversations, select one of three provider-compatible proxy interfaces,
and use a wallet with auditable, token-metered charges.

## Architecture

The project is split by responsibility while remaining one synchronous Django
application:

| Area | Responsibility |
| --- | --- |
| `allchat_project/` | Django settings, root URL configuration, and WSGI/ASGI entry points |
| `chat/accounts/` | Signup and account forms; Django supplies session login/logout and password handling |
| `chat/models/` | Custom `AbstractUser` implementation; each newly created user receives an initialized wallet |
| `chat/conversations/` | Conversation/message persistence, owner-scoped access, bounded context construction, and turn orchestration |
| `chat/billing/` | Wallet, append-only ledger, usage records, reservation/settlement, refunds, and administrative adjustments |
| `chat/proxy/` | Server-side OpenAI-, Anthropic-, and Google-compatible HTTP adapters with normalized results |
| `chat/views.py`, `templates/`, `chat/static/` | Authenticated views and responsive chat UI with an app rail, session sidebar, transcript, and docked composer; no browser-side provider calls or JavaScript are used |

Each conversation belongs to one user and contains ordered user and assistant
messages. Provider selection is recorded per assistant turn. A usage request
links the user, conversation, submitted message, resulting assistant message
when available, and billing state. Ownership checks are applied by the server
when conversations are read or changed.

The browser submits an ordinary CSRF-protected form. The server validates the
allow-listed route, records a durable idempotent request, reserves wallet funds,
calls the proxy outside the database transaction, then persists the response
and settlement. Successful submissions use post/redirect/get to avoid a second
provider request on refresh. Proxy credentials and model identifiers are read
from server configuration; they are never selected by arbitrary browser input.
The assistant transcript supports common Markdown formatting and fenced code;
rendered HTML is sanitized through an allow-list. User messages remain escaped
plain text.

The three choices represent API-compatible routes, not a guarantee of three
different underlying vendor models. The proxy documentation recorded during
implementation says all three interfaces use DeepSeek Flash. Reconfirm that
behavior and the configured model identifiers against the proxy before a live
release.

## Metered Billing

All monetary amounts are integer micro-dollars: `1 USD = 1,000,000 micro-USD`.
The wallet is initialized with exactly `5,000,000` micro-USD ($5.00), alongside
an initial-credit ledger entry. Floating-point arithmetic is not used for
balances or charges.

The configured price is two micro-dollars per reported token, combining input
and output usage:

```text
charge_micro_usd = (input_tokens + output_tokens) * 2
```

That is $0.002 per 1,000 combined tokens. For example, 1,000 total tokens cost
2,000 micro-USD ($0.002). Wallet balances and charges are formatted to six
decimal places so small debits remain visible. Each usage record retains a
pricing-version identifier.

Before contacting the proxy, the application reserves funds for its estimated
input-token budget and configured maximum output-token count. The default
output cap is `MAX_CHAT_OUTPUT_TOKENS=2048`. Reservation debits the cached
available wallet balance and appends a ledger entry in one transaction. On a
successful response, reported token counts determine the final charge; the
application appends the usage debit and releases the unused reservation. If
reported usage exceeds the reservation, the request is marked for
reconciliation rather than silently overdrawing the wallet.

Failure handling is intentionally explicit:

- A failure known to occur before the upstream request is sent releases the
  reservation and marks the request `failed_before_upstream`.
- A timeout, transport failure, malformed response, or missing usage after the
  request may have reached the proxy is not automatically retried. The request
  enters `reconciliation_required`, and its reservation remains held until an
  operator resolves it.
- Staff can release an unknown-usage reservation with a required reason. The
  release is recorded as a ledger entry with operator/reason metadata.
- Staff wallet adjustments require a signed amount and reason and create
  ledger entries; operators do not silently edit a wallet balance.

Ledger entries and usage history are read-only in Django admin. The wallet
balance is a transactional cache maintained together with ledger operations.
For concurrent spending, use PostgreSQL in production: the billing service uses
database row locks, while SQLite is intended for local development and tests.

## Local Setup

Use Python 3.10 or newer. SQLite is the default local database.

1. Clone the GitHub repository and enter its directory:

   ```sh
   git clone <repository-url>
   cd litechat
   ```

   Replace `<repository-url>` with the repository's GitHub URL. If Git checks
   out the project into a differently named directory, use that directory in
   the `cd` command.

2. Create and activate a virtual environment, then install dependencies:

   ```sh
   python -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

   On Windows PowerShell, activate the environment with
   `.\.venv\Scripts\Activate.ps1` instead of the `source` command.

3. Create a local environment file and configure it:

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and set `OPENAI_PROXY_KEY`, `ANTHROPIC_PROXY_KEY`, and
   `GOOGLE_PROXY_KEY` to the credentials supplied for your environment. Also
   replace `DJANGO_SECRET_KEY` with a private random value. Keep these values
   only in the local `.env` or a deployment secret manager. **Never commit
   `.env`, proxy keys, or other secret values.** `.gitignore` excludes `.env`
   and environment-specific `.env.*` files while allowing the safe
   `.env.example`. It also excludes local session transcripts and conversation
   exports, SQLite databases, virtual environments, and Python cache files.
   Ignore rules help prevent accidental commits; they cannot remove files or
   credentials already present in Git history. Revoke any credential that was
   ever committed or shared.

   `.env.example` contains variable names and safe development placeholders.
   `PROXY_BASE_URL`, provider model IDs, connect/read timeouts, and the maximum
   output token count can also be configured there. Do not put secret values in
   `.env.example` or any tracked file. A clone has no usable proxy credentials:
   the three key fields in `.env.example` are blank. You must supply your own
   credentials for live provider calls; the automated tests use mocked calls.

4. Apply migrations and start the development server:

   ```sh
   python manage.py migrate
   python manage.py runserver
   ```

   Visit `http://127.0.0.1:8000/` and create an account. Signup provisions the
   initial wallet automatically. To administer wallets and reconcile requests,
   create a staff account with `python manage.py createsuperuser` and visit
   `/admin/`.

Useful verification commands:

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

The automated suite uses mocked proxy responses and does not require provider
credentials. Live proxy verification must be done separately in a controlled
environment with secrets injected outside source control.

## Configuration and Deployment Notes

- `DJANGO_DEBUG` defaults to `True` for local development. For deployment, set
  it to `False`, set a private `DJANGO_SECRET_KEY`, configure
  `DJANGO_ALLOWED_HOSTS`, and provide all three proxy keys. Startup rejects a
  missing production secret or provider key.
- Set `DB_ENGINE=postgresql` and provide `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
  `DB_HOST`, and `DB_PORT` to use PostgreSQL. Keep database credentials in the
  deployment secret store.
- The proxy base URL must use HTTPS. The router has bounded connect/read
  timeouts, does not follow redirects, limits response size, and avoids
  automatic retries for ambiguous outcomes.
- The route adapters call `/openai/v1/chat/completions`,
  `/anthropic/v1/messages`, and
  `/google/v1beta/models/{model}:generateContent`, with each protocol's
  authentication, payload, and usage response format.
- Chat is synchronous and non-streaming. No API key is sent to or exposed in
  the browser. Configure production static-file serving for `chat/static/`.
- Assistant Markdown uses a server-side renderer and HTML sanitizer; raw HTML
  and unsafe link protocols are not passed through to the browser.
