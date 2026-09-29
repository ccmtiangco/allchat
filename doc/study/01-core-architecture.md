# AllChat Core Architecture Study

## Purpose

Assess the feasibility, tradeoffs, and proposed structure for AllChat: a Django application that gives authenticated users a wallet-funded chat interface and routes each submitted assistant turn through one of three provider-compatible interfaces on `https://proxy.litechat.ai`.

This is the study phase only. It records architectural direction and open decisions; it does not define implementation tasks or include application code.

## Current State

- The repository currently contains no Django application, frontend, or existing product documentation. The root `agents.md` defines a workflow in which this study precedes a separate plan and implementation phase.
- `.gitignore` already excludes `.env`, SQLite database files, and Python cache files. Environment-specific credentials should remain outside version control.
- The proxy documentation describes non-streaming and streaming APIs for the three requested interfaces and reports token usage in normal responses.

## Feasibility

The requested first version is feasible with Django, its built-in authentication and session framework, Django templates, and ordinary HTML forms styled with CSS. Django can render the authenticated chat screen, persist conversations and messages, validate the provider choice, call the proxy from the server, and render the resulting response. No client-side framework is needed for a synchronous, non-streaming MVP.

The key feasibility condition is billing policy. The proxy returns token counts, but the available documentation does not establish the prices AllChat should charge users, whether proxy usage is billed at a known rate, or how to price a failed or interrupted request whose usage is unknown. The wallet UI and accounting model can be designed now, but reliable debiting requires a defined price schedule and settlement rules before execution.

There is also a product naming caveat: the proxy's current getting-started documentation says it uses DeepSeek Flash for all three provider interfaces and that the interfaces do not reproduce the named providers' model behavior. The user can select OpenAI-, Anthropic-, or Google-compatible routing, but the current service documentation does not support promising three distinct underlying model vendors. Confirm that this matches the intended product semantics before describing the choices as actual provider/model selection.

## Proxy Interface Findings

The proxy expects requests from a backend and separate credentials for each interface. Its documentation currently describes:

| User selection | Route | Authentication | Documented model example |
| --- | --- | --- | --- |
| OpenAI | `POST /openai/v1/chat/completions` | Bearer key | `gpt-5.6-luna` |
| Anthropic | `POST /anthropic/v1/messages` | `x-api-key` plus `anthropic-version` | `claude-haiku-4-5-20251001` |
| Google Gemini | `POST /google/v1beta/models/gemini-3.8-flash:generateContent` | `x-goog-api-key` | Model is part of the URL |

The response formats differ. OpenAI reports usage under `usage.prompt_tokens` and `usage.completion_tokens`; Anthropic reports `usage.input_tokens` and `usage.output_tokens`; Gemini reports `usageMetadata.promptTokenCount` and `usageMetadata.candidatesTokenCount`. A backend routing boundary should normalize these response shapes into a common result containing answer text, input/output token counts, completion status, and provider/interface metadata. Provider-specific request and response handling should stay behind this boundary rather than leak into views or templates.

The proxy documentation says normal whole-response requests return token usage, while errors or interrupted streams can leave usage unknown. It advises against automatic retries after partial output. For the initial version, non-streaming requests are therefore simpler to account for and recover than streaming requests.

Proxy documentation consulted (revision dated 2026-09-19):

