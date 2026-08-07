#!/usr/bin/env python3
"""
Conversation + Feedback API Server — Multi-Channel
Serves threads from ALL channels: GHL email, platform email (planning@),
SMS (Twilio), and Vapi voice calls. Provides feedback capture + training loop.

Channels covered:
  - GHL webform → AgentMail → mary@ threads
  - Platform leads (WW/TK/Zola) → planning@ Gmail threads
  - TheKnot direct threads
  - Direct email conversations (UUID-named threads)
  - Inbound SMS (Twilio → /sms/inbound webhook)
  - Post-call SMS (Vapi EOC handler)
  - Vapi voice call transcripts (fetched via Vapi API)

Routes:
  GET  /api/threads              - List all threads (email + SMS + voice)
  GET  /api/threads/:id          - Get full thread with messages
  POST /api/feedback             - Create feedback on a message
  GET  /api/feedback             - List feedback (with filters)
  PUT  /api/feedback/:id         - Update feedback
  POST /api/feedback/:id/approve - Approve feedback → example bank
  GET  /api/training/examples    - List approved training examples (filterable)
  GET  /api/health               - Server health + channel coverage
"""

import json
import os
import re
import uuid
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Paths
HERMES_ROOT = Path('/home/ubuntu/.hermes')
THREADS_DIR = HERMES_ROOT / 'agents' / 'mary' / 'threads'
SMS_THREADS_DIR = HERMES_ROOT / 'sms-threads'
FEEDBACK_DIR = HERMES_ROOT / 'feedback'
TRAINING_DIR = HERMES_ROOT / 'training'

# Vapi config
MARY_ASSISTANT_ID = "ab64ee08-aef6-42e0-a585-01354352232c"
VAPI_CACHE_FILE = HERMES_ROOT / 'cache' / 'vapi_calls_cache.json'
VAPI_CACHE_TTL = 300  # 5 minutes

# Ensure directories exist
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
TRAINING_DIR.mkdir(parents=True, exist_ok=True)
(HERMES_ROOT / 'cache').mkdir(parents=True, exist_ok=True)


def _classify_email_source(filename, thread_data):
    """Classify email thread source: ghl, platform_ww, platform_tk, platform_zola, direct."""
    fname = filename.lower()
    if fname.startswith('ghl-lead-'):
        return 'ghl'
    elif fname.startswith('hctg-lead-knot'):
        return 'platform_tk'
    elif fname.startswith('hctg-lead-'):
        return 'platform'  # WeddingWire or other platform
    elif fname.startswith('mary-') and '@' in fname:
        return 'platform_gmail'  # planning@ Gmail threads
    elif 'platform_source' in thread_data:
        ps = thread_data.get('platform_source', '').lower()
        if 'weddingwire' in ps or 'ww' in ps:
            return 'platform_ww'
        elif 'theknot' in ps or 'knot' in ps:
            return 'platform_tk'
        elif 'zola' in ps:
            return 'platform_zola'
        return f'platform_{ps}'
    elif 'source' in thread_data and thread_data['source']:
        return str(thread_data['source']).lower()
    else:
        # UUID-named = direct email conversations (not from GHL or platform)
        return 'direct_email'


