# Conversation + Feedback System Architecture
## Full Multi-Channel Integration

---

## Channel Inventory

| Channel | Source | Thread Storage | Agent | Subsource Label |
|---------|--------|----------------|-------|-----------------|
| GHL Webforms | AgentMail webhook → mary@ | `/agents/mary/threads/ghl-lead-*.json` | Mary | `ghl` |
| WeddingWire | Platform lead → planning@ Gmail | `/agents/mary/threads/mary-*@*.json` | Mary | `platform_ww` |
| TheKnot | Platform lead → planning@ Gmail | `/agents/mary/threads/hctg-lead-KNOT-*.json` | Mary | `platform_tk` |
| Zola | Platform lead → planning@ Gmail | `/agents/mary/threads/` | Mary | `platform_zola` |
| Platform Gmail (other) | planning@ Gmail threads | `/agents/mary/threads/mary-*@*.json` | Mary | `platform_gmail` |
| Direct Email | Non-platform, non-GHL emails | `/agents/mary/threads/{uuid}.json` | Mary | `direct_email` |
| Inbound SMS | Twilio → `/sms/inbound` webhook | `/sms-threads/+1*.json` | Mary | `inbound_sms` |
| Post-call SMS | Vapi EOC handler | `/sms-threads/+1*.json` | Mary | `post_call_sms` |
| Vapi Voice Calls | Vapi API (fetched, 5-min cache) | API → `cache/vapi_calls_cache.json` | Mary | `vapi_call` |

---

## How It Works

### 1. Thread Aggregation (`feedback_api_server.py`, port 3980)

The feedback API server pulls threads from ALL sources on every request:

```
GET /api/threads → loads:
  1. All email threads (140 files) → classified into 6 subsource categories
  2. All SMS threads (123 files) → classified into inbound_sms / post_call_sms
  3. Vapi API call list (200 calls) → cached for 5 min, linked to SMS threads
= 463 total threads in unified list
```

Each thread is normalized into a common format with:
- `source`: email | sms | voice
- `subsource`: ghl | platform_ww | platform_tk | platform_zola | platform_gmail | direct_email | inbound_sms | post_call_sms | vapi_call
- `messages[]`: standardized chat messages with role (agent/customer), text, timestamp
- `customer_name`, `customer_phone`, `event_type` from lead data

### 2. Voice Call Integration

Vapi's list API returns call summaries but not full transcripts (transcript field is empty on list endpoint). The system:

1. Fetches 200 most recent calls (last 14 days) via `https://api.vapi.ai/call`
2. Uses the `summary` field as the primary content
3. Links calls to SMS threads by phone number — if a caller has SMS follow-ups, those messages are appended to the voice thread
4. Shows call outcome (`endedReason`) and linked SMS in one unified view
5. Caches API responses for 5 minutes to avoid rate limits

### 3. Feedback Capture (Channel-Agnostic)

Feedback can be given on any agent message in any channel:

| Channel | What You Review | Feedback Trigger |
|---------|----------------|-------------------|
| Email (GHL/Platform/Direct) | Mary's email reply | Click "Feedback" on agent message |
| SMS (Inbound/Post-call) | Mary's SMS response | Click "Feedback" on agent message |
| Voice (Vapi) | Call summary + SMS follow-up | Click "Feedback" on agent message |

Feedback automatically captures:
- Channel and subsource (from thread metadata)
- Customer name, phone, event type
- Original agent message text
- Rating: Good / Needs Improvement / Wrong
- Corrected response (optional)
- Coaching note
- Failure tags (missed_tour_ask, wrong_tone, etc.)

### 4. Training Loop

```
Roger reviews conversation → Rates response → Writes correction
    ↓
Feedback stored (pending_review)
    ↓
Roger approves → Status: approved
    ↓
If rating is "needs_improvement" or "wrong" + has corrected_response:
    ↓
Added to example bank (examples.jsonl)
    ↓
Tagged with: channel, subsource, event_type, failure_tags
    ↓
Agent retrieves matching examples before responding:
  - Mary AgentMail service (port 3013) → GET /api/training/examples?channel=email&event_type=bridal
  - SMS handler → GET /api/training/examples?channel=sms&event_type=bridal
  - Vapi custom tool → GET /api/training/examples?channel=voice&event_type=bridal
```

### 5. API Endpoints

