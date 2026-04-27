---
name: mattermost-thread-reader
description: Read and summarize a specific Mattermost thread (not whole channel) from a permalink like /pl/<postId>. Use when user asks to analyze exactly one thread, extract agreements/decisions, or provide thread-only summary. Supports VPN precheck and direct Mattermost API thread fetch via authenticated browser session.
---

# Mattermost Thread Reader

1. Ensure VPN is connected if target is `mm.2gis.one`.
2. Extract `postId` from permalink `/pl/<postId>`.
3. Fetch thread via Mattermost API endpoint:
   - `/api/v4/posts/<postId>/thread?skipFetchThreads=false&collapsedThreads=true&collapsedThreadsExtended=false&direction=down&perPage=200`
4. Use authenticated browser profile session (`/root/.openclaw/browser/openclaw/user-data`) so cookies are reused.
5. Parse JSON fields:
   - `order` = chronological post ids
   - `posts[postId].message` = text
   - `posts[postId].user_id` = author id
   - root post = discussion topic

## Mandatory deep-follow rule (ALWAYS)

When any message in the thread contains links to:
- another Mattermost thread (`mm.2gis.one/.../pl/<id>`),
- Jira issue (`jira.2gis.ru/browse/...`),
- Confluence page (`confluence.2gis.ru/pages/...`),

you MUST open and read them before making conclusions.

Non-optional behavior:
1. Extract all such links from thread messages.
2. Read every linked resource.
3. If those linked resources contain more links of the same 3 types, recursively read them too.
4. Continue until no new MM/Jira/Confluence links remain.
5. Only then produce summary/analytics.

Never analyze a thread in isolation when supporting links exist.

6. Summarize with full context:
   - topic/problem
   - options discussed
   - final agreements/owners/next steps
   - unresolved questions
   - external context from linked MM/Jira/Confluence
7. If API fails with auth/network:
   - report exact blocker
   - retry after VPN check / session refresh

Use script: `scripts/fetch_thread_api.js <postId>` to get raw JSON quickly.