def _get_vapi_key():
    """Get Vapi API key from 1password.env."""
    env_file = HERMES_ROOT / 'secrets' / '1password.env'
    if not env_file.exists():
        return os.environ.get('VAPI_PRIVATE_KEY', '')
    try:
        with open(env_file) as f:
            for line in f:
                if line.startswith('export VAPI_PRIVATE_KEY='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
                elif line.startswith('VAPI_PRIVATE_KEY='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
        return os.environ.get('VAPI_PRIVATE_KEY', '')
    except Exception:
        return os.environ.get('VAPI_PRIVATE_KEY', '')


def _fetch_vapi_calls():
    """Fetch recent Vapi call transcripts via API, with 5-minute cache."""
    # Check cache
    if VAPI_CACHE_FILE.exists():
        try:
            mtime = VAPI_CACHE_FILE.stat().st_mtime
            age = datetime.now().timestamp() - mtime
            if age < VAPI_CACHE_TTL:
                with open(VAPI_CACHE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass

    key = _get_vapi_key()
    if not key:
        return []

    week_ago = (datetime.now(timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = f"https://api.vapi.ai/call?createdAtGe={week_ago}&limit=200&assistantId={MARY_ASSISTANT_ID}"

    try:
        result = subprocess.run(
            ['curl', '-sS', '--max-time', '30',
             '-H', f'Authorization: Bearer {key}',
             '-H', 'Content-Type: application/json',
             url],
            capture_output=True, text=True, timeout=35
        )
        calls = json.loads(result.stdout)
        if isinstance(calls, dict):
            calls = calls.get('data', calls.get('calls', []))
        if not isinstance(calls, list):
            calls = []
    except Exception:
        calls = []

    # Cache the result
    try:
        with open(VAPI_CACHE_FILE, 'w') as f:
            json.dump(calls, f)
    except Exception:
        pass

    return calls


def _format_vapi_call_as_thread(call):
    """Convert a Vapi API call record into the unified thread format.
    
    Vapi's list API returns summary but not full transcripts.
    We use the summary as the display content and also check for
    SMS threads from the same phone number to show the full picture.
    """
    call_id = call.get('id', '')
    transcript_parts = []

    # Vapi list API returns transcript/messages as empty strings.
    # The summary field has the call summary text.
    summary = call.get('summary', '')
    
    if summary:
        # Create a single conversation entry from the summary
        transcript_parts.append({
            'role': 'assistant',
            'text': f'[Call Summary] {summary}',
            'timestamp': call.get('createdAt', ''),
            'direction': 'outbound'
        })
    
    # Also check if Vapi returned actual transcript messages (newer API or different endpoint)
    raw_transcript = call.get('transcript', [])
    if isinstance(raw_transcript, list):
        for msg in raw_transcript:
            role = msg.get('role', 'unknown')
            text = msg.get('content', msg.get('text', ''))
            if text:
                transcript_parts.append({
                    'role': role,
                    'text': text,
                    'timestamp': msg.get('time', call.get('createdAt', '')),
                    'direction': 'inbound' if role in ('user', 'caller') else 'outbound'
                })
    
    # Extract customer phone
    phone = ''
    customer = call.get('customer')
    if isinstance(customer, dict):
        phone = customer.get('number', '')
    if not phone:
        phone = call.get('phoneNumberId', '') or call.get('phone', '')

    # Determine call outcome
    status = 'completed' if call.get('status') == 'ended' else call.get('status', 'unknown')
    ended_reason = call.get('endedReason', '')
    
    # If we have a summary, we can show the call even without full transcript
    has_content = bool(transcript_parts) or bool(summary)
    if not has_content:
        # Still include it — might have SMS thread linked
        transcript_parts.append({
            'role': 'assistant',
            'text': f'[Call ended — {ended_reason or "no summary available"}]',
            'timestamp': call.get('createdAt', ''),
            'direction': 'outbound'
        })

    # Check for linked SMS thread from same phone
    sms_messages = []
    if phone:
        sms_file = SMS_THREADS_DIR / f"{phone}.json"
        if sms_file.exists():
            try:
                with open(sms_file) as f:
                    sms_thread = json.load(f)
                for m in sms_thread.get('messages', []):
                    sms_messages.append({
                        'role': 'customer' if m.get('direction') == 'inbound' else 'agent',
                        'text': m.get('body', m.get('text', '')),
                        'timestamp': m.get('timestamp', ''),
                        'direction': m.get('direction', 'outbound'),
                        'source': m.get('source', 'sms')
                    })
            except Exception:
                pass

    # Combine call transcript + SMS messages
    all_messages = transcript_parts + sms_messages

    return {
        'thread_id': f'vapi-{call_id}',
        '_source': 'voice',
        '_vapi_call_id': call_id,
        'messages': all_messages,
        'status': status,
        'lead_data': {
            'prospect_name': '',
            'prospect_phone': phone,
            'call_summary': summary,
            'call_outcome': ended_reason,
            'call_duration': call.get('cost', 0),
        },
        'customer_phone': phone,
        'created_at': call.get('createdAt', ''),
        'call_analysis': {'summary': summary, 'outcome': ended_reason},
        'has_sms_followup': len(sms_messages) > 0,
    }


def get_all_threads():
    """Load all threads from email, SMS, and Vapi voice channels."""
    threads = []

    # 1. Email threads (Mary) — covers GHL, platform, and direct
    if THREADS_DIR.exists():
        for p in THREADS_DIR.glob('*.json'):
            try:
                with open(p) as f:
                    t = json.load(f)
                t['_source'] = 'email'
                t['_email_subsource'] = _classify_email_source(p.name, t)
                t['_file'] = str(p)
                threads.append(t)
            except Exception:
                continue

    # 2. SMS threads (Twilio — inbound + post-call)
    if SMS_THREADS_DIR.exists():
        for p in SMS_THREADS_DIR.glob('*.json'):
            try:
                with open(p) as f:
                    t = json.load(f)
                t['_source'] = 'sms'
                t['_file'] = str(p)
                threads.append(t)
            except Exception:
                continue

    # 3. Vapi voice call transcripts (API fetch, cached)
    try:
        vapi_calls = _fetch_vapi_calls()
        for call in vapi_calls:
            try:
                thread = _format_vapi_call_as_thread(call)
                if thread.get('messages'):
                    threads.append(thread)
            except Exception:
                continue
    except Exception:
        pass

    return threads


def get_thread_by_id(thread_id):
    """Find and load a specific thread."""
    threads = get_all_threads()
    for t in threads:
        if t.get('thread_id') == thread_id or t.get('phone') == thread_id:
            return t
    return None


def format_thread_for_display(thread):
    """Format thread as chat-style messages — handles email, SMS, and voice."""
    messages = []
    
    for idx, msg in enumerate(thread.get('messages', [])):
        direction = msg.get('direction', 'inbound')
        role = msg.get('role', '')
        
        # Voice transcripts use role directly (assistant/user/caller)
        if thread.get('_source') == 'voice':
            role = 'agent' if role in ('assistant', 'bot', 'mary') else 'customer'
        else:
            role = 'customer' if direction == 'inbound' else 'agent'
        
        messages.append({
            'idx': idx,
            'role': role,
            'direction': direction,
            'text': msg.get('text') or msg.get('body') or msg.get('content', ''),
            'timestamp': msg.get('timestamp', ''),
            'from': msg.get('from', ''),
            'to': msg.get('to', ''),
            'sid': msg.get('sid', ''),
            'has_feedback': check_message_feedback(thread.get('thread_id') or thread.get('phone', ''), idx)
        })
    
    # Determine channel and subsource
    source = thread.get('_source', 'unknown')
    subsource = thread.get('_email_subsource', '')
    
    # For SMS, check if post-call or inbound
    if source == 'sms':
        for m in thread.get('messages', []):
            if m.get('source') == 'post_call_sms':
                subsource = 'post_call_sms'
                break
        if not subsource:
            subsource = 'inbound_sms'
    
    return {
        'thread_id': thread.get('thread_id') or thread.get('phone', ''),
        'source': source,
        'subsource': subsource,
        'status': thread.get('status', 'open'),
        'lead_data': thread.get('lead_data', {}),
        'messages': messages,
        'customer_name': thread.get('first_name', '') or thread.get('lead_data', {}).get('prospect_name', ''),
        'customer_email': thread.get('lead_data', {}).get('prospect_email', ''),
        'customer_phone': thread.get('customer_phone', '') or thread.get('lead_data', {}).get('prospect_phone', ''),
        'event_type': thread.get('event_type', '') or thread.get('lead_data', {}).get('event_type', ''),
        'call_summary': thread.get('call_analysis', {}).get('summary', '') if source == 'voice' else '',
        'call_outcome': thread.get('call_analysis', {}).get('outcome', '') if source == 'voice' else '',
        'created_at': thread.get('created_at', '') or (thread.get('messages', [{}])[0].get('timestamp', '') if thread.get('messages') else '')
    }


def check_message_feedback(thread_id, message_idx):
    """Check if a message already has feedback."""
    if not FEEDBACK_DIR.exists():
        return False
    
    for fb_file in FEEDBACK_DIR.glob('fb_*.json'):
        try:
            with open(fb_file) as f:
                fb = json.load(f)
            if fb.get('thread_id') == thread_id and fb.get('message_idx') == message_idx:
                return True
        except Exception:
            continue
    return False


def create_feedback(data):
    """Create a new feedback entry."""
    feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # Enrich context with channel info from thread if not provided
    context = data.get('context', {})
    thread_id = data.get('thread_id', '')
    
    if thread_id and not context.get('channel'):
        thread = get_thread_by_id(thread_id)
        if thread:
            formatted = format_thread_for_display(thread)
            context['channel'] = formatted['source']
            context['subsource'] = formatted.get('subsource', '')
            if not context.get('customer_name'):
                context['customer_name'] = formatted['customer_name']
            if not context.get('event_type') and formatted.get('event_type'):
                context['event_type'] = formatted['event_type']
    
    feedback = {
        'feedback_id': feedback_id,
        'thread_id': thread_id,
        'message_idx': data.get('message_idx', 0),
        'timestamp': timestamp,
        'reviewer': data.get('reviewer', 'Unknown'),
        
        'original': {
            'role': data.get('original_role', 'agent'),
            'text': data.get('original_text', ''),
            'agent': data.get('agent', 'Mary')
        },
        
        'rating': data.get('rating', 'good'),
        'corrected_response': data.get('corrected_response', ''),
        'coaching_note': data.get('coaching_note', ''),
        'failure_tags': data.get('failure_tags', []),
        
        'context': context,
        
        'status': 'pending_review',
        'applied_to': None,
        'created_by': 'human',
        'created_at': timestamp
    }
    
    # Save to file
    fb_path = FEEDBACK_DIR / f"{feedback_id}.json"
    with open(fb_path, 'w') as f:
        json.dump(feedback, f, indent=2)
    
    return feedback


def list_feedback(status=None, thread_id=None):
    """List feedback entries with optional filters."""
    feedbacks = []
    
    if not FEEDBACK_DIR.exists():
        return feedbacks
    
    for fb_file in FEEDBACK_DIR.glob('fb_*.json'):
        try:
            with open(fb_file) as f:
                fb = json.load(f)
            
            if status and fb.get('status') != status:
                continue
            if thread_id and fb.get('thread_id') != thread_id:
                continue
            
            feedbacks.append(fb)
        except Exception:
            continue
    
    # Sort by timestamp descending
    feedbacks.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return feedbacks


def approve_feedback(feedback_id):
    """Approve feedback and add to training example bank."""
    fb_path = FEEDBACK_DIR / f"{feedback_id}.json"
    
    if not fb_path.exists():
        return None
    
    with open(fb_path) as f:
        feedback = json.load(f)
    
    feedback['status'] = 'approved'
    feedback['approved_at'] = datetime.now(timezone.utc).isoformat()
    
    with open(fb_path, 'w') as f:
        json.dump(feedback, f, indent=2)
    
    # If it has a corrected response, add to example bank
    if feedback.get('corrected_response') and feedback.get('rating') in ('needs_improvement', 'wrong'):
        add_to_example_bank(feedback)
    
    return feedback


def add_to_example_bank(feedback):
    """Add approved feedback to training example bank."""
    example_id = f"ex_{uuid.uuid4().hex[:12]}"
    
    ctx = feedback.get('context', {})
    channel = ctx.get('channel', 'email')
    subsource = ctx.get('subsource', '')
    event_type = ctx.get('event_type', '')
    
    example = {
        'example_id': example_id,
        'feedback_id': feedback.get('feedback_id'),
        'scenario': 'agent_response_correction',
        'trigger_conditions': {
            'event_type': event_type,
            'channel': channel,
            'subsource': subsource,
            'failure_tags': feedback.get('failure_tags', [])
        },
        'input': {
            'customer_name': ctx.get('customer_name', ''),
            'event_type': event_type,
            'channel': channel,
            'original_response': feedback.get('original', {}).get('text', '')
        },
        'preferred_response': feedback.get('corrected_response', ''),
        'why_better': feedback.get('coaching_note', ''),
        'failure_tags': feedback.get('failure_tags', []),
        'source': 'human_correction',
        'approved_by': feedback.get('reviewer', 'Unknown'),
        'approved_at': feedback.get('approved_at', ''),
        'active': True
    }
    
    # Append to examples.jsonl
    examples_path = TRAINING_DIR / 'examples.jsonl'
    with open(examples_path, 'a') as f:
        f.write(json.dumps(example) + '\n')
    
    return example


def get_training_examples():
    """Load all approved training examples."""
    examples = []
    examples_path = TRAINING_DIR / 'examples.jsonl'
    
    if not examples_path.exists():
        return examples
    
    with open(examples_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    examples.append(json.loads(line))
                except Exception:
                    continue
    
    return examples


class FeedbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for feedback API routes."""
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        
        try:
            if path == '/api/threads':
                self.handle_list_threads(params)
            elif path.startswith('/api/threads/') and path != '/api/threads/':
                thread_id = path.split('/api/threads/')[1]
                self.handle_get_thread(thread_id)
            elif path == '/api/feedback':
                self.handle_list_feedback(params)
            elif path.startswith('/api/feedback/') and not path.endswith('/approve'):
                feedback_id = path.split('/api/feedback/')[1]
                self.handle_get_feedback(feedback_id)
            elif path == '/api/training/examples':
                self.handle_list_examples(params)
            elif path == '/api/health':
                self.handle_health()
            elif path == '/api/tools/training_examples':
                self.handle_vapi_training_tool()
            else:
                self.send_error(404)
        except Exception as e:
            self.send_json_response({'error': str(e)}, 500)
    
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        try:
            # Only parse body for routes that need it
            data = {}
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    data = {}
            
            if path == '/api/feedback':
                self.handle_create_feedback(data)
            elif path.startswith('/api/feedback/') and path.endswith('/approve'):
                feedback_id = path.split('/api/feedback/')[1].replace('/approve', '')
                self.handle_approve_feedback(feedback_id)
            else:
                self.send_error(404)
        except Exception as e:
            self.send_json_response({'error': str(e)}, 500)
    
    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        try:
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            data = json.loads(body.decode('utf-8'))
            
            if path.startswith('/api/feedback/'):
                feedback_id = path.split('/api/feedback/')[1]
                self.handle_update_feedback(feedback_id, data)
            else:
                self.send_error(404)
        except Exception as e:
            self.send_json_response({'error': str(e)}, 500)
    
    def handle_list_threads(self, params):
        """List all threads with optional filters."""
        threads = get_all_threads()
        
        # Format for display
        result = []
        for t in threads:
            formatted = format_thread_for_display(t)
            
            # Check if needs review (has agent messages without feedback)
            has_agent_msg = any(m['role'] == 'agent' for m in formatted['messages'])
            has_feedback = any(m['has_feedback'] for m in formatted['messages'])
            needs_review = has_agent_msg and not has_feedback
            
            result.append({
                'thread_id': formatted['thread_id'],
                'source': formatted['source'],
                'subsource': formatted.get('subsource', ''),
                'customer_name': formatted['customer_name'],
                'customer_phone': formatted.get('customer_phone', ''),
                'status': formatted['status'],
                'message_count': len(formatted['messages']),
                'needs_review': needs_review,
                'created_at': formatted['created_at'],
                'event_type': formatted.get('event_type', ''),
                'call_summary': formatted.get('call_summary', ''),
                'call_outcome': formatted.get('call_outcome', '')
            })
        
        # Filter by needs_review if requested
        if params.get('needs_review'):
            result = [t for t in result if t['needs_review']]
        
        # Filter by source if requested
        if params.get('source'):
            result = [t for t in result if t['source'] == params['source'][0]]
        
        # Filter by subsource if requested
        if params.get('subsource'):
            result = [t for t in result if t.get('subsource', '') == params['subsource'][0]]
        
        # Sort by created_at descending (handle None values)
        def _sort_key(x):
            ts = x.get('created_at', '') or ''
            return ts if ts else ''
        result.sort(key=_sort_key, reverse=True)
        
        self.send_json_response({'threads': result, 'count': len(result)})
    
    def handle_get_thread(self, thread_id):
        """Get a specific thread with full details."""
        thread = get_thread_by_id(thread_id)
        
        if not thread:
            self.send_json_response({'error': 'Thread not found'}, 404)
            return
        
        formatted = format_thread_for_display(thread)
        self.send_json_response(formatted)
    
    def handle_create_feedback(self, data):
        """Create new feedback entry."""
        feedback = create_feedback(data)
        self.send_json_response(feedback, 201)
    
    def handle_list_feedback(self, params):
        """List feedback entries."""
        status = params.get('status', [None])[0]
        thread_id = params.get('thread_id', [None])[0]
        
        feedbacks = list_feedback(status=status, thread_id=thread_id)
        self.send_json_response({'feedback': feedbacks})
    
    def handle_get_feedback(self, feedback_id):
        """Get a specific feedback entry."""
        fb_path = FEEDBACK_DIR / f"{feedback_id}.json"
        
        if not fb_path.exists():
            self.send_json_response({'error': 'Feedback not found'}, 404)
            return
        
        with open(fb_path) as f:
            feedback = json.load(f)
        
        self.send_json_response(feedback)
    
    def handle_approve_feedback(self, feedback_id):
        """Approve feedback and add to example bank."""
        feedback = approve_feedback(feedback_id)
        
        if not feedback:
            self.send_json_response({'error': 'Feedback not found'}, 404)
            return
        
        self.send_json_response(feedback)
    
    def handle_update_feedback(self, feedback_id, data):
        """Update existing feedback."""
        fb_path = FEEDBACK_DIR / f"{feedback_id}.json"
        
        if not fb_path.exists():
            self.send_json_response({'error': 'Feedback not found'}, 404)
            return
        
        with open(fb_path) as f:
            feedback = json.load(f)
        
        # Update fields
        for key in ['rating', 'corrected_response', 'coaching_note', 'failure_tags']:
            if key in data:
                feedback[key] = data[key]
        
        feedback['updated_at'] = datetime.now(timezone.utc).isoformat()
        
        with open(fb_path, 'w') as f:
            json.dump(feedback, f, indent=2)
        
        self.send_json_response(feedback)
    
    def handle_list_examples(self, params):
        """List training examples, optionally filtered by event_type and channel."""
        examples = get_training_examples()
        
        # Filter by event_type if provided
        if params.get('event_type'):
            et = params.get('event_type')[0].lower()
            examples = [e for e in examples
                        if str(e.get('trigger_conditions', {}).get('event_type', '')).lower() == et]
        
        # Filter by channel if provided
        if params.get('channel'):
            ch = params.get('channel')[0].lower()
            examples = [e for e in examples
                        if str(e.get('trigger_conditions', {}).get('channel', '')).lower() == ch]
        
        # Filter by active status
        if params.get('active'):
            examples = [e for e in examples if e.get('active', True)]
        
        self.send_json_response({'examples': examples, 'count': len(examples)})
    
    def handle_health(self):
        """Health check with channel coverage breakdown."""
        threads = get_all_threads()
        
        # Count by source
        source_counts = {}
        subsource_counts = {}
        needs_review_count = 0
        
        for t in threads:
            formatted = format_thread_for_display(t)
            src = formatted['source']
            sub = formatted.get('subsource', '')
            source_counts[src] = source_counts.get(src, 0) + 1
            if sub:
                subsource_counts[sub] = subsource_counts.get(sub, 0) + 1
            
            has_agent_msg = any(m['role'] == 'agent' for m in formatted['messages'])
            has_feedback = any(m['has_feedback'] for m in formatted['messages'])
            if has_agent_msg and not has_feedback:
                needs_review_count += 1
        
        # Count feedback
        total_feedback = 0
        pending_feedback = 0
        if FEEDBACK_DIR.exists():
            for f in FEEDBACK_DIR.glob('fb_*.json'):
                total_feedback += 1
                try:
                    with open(f) as fh:
                        fb = json.load(fh)
                    if fb.get('status') == 'pending_review':
                        pending_feedback += 1
                except Exception:
                    continue
        
        # Count training examples
        total_examples = len(get_training_examples())
        
        self.send_json_response({
            'status': 'healthy',
            'threads_total': len(threads),
            'threads_by_source': source_counts,
            'threads_by_subsource': subsource_counts,
            'threads_needs_review': needs_review_count,
            'feedback_total': total_feedback,
            'feedback_pending': pending_feedback,
            'training_examples': total_examples,
            'channels': ['email', 'sms', 'voice'],
            'email_subsources': ['ghl', 'platform_ww', 'platform_tk', 'platform_zola', 'platform_gmail', 'direct_email'],
            'sms_subsources': ['inbound_sms', 'post_call_sms'],
            'voice_subsources': ['vapi_call']
        })
    
    def handle_vapi_training_tool(self):
        """Vapi tool endpoint for get_training_examples.
        
        Called by Mary during voice calls to retrieve approved corrections.
        Returns examples in a format suitable for injection into the LLM context.
        
        Vapi tool definition:
        {
            "type": "http",
            "server": {
                "url": "https://webhooks.bell34hooks.com/api/tools/training_examples"
            },
            "name": "get_training_examples",
            "description": "Retrieve approved response corrections for the current scenario. Use before responding to improve response quality based on human feedback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "The communication channel: voice, email, or sms"},
                    "event_type": {"type": "string", "description": "The event type if known: bridal, corporate, social, etc."}
                }
            }
        }
        """
        # Parse Vapi tool call format
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                data = json.loads(body.decode('utf-8'))
            else:
                data = {}
        except Exception:
            data = {}
        
        # Extract parameters from Vapi's tool call format
        # Vapi sends: {"toolCallId": "...", "name": "get_training_examples", "arguments": {...}}
        args = data.get('arguments', data.get('args', {}))
        channel = args.get('channel', 'voice')
        event_type = args.get('event_type', None)
        
        # Fetch examples
        examples = get_training_examples()
        
        # Filter by channel
        if channel:
            examples = [e for e in examples 
                        if str(e.get('trigger_conditions', {}).get('channel', '')).lower() == channel.lower()]
        
        # Filter by event_type
        if event_type:
            et_lower = event_type.lower()
            examples = [e for e in examples 
                        if str(e.get('trigger_conditions', {}).get('event_type', '')).lower() in (et_lower, '', 'unknown')]
        
        # Limit to top 3
        examples = examples[:3]
        
        # Format for Vapi
        if examples:
            formatted = []
            for ex in examples:
                formatted.append({
                    'preferred_response': ex.get('preferred_response', ''),
                    'why_better': ex.get('why_better', ''),
                    'failure_tags': ex.get('trigger_conditions', {}).get('failure_tags', [])
                })
            result = {
                'status': 'success',
                'count': len(formatted),
                'examples': formatted,
                'instruction': 'Learn from these approved corrections. Apply the style and approach to your response.'
            }
        else:
            result = {
                'status': 'success',
                'count': 0,
                'examples': [],
                'instruction': 'No approved corrections for this scenario. Respond normally.'
            }
        
        self.send_json_response(result)
    
    def send_json_response(self, data, status=200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())
    
    def log_message(self, format, *args):
        """Suppress logging."""
        pass


def run_server(port=3980):
    """Run the feedback API server."""
    server = HTTPServer(('0.0.0.0', port), FeedbackHandler)
    print(f"Feedback API server running on port {port}")
    server.serve_forever()


if __name__ == '__main__':
    run_server()