```
GET  /api/threads                         # Unified list (463 threads, all channels)
GET  /api/threads?source=email            # Filter by channel
GET  /api/threads?source=sms
GET  /api/threads?source=voice
GET  /api/threads?needs_review=true        # Only threads needing feedback
GET  /api/threads/:id                     # Full thread with all messages
POST /api/feedback                         # Create feedback
GET  /api/feedback                         # List feedback (filterable)
GET  /api/feedback?status=pending_review   # Pending only
PUT  /api/feedback/:id                     # Update feedback
POST /api/feedback/:id/approve            # Approve → adds to example bank
GET  /api/training/examples                # List approved examples
GET  /api/training/examples?channel=email&event_type=bridal  # Filtered for agent retrieval
GET  /api/health                           # Channel coverage + system health
```

---

## Integration Points (Existing Infrastructure)

| Service | Port | Role | Integration |
|---------|------|------|-------------|
| Leads Report API | 3979 | Lead list + Vapi call data | Links to /conversations |
| Feedback API | 3980 | Thread viewer + feedback + examples | New — serves all channels |
| Mary AgentMail | 3013 | Email response generation | Calls feedback API for examples (TODO) |
| SMS Handler | in-process | SMS response generation | Calls feedback API for examples (TODO) |
| Vapi (voice) | cloud | Voice call handling | Custom tool → feedback API (TODO) |
| Cloudflare Tunnel | - | Webhook routing | No change needed |
| Cron: lead-assurance-sweep | 3x daily | Channel health check | No change needed |
| Cron: funnel-drift-monitor | 6h | Email delivery health | No change needed |

---

## Data Flow

```
INBOUND CHANNELS
├── GHL webform ──→ AgentMail webhook ──→ mary@ thread
├── WW/TK/Zola  ──→ planning@ Gmail    ──→ mary- thread
├── Direct email ──→ AgentMail/Gmail   ──→ UUID thread
├── SMS reply    ──→ Twilio webhook    ──→ sms-threads
├── Post-call    ──→ Vapi EOC handler   ──→ sms-threads
└── Voice call   ──→ Vapi API fetch     ──→ cache (5-min TTL)
         │
         ▼
THREAD STORAGE (263 local files + 200 Vapi API calls = 463 threads)
         │
         ▼
FEEDBACK API (port 3980)
├── Unified thread list (all channels)
├── Chat-style thread viewer
├── Feedback capture (channel-aware)
└── Example bank (filterable by channel + event_type)
         │
         ▼
CONVERSATION REVIEW UI (/conversations)
├── Channel filter (All / Email / SMS / Voice)
├── Subsource badges (GHL, WW, TK, Zola, Gmail, Direct, Inbound, Post-call, Vapi)
├── Chat-style message viewer
└── Feedback panel on every agent message
         │
         ▼
TRAINING LOOP
├── Roger reviews → rates → corrects
├── Approved feedback → example bank (JSONL)
└── Agents retrieve examples by channel + event_type (TODO: wire to agents)
```

---

## Deployment Status

| Component | Status |
|-----------|--------|
| Feedback API server | ✅ Running (port 3980) |
| Email threads (6 subsources) | ✅ Working (140 threads) |
| SMS threads (2 subsources) | ✅ Working (123 threads) |
| Vapi voice calls | ✅ Working (200 calls, cached) |
| Conversation UI | ✅ Built (needs Vercel deploy) |
| Channel filters | ✅ Built |
| Health endpoint | ✅ Working |
| Vercel deploy | ⏳ Blocked (token expired) |
| Agent example retrieval | ⏳ TODO |
| Vapi custom tool | ⏳ TODO |

---

## File Locations

```
/home/ubuntu/.hermes/
├── agents/mary/threads/          # 140 email threads (GHL + platform + direct)
├── sms-threads/                  # 123 SMS threads (inbound + post-call)
├── cache/vapi_calls_cache.json   # Vapi API response cache (5-min TTL)
├── feedback/                     # Feedback entries (fb_*.json)
├── training/examples.jsonl       # Approved training examples
└── scripts/
    └── funnel_drift_monitor.py   # Funnel health monitor

/home/ubuntu/bell-tower-leads-report/
├── leads_api_server.py           # Leads report API (port 3979)
├── feedback_api_server.py        # Feedback API (port 3980) — multi-channel
├── conversations.html            # Conversation review UI — multi-channel
├── index.html                    # Leads report (links to /conversations)
└── vercel.json                   # Routing config
```
