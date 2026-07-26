# FirstPass EDI — Blog Post Drafts

> **Target site:** firstpassedi.com  
> **Voice:** Senior practitioner, direct, no fluff. First-person "I" — no personal names.  
> **CTAs:** Trading-Partner Compliance Audit ($5,500 fixed fee) or contact page.

---

# Post 1

# Why Your 997 Acknowledgments Keep Rejecting — And It's Not the Data You Think

*Keywords: 997 EDI rejection, 997 acknowledgment rejected, EDI 997 error codes*

---

The 997 Functional Acknowledgment is supposed to be boring. Your system sends a transaction set, the trading partner acknowledges receipt, everyone moves on. When it works, nobody thinks about it.

When it doesn't work, you get a ticket at 7 AM because a retailer's portal is showing "rejected" against 40 purchase orders that shipped two days ago. The orders are real. The data looks fine. Nothing changed. And yet.

Here's what I've learned after debugging EDI rejections across dozens of Sage 100 manufacturing environments: **most 997 rejections have nothing to do with the actual transaction content.** The PO number is fine. The line items match. The quantities are correct. The problem is almost always in the envelope — and specifically, in places that most EDI troubleshooting guides never talk about.

---

## The Envelope Is Not What You Think It Is

When people say "EDI envelope," they typically picture the ISA/IEA interchange wrapper. But there are actually two envelopes in every X12 transmission:

1. **The ISA/IEA interchange envelope** — identifies the sender and receiver at the network level
2. **The GS/GE functional group envelope** — groups related transaction sets and identifies the application layer

The transaction set itself (your 850 purchase order, your 856 ASN) lives inside both. The critical thing to understand: **the 997 is generated at the GS/GE level.** Your trading partner's system receives the interchange, strips the ISA envelope, routes the functional group to its internal application, and then generates a 997 that says accepted or rejected.

If your 997s are coming back rejected, start at the GS segment. Not at the transaction set. Not at the data values. At GS.

---

## The Most Common Cause: GS Qualifier Mismatches

The GS segment has eight data elements. Most people only pay attention to GS-01 (functional identifier code — like `PO` for purchase orders) and GS-08 (version string). Both of those can absolutely cause rejections. But the one I see most often is **GS-02 and GS-03** — the sender and receiver application identifiers.

These are **not** the same as your ISA-06 and ISA-08 interchange IDs. The ISA qualifiers identify the trading parties at the network level. GS-02 and GS-03 identify the *applications* within those parties. A retailer's implementation guide might specify:

- ISA-08: `WALMARTSTO`
- GS-03: `WALMART`

If you're populating both fields with `WALMARTSTO`, the trading partner's application router doesn't know where to deliver the functional group. You'll get a 997 rejection with an error that says something like "unknown application ID" or just a generic GS-level reject code with no useful description.

Pull your trading partner's implementation guide and verify GS-02 and GS-03 explicitly. Don't assume they match your ISA qualifiers. In my experience, about one-third of the time, they don't.

---

## Version String Problems Are Quiet and Brutal

GS-08 carries the version/release/industry identifier. For X12 version 4010, this should be `004010`. For 5010, `005010`. For 5010 with a subrelease or industry supplement, it might be `005010UCS` or `005010VICS`.

Retailers running older EDI infrastructure will reject anything that doesn't match their expected version string exactly. I've seen manufacturers upgrade their EDI platform, default to 5010, and watch every single 997 come back rejected — because the retailer was on 4010 and the new platform never got reconfigured.

The VICS supplement (Voluntary Interindustry Commerce Standards) causes particular confusion in retail EDI. VICS is a retail-specific X12 implementation overlay. If a retailer's spec references VICS and your system is sending vanilla X12, the 997 rejection often says "invalid version" or "GS08 mismatch" — even though your actual transaction set data was fine.

Check GS-08. Match it to the spec character for character.

---

## Control Number Drift

Every X12 interchange has three levels of control numbers: ISA-13 at the interchange level, GS-06 at the functional group level, and ST-02 at the transaction set level. All three must be unique and sequential within their scope. If your EDI system loses track of its counter — after a database restore, a failed test environment, a platform migration — you can start re-using control numbers you've already sent.

