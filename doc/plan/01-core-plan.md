# AllChat Core Implementation Plan

This checklist turns the architecture study in [`doc/study/01-core-architecture.md`](../study/01-core-architecture.md) into implementation tasks. It is a plan only; no application code is included here.

## Product Decisions for This MVP

- [ ] Starting wallet grant: credit each newly registered user with **$5.00** exactly once.
- [ ] Usage rate: charge **$0.002 per 1,000 combined input and output tokens**, uniformly for OpenAI-compatible, Anthropic-compatible, and Google-compatible requests.
- [ ] Exact arithmetic: represent USD in integer micro-dollars; the rate is **2 micro-dollars per token**, and the initial credit is **5,000,000 micro-dollars**. Never use floating-point arithmetic for balances or charges.
- [ ] Balance display: show enough precision to make small debits visible (six decimal places, for example `$4.998000`) and show the individual charge with the assistant response.
- [ ] Chat interaction: use synchronous, non-streaming Django form submissions and server-rendered HTML/CSS. Do not add JavaScript or streaming for the MVP.
- [ ] Provider selection: expose the three documented proxy interfaces. Before finalizing user-facing copy, verify and accurately explain that the proxy's current documentation says all three interfaces use DeepSeek Flash rather than distinct underlying vendor models.
- [ ] Keep the provided credential values out of this plan, source control, templates, logs, and browser requests. Use only environment variable names and local/deployment secret configuration.

## 1. Project Bootstrap and Configuration

- [ ] Create a Django project with a clear project package (settings, URL configuration, WSGI/ASGI entry points) and separate applications/modules for accounts, wallet/billing, conversations, and proxy integration.
- [ ] Add and pin the Django version and synchronous HTTP client dependency in the project dependency manifest. Use a server-side HTTP client with explicit connect/read timeouts; do not call the proxy from the browser.
- [ ] Add environment-based settings for Django secret key, debug/allowed hosts, database, proxy base URL, provider keys, provider model identifiers, and request timeouts.
- [ ] Configure the local environment loader (or documented shell environment) without committing `.env`; add a `.env.example` containing variable names and safe placeholders only.
- [ ] Confirm `.env`, database files, bytecode, and local virtual environments remain ignored by Git. Do not copy the supplied key values into `.env.example` or any tracked file.
- [ ] Configure templates/static asset paths and Django's static-file handling for CSS.
- [ ] Choose SQLite for initial local development and PostgreSQL-compatible settings for production. Ensure the production database supports the locking/transaction behavior required by wallet operations.
- [ ] Record the supported provider-interface labels and model IDs as server configuration, not as values trusted from the browser. Verify the documented IDs and routes against the proxy before enabling them.
- [ ] Add a basic project health/startup check that fails clearly when required settings are missing, without printing secret values.

## 2. Custom User, Signup, and Authentication

- [ ] Define a custom Django user model based on `AbstractUser` before creating the first database migration; configure `AUTH_USER_MODEL` before any migrations are run.
- [ ] Keep Django's standard password hashing, session authentication, login/logout, and password validation behavior.
- [ ] Implement a server-rendered signup form with appropriate validation and safe error messages. Registration is needed so new users can receive their automatic wallet credit.
- [ ] Create the user, wallet, and initial-credit ledger entry atomically so signup cannot produce a user without a wallet or grant multiple initial credits after a retry.
- [ ] Ensure the signup path is the single controlled path for wallet initialization; add a database uniqueness constraint or equivalent invariant to prevent duplicate initial grants.
- [ ] Implement login and logout views using Django authentication and CSRF-protected forms. Require authentication for all chat, conversation, wallet, and usage-history pages.
- [ ] Ensure every supported new-user creation path (signup, Django admin, and user-manager/superuser creation where applicable) applies the same one-time `$5.00` wallet initialization rule.
- [ ] Add tests for signup, password hashing, login/logout, anonymous access redirects, and exactly-one `$5.00` opening credit per user.

## 3. Wallet and Usage-Billing Models

