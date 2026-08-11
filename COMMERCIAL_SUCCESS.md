# Commercial Success Architecture

Status: authoritative commercial and editorial-success specification

## 1. Purpose and business thesis

Autonomous YouTube Studio is building a premium, evidence-first AI and future-technology media brand. It is not a generic AI content factory, a faceless-video generator, a clickbait optimizer, an upload-volume optimizer, or an unconstrained revenue optimizer.

The product objective is to consistently identify high-demand stories, package them for qualified clicks without misleading viewers, hold attention, earn trust, and convert that audience into durable revenue. Automation is an operational advantage; audience value and editorial quality are the business objective.

## 2. North-star hierarchy

The metric priority is, in order:

1. Viewer satisfaction.
2. Watch time and retention.
3. Qualified click appeal.
4. Returning-viewer growth.
5. Subscriber conversion.
6. Revenue per campaign.
7. Production efficiency.

Production volume is not a north-star metric. It is subordinate to quality and viewer satisfaction.

## 3. Constrained optimization

The effective objective is to maximize long-term viewer value and sustainable campaign economics, subject to truth, sourceability, media rights, editorial integrity, safety, brand consistency, non-repetition, bounded retries/time/cost, deterministic hard gates, and hash-bound human release approval.

No numerical commercial score may override an unsupported-claim failure, source failure, rights failure, media-integrity failure, disclosure failure, approval failure, or publication failure. Commercial scores cannot override truth, rights, or safety gates.

There is never an autonomous top-level objective equivalent to maximizing views without constraints, maximizing revenue without constraints, or maximizing upload volume.

## 4. Campaign Budget Policy

The versioned contract is `policies/campaign_budget.v1.json`. For a normal 8–12 minute long-form campaign, the target variable metered production cost is approximately $20.00 (20,000,000 micro-USD), the soft warning is $25.00 (25,000,000 micro-USD), and the default hard cap is $35.00 (35,000,000 micro-USD).

The metered scope includes LLM, research/search, TTS, image generation, video generation, and other metered generation APIs. Local FFmpeg and deterministic local rendering are excluded unless they later create a measurable metered hosted-compute charge.

The $25.00 soft warning requires cost-reduction behavior; it is not permission to sacrifice editorial quality or truth. The system should prefer lower-cost visual modes, regenerate only failed or rejected scenes rather than whole videos, avoid unnecessary premium generated-video calls, and prefer diagrams, screenshots, stills, documents, or deterministic graphics when editorially suitable. Expensive retries must trigger visual-mode substitution rather than uncontrolled spending.

Before every metered provider request, the future implementation must add the campaign's recorded or committed variable cost to a conservative estimate for the proposed request. If that sum would exceed the currently authorized campaign hard cap, the provider request must not be sent. The system must use an eligible lower-cost fallback or stop and require explicit owner authorization.

An override is never automatic. It applies only to one identified campaign, specifies the new authorized cap, and records the actor, reason, and timestamp for audit. Providers and analytics cannot change budgets, the global policy, or its caps. Sponsor economics cannot change editorial gates or budget-safety behavior.

Budget never overrides truth, source, rights, safety, approval, or editorial-quality requirements. Low cost is an efficiency constraint, not the channel's north-star objective.

## 5. I4 commercial topic intelligence

Every sourceable topic may be commercially ranked for audience relevance, current demand, novelty, consequence or stakes, narrative tension, broad-interest bridge, visual potential, shelf life, authority opportunity, and sponsor compatibility. Commercial score never overrides sourceability or safety.

I4 requires a first-class Viewer Promise containing, at minimum:

- `viewer_promise`
- `core_question`
- `why_now`
- `stakes`
- `novelty`
- `target_viewer`
- `broad_interest_bridge`
- `expected_takeaway`

If a compelling truthful Viewer Promise cannot be formed, the topic is not ready.

## 6. Narrative engineering and narrator identity

Scripts must be stories or explanations, not information dumps. They require an immediate continuation reason, an unresolved question or tension where appropriate, information progression, periodic payoff, genuine explanatory value, a clear thesis, and a useful resolution describing what happens next.

The narrator identity is curious, technically literate, skeptical of hype, clear about uncertainty, and interested in consequences. The channel distinguishes what is known, what companies or people claim, what evidence suggests, what remains uncertain, and what is explicitly editorial analysis. It must not fabricate a human identity.

## 7. I5 retention-oriented visual architecture