Most trading partners reject duplicates at the 997 level. The error message usually says something like "duplicate interchange" or "duplicate functional group control number."

**The fix is simple but easy to miss:** increment your control number counter past the highest value you've ever sent. If you're not sure what that is, move well past your estimate. Wasted control numbers cost you nothing. Duplicate control numbers cost you rejected transactions and phone calls you don't want to make.

---

## How to Actually Debug a 997 Rejection

This is the process I use when a client calls with 997 rejections they can't explain:

**Step 1: Get the raw 997 file.** Not a translated summary. Not a portal display. The actual raw X12. The AK1 and AK9 segments tell you exactly what was rejected. AK1 carries the GS functional identifier code and the group control number. AK5 and AK9 carry the acknowledgment codes.

**Step 2: Read the AK error codes.** AK5-01 is the transaction set acknowledgment code: `A` = accepted, `R` = rejected. For rejections, AK4 has data element error codes. Error code 2 = mandatory element missing. Code 7 = invalid code value. Code 8 = invalid date format. These point you straight at the problem field.

**Step 3: Pull the raw outbound file.** Compare your ISA and GS segments field by field to what the trading partner's spec says they should be. Don't compare to memory. Don't compare to another trading partner's setup. Compare to the actual spec document.

**Step 4: Check your control number log.** Most EDI platforms track outbound control numbers. Verify the ISA-13 and GS-06 values in your transmission are sequential and not reused.

**Step 5: Call the trading partner's EDI desk.** I know nobody wants to do this. But if your file looks clean and you're still getting rejections, sometimes the problem is on their end — a misconfigured routing rule, a stale qualifier in their system, an account that needs to be re-provisioned. Ask them to pull their inbound log and read you the rejection reason verbatim. Their system knows what it rejected and why.

---

## One More Thing: ISA-15

ISA-15 is the interchange usage indicator. `T` for test, `P` for production. If you're sending `T` and the trading partner's system is configured to only process production traffic, you'll get either silence or a rejection. If you're sending `P` in an environment where your trading partner account hasn't been activated for production, same result.

It's one character. Check it. I've seen it burn multiple days on a launch that was actually ready to go.

---

If your team is losing hours to 997 rejections that aren't going away, that's usually a sign that something in your EDI setup was configured without a proper compliance pass. The [Trading-Partner Compliance Audit](/contact) I offer covers ISA/GS qualifier validation, control number hygiene, version string verification, and live transaction testing against your actual partners — fixed fee, no surprises. If this sounds like where you are, [reach out](/contact) and we'll scope it.

---

---

# Post 2

# Sage 100 EDI Without a Legacy VAN: What Actually Works in 2026

*Keywords: Sage 100 EDI integration, Sage 100 EDI without VAN, EDI integration Sage 100 manufacturing*

---

Most Sage 100 manufacturers doing EDI today are running an architecture that hasn't changed in 20 years. A Value-Added Network in the middle. AS2 or FTP handoffs. A translation engine that produces flat files. A scheduled job that imports those flat files into Sage. It works — sort of — until it doesn't, and when it breaks, it's never obvious where.

I'm not going to tell you VAN-based EDI is dead. It's not. But I am going to tell you that for mid-market manufacturers on Sage 100, there are now better options for most use cases — and a lot of shops are paying for complexity they don't need.

Here's how I think about the architecture decision in 2026.

---

## Why the Legacy VAN Stack Exists

The VAN model made sense in the 1990s. EDI connections were point-to-point. Every trading partner required a separate network connection, a separate AS2 certificate, a separate translation map. VANs emerged as the clearinghouse layer — you connected once to the VAN, and the VAN handled connectivity to every trading partner in their network.

The VAN also handled translation in many setups. You'd send them a flat file, they'd produce X12. Or the reverse. Your EDI system (EDIBANX, True Commerce, SPS Commerce, Epicor EDI) would connect to the VAN and handle the mapping.

The pricing model followed: you paid per-character or per-document. Low volume? Fine. High volume, or many trading partners, and the bills got interesting fast.

**The actual problems with VAN architecture in 2026:**

- **Per-document costs** that scale against you as your order volume grows
- **Slow SLAs** — "same-day processing" is the standard, which means hours of latency between order receipt and Sage import
- **Opaque error handling** — you often learn about a failed transmission when a trading partner calls, not when it fails
- **Vendor lock-in** — your translation maps live in the VAN's proprietary system; migrating means rebuilding everything
- **AS2 certificate maintenance** — every direct trading partner connection requires cert management, often handled manually

None of these are fatal. But they add up, especially for a manufacturer with 15-30 active trading partners and seasonal volume swings.

---

## What API-First EDI Actually Means

Platforms like Orderful have changed the architecture options. Orderful connects to the EDI network at the interchange level — they maintain the trading partner relationships, handle AS2/SFTP connectivity, manage acknowledgments — but they expose everything to your systems via REST API and webhook.

Instead of your Sage 100 environment polling an FTP folder every 15 minutes for flat files, Orderful sends a webhook the moment a transaction arrives. Instead of submitting a flat file and hoping the VAN translates it correctly, you POST a JSON payload to Orderful's API and they produce the X12 on the other end.

The translation model is also different. Orderful normalizes all inbound X12 into a consistent JSON schema regardless of which trading partner sent it. A Walmart 850 and a Home Depot 850 look the same on the API side. You write one integration, not one per trading partner.

**What this changes for Sage 100 shops:**

- Order latency drops from hours to minutes
- Error notifications are real-time, not discovered-after-the-fact
- Trading partner onboarding through Orderful is fast — they already have connections established with most major retailers
- Pricing is flat monthly, not per-document

---

## The IN-SYNCH Layer

Orderful handles the EDI network side. The other half of the problem is getting data in and out of Sage 100.

IN-SYNCH (from ROI Consulting Group) is the integration middleware I use for this. It's purpose-built for Sage 100, handles bi-directional data flow, and can call REST APIs — which means it can talk to Orderful's API directly without a flat-file intermediary.

A working Sage 100 + Orderful + IN-SYNCH stack for inbound purchase orders looks like this:

1. Retailer sends 850 → Orderful receives, validates, normalizes to JSON
2. Orderful fires webhook to your endpoint (or IN-SYNCH polls the API on a short interval)
3. IN-SYNCH maps the Orderful JSON payload to Sage 100 Sales Order fields
4. Sales order created in Sage, confirmation written back to Orderful
5. Orderful generates and sends 997 acknowledgment to retailer

End to end, orders are in Sage in under 5 minutes from retailer transmission. Compare that to a VAN setup where the batch runs every 30-60 minutes and failures surface hours later.

For outbound 856 ASNs and 810 invoices, the flow reverses: Sage 100 ship confirmation or invoice triggers IN-SYNCH, which calls the Orderful API to submit the outbound transaction.

---

## Cost Comparison: What the Numbers Actually Look Like

I'm going to be specific here because vague "could be cheaper" claims are useless.

**Typical legacy VAN setup for a mid-market manufacturer:**

- VAN document fees: $0.08–$0.35 per document (850s, 856s, 810s, 997s each count)
- At 500 documents/month (modest), that's $40–$175/month in document fees alone
- At 3,000 documents/month during peak season, you're at $240–$1,050/month just for transmission
- Plus: EDI software license (True Commerce, SPS, etc.) — $300–$1,500/month
- Plus: IT time for AS2 cert renewals, map updates, and partner onboarding

**Orderful + IN-SYNCH:**