- [ ] Create a `Wallet` model associated one-to-one with the custom user. Store any cached available balance as an integer number of micro-dollars and keep it consistent with ledger operations.
- [ ] Create an append-only wallet ledger model with wallet, entry type, positive amount in micro-dollars, timestamp, optional related request, and safe audit metadata. Support at least initial credit, reservation, usage debit/settlement, and refund/release entries.
- [ ] Make the opening credit auditable as one `$5.00` credit entry. Do not set an unexplained initial balance that has no matching ledger record.
- [ ] Create a usage/request record that captures owner, conversation, chosen interface, idempotency key, request status, input tokens, output tokens, total tokens, charge in micro-dollars, pricing version, timestamps, and a non-secret upstream request identifier when available.
- [ ] Use explicit statuses for pending/reserved, succeeded/settled, failed-before-upstream, and usage-unknown/reconciliation-required outcomes. Keep token counts nullable when the proxy did not report them; never substitute zero for unknown usage.
- [ ] Make duplicate submissions idempotent with a unique request/idempotency identifier scoped to the user. A retry of the same browser submission must not create a second provider call or second charge.
- [ ] Implement the exact charge formula in one billing boundary: `(input_tokens + output_tokens) * 2` micro-dollars. Add a pricing-version identifier so later rate changes do not rewrite the meaning of historical charges.
- [ ] Define and test rounding/display rules. The selected rate is exactly representable in micro-dollars per token, so no per-request fractional rounding should be needed.
- [ ] Define the preflight/reservation policy before enabling paid proxy calls. Enforce prompt/context and output limits and ensure the maximum reserved exposure cannot exceed available funds; if this cannot be bounded safely with the chosen token-counting approach, record the limitation and block live billing until a product decision is made.
- [ ] On settlement, atomically append the debit/refund entries and update the cached balance. Reject any settlement that would violate wallet invariants.
- [ ] On a clear failure before an upstream request is sent, release the reservation. On an ambiguous timeout or missing usage after the request may have reached the proxy, preserve an explicit pending/unknown state and reservation for reconciliation; do not silently refund, guess zero, or automatically retry.
- [ ] Add transaction and locking behavior so concurrent turns from the same wallet cannot spend the same available balance. Do not hold a database transaction open during the network call.
- [ ] Add tests for the $5.00 grant, exact one-token/one-thousand-token charges, combined input/output usage, zero-usage behavior, insufficient balance, concurrent reservation/settlement, idempotency, refunds, and unknown usage.

## 4. Conversation and Message Models

- [ ] Create a `Conversation` model with owner, title, created timestamp, and updated timestamp; index owner and updated timestamp for the authenticated history view.
- [ ] Create an ordered `Message` model related to a conversation with role (user/assistant), text content, creation time, and selected provider interface on assistant responses. Keep provider choice associated with each assistant turn, not only the conversation, so it can vary message by message.
- [ ] Relate each assistant generation/usage record to its user, conversation, and resulting message where applicable. Store tokens and charge in the usage record rather than deriving them from untrusted template values.
- [ ] Define behavior for failed generations: preserve the submitted user message and a failed/unknown request record for diagnosis, but do not fabricate an assistant reply or a successful usage charge.
- [ ] Use deterministic ordering for message history and bound the conversation context included in a provider request. Define how older turns are truncated or excluded when limits are reached.
- [ ] Generate an initial conversation title from the first user message with a safe length limit, or use a neutral title if automatic naming is not desired.
- [ ] Ensure all lookups, message writes, edits, and deletes scope the conversation by both its identifier and authenticated owner. Return a not-found response for another user's conversation rather than disclosing its existence.
- [ ] Add tests for ownership isolation, ordering, provider selection stored per assistant turn, context construction, and conversation title behavior.

## 5. Proxy Router and Provider Adapters

- [ ] Define one server-side router interface that accepts an allow-listed interface, conversation messages, and generation limits, then returns a normalized result containing answer text, input/output tokens, completion status, model/interface metadata, and upstream request ID when available.
- [ ] Add shared request behavior: HTTPS base URL, JSON content type, bounded timeouts, response-size limits where practical, structured non-secret logging, and explicit handling of connection errors, timeouts, HTTP errors, invalid JSON, and incomplete responses.
- [ ] Implement the OpenAI-compatible adapter for `POST /openai/v1/chat/completions`, Bearer authentication using `OPENAI_PROXY_KEY`, the configured documented model identifier, chat-message payload, and response extraction from `choices[0].message.content` plus `usage.prompt_tokens` / `usage.completion_tokens`.
- [ ] Implement the Anthropic-compatible adapter for `POST /anthropic/v1/messages`, `x-api-key` authentication using `ANTHROPIC_PROXY_KEY`, the required `anthropic-version` header, configured model, separate system/message fields as needed, and response extraction from text content blocks plus `usage.input_tokens` / `usage.output_tokens`.
- [ ] Implement the Google-compatible adapter for `POST /google/v1beta/models/{model}:generateContent`, `x-goog-api-key` authentication using `GOOGLE_PROXY_KEY`, provider-specific contents/generation configuration, and response extraction from candidate text parts plus `usageMetadata.promptTokenCount` / `usageMetadata.candidatesTokenCount`.
- [ ] Normalize each adapter's finish/stop/status values. Treat truncation, safety filtering, missing answer text, tool/function calls, and malformed responses as explicit outcomes rather than assuming every HTTP 200 is a normal completed text answer.
- [ ] Reject unknown provider values and model/route combinations server-side. The browser must submit only a selection identifier; it must never choose a URL, header, key, or arbitrary model string.
- [ ] Validate all three configured credentials and routes using controlled non-production test calls, without logging or exposing keys. Confirm whether the currently documented model identifiers remain valid and verify the proxy's documented shared DeepSeek Flash behavior.
- [ ] Do not automatically retry requests on timeout, partial response, 429, or ambiguous upstream failure. Return a safe user message and preserve the request's accounting status for the billing policy.
- [ ] Add mocked adapter tests for URL, authentication header presence (not secret value), payload conversion, answer extraction, usage normalization, incomplete/error responses, timeouts, and missing usage.