- [Getting started](https://proxy.litechat.ai/docs)
- [OpenAI Chat Completions](https://proxy.litechat.ai/docs/openai/chat-completions)
- [Anthropic Messages](https://proxy.litechat.ai/docs/anthropic/messages)
- [Google Gemini Generate Content](https://proxy.litechat.ai/docs/google/gemini)

Model identifiers and service behavior are external configuration, not stable assumptions; validate them against the proxy at implementation time.

## Proposed Application Structure

Keep the first version as a conventional, server-rendered Django application with a small number of clear responsibilities:

- **Accounts**: Use Django's built-in `User`, login, logout, password handling, sessions, and authentication checks. Add a profile or wallet record associated one-to-one with each user for account-specific balance state.
- **Conversations**: Store conversations with an owning user, title, and timestamps. Store ordered messages under each conversation. Every conversation read or write must be scoped to the authenticated owner.
- **Wallet and usage**: Keep an auditable append-only ledger of credits, reservations, settlements, and refunds. A displayed balance should be derived from, or transactionally consistent with, these entries. Store a durable usage record for each completed or uncertain proxy request, including the selected interface, token counts when available, charged amount, and the pricing version used.
- **Proxy routing**: Provide a server-side integration boundary responsible for selecting the correct endpoint, authentication header, model configuration, request shape, timeout, and response normalization. Accept only a fixed allow-list of provider choices from the browser.
- **Views and templates**: Use authenticated Django views and templates for login, the chat screen, conversation history, and message submission. Keep provider selection and balance display in the HTML form/page; templates display persisted server state rather than being the source of truth for billing.
- **Configuration**: Read the three provider credentials and proxy base URL from the server environment. Never return credentials to the browser, render them into templates, or commit them in documentation or source control. The supplied credential values are intentionally not reproduced in this study.

The core relationship is: one Django user owns one wallet/profile and many conversations; each conversation owns ordered messages; each billable assistant response is associated with a selected provider interface and a usage/ledger record.

## Request and Accounting Flow

1. The user signs in through Django authentication and opens their conversation list or an existing conversation.
2. The chat form submits the message and one allow-listed interface choice. The server verifies authentication, CSRF protection, input limits, and ownership of any referenced conversation.
3. The server loads the conversation context, checks the user's available balance against the product's request policy, and records a durable pending/reserved state before contacting the proxy.
4. A server-side router calls the selected endpoint with the matching secret and protocol-specific payload. The request uses a bounded timeout and does not blindly retry an ambiguous failure.
5. The router extracts response text, completion status, and usage, then normalizes the provider-specific response. The app stores the user message, assistant result, and usage/charge outcome consistently.
6. The page renders the new conversation state and updated balance. An ordinary POST/redirect/GET flow is appropriate for this non-streaming interface.

The wallet check and remote call cannot be one atomic database operation. A database transaction should not remain open during a slow network call. The accounting design therefore needs an explicit reservation/settlement lifecycle: reserve sufficient funds before the request, settle against reported usage on success, and release or flag the reservation according to the failure policy. Use database-level locking or another concurrency-safe mechanism so simultaneous requests cannot spend the same balance. Idempotency or duplicate-submission handling is needed to avoid charging twice when a browser resubmits or a client loses the response.

If a proxy failure leaves usage unknown, the system must not silently assume zero cost or retry and potentially cause a second upstream request. The product must choose whether to hold the reservation for review, charge a conservative amount, or refund while accepting the risk of unmetered upstream usage. This state should be visible to operators and recorded for reconciliation.

## Wallet and Pricing Tradeoffs

- **Pricing basis is unresolved**: token counts alone do not define a charge. Decide whether AllChat uses an internal rate card, passes through proxy/provider cost, or applies a markup. Confirm who supplies authoritative rates and how rate changes are communicated.
- **Input and output rates may differ**: store both token counts and a versioned rate schedule. Conversation history is usually sent again with each turn, so input tokens can grow with conversation length and must be included in the estimate and final charge.
- **Use exact accounting values**: avoid binary floating-point balances. Choose a currency and represent wallet/ledger amounts in integer minor units or fixed-precision decimal values, with an explicit rounding rule.
- **Choose a sufficient reservation**: token usage is only final after the response. Define a maximum output limit and reserve strategy to prevent overspending while avoiding excessively large temporary holds.
- **Separate ledger history from current balance**: an immutable ledger provides an audit trail and supports support queries, refunds, and reconciliation. A cached balance can be added for efficiency later, but must be updated atomically with ledger entries.
- **Seed-credit policy is missing**: decide whether new users receive a free balance, must be manually funded, or use another funding flow. No payment provider or top-up capability is included in the stated requirements.

## Frontend Tradeoffs

Raw HTML and CSS are sufficient for login forms, a provider dropdown, message submission, conversation links, and server-rendered balance/history. This is the smallest and most robust first version and keeps authorization and balance calculations on the server.

Without JavaScript, the user submits a form and waits for the synchronous request to finish; the page then reloads with the new result. This is simpler than streaming but can feel slow and depends on application/proxy timeouts being configured coherently. Streaming tokens, optimistic updates, an in-place provider/model interaction, and live balance changes would require client-side JavaScript and more complex accounting of partial results. Defer streaming until the MVP billing behavior is understood.

## Security, Reliability, and Data Isolation

- Keep all three keys server-side in environment configuration. `.env` is ignored by Git today, but production should use the deployment platform's secret manager. Rotate any key that may have been exposed outside its intended secret store.
- Require login for chat and wallet views, use Django CSRF protection for all state-changing forms, and enforce per-user ownership checks on every conversation and message operation.
- Treat submitted provider values, message content, and conversation identifiers as untrusted input. Limit message size and request duration; escape rendered content using Django's normal template auto-escaping.
- Do not log API keys or unnecessarily retain full prompts in operational logs. Define conversation retention and deletion behavior because chat histories can contain sensitive user data.
- Distinguish validation/authentication errors, rate limits, upstream failures, timeouts, and incomplete outputs. Present safe user-facing messages while retaining enough non-secret metadata for diagnosis.
- For local development, SQLite is adequate for basic Django workflows. Wallet concurrency and row-level locking are more dependable with PostgreSQL; use PostgreSQL for production or any environment where concurrent spending must be guaranteed.

## Main Tradeoffs

| Decision | Minimal first version | Cost or limitation |
| --- | --- | --- |
| UI interaction | Django templates and full-page form submission | No token streaming or rich in-place updates |
| Proxy calls | Three explicit provider-interface adapters behind one router | Requires maintaining protocol-specific payload and response handling |
| Accounting | Ledger plus pending reservation and settlement | More concepts than a single mutable balance, but needed for auditability and concurrency |
| Provider choice | Persist the selected interface with each assistant turn | Current proxy docs do not promise distinct underlying provider models |
| Persistence | Django ORM with SQLite locally and PostgreSQL in production | Production database setup is an additional deployment requirement |
| Usage failures | Record an explicit unknown/reconciliation state | Requires a product/operations policy; automatic retry is unsafe in ambiguous cases |

## Decisions Needed Before Planning

1. Does provider selection mean selecting an API-compatible interface, or must it guarantee a distinct underlying OpenAI, Anthropic, or Google model? The proxy's current documentation says all three interfaces use DeepSeek Flash.
2. What exact prices are charged for input and output tokens for each selection, and who owns/updates those rates?
3. What currency, initial wallet balance, top-up mechanism, and insufficient-balance behavior are in scope?
4. What is the policy when a request times out or fails after the proxy may have incurred usage but returns no usage counts?
5. Which exact model identifiers, output limits, and proxy options should be enabled for each choice?
6. Are message streaming, conversation deletion, account registration, and password reset required for the initial release, or only a basic authenticated MVP?

## Conclusion

Django is a good fit for the requested authenticated, server-rendered MVP. The application can keep provider credentials and routing server-side, provide per-user chat isolation, and present wallet balances without a frontend framework. The main architectural risks are not the HTML or proxy calls; they are defining accurate prices and safe wallet settlement under concurrent or ambiguous upstream outcomes, plus ensuring that the proxy's provider-interface semantics match the product's claims. Resolve those policy questions in the planning phase before implementing charges or labeling the provider choices as distinct vendor models.
