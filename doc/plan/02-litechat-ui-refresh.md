# LiteChat-Inspired Chat UI Refresh Plan

This plan scopes a visual refresh of AllChat's chat system to follow the
reference at `https://litechat.ai/chat` without changing existing billing or
provider behavior.

## Screenshot Basis

- [x] Review the user-provided desktop screenshot of an active chat. It shows a
  slim icon rail, adjacent app/session navigation pane, pale-blue right-aligned
  user bubble, centered neutral assistant response panel, and bottom-docked
  composer with a separate toolbar.
- [x] Record visible toolbar controls: model chip, memories toggle, Tools, and
  thinking-effort selector. These are visual references only; AllChat does not
  currently implement these capabilities.
- [x] Treat the work as a visual refresh. Keep AllChat's existing provider
  routes, billing, synchronous submission, and authentication behavior; do not
  add unobserved interactions by assumption.

The screenshot is `doc/plan/Screenshot 2026-09-29 221335.png`. It contains chat
content and is intentionally ignored by Git. The supplied image shows only the
desktop active-chat state; empty-state and mobile behavior must be designed
from AllChat's existing behavior and accessibility requirements.

## Visual Mapping

- Map the reference's slim global icon rail only to real AllChat destinations;
  do not add nonfunctional links for apps that do not exist.
- Use the adjacent pane for AllChat branding, conversation history, a new-chat
  action, and account/logout navigation. Add rename/delete affordances only if
  they call supported, owner-scoped server actions.
- Give the main chat area a light neutral canvas, a compact right-aligned user
  bubble, and a centered, readable assistant response surface. Keep the
  transcript independently scrollable where practical.
- Dock the composer at the bottom of the chat area with a distinct toolbar row.
  Use the existing provider-compatible route dropdown in the model-control
  position, and retain accurate explanatory copy about what a route means.
- Keep AllChat's wallet balance, charge/usage receipts, and pending or
  reconciliation notices discoverable without displacing the conversation.
- Render common assistant Markdown (headings, lists, emphasis, and fenced code)
  through an HTML allow-list sanitizer; keep user messages escaped as plain text.
- Omit code-copy, file-upload, memory, tools, and thinking-effort controls
  unless their behavior is implemented; do not render decorative controls that
  cannot work in the synchronous server-rendered UI.

## Implementation

- [x] Create a feature branch from `main` after the reference and scope are
  confirmed, following `agents.md`.
- [x] Restructure the shared shell in `templates/base.html` to establish a slim
  navigation rail and adjacent conversation/account pane, retaining AllChat's
  own identity and working routes.
- [x] Rework `templates/chat/home.html`, `conversation.html`,
  `_conversation_sidebar.html`, and `_message_form.html` as needed to match the
  observed history navigation, active transcript, and bottom composer. Keep an
  understandable empty state using the existing server-rendered flow.
- [x] Style user and assistant messages differently, constrain assistant
  reading width, and support safe rich-text/code presentation if included in
  the agreed scope.
- [x] Update `chat/static/chat/styles.css` for the reference-inspired visual
  treatment, desktop pane proportions, mobile layout, visible keyboard focus,
  and accessible contrast.
- [x] Keep all existing form submissions synchronous and CSRF-protected. Keep
  server-side ownership checks, post/redirect/get, and message auto-escaping.
- [x] Keep the three provider-compatible route choices, wallet balance, usage
  receipts, and pending/reconciliation notices visible and understandable.
- [x] Add or update template/view tests for changed rendering and Markdown
  sanitization; preserve all billing, authentication, ownership, and proxy tests.

## Verification and Rendezvous

- [x] Run `python manage.py check` and `python manage.py test`.
- [ ] Compare the desktop active-chat rendering with the supplied reference and
  review the empty state and mobile layout; verify keyboard navigation, focus
  visibility, labels, contrast, and long-message wrapping.
- [x] Verify that no API key is rendered into HTML and all state-changing forms
  retain CSRF protection.
- [ ] Merge the feature branch back to `main` only after checks pass, then sync
  `doc/wiki/README.md` if the user-visible behavior or setup changed.

The full test suite passes (65 tests, 2 skipped) and `python manage.py check`
reports no issues. A browser executable is unavailable in the current
environment, so rendered desktop/mobile visual review remains outstanding.