The storyboard selects the best visual mode for each narrative beat rather than asking only whether generative video can make something visually attractive. Representative modes are:

- `GENERATED_CINEMATIC`
- `REAL_SOURCE_MEDIA`
- `PRODUCT_FOOTAGE`
- `DOCUMENT`
- `SCREENSHOT`
- `DIAGRAM`
- `DATA_VISUALIZATION`
- `MAP`
- `TIMELINE`
- `CODE`
- `UI_RECONSTRUCTION`
- `KINETIC_TEXT`
- `DETERMINISTIC_MOTION_GRAPHIC`

Generated cinematic media is a bounded ingredient, not a requirement to fill the runtime. Meaningful visual progression should be measurable later without forcing frantic short-form editing.

I5 must implement the campaign budget preflight guard for metered providers, soft-warning cost fallbacks, and hard-cap provider blocking. These cost controls must preserve editorial suitability and every hard gate.

## 8. I6 packaging intelligence

Every episode must produce multiple genuinely different positioning concepts, rather than trivial title rewrites. Each concept should eventually represent a title, thumbnail concept, thumbnail text, viewer trigger, promise, risk of overclaiming, and target audience. Commercial ranking is permitted, but truth is absolute: high predicted CTR plus an unsupported implication is `FAIL`. Qualified clicks matter more than misleading clicks.

### Thumbnail principles

Favor one dominant idea, immediate comprehension, strong hierarchy, minimal text, mobile readability, and relevant recognizable objects, products, or people when lawful and appropriate.

Reject clutter, tiny unreadable UI, paragraphs, competing focal points, irrelevant dramatic imagery, and generic decorative AI-glow imagery without editorial purpose.

### First-30-seconds hook QA

The opening must be separately reviewable for fulfillment of the title/thumbnail promise, stakes, unnecessary introduction, information density, unresolved continuation reason, and avoidable length.

### Retention checkpoints

The conceptual retention zones are 0:00–0:30, 0:30–1:30, 1:30–3:00, 3:00–midpoint, midpoint–final third, and ending. At each boundary, the system should eventually answer: “Why does the viewer continue?” These are editorial QA concepts, not rigid runtime stages.

## 9. I7 analytics learning and campaign economics

Analytics may track supported metrics including views, impressions, CTR, watch time, average view duration, average percentage viewed, likes, comments, shares, subscribers gained/lost, and returning-viewer or traffic-source information when available. It may create recommendations, but may not automatically mutate prompts, policy, thresholds, source rules, architecture, safety rules, or budget rules.

Campaign unit economics eventually track LLM, TTS, video generation, image generation, storage, other provider costs, human review minutes, and total production cost. Revenue may include YouTube advertising/Premium, sponsor revenue, affiliate revenue, and other attributable revenue. Derived economics may include revenue per 1,000 views, cost per 1,000 views, profit per campaign, margin, cost per published minute, and revenue per watch hour.

I7 must include campaign cost accounting, cost per published minute, and auditable reporting of every campaign-specific budget override.

These metrics are observational and recommendational. They must not override editorial hard gates.

## 10. Sponsor/editorial separation

Sponsors may be matched using audience relevance, campaign topic, brand category, expected reach, historical sponsor performance, and conflict rules. Sponsor economics must never influence claim verification, source weighting, safety decisions, story conclusions, criticism of a product or company, or disclosure requirements.

## 11. Controlled exploration

The system must preserve controlled editorial experimentation to prevent premature optimization into repetitive templates. No fixed exploration percentage is locked at this time.

## 12. First ten public campaigns

Do not declare channel-level editorial optimization success or failure from one or two uploads. The first meaningful aggregate optimization review occurs after ten public long-form campaigns.

Compare topics, packaging, hooks, runtime, visual approaches, retention, CTR, subscribers per campaign, production cost, and 24h/7d/30d performance where available. Analytics generates hypotheses, not automatic policy mutation.

## 13. Business moat

Individual providers are replaceable. The long-term moat is the accumulated combination of historical topic decisions, verified research, narrative structures, packaging experiments, thumbnail patterns, retention outcomes, audience behavior, production economics, and editorial rules.

## 14. Change control

Weakening or removing constrained optimization, the campaign budget policy or caps, sponsor/editorial separation, commercial hard-gate dominance, analytics nonmutation, controlled exploration, or phase commercial requirements requires an `architecture.lock.json` schema-version increment and independent review.
