# Live Chat Streaming and Pink Accent Study

## Purpose

Assess the change from AllChat's current full-page, synchronous chat submission
to an in-place streaming chat experience with a live elapsed-time display, and
recolor the interface's blue accent tones to pink. Preserve the current
provider routing, CSRF, idempotency, ownership, reservation, and settlement
guarantees.

## Current Behavior

- The composer posts to `chat:send_message`. Django validates the form, calls
  `submit_message`, waits for the entire proxy response, and redirects to the
  conversation page.
- `submit_message` creates a durable user message/request, reserves the maximum
  exposure in a short transaction, makes the upstream call outside the
  transaction, then creates the assistant message and settles only after final
  token usage is available.
- `route_request` uses synchronous `requests.post` and parses one whole JSON
  response. The browser does not receive text while the proxy is generating.
- AllChat already holds a reservation before upstream work. A timeout,
  malformed stream, or missing final usage must remain an unknown-usage state;
  it cannot be represented as a successful zero-cost response or retried
  automatically.
- The current chat UI uses blue/cyan accents for navigation, the user bubble,
  route controls, and the send action. Warning and error colors have separate
  semantic uses.

## Proxy Streaming Evidence

The proxy documentation was fetched on 2026-09-29; its displayed revision is
2026-09-19. It documents streaming for all three configured interfaces:

| Interface | Streaming request | Text and completion signals | Usage considerations |
| --- | --- | --- | --- |
| OpenAI-compatible | `POST /openai/v1/chat/completions` with `stream: true` | SSE `choices[].delta.content`; finish reason and `[DONE]` | Request `stream_options.include_usage`; usage is sent in a final chunk when available |
| Anthropic-compatible | `POST /anthropic/v1/messages` with `stream: true` | SSE content-block events; read `content_block_delta` text and wait for `message_stop` | Usage is carried by message events; an ended text block alone is not turn completion |
| Google-compatible | `POST /google/v1beta/models/{model}:streamGenerateContent?alt=sse` | SSE candidate parts and finish information | Usage events may omit candidates; read through stream termination, with no `[DONE]` sentinel |

References:

- [OpenAI-compatible Chat Completions streaming](https://proxy.litechat.ai/docs/openai/chat-completions)
- [Anthropic-compatible Messages streaming](https://proxy.litechat.ai/docs/anthropic/messages)
- [Google Gemini streaming](https://proxy.litechat.ai/docs/google/gemini)

The APIs use different framing and final-usage behavior, so the existing
whole-JSON adapters cannot be reused unchanged. Do not expose any provider key
or make a provider request directly from the browser.

## Feasibility and Recommended Shape

True token streaming is feasible because each configured proxy interface
documents a streaming route. Use a same-origin, CSRF-protected POST handled by
Django and a browser `fetch` response reader. A POST is necessary to carry the
message and idempotency fields; the browser's `EventSource` API is GET-only and
would be a poor fit for message submission. Django can emit server-sent-event
frames through a `StreamingHttpResponse`, with normalized events such as
`started`, `delta`, `completed`, and `error`.

The browser should append the user bubble in place, consume deltas into an
assistant response container, and leave the conversation page loaded. Keep the
existing ordinary POST/redirect path as a no-JavaScript fallback. Use
`textContent` or an equivalently safe text update for streamed text; do not
insert upstream chunks as HTML. The final completed response can then use the
existing allow-listed Markdown rendering path on the next render.

The elapsed timer belongs in the browser: start with `performance.now()` on
submission, update a visible `MM:SS.t` display while waiting/streaming, and stop
on completion or error. Show a concise final duration for the response. Avoid
announcing every timer tick to screen readers; use a separate polite status for
state changes such as "Generating response" and "Response complete". The user
confirmed that duration should persist, so add a nullable `latency_ms` field to
`UsageRequest` and store a server-measured duration from accepted submission
through terminal completion or interruption. The browser timer remains live;
the terminal event returns the recorded duration for the final display and
conversation history.

Streaming changes the orchestration boundary and connection lifecycle. The
service must continue to reserve funds before network I/O and never hold a DB
transaction open while reading the stream. Only a normal terminal event with
valid final usage may create a settled charge. A stream error after response
headers have been sent must be communicated as an in-band error event; an HTTP
status can no longer be changed at that point. Reverse proxies and application
servers may buffer streamed output unless configured not to.

WSGI streaming occupies a worker for the duration of each active response. The
existing code and client are synchronous, so a synchronous streaming
implementation is feasible for the current deployment shape but has a worker
capacity tradeoff. Revisit an ASGI/async HTTP client if production concurrency
requires non-blocking upstream reads; do not mix synchronous ORM work into an
async stream without an explicit boundary.

## Billing and Disconnect Semantics

The usage record starts pending, then is reserved as it is today. During
streaming, the UI can display text but the backend must not call the request
settled or debit by guessed token counts. At normal stream end, normalize the
provider's terminal usage, persist the complete assistant message, settle the
actual charge, and release unused reservation atomically.

If the upstream stream fails, omits usage, or the client disconnects after the
proxy may have accepted the request, usage is unknown. The reservation must
remain held and the request must be marked for reconciliation; no automatic
retry is safe. The user confirmed that already-streamed partial text should be
persisted as an explicitly incomplete assistant response associated with the
reconciliation-required request. Display the partial response and unknown-usage
notice, but do not present it as a successful settled turn. Record elapsed time
through the interruption. A failure proven to occur before the upstream request
starts may continue to release the reservation.

Retain the per-user idempotency key across the streamed POST. A repeated
submission must not start a second upstream request or create a second charge.
Handle an already-processed idempotency key by returning the stored result (or
an explicit already-processed event) without contacting the proxy again.

## Confirmed Pink Accent Scope

Replace the current blue/cyan interface accents with a coordinated pink palette
using CSS custom properties for accent, hover, selected, and pale-surface
variants. Apply it to the rail and active navigation, selected session,
user-message bubble, route selector, links, focus treatment, and send action.
Keep the light neutral canvas and readable dark text. Preserve warning/error
colors for their semantic meaning rather than recoloring every status.

Check text/background contrast, visible keyboard focus, and reduced-motion
preferences for any streaming activity indicator. Do not make the pink bubble
the only indication of user/assistant role.

## Confirmed Decisions

- Persist partial assistant output on an interrupted stream, link it to a
  reconciliation-required usage request, and keep the reservation held.
- Persist elapsed response duration in `UsageRequest.latency_ms` as well as
  showing the live browser stopwatch and final duration.
- Apply pink to interface accents and user bubbles while retaining neutral
  backgrounds and semantic warning/error colors.
