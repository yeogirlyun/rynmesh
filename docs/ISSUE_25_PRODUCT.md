# Issue #25 product specification — Ask about this item

Status: implemented and accepted on `feature/issue-25-ask-about-item`

Depends on: Issue #24 provider/model switching (`ef817bc`)

## Problem

Rynmesh can extract an article through the owner's local node and can run a
Private AI conversation through a selected Provider, but the user previously
had to copy text between those experiences. Copying loses provenance and makes
it unclear whether the model saw a full article, a feed summary, or an
arbitrary pasted excerpt.

## User outcome

After the local reader has produced non-empty article blocks, the item viewer
shows **Ask about this item**. One click opens a fresh Private AI conversation
with a visible article card containing:

- article title, source, byline, and a local source link;
- whether the full extract fits the selected model;
- exact included/original character and block counts when shortened;
- a Remove action available before sending.

The user writes the question normally. The article is evidence for the prompt,
not a giant message in the transcript. Removing the card re-encrypts the
conversation without the grounding and prevents the stored article from being
added to later prompts.

## Product rules

1. The action is enabled only for node-extracted, non-empty reader content.
2. Reader failure retains the original-link fallback and explicitly says that
   grounded asking is unavailable.
3. The handoff creates a new conversation in the currently selected
   Provider/package history bucket. Switching providers never moves or exposes
   that conversation; switching back restores it.
4. Provider busy/offline/disappeared states retain the user's draft.
5. A request failure restores the submitted text to the composer for editing
   or retry while retaining the failed attempt in local history.
6. A context window too small for the question and a useful excerpt disables
   sending and directs the user to a larger-context Provider or removal.

## Privacy and trust boundary

- Full article content never enters URL parameters, `history.state`,
  `localStorage`, `sessionStorage`, Registry records, or normal logs.
- The viewer and chat exchange only a random 192-bit, five-minute, one-time
  in-memory identifier.
- After consumption, the article and its provenance live only in the existing
  AES-GCM encrypted conversation record (or session memory when encrypted
  persistence is unavailable).
- The Browser still submits only to the local Consumer node. It never calls a
  Provider directly.
- Article content is untrusted data. Fixed instructions outside the quote
  boundary tell the model not to follow article instructions, and delimiter
  lookalikes inside the article are neutralized.

## Not in scope

- Arbitrary file/PDF/audio/video grounding.
- Browser-side publisher fetching or Browser-to-Provider requests.
- Remote embeddings, vector databases, or hidden remote summarization.
- Treating a feed description as the article body.

## User-visible failure states

| State | Behavior |
| --- | --- |
| Reader loading | Disabled `Preparing article…` action |
| Empty/failed extraction | Grounded asking unavailable; Original remains |
| No Provider | Handoff remains unconsumed until expiry; manage/wait message |
| Expired/already-used handoff | Reopen-item instruction |
| Context too small | Send disabled; choose larger Provider/remove context |
| Provider storage switch fails | Current bucket/history/draft remain; switch releases |
| Order fails | Draft is restored and failure remains visible |

## Acceptance evidence

The repeatable local browser package is committed under
`docs/evidence/issue-25/`. It exercises a cached multilingual Reader article,
the one-click handoff, visible truncation, local-Consumer submission, and
removal without Docker or external services. The companion automated tests
cover history-state and storage leakage plus AES-GCM persistence.