## 6. Chat Request Orchestration

- [ ] Implement the synchronous message-submission service in this order: validate user/input/provider; validate owned conversation or create one; create an idempotent pending request; preflight balance and reserve; call the selected adapter outside the database transaction; normalize and validate the result; persist messages/usage; settle billing atomically.
- [ ] Ensure a single submitted user turn can select a different provider interface from the previous assistant turn in the same conversation.
- [ ] Build provider context from persisted user/assistant messages in order and include the current submitted message exactly once. Apply configured history/input limits before making the request.
- [ ] Store successful user and assistant messages, selected interface, token counts, charge, and pricing version in a consistent database state. Do not mark a request settled before usage and ledger changes commit.
- [ ] Handle validation errors, insufficient balance, proxy auth errors, rate limits, service errors, timeouts, malformed responses, and usage-unknown outcomes with clear user-facing messages and safe persisted status.
- [ ] Prevent accidental duplicate charges on browser refresh using POST/redirect/GET and the request idempotency key. Do not issue a provider call merely because a result page is refreshed.
- [ ] Add orchestration tests using a mocked proxy, including successful response, each adapter choice, insufficient balance, failed upstream call, unknown usage, duplicate submission, and concurrent sends.

## 7. Server-Rendered HTML and CSS

- [ ] Create a shared base template with navigation, authenticated-user indication, logout form, and current wallet balance.
- [ ] Create signup, login, conversation-list, conversation-detail/chat, and safe error/empty-state templates.
- [ ] On the chat screen, render ordered message history, a message text area, a required dropdown for OpenAI-compatible / Anthropic-compatible / Google-compatible routing, and a submit button. Clearly describe interface semantics consistent with proxy verification.
- [ ] Render the selected provider with each assistant response, plus input/output/combined token usage and the exact charge when usage is known.
- [ ] Show the current balance in six decimal places and update it from authoritative server-side wallet state after settlement. Show pending or reconciliation-required status without representing unknown usage as a settled debit.
- [ ] Keep forms synchronous and use POST/redirect/GET for successful submissions. Provide a waiting/submission state using native browser behavior only; do not add JavaScript, fetch, SSE, or WebSockets in this MVP.
- [ ] Include Django CSRF tokens in all state-changing forms and use server-side validation/error messages.
- [ ] Add responsive CSS for desktop and mobile, readable message roles, visible focus states, adequate contrast, and semantic form labels. Keep user content escaped by Django templates.
- [ ] Add template/view tests for provider dropdown choices, balance and charge formatting, message history, validation errors, CSRF behavior where enabled in tests, and mobile-relevant stylesheet assets being served.

## 8. Admin and Operational Visibility

- [ ] Register users, wallets, conversations, messages, ledger entries, and usage/request records in Django admin as appropriate.
- [ ] Make billing ledger and settled usage records read-only in admin; do not allow casual editing of historical charges or token counts.
- [ ] Provide a way for an operator to find pending/unknown requests and reconcile or release their reservation with an auditable adjustment entry.
- [ ] Ensure admin actions for wallet adjustment require explicit amount/reason and create ledger entries rather than editing the balance silently.
- [ ] Log request correlation IDs, selected interface, status, latency, and safe error categories; never log API keys or full message bodies by default.

## 9. End-to-End Verification and Release Gate

- [ ] Run Django system checks and migrations from a clean local database; verify the custom user model is configured before initial migrations.
- [ ] Run the full automated test suite, including model invariants, billing math, ownership/authentication, all three mocked adapters, and server-rendered chat flows.
- [ ] Manually verify the complete flow: register; confirm `$5.000000`; sign in; start a conversation; select each interface on separate turns; receive a response; confirm combined token charge and reduced balance; revisit history; log out; confirm isolation with a second user.
- [ ] Verify insufficient-balance behavior and that concurrent requests cannot overdraw or double-spend the same wallet.
- [ ] Verify timeout/unknown-usage behavior leaves an explicit pending/reconciliation state and does not automatically retry or silently claim a zero-cost success.
- [ ] Verify actual proxy calls only in a controlled environment with secrets injected outside source control; inspect logs, HTML, and browser network requests to confirm no credential is exposed.
- [ ] Verify responsive layout, keyboard navigation, accessible labels, auto-escaping, CSRF, and ownership checks.
- [ ] Document local setup, required environment variable names, model configuration, pricing formula, wallet policy, and known proxy provider-interface semantics without documenting secret values.
- [ ] Do not release live billing until the maximum request exposure/reservation behavior and unknown-usage policy are implemented and verified.

## Execution Workflow Gate

- [ ] Before executing this plan, create a separate Git branch from `main` as required by the root workflow. Keep implementation changes scoped to the plan and use conventional commit prefixes.
- [ ] After execution, follow the workflow's rendezvous step to merge back to `main`, verify a workable codebase, and then update living documentation under `doc/wiki/` to match the implemented behavior.
