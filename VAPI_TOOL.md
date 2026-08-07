# Vapi Tool: get_training_examples

This tool allows Mary to retrieve approved response corrections during voice calls.

## Tool Definition (add to Mary's assistant in Vapi dashboard)

```json
{
  "type": "http",
  "server": {
    "url": "https://webhooks.bell34hooks.com/api/tools/training_examples"
  },
  "name": "get_training_examples",
  "description": "Retrieve approved response corrections for the current scenario. Use this tool before responding to improve response quality based on human feedback. Call with the current channel (voice) and event type if known.",
  "parameters": {
    "type": "object",
    "properties": {
      "channel": {
        "type": "string",
        "description": "The communication channel: voice, email, or sms. Always use 'voice' for phone calls.",
        "enum": ["voice", "email", "sms"]
      },
      "event_type": {
        "type": "string",
        "description": "The event type if known: bridal, corporate, social, nonprofit, milestone, portrait, or unknown."
      }
    },
    "required": ["channel"]
  }
}
```

## How It Works

1. Vapi calls this tool when Mary needs to respond to a lead
2. The endpoint fetches approved corrections from the example bank
3. Filters by channel and event_type
4. Returns up to 3 examples with:
   - `preferred_response`: The corrected response that was approved
   - `why_better`: The coaching note explaining the improvement
   - `failure_tags`: What was fixed (missed_tour_ask, wrong_tone, etc.)

## Response Format

```json
{
  "status": "success",
  "count": 1,
  "examples": [
    {
      "preferred_response": "Hi — May 2027 is in play! The best way...",
      "why_better": "Tour-first approach, specific date acknowledgment",
      "failure_tags": ["missed_tour_ask"]
    }
  ],
  "instruction": "Learn from these approved corrections..."
}
```

## Adding to Vapi

1. Go to Vapi Dashboard → Assistants → Mary
2. Scroll to "Tools" section
3. Click "Add Tool" → "HTTP"
4. Paste the JSON definition above
5. Save

The tool will automatically call this endpoint before generating responses.

## Endpoint Location

- **Local**: `http://localhost:3980/api/tools/training_examples`
- **Production**: `https://webhooks.bell34hooks.com/api/tools/training_examples`

The production URL is routed through the Cloudflare tunnel to the feedback API server.