- Orderful: starts around $500/month for most mid-market volumes (flat, not per-document)
- IN-SYNCH: typically $300–$700/month depending on tier and number of integrations
- Setup cost: one-time integration project (varies, but you're paying for configuration once, not forever)

For most Sage 100 manufacturers I talk to, the ongoing cost is lower under the API-first model, and the operational burden is significantly lower. The main investment is the implementation project up front.

---

## When the Legacy VAN Still Makes Sense

I'm not here to sell a platform. There are cases where staying VAN-based is the right call:

- You have fewer than 5 active trading partners with very low volume — the flat monthly of Orderful might cost more than your current document fees
- Your trading partners have unusual connectivity requirements that Orderful doesn't support natively
- Your existing VAN setup is working and you have zero appetite for a migration project right now
- You're on a version of Sage 100 that IN-SYNCH doesn't fully support (check before assuming)

But if you have 10+ trading partners, seasonal volume peaks, ongoing trouble with EDI errors you can't trace, or you're paying someone to manage VAN mapping every time a retailer changes their specs — it's worth modeling out the alternative.

---

If you're on Sage 100 and trying to figure out whether your EDI architecture is working for you or against you, the [Trading-Partner Compliance Audit](/contact) is a good place to start. I map out your current stack, identify where errors and inefficiencies are coming from, and give you a clear picture of what a modern integration would look like for your specific trading partners — fixed fee, no guesswork. [Get in touch.](/contact)

---

---

# Post 3

# Trading-Partner Onboarding Doesn't Take 12 Weeks

*Keywords: EDI onboarding timeline, trading partner certification, EDI partner setup time*

---

Twelve weeks is what everyone quotes. Twelve weeks is what the big box retailer's EDI portal says. Twelve weeks is what the onboarding coordinator emails back when you ask how long certification takes.

I've watched manufacturers lose quarters of revenue waiting for EDI certifications that — when you actually look at what's happening — involve about 4-6 hours of real technical work spread over 84 days of queue-sitting, back-and-forth email, and handoff delays.

The 12-week number is not a technical constraint. It's an organizational one. And once you understand the difference, you can compress it significantly.

---

## Why 12 Weeks Became the Industry Default

Let's be honest about what a "12-week onboarding" actually contains:

**Week 1–2:** New vendor account provisioned. EDI coordinator assigned. Implementation guide sent (or pointed to a portal). You review the guide and start asking questions.

**Week 3–4:** You build your transaction maps. Submit test files.

**Week 5–6:** Trading partner reviews test files. Feedback comes back. You fix things.

**Week 7–8:** Another round of test file submissions. Maybe another round of feedback.

**Week 9–10:** Functional certification call (which is often just portal validation).

**Week 11–12:** "Go live" approval, system activation, production test transaction.

Read that again. Most of that isn't development time. It's **wait time**. Specifically:

- Waiting for the retailer's EDI team to review your test files
- Waiting for IT tickets to provision accounts
- Waiting for email replies that take 3 business days
- Waiting for a 30-minute certification call that has a 2-week scheduling lead time
- Waiting for a portal that's only checked by someone twice a week

The technical work — actually building the maps, generating correct test files, validating against the implementation guide — takes days, not weeks. The waiting takes weeks.

---

## The Two Buckets of Delay

When I scope a trading-partner onboarding engagement, I separate delays into two categories: **process delays** and **technical delays**.

**Process delays** are scheduling gaps, queue times, handoffs between people who don't talk to each other, and documentation that's in a portal no one told you about. These are almost entirely on the trading partner's side and can't be eliminated — but they can be anticipated and worked around.

**Technical delays** are mapping errors, missing mandatory segments, incorrect qualifiers, data validation failures. These are on your side, and they're preventable.

The manufacturers I see spending 12 weeks on onboarding are almost always burning time on technical delays that could have been caught before they submitted a single test file. Every round of "your test files have errors, please resubmit" adds 2–3 weeks to your timeline because you're back in the review queue.

**The key insight:** most certification queues are first-in, first-out. Every time you resubmit, you go to the back of the line.

---

## How to Compress the Timeline

**Get the implementation guide before anything else.** Not from your EDI coordinator. Not from a summary someone emailed. The actual implementation guide PDF from the trading partner's EDI portal or documentation site. This document specifies exactly which segments are required, which are optional, which qualifiers to use, and what their specific validation rules are.

I've seen manufacturers start building maps against a generic X12 spec and then get surprised when the retailer rejects their files for missing a proprietary segment or an unexpected qualifier. The implementation guide is the ground truth. Start there.

**Build to the spec before you test.** Before you submit a single test file, map every mandatory segment. Run your test file through an X12 validator — there are free tools, and Orderful's platform has one built in — and confirm it's structurally valid. Then cross-reference every mandatory element from the implementation guide against your output. Don't assume your translation tool is generating everything correctly. Check it.

**Submit complete, correct files on the first attempt.** This sounds obvious but it's not common. Most shops submit something that's "close" and expect to iterate. That's fine in a development environment. In a certification queue, every iteration costs you weeks. Aim for a clean first submission.

**Run multiple certifications in parallel.** If you're onboarding five trading partners, don't do them sequentially. Submit test files to all five at the same time. The review timelines overlap instead of stacking. This alone can turn a 6-month multi-partner rollout into a 6-week one.

**Know the difference between mandatory and "suggested."** Implementation guides often have tables where every segment looks required. Read the cardinality carefully. An `M` in the must-use column is mandatory. A `C` is conditional. An `O` is optional. Retailers generally won't reject you for missing optional segments. They will reject you for missing mandatory ones. Focus your energy accordingly.

**Use a platform that doesn't require per-partner AS2 setup.** If your EDI architecture requires a new AS2 connection, a new certificate, and an IT ticket for every new trading partner, that's overhead that adds days to every onboarding. API-first platforms like Orderful already maintain connections with most major retailers. When you add a new partner through Orderful, you're not setting up new connectivity — you're activating a route that already exists.

---

## The Certification Call Myth

A lot of manufacturers dread "the certification call." It sounds formal. It sounds like a gate.

In practice, for most major retailers, there is no call. Certification is a portal-based automated test. You submit your transactions, the portal validates them against the implementation guide, and you get a pass/fail result. If you pass, your account moves to production-eligible status. If you fail, you get an error report and go back in queue.

For smaller trading partners who do have live certification calls, the call itself is usually 20-30 minutes. The preparation is what matters. If you come in with a clean test file and you've read their implementation guide, the call is a formality.

What makes certification calls go long is showing up unprepared — not understanding your own output, not having read the spec, not knowing what qualifiers you're sending. I've sat in on calls like that. They're painful. They also extend your timeline by weeks because you need to schedule a follow-up.

---

## What "Compressed" Actually Looks Like

Here's a realistic timeline for a single trading-partner onboarding when you come in prepared:

**Day 1:** Get the implementation guide. Set up your test credentials on their EDI portal.

**Day 2–3:** Build your transaction maps against the spec. Validate locally.

**Day 4:** Submit test files.

**Day 5–14:** Waiting for review (this part you can't fully compress, but you can work on the next partner during this time).

**Day 15:** Feedback or pass. If feedback, fix and resubmit same day. If pass, request production activation.

**Day 16–20:** Production activation (this also has queue time; escalate if possible through your account rep).

Three to four weeks, single partner, one review cycle. That's achievable. Compare it to 12 weeks.

The variable you can't control is the trading partner's queue. What you can control is how many review cycles you need and how many partners you're moving through simultaneously.

---

## When It Actually Does Take 12 Weeks

There are legitimate reasons onboarding takes longer:

- The trading partner has a slow EDI team and there's no internal advocate to escalate through
- You're doing a net-new EDI implementation and your translation platform isn't configured yet
- The retailer requires a test order to flow all the way through their fulfillment system before they'll activate production
- You're implementing end-to-end automation (EDI → Sage → warehouse → ASN → invoice) for the first time, not just transmission

In those cases, 12 weeks might be accurate for the full implementation. But "EDI certification" is a subset of that work, and it shouldn't be the bottleneck.

---

If you're about to start a trading-partner rollout and you want to do it right the first time — clean maps, correct qualifiers, no resubmission cycles — that's exactly what the [Trading-Partner Compliance Audit](/contact) is designed to support. I'll review your implementation guide coverage, validate your test files before you submit them, and flag the issues that would put you back in queue. Fixed fee, fast turnaround. [Let's talk.](/contact)
