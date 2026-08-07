# Conversation + Feedback System — Implementation Summary

## What Was Built

I've created the foundation for a recursive training loop that captures human feedback on agent conversations and uses it to improve future responses.

---

## Components Delivered

### 1. Feedback API Server (`feedback_api_server.py`)
**Port:** 3980

**Routes:**
- `GET /api/threads` — List all threads (email + SMS)
- `GET /api/threads/:id` — Get specific thread with messages
- `POST /api/feedback` — Create feedback on a message
- `GET /api/feedback` — List feedback (filterable by status/thread)
- `PUT /api/feedback/:id` — Update feedback
- `POST /api/feedback/:id/approve` — Approve feedback → adds to example bank
- `GET /api/training/examples` — List approved training examples

### 2. Conversation Review UI (`conversations.html`)
**Location:** `/conversations` (when deployed)

**Features:**
- Unified thread list (email + SMS combined)
- Full chat-style viewer with speaker labels
- "Needs Review" status indicator
- Feedback panel on each agent message:
  - Rating: Good / Needs Improvement / Wrong
  - Corrected response field
  - Coaching note field
  - Failure tags (missed tour ask, wrong tone, etc.)

### 3. Training Data Storage
**Feedback Storage:** `/home/ubuntu/.hermes/feedback/fb_*.json`
```json
{
  "feedback_id": "fb_abc123",
  "thread_id": "ghl-lead-xxx",
  "message_idx": 2,
  "rating": "needs_improvement",
  "corrected_response": "Better response here...",
  "coaching_note": "Should have used tour-first",
  "failure_tags": ["missed_tour_ask"],
  "status": "pending_review"
}
```

**Example Bank:** `/home/ubuntu/.hermes/training/examples.jsonl`
- Approved corrections stored as few-shot examples
- Indexed by scenario, event type, channel

---

## Recursive Training Loop Design

### Flow
```
Conversation happens → Human reviews → Rates response →
Provides correction → Feedback saved →
Roger approves → Added to example bank →
Agent retrieves on similar scenarios → Better future response
```

### How It Improves Agents

**Priority 1: Few-Shot Example Bank (Implemented)**
- Approved corrections stored as examples
- Indexed by scenario type
- Can be retrieved by agents at generation time

**Priority 2: Vapi Custom Tool (Next Step)**
```python
# Agent tool to retrieve examples
def get_approved_examples(scenario_type, event_type, channel):
    # Returns top 3 approved corrections for this scenario
    # Agent uses as few-shot guidance
```

**Priority 3: Prompt Update Proposals (Future)**
- Batch analysis of feedback patterns
- Propose specific prompt section changes
- Human approval before applying

---

## Integration with Existing System

**Leads Report:**
- Added link: "📣 Review Conversations" in header
- Points to `/conversations`

**Thread Storage:**
- Uses existing thread files (no migration)
- Email threads: `/home/ubuntu/.hermes/agents/mary/threads/`
- SMS threads: `/home/ubuntu/.hermes/sms-threads/`

---

## Deployment Status

| Component | Status |
|-----------|--------|
| Feedback API Server | ✅ Running on port 3980 |
| Conversation UI | ⚠️ Ready, needs Vercel deploy |
| Feedback Storage | ✅ Created |
| Example Bank | ✅ Created |
| Vercel Config | ✅ Updated |

**Deploy to Vercel:**
```bash
cd /home/ubuntu/bell-tower-leads-report
vercel --prod
```

---

## Next Steps

1. **Deploy to Vercel** — Run `vercel --prod` from the leads report directory
2. **Test feedback flow** — Submit feedback on a real conversation
3. **Create Vapi tool** — Add `get_approved_examples` to Mary's tool set
4. **Integrate example retrieval** — Mary calls the tool before responding
5. **Add prompt update proposals** — Batch feedback analysis → prompt improvements

---

## Example Use Case

**Scenario:** Mary responds to a corporate lead with "What's most important to you as you evaluate venues?"

**Problem:** She should have used tour-first immediately.

**Feedback Flow:**
1. Roger opens `/conversations`
2. Finds Angela Rodriguez thread
3. Clicks "Feedback" on Mary's response
4. Selects "Needs Improvement"
5. Enters correction: "Hi Angela — December 4th is in play. Let me get you on the calendar..."
6. Adds coaching note: "Should use tour-first immediately for non-bridal"
7. Tags: "Missed tour ask"
8. Clicks Submit

**Training Loop:**
1. Feedback stored with full context
2. Roger reviews pending feedback
3. Clicks "Approve"
4. Added to example bank
5. Next time Mary sees a similar corporate inquiry, she retrieves this example
6. Better response

---

## Architecture Documentation

Full architecture document: `/home/ubuntu/bell-tower-leads-report/ARCHITECTURE.md`