# Cache Breakpoint Placement — Tracked Firms & Briefing Classifier Keywords

**Question:** do the tracked-firms list and the briefing classifier INCLUDE/EXCLUDE keyword block belong **above** `<!-- CACHE_BREAKPOINT_1 -->` (in the truly-stable core, sections 1–5) or **below** it (inside the `{{TENANT_BUNDLE}}` slot between BP1 and BP2)?

**Recommendation in one sentence:** promote both blocks **above** CACHE_BREAKPOINT_1, into the stable core.

---

## Edit-frequency evidence

- `~/.claude/CLAUDE.md` is not under version control. `git log --since="60 days ago" -p ~/.claude/CLAUDE.md` returns nothing — no `.git` directory in `~/.claude/`. Stating this explicitly because the answer depends on it: there is no measured cadence to point at.
- Qualitative read of the file's investment-context section: the firm list is ~10 firms (Stonepeak, I Squared, ECP, Quantum, KKR Infra, TPG Rise Climate, ArcLight, LS Power, Brookfield Infra, Nuveen Infrastructure, Ridgewood). The briefing INCLUDE/EXCLUDE blocks are ~12 keywords each. Both lists are doctrine-sized, not list-database-sized.
- New firms appear when a credible competitor surfaces in a podcast or memo — empirically that's a monthly-or-less event, not weekly. Keyword classifier edits track new deal-firm names that need to graduate from INCLUDE to EXCLUDE; same cadence.
- Counter-evidence to track: `MEMORY.md` shows session-level updates (Cholla deal added 2026-05-02), and the deal-pipeline data is updated weekly. Those go into the **dynamic** slot anyway — they're not in the firm-list / classifier blocks under discussion.

## Trade-off

| | Frozen-monthly (above BP1) | In tenant bundle (below BP1) |
|---|---|---|
| Cache hit on every call | yes — single shared prefix | yes, but per-tenant key |
| Cost when CLAUDE.md edits | one cache write (1.25× for 5-min TTL); pays back after 2 reads | identical write cost, scoped per tenant |
| Multi-tenant separation | none | clean per-tenant cache lanes |
| **Effect on Opus 4.7 caching** | **decisive** — promotes static core from 3,733 tokens to ~4,300+ tokens, crossing the 4,096-token Opus minimum cacheable prefix | static core stays below 4,096; Opus calls silently miss the cache |
| Effect on Sonnet 4.6 caching | same (Sonnet minimum is 2,048 — already met either way) | same |

The Opus minimum is the load-bearing factor. Sections 1–5 measure 3,733 tokens via `client.messages.count_tokens(model="claude-opus-4-7")`. That's below the 4,096-token floor for Opus 4.7's prefix cache (per `shared/prompt-caching.md`). Today, every Pass 2 Opus call writes the cache (paying 1.25×) and reads zero. Promoting firms + keywords above BP1 adds an estimated 400–700 tokens, pushing the prefix into Opus's cacheable range — and a 90% discount on every Opus call after the first.

## Counter-arguments considered

- **Multi-tenant separation lost.** True. But Tomac is single-tenant today and the second tenant (P / `re-dev`) carries a different firm list and its own keyword classifier. When the second tenant onboards, those tenant-specific overlays go into `{{TENANT_BUNDLE}}` — not in CLAUDE.md. Promotion does not block multi-tenant; it just clarifies that CLAUDE.md is *Yoni's* doctrine, not the platform's.
- **Edit invalidates the whole cache.** True for any prefix change. But promoted content is exactly the kind that should rarely change — and when it does, Opus reads still pay back the rewrite after 2 calls.
- **CLAUDE.md is currently large enough already.** Sections 1–5 measure 3,733 tokens. The Opus minimum is 4,096. There is a clear gap; this is the cheapest way to close it.

## Reversal trigger

Move both blocks **back into `{{TENANT_BUNDLE}}`** if any one of these becomes true:

1. Edit cadence on the firm list or classifier exceeds ~weekly (write cost outpaces read savings even on Opus).
2. A second tenant joins and needs a *different* base firm list (not just additions).
3. The static core organically grows past 4,096 tokens through other content — at that point promoting firms is no longer load-bearing for Opus caching, and tenant separation becomes the dominant criterion.
