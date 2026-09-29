# LiteChat-Inspired Chat UI Refresh Study

## Purpose

Assess how to make AllChat's chat experience feel closer to the reference at
`https://litechat.ai/chat`, with particular attention to the signed-in chat
workspace, while keeping AllChat's existing authentication, provider routing,
and metered-billing behavior intact.

## Reference Inspection

The public `/chat` URL initially returned a login page. The user subsequently
provided a desktop screenshot of an authenticated, active conversation at
approximately 1920 x 1080. The screenshot shows:

- A narrow icon rail at the far left and a wider white navigation pane beside
  it. The pane groups app links, account navigation, and sessions, with a
  selected session row, date, new-session action, and edit/delete affordances.
- A light neutral conversation canvas. The user's message is a compact,
  right-aligned pale-blue bubble; the assistant response is a broad centered
  neutral panel with formatted text, headings, a list, and a code block with a
  copy affordance.
- A composer docked across the bottom of the chat area. A separate toolbar row
  contains a model chip, a memories toggle, Tools, and a thinking-effort
  selector. The input area includes a message/file-drop prompt, attachment
  affordance, and send action.

The screenshot only establishes the desktop active-conversation state. It does
not show an empty conversation, mobile layout, or interactions for the visible
tools and settings. Its sample conversation content is not repeated here. The
image remains a local planning reference and is excluded from Git because it
contains chat content.

## Current AllChat Baseline

- `templates/base.html` renders a compact account header with wallet balance
  and a CSRF-protected logout form.
- `templates/chat/home.html` and `conversation.html` render a two-column
  workspace: a conversation-history sidebar and a card-like main chat panel.
- `templates/chat/conversation.html` renders ordered messages, per-turn
  provider labels, usage receipts, and pending/reconciliation notices.
- `templates/chat/_message_form.html` provides a synchronous message textarea,
  a provider-compatible route dropdown, and a submit button.
- `chat/static/chat/styles.css` supplies the full responsive layout and visual
  styling. There is no client-side JavaScript or streaming behavior.
- The backend owns conversation history, provider selection, CSRF validation,
  and wallet/usage state. Those behaviors must remain authoritative after a
  visual refresh.

## Feasibility and Constraints

The visual layer can be substantially reshaped with Django templates and CSS
without changing the synchronous server-rendered architecture. The likely seam
is the shared application shell and chat workspace: navigation/sidebar,
transcript hierarchy, composer placement, and the way route and wallet context
are presented.

The refresh must preserve authenticated ownership, escaped message content,
CSRF-protected POST forms, post/redirect/get behavior, the three allow-listed
provider-compatible routes, six-decimal wallet display, per-turn usage
receipts, and pending/unknown billing notices. It must remain usable on narrow
screens and with keyboard navigation.

The screenshot does not establish empty-state or mobile behavior, or how its
memories, tools, thinking-effort, and attachment controls work. Implementing
unobserved features such as streaming, model discovery, attachments, or
client-side conversation switching would exceed a visual refresh and conflict
with the current MVP unless explicitly requested.

## Recommended Direction

Use the supplied signed-in reference to compare visible structure rather than
copying inaccessible implementation details. Prioritize the chat screen:

- Use a two-part left navigation: a slim rail only for real AllChat navigation
  destinations, and a wider conversation/account pane. Do not add decorative
  dead-end icons for features AllChat does not provide.
- Match the reference's light neutral canvas, compact user bubble, centered
  readable assistant response panel, and bottom composer/toolbar hierarchy.
- Map the reference's model chip to AllChat's existing provider-compatible
  route selector. Keep the configured model and route semantics accurate; do
  not claim the selector chooses a distinct underlying vendor model.
- Keep AllChat's wallet balance, usage receipts, and pending/reconciliation
  disclosures available without letting them overwhelm the transcript.
- The reference renders rich response formatting. The implementation plan
  includes common assistant Markdown using an allow-list sanitizer; user
  messages remain escaped plain text. A copy-code action is omitted because the
  current app has no client-side interaction layer.
- Do not present memories, tools, thinking-effort settings, or file attachments
  as functional controls unless their corresponding backend behavior is added
  in a separately scoped change.
- Preserve server-rendered navigation and full-page submission; do not imply
  streaming or instantaneous client-side updates that the app does not support.
- Use AllChat-owned wording and assets. Do not copy protected logos, proprietary
  icons, or brand copy from the reference.

## Remaining Reference Gaps

The supplied active-chat screenshot is enough to guide a desktop visual
refresh. Empty-state and mobile behavior remain unobserved; derive those states
from AllChat's existing behavior and responsive requirements unless the user
provides additional references. The plan treats this as a visual refresh only,
not a request to add new chat capabilities.
