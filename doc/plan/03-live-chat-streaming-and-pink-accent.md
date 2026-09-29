# Live Chat Streaming and Pink Accent Plan

Implement the requested in-place, token-streaming chat response, live elapsed
timer, and pink interface accents. Preserve the current non-streaming form as a
progressive fallback and keep billing authoritative on the server.

## Confirmed Decisions

- [x] Persist received partial text as explicitly incomplete when a stream is
  interrupted, mark its usage request `reconciliation_required`, and keep the
  reservation held.
- [x] Show a live stopwatch and final duration, and persist server-measured
  elapsed milliseconds as nullable `UsageRequest.latency_ms`.
- [x] Apply pink to interaction accents and user bubbles while retaining
  neutral surfaces and semantic warning/error colors.

## Server-Side Streaming and Billing

- [ ] Create a feature branch from `main` following `agents.md`.
- [ ] Add provider streaming adapters for OpenAI-compatible SSE, Anthropic
  Messages events, and Google `streamGenerateContent?alt=sse`. Normalize text
  deltas, terminal status, usage, request IDs, and provider-specific errors.
- [ ] Parse streams incrementally across arbitrary byte/chunk boundaries,
  enforce response limits and timeouts, and close upstream connections when the
  downstream client disconnects where possible.
- [ ] Add an authenticated, CSRF-protected POST streaming endpoint. Use
  `StreamingHttpResponse` with no-cache/no-buffer headers and in-band
  completion/error events. Do not expose provider credentials to the browser.
- [ ] Refactor orchestration to preserve this order: validate and create the
  idempotent request; reserve funds transactionally; call upstream outside a
  DB transaction; stream text; persist partial output as incomplete on
  interruption; settle only after final valid usage. Record `latency_ms` through
  completion or interruption.
- [ ] Add a nullable `latency_ms` field to `UsageRequest` and create/apply its
  migration. Keep it read-only in admin and render it with completed or
  reconciliation-required turns as appropriate.
- [ ] On a pre-upstream failure, release the reservation. On a post-acceptance
  stream failure, missing usage, or disconnect, do not retry or guess cost;
  persist the approved partial-output behavior and retain the reservation for
  reconciliation.
- [ ] Make repeat submissions with the same idempotency key return the stored
  result without starting another provider request.

## Browser Chat and Timer

- [ ] Add a small chat client script that intercepts the chat form, submits the
  existing CSRF/idempotency fields with same-origin `fetch`, and reads the
  streamed response without reloading the page.
- [ ] Append the submitted user message in place, render incoming assistant
  text safely as text while streaming, then present the final response using
  the existing safe Markdown rendering behavior.
- [ ] Display a live elapsed timer from submission until completion/error, then
  show the server-recorded final duration. Announce response state changes
  accessibly without announcing every clock tick.
- [ ] Keep the ordinary form POST as a no-JavaScript fallback. Prevent duplicate
  submits while active and never automatically retry after an ambiguous result.
- [ ] Add a restrained typing/streaming status indicator with reduced-motion
  support; indicate activity with text as well as animation.

## Pink Accent Refresh

- [ ] Define CSS custom properties for pink accent, hover, selection, and pale
  surface shades; replace blue/cyan accents across chat navigation, user
  bubbles, route controls, links, focus indicators, and send action.
- [ ] Retain neutral base surfaces and semantic warning/error colors. Verify
  text contrast, keyboard focus visibility, and that message roles remain
  distinguishable without color alone.

## Tests and Release Checks

- [ ] Add mocked streaming parser/adapter tests for each provider's text events,
  usage events, terminal markers, malformed events, upstream errors, missing
  usage, and interrupted streams.
- [ ] Add orchestration tests proving reservation occurs before upstream work,
  settlement waits for valid final usage, unused funds release correctly,
  unknown usage retains the reservation, and duplicate keys do not make a
  second upstream call.
- [ ] Add view tests for authentication, CSRF, POST-only behavior, stream event
  types/headers, and safe in-band errors after streaming begins.
- [ ] Test the client behavior for no reload, in-place deltas, live timer
  updates/final duration, errors, duplicate submission prevention, and the
  non-JavaScript fallback.
- [ ] Run Django checks, model migration checks, and the full suite:
  `python manage.py check`, `python manage.py makemigrations --check --dry-run`,
  and `python manage.py test`.
- [ ] Manually verify all three providers in a controlled environment, including
  final usage and timeout/disconnect reconciliation; never use real keys in
  browser requests or tracked files.
- [ ] Review desktop and mobile chat, keyboard and screen-reader status, pink
  contrast, reduced motion, and proxy buffering behavior.
- [ ] Merge the feature branch into `main` after checks pass and sync
  `doc/wiki/README.md` with streaming behavior and setup/configuration changes.
