#!/usr/bin/env python3
"""Local API server for Leads Report — serves /api/leads on port 3979"""
import json, os, re, subprocess, urllib.parse, base64, sys
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CT = timezone(timedelta(hours=-5))
THREADS_DIR = Path('/home/ubuntu/.hermes/agents/mary/threads')
SMS_THREADS_DIR = Path('/home/ubuntu/.hermes/sms-threads')
VAPI_BUILD = '/home/ubuntu/.hermes/vapi-build'

def curl_json(url, headers=None, data=None, method='GET', timeout=15):
    """Use curl subprocess for HTTP requests (GHL blocks Python urllib)"""
    cmd = ['curl', '-s', '-X', method, url, '--max-time', str(timeout)]
    if headers:
        for k, v in headers.items():
            cmd.extend(['-H', f'{k}: {v}'])
    if data:
        cmd.extend(['-d', data])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    try:
        return json.loads(result.stdout)
    except:
        return {}

def load_env():
    result = subprocess.run(
        ["bash", "-c", "set -a; source /home/ubuntu/.hermes/secrets/1password.env 2>/dev/null; env"],
        capture_output=True, text=True
    )
    env = {}
    for line in result.stdout.strip().split('\n'):
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env

def parse_date_range(start_str, end_str):
    start_ct = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=CT)
    end_ct = datetime.strptime(end_str, '%Y-%m-%d').replace(tzinfo=CT)
    end_ct = end_ct.replace(hour=23, minute=59, second=59)
    return start_ct, end_ct

def fmt_phone(phone):
    if not phone:
        return ''
    digits = re.sub(r'\D', '', str(phone))
    if len(digits) == 11 and digits.startswith('1'):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    elif len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return str(phone)

def get_thread_enrichment():
    """Scan thread files once. Returns (email_idx, platform_name_idx).
    email_idx: prospect-email-lower -> thread record
    platform_name_idx: (platform, name-lower) -> thread record
    Each record: {'thread': dict, 'replied': bool, 'first_out': ts}
    replied = inbound message exists AFTER our first outbound (prospect actually replied)."""
    email_idx, name_idx = {}, {}
    if not THREADS_DIR.exists():
        return email_idx, name_idx
    for p in THREADS_DIR.glob('*.json'):
        try:
            with open(p) as f:
                t = json.load(f)
        except Exception:
            continue
        if not isinstance(t, dict):
            continue
        ld = t.get('lead_data') or {}
        platform = (ld.get('platform') or t.get('platform_source') or '').lower()
        msgs = t.get('messages') or []
        first_out = None
        for m in msgs:
            if isinstance(m, dict) and m.get('direction') == 'outbound':
                first_out = m.get('timestamp') or first_out
                if first_out:
                    break
        replied = False
        if first_out:
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get('direction') == 'inbound' and m.get('timestamp', '') > first_out:
                    replied = True
                    break
        rec = {'thread': t, 'replied': replied, 'first_out': first_out}
        for m in msgs:
            if not isinstance(m, dict):
                continue
            for k in ('from', 'to'):
                e = (m.get(k) or '').strip().lower()
                if e and '@' in e and 'belltoweron34th' not in e and 'planning@' not in e:
                    email_idx.setdefault(e, rec)
        name = ld.get('prospect_name') or t.get('first_name') or ''
        if name and platform:
            name_idx.setdefault((platform, name.lower()), rec)
    return email_idx, name_idx


def get_sms_thread_enrichment():
    """Scan SMS thread files. Returns phone_idx: E164 -> thread record.
    Each record: {'thread': dict, 'replied': bool, 'first_out': ts, 'name': str, 'sms_sent': int, 'sms_replies': int}.
    replied = inbound message exists AFTER our first outbound."""
    phone_idx = {}
    if not SMS_THREADS_DIR.exists():
        return phone_idx
    for p in SMS_THREADS_DIR.glob('*.json'):
        try:
            with open(p) as f:
                t = json.load(f)
        except Exception:
            continue
        if not isinstance(t, dict):
            continue
        phone = t.get('phone', '')
        if not phone:
            phone = p.stem
        msgs = t.get('messages') or []
        first_out = None
        outbound_count = 0
        inbound_count = 0
        for m in msgs:
            if not isinstance(m, dict):
                continue
            d = m.get('direction', '')
            if d == 'outbound':
                outbound_count += 1
                if not first_out:
                    first_out = m.get('timestamp')
            elif d == 'inbound':
                inbound_count += 1
        replied = False
        if first_out:
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get('direction') == 'inbound' and (m.get('timestamp') or '') > first_out:
                    replied = True
                    break
        name = t.get('first_name', '') or ''
        rec = {
            'thread': t,
            'replied': replied,
            'first_out': first_out,
            'name': name,
            'phone': phone,
            'sms_sent': outbound_count,
            'sms_replies': inbound_count,
        }
        phone_digits = re.sub(r'\D', '', phone)
        if phone_digits:
            phone_idx[phone_digits] = rec
    return phone_idx

def extract_ghl_inquiry(thread):
    """Parse GHL webform substance from the thread's first message.
    Format: 'Name: X | Event Type: Y | Preferred Date: Z | Notes: N'"""
    msgs = thread.get('messages') or []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        text = m.get('text') or m.get('body') or ''
        if 'Event Type:' in text or 'Preferred Date:' in text:
            parts = [p.strip() for p in text.split('|') if p.strip()]
            # Drop the leading 'Name: ...' part if present (already in Name column)
            out = []
            for p in parts:
                if p.lower().startswith('name:'):
                    continue
                out.append(p)
            return ' | '.join(out)
    return ''

def get_reminder_stats(start_ct, end_ct):
    """Slot Open Reminders for the window.
    scheduled = waitlist sheet rows created in window (each -> Created row)
    sent = per-send events from poller log (each -> Sent row, enriched with
           preferred days from sheet lookup by phone/email)
    rows = scheduled rows + sent rows (list length always = scheduled + sent)."""
    stats = {'scheduled': 0, 'sent': 0, 'rows': []}
    # ── Load full waitlist for lookups ──
    sheet_rows = []
    try:
        sys.path.insert(0, VAPI_BUILD + '/lib')
        from google_sheets_oauth import sheets_get
        with open(VAPI_BUILD + '/waitlist_sheet_meta.json') as f:
            sheet_id = json.load(f)['sheet_id']
        sheet_rows = sheets_get(sheet_id, 'Waitlist Prospects')
    except Exception as e:
        stats['sheet_error'] = str(e)

    # Build lookup: phone_digits -> preferred_days, email_lower -> preferred_days
    pref_by_phone, pref_by_email = {}, {}
    for r in sheet_rows[1:]:
        if not r or not r[0]:
            continue
        # Handle TSV-blob rows (gog append bug: entire row in one tab-separated cell)
        if len(r) == 1 and '\t' in str(r[0]):
            parts = str(r[0]).split('\t')
        else:
            parts = r
        # Pad to 12 columns
        parts = list(parts) + [''] * max(0, 12 - len(parts))
        phone = parts[2] if len(parts) > 2 else ''
        email = parts[3] if len(parts) > 3 else ''
        pref_days = parts[5] if len(parts) > 5 else ''
        pref_weekday = parts[6] if len(parts) > 6 else ''
        pref_combined = pref_days or pref_weekday
        if phone:
            digits = re.sub(r'\D', '', phone)
            if digits:
                pref_by_phone[digits] = pref_combined
        if email:
            pref_by_email[email.strip().lower()] = pref_combined

    # ── Scheduled: waitlist sheet rows created in window ──
    for r in sheet_rows[1:]:
        if not r or not r[0]:
            continue
        # Handle TSV-blob rows
        if len(r) == 1 and '\t' in str(r[0]):
            parts = str(r[0]).split('\t')
        else:
            parts = r
        ts_raw = parts[0] if parts else ''
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace('Z', '+00:00'))
            ts_ct = ts.astimezone(CT)
        except Exception:
            try:
                ts = datetime.strptime(str(ts_raw)[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=CT)
                ts_ct = ts
            except Exception:
                continue
        if not (start_ct <= ts_ct <= end_ct):
            continue
        stats['scheduled'] += 1
        parts = list(parts) + [''] * max(0, 12 - len(parts))
        name = parts[1] if len(parts) > 1 else ''
        phone = parts[2] if len(parts) > 2 else ''
        pref = parts[5] if len(parts) > 5 else ''
        pref_weekday = parts[6] if len(parts) > 6 else ''
        stats['rows'].append({
            'name': name,
            'phone': phone,
            'prospect': name or phone,
            'preferred': pref or pref_weekday,
            'status': 'Created',
            'timestamp': ts_ct.isoformat(),
        })
    # ── Sent: per-send lines from poller log (log timestamps are CT) ──
    log_path = Path(VAPI_BUILD + '/logs/slot-open-poller.log')
    if log_path.exists():
        for line in log_path.read_text(errors='ignore').splitlines():
            m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=CT)
            except Exception:
                continue
            if not (start_ct <= ts <= end_ct):
                continue
            # SMS send (ground truth: actual delivery line)
            if 'Twilio SMS SUCCESS' in line:
                m_to = re.search(r'to=(\+?\d+)', line)
                m_name = re.search(r'name=([^\s]+)', line)
                phone = m_to.group(1) if m_to else ''
                name = m_name.group(1) if m_name else ''
                # Look up preferred days from sheet by phone
                digits = re.sub(r'\D', '', phone)
                pref = pref_by_phone.get(digits, '')
                stats['sent'] += 1
                stats['rows'].append({
                    'name': name, 'phone': phone,
                    'prospect': name or phone,
                    'preferred': pref,
                    'status': 'Sent',
                    'timestamp': ts.isoformat(),
                })
            # Email send
            elif '[SENT] email to' in line:
                m_email = re.search(r'email to ([^\s]+)', line)
                email = m_email.group(1) if m_email else ''
                # Look up preferred days from sheet by email
                pref = pref_by_email.get(email.strip().lower(), '')
                stats['sent'] += 1
                stats['rows'].append({
                    'name': '', 'phone': '',
                    'prospect': email,
                    'preferred': pref,
                    'status': 'Sent',
                    'timestamp': ts.isoformat(),
                })
    # Sort chronological
    stats['rows'].sort(key=lambda x: str(x.get('timestamp', '')))
    return stats

def get_ghl_leads(env, start_ct, end_ct, email_idx=None):
    email_idx = email_idx or {}
    token = env.get('GHL_PRIVATE_TOKEN', '')
    location_id = env.get('GHL_LOCATION_ID', '')
    if not token or not location_id:
        return []
    leads = []
    page = 0
    while page < 50:
        body = json.dumps({"locationId": location_id, "pageLimit": 100, "page": page})
        data = curl_json(
            "https://services.leadconnectorhq.com/contacts/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Version": "2021-07-28"
            },
            data=body,
            method="POST"
        )
        contacts = data.get('contacts', [])
        if not contacts:
            break
        stop = False
        for c in contacts:
            da = c.get('dateAdded', '')
            if not da:
                continue
            try:
                ts = datetime.fromisoformat(da.replace('Z', '+00:00'))
                ts_ct = ts.astimezone(CT)
                if start_ct <= ts_ct <= end_ct:
                    tags = c.get('tags', [])
                    is_bridal = any('bridal' in t.lower() and 'non bridal' not in t.lower() for t in tags)
                    is_non_bridal = any('non bridal' in t.lower() for t in tags)
                    has_contact = bool(c.get('email') or c.get('phone'))
                    if is_non_bridal and not is_bridal:
                        lead_type = 'Non-Bridal'
                    elif is_bridal:
                        lead_type = 'Bridal'
                    else:
                        lead_type = 'Lead'
                    if not c.get('email') and not c.get('phone'):
                        status = 'spam'
                        notes = 'No email/phone/source — incomplete submission'
                    else:
                        status = 'responded'
                        notes = 'First-touch sent (email + SMS), awaiting reply'
                    # Inquiry substance + replied flag from thread files
                    inquiry = ''
                    replied = False
                    if c.get('email'):
                        rec = email_idx.get(c['email'].strip().lower())
                        if rec:
                            inquiry = extract_ghl_inquiry(rec['thread'])
                            replied = rec['replied']
                    leads.append({
                        'name': f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                        'channel': 'GHL Webform',
                        'phone': c.get('phone', ''),
                        'email': c.get('email', ''),
                        'timestamp': da,
                        'type': lead_type if has_contact else 'Spam',
                        'status': status,
                        'notes': notes,
                        'source': c.get('source', ''),
                        'inquiry': inquiry,
                        'prospectReplied': replied,
                    })
                elif ts_ct < start_ct:
                    stop = True
            except:
                pass
        if stop:
            break
        page += 1
    
    # Merge in GHL leads from the webhook log that don't appear in the API results
    # (existing contacts re-submitting the webform keep their original dateAdded,
    # so they don't show up in the date-filtered API search).
    seen_emails = {l.get('email', '').lower() for l in leads if l.get('email')}
    seen_phones = {l.get('phone', '') for l in leads if l.get('phone')}
    webhook_log = "/home/ubuntu/.hermes/vapi-build/logs/vapi-webhook-stdout.log"
    try:
        for line in open(webhook_log, errors='ignore'):
            if '[GHL-FORM] name=' not in line:
                continue
            # Extract the date from the log line (format: 2026-08-06 ...)
            # Log lines start with YYYY-MM-DD HH:MM:SS
            line_date = line[:10]  # First 10 chars are the date
            try:
                line_dt = datetime.strptime(line_date, "%Y-%m-%d").replace(tzinfo=CT)
                # Skip if this line's date is outside the requested range
                if not (start_ct <= line_dt <= end_ct):
                    continue
            except:
                continue
            # Parse: name=X | email=Y | phone=Z | event_type=W | notes=...
            parts = line.split('[GHL-FORM] name=')[1]
            name = parts.split(' | email=')[0].strip() if ' | email=' in parts else ''
            email = parts.split(' | email=')[1].split(' | phone=')[0].strip() if ' | email=' in parts and ' | phone=' in parts else ''
            phone = parts.split(' | phone=')[1].split(' | event_type=')[0].strip() if ' | phone=' in parts and ' | event_type=' in parts else ''
            event_type = parts.split(' | event_type=')[1].split(' | notes=')[0].strip() if ' | event_type=' in parts else ''
            notes = parts.split(' | notes=')[1].strip() if ' | notes=' in parts else ''
            
            if email.lower() in seen_emails or phone in seen_phones:
                continue  # already in API results
            seen_emails.add(email.lower())
            seen_phones.add(phone)
            
            is_bridal = 'bridal' in event_type.lower() and 'non bridal' not in event_type.lower()
            is_non_bridal = 'non bridal' in event_type.lower()
            if is_non_bridal:
                lead_type = 'Non-Bridal'
            elif is_bridal:
                lead_type = 'Bridal'
            else:
                lead_type = 'Lead'
            
            leads.append({
                'name': name,
                'channel': 'GHL Webform',
                'phone': phone,
                'email': email,
                'timestamp': line_dt.isoformat(),
                'type': lead_type,
                'status': 'responded',
                'notes': 'First-touch sent (email + SMS), awaiting reply',
                'source': 'GHL Webform',
                'inquiry': notes[:300],
                'prospectReplied': False,
            })
    except Exception:
        pass
    
    return leads

def get_platform_leads(env, start_ct, end_ct, name_idx=None):
    name_idx = name_idx or {}
    log_path = "/home/ubuntu/.hermes/vapi-build/logs/zola_webhook.log"
    leads = []
    if not os.path.exists(log_path):
        return leads
    with open(log_path) as f:
        for line in f:
            if 'Parsed lead platform=' not in line:
                continue
            try:
                ts_str = line[:19]
                today_str = start_ct.strftime("%Y-%m-%d")
                ts = datetime.strptime(today_str + " " + ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CT)
                ts_ct = ts
                if not (start_ct <= ts_ct <= end_ct):
                    continue
                parts = line.split('platform=')[1].split(' ')[0]
                platform = 'WeddingWire' if 'weddingwire' in parts.lower() else \
                           'TheKnot' if 'theknot' in parts.lower() else \
                           'Zola' if 'zola' in parts.lower() else parts.capitalize()
                name = line.split('name=')[1].split(' date=')[0].strip() if 'name=' in line else ''
                event_date = line.split('date=')[1].split(' guests=')[0].strip() if 'date=' in line else ''
                guests = line.split('guests=')[1].split(' path=')[0].strip() if 'guests=' in line else ''
                # Inquiry substance + replied flag from thread files
                inquiry = ''
                replied = False
                budget = ''
                # Extract budget from raw_email_excerpt
                raw_excerpt = ''
                lead_file_path = line.split('path=')[1].strip() if 'path=' in line else ''
                if lead_file_path and os.path.exists(lead_file_path):
                    try:
                        with open(lead_file_path) as lf:
                            ld = json.load(lf)
                        raw_excerpt = ld.get('raw_email_excerpt', '') or ''
                        # Try to extract budget
                        bm = re.search(r'[Bb]udget\s*:?\s*(\$[\d,\s\-]+(?:less\s+than|up\s+to)?[^;\n\r]{0,40})', raw_excerpt)
                        if bm:
                            budget = bm.group(1).strip()
                        else:
                            bm2 = re.search(r'(?:Less\s+than|Up\s+to)\s+\$[\d,]+', raw_excerpt, re.I)
                            if bm2:
                                budget = bm2.group(0).strip()
                    except:
                        pass
                if name:
                    rec = name_idx.get((platform.lower(), name.lower()))
                    if rec:
                        ld = rec['thread'].get('lead_data') or {}
                        pm = ld.get('prospect_message', '') or ''
                        if pm:
                            inquiry = pm[:300]
                        replied = rec['replied']
                        # Response speed: lead timestamp → first outbound
                        first_out = rec.get('first_out')
                        if first_out:
                            try:
                                delta = (datetime.fromisoformat(first_out.replace('Z','+00:00')) - ts).total_seconds()
                                if delta < 0:
                                    delta = 0  # clamp negative (second notification before first outbound)
                                if delta < 60:
                                    response_speed = f"{int(delta)}s"
                                elif delta < 3600:
                                    response_speed = f"{int(delta/60)}m"
                                else:
                                    response_speed = f"{delta/3600:.1f}h"
                            except:
                                response_speed = "—"
                        else:
                            response_speed = "—"
                    else:
                        response_speed = "—"
                else:
                    response_speed = "—"
                # Build event details with budget
                event_details = f"{event_date}"
                if guests:
                    event_details += f" · {guests} guests"
                if budget:
                    event_details += f" · Budget: {budget}"
                leads.append({
                    'name': name,
                    'channel': platform,
                    'phone': '', 'email': '',
                    'timestamp': ts.isoformat(),
                    'type': 'Bridal',
                    'status': 'responded',
                    'notes': 'Platform reply sent via planning@',
                    'eventDetails': event_details,
                    'inquiry': inquiry,
                    'prospectReplied': replied,
                    'responseSpeed': response_speed,
                })
            except:
                pass
    return leads

def extract_name_from_transcript(transcript):
    """Extract caller name from VAPI transcript — Mary asks 'Who do I have the pleasure of speaking with?'"""
    if not transcript:
        return ''
    transcript = transcript.strip('"')
    lines = transcript.split('\n')
    skip_words = {'right', 'yes', 'no', 'hi', 'hello', 'yeah', 'sure', 'ok', 'okay', '4', 
                  'hi there', 'hey', 'um', 'uh', 'hello?', "hi. mary.", "hi, mary.", "hi mary.",
                  "mary.", "mary",
                  # Day-of-week answers to tour time questions (not names)
                  'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
                  'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun',
                  # Time answers
                  'am', 'pm', 'morning', 'afternoon', 'evening',
                  # Common false positives
                  'saturday', 'here', 'speaking'}
    for i, line in enumerate(lines):
        ll = line.lower().strip()
        if ('pleasure of speaking with' in ll or 
            'what should i call you' in ll or
            "what's your name" in ll or
            'can i get your name' in ll or
            'who am i speaking' in ll or
            'who do i have' in ll or
            'could you please provide your name' in ll or
            'could you please provide me with your' in ll or
            '1st and last name' in ll or
            'first and last name' in ll or
            'who am i talking' in ll or
            'may i ask who' in ll or
            'may i have your name' in ll or
            'can i ask who' in ll):
            for j in range(i+1, min(i+6, len(lines))):
                next_line = lines[j].strip()
                if not next_line.startswith('User:'):
                    continue
                response = next_line[5:].strip()
                if not response or response.lower() in skip_words:
                    continue
                rl = response.lower()
                name = ''
                if "my name is " in rl:
                    name = response[rl.index("my name is ")+11:].strip()
                elif "it's " in rl:
                    name = response[rl.index("it's ")+5:].strip()
                elif "this is " in rl:
                    name = response[rl.index("this is ")+8:].strip()
                elif "i'm " in rl:
                    name = response[rl.index("i'm ")+3:].strip()
                elif "it is " in rl:
                    name = response[rl.index("it is ")+6:].strip()
                elif "name is " in rl:
                    name = response[rl.index("name is ")+8:].strip()
                else:
                    name = response
                for sep in [',', '.', ' am i', ' am', ' can i', ' can', ' i was', " i'm", 
                           ' how', ' i need', ' i have', ' i would', ' i was', ' speaking',
                           ' here']:
                    if sep in name.lower():
                        idx = name.lower().index(sep)
                        name = name[:idx]
                name = name.strip().rstrip('.').strip()
                if name.lower() in skip_words or len(name) < 2 or len(name) > 50:
                    continue
                # Filter out pure time/day answers (e.g., "Saturday", "Late next week")
                name_lower = name.lower()
                if name_lower in skip_words:
                    continue
                sentence_starts = ['i was ', 'i am ', "i'm ", 'do you ', 'can you ', 'how much', 
                                  'is there ', 'are you ', 'what ', 'when ', 'where ', 'why ',
                                  'i need', 'i have', 'i would', 'we ', 'yeah', 'yes ', 'no ',
                                  'hi ', 'hey ', 'hello', 'um ', 'uh ', 'transfer ', 'just ',
                                  'sorry', "i'd ", 'could ', 'late ', 'earlier ']
                if any(name_lower.startswith(s) for s in sentence_starts):
                    continue
                return name
            # Don't break — keep looking for the next name-ask in the transcript
    return ''

def analyze_call_outcome(transcript, summary, duration, ended_reason):
    """Generate substantive notes from VAPI call transcript and metadata"""
    notes = []
    tl = (transcript or '').lower()
    sl = (summary or '').lower()
    
    # Duration context
    if duration < 20:
        notes.append(f"Call {duration:.0f}s — caller hung up quickly")
    elif duration > 240:
        notes.append(f"Call {duration/60:.1f}min — extended conversation")
    else:
        notes.append(f"Call {duration:.0f}s")
    
    # What was discussed
    if 'quincea' in sl or 'quincea' in tl:
        notes.append("Quinceañera inquiry — asked about all-inclusive packages")
    elif 'tour' in tl and ('schedule' in tl or 'book' in tl or 'october' in tl):
        notes.append("Tried to book a tour — no slots available for requested date")
    elif 'speak with roger' in tl or 'speak with the general manager' in tl or 'speak with the manager' in tl:
        notes.append("Asked to speak with staff/management")
    elif 'accounting' in tl:
        notes.append("Accounting department request")
    elif 'contract' in tl or 'change' in tl:
        notes.append("Contract changes discussion")
    elif 'availability' in tl and 'tour' in tl:
        notes.append("Asked about tour availability")
    elif 'pricing' in tl or 'price' in tl or 'cost' in tl:
        notes.append("Pricing inquiry")
    elif duration < 20:
        notes.append("Caller disconnected before providing any information")
    
    # Tour offer
    if any(w in tl for w in ['weekday', 'come in for a tour', 'schedule a tour']):
        notes.append("Tour offered")
    if any(w in tl for w in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']) and 'am' in tl:
        notes.append("Specific tour times offered")
    
    # Call outcome
    if ended_reason == 'customer-ended-call':
        if duration < 20:
            notes.append("Caller hung up before engaging")
        else:
            notes.append("Caller ended the call")
    elif ended_reason == 'exceeded-max-duration':
        notes.append("Call hit max duration limit")
    
    return ' · '.join(notes)

def get_post_call_sms_status(env, phone, call_end_time):
    """Check if post-call SMS was sent and if caller responded"""
    if not phone:
        return ''
    
    sid = env.get('TWILIO_ACCOUNT_SID', '')
    token = env.get('TWILIO_AUTH_TOKEN', '')
    if not sid or not token:
        return ''
    
    import base64
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode().strip()
    
    # Get messages for this phone number
    phone_encoded = phone.replace('+', '%2B')
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json?PageSize=100&To={phone_encoded}"
    data = curl_json(url, headers={"Authorization": f"Basic {auth}"})
    
    messages = data.get('messages', [])
    if not messages:
        # Also check from this number
        url2 = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json?PageSize=100&From={phone_encoded}"
        data2 = curl_json(url2, headers={"Authorization": f"Basic {auth}"})
        messages = data2.get('messages', [])
    
    if not messages:
        return ''
    
    # Look for outbound SMS after the call ended
    outbound_after = []
    inbound_after = []
    for m in messages:
        direction = m.get('direction', '')
        date_sent = m.get('date_sent', m.get('date_created', ''))
        if not date_sent:
            continue
        try:
            from email.utils import parsedate_to_datetime
            ts = parsedate_to_datetime(date_sent)
            if 'outbound' in direction and ts > call_end_time:
                outbound_after.append(m)
            elif 'inbound' in direction and ts > call_end_time:
                inbound_after.append(m)
        except:
            pass
    
    if outbound_after and inbound_after:
        return "Post-call SMS sent + prospect replied"
    elif outbound_after:
        return "Post-call SMS sent, no reply yet"
    else:
        return "No post-call SMS sent"

def get_vapi_leads(env, start_ct, end_ct):
    vapi_key = env.get('VAPI_PRIVATE_KEY', '')
    if not vapi_key:
        return [], []
    start_utc = start_ct.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    url = f"https://api.vapi.ai/call?limit=200&createdAtGe={start_utc}"
    data = curl_json(url, headers={"Authorization": f"Bearer {vapi_key}"})
    leads, non_leads = [], []
    calls = data if isinstance(data, list) else data.get('calls', [])
    for c in calls:
        started = c.get('startedAt', '')
        if not started:
            continue
        try:
            ts = datetime.fromisoformat(started.replace('Z', '+00:00'))
            ts_ct = ts.astimezone(CT)
            if not (start_ct <= ts_ct <= end_ct):
                continue
        except:
            continue
        call_id = c.get('id', '')
        phone = c.get('customer', {}).get('number', '') if isinstance(c.get('customer'), dict) else ''
        summary = c.get('summary', '')
        transcript = c.get('transcript', '')
        ended_reason = c.get('endedReason', '')
        sl = summary.lower()
        
        # Calculate call duration
        duration = 0
        started_dt = None
        ended_dt = None
        if started:
            try:
                started_dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
            except: pass
        ended_at = c.get('endedAt', '')
        if ended_at:
            try:
                ended_dt = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
                if started_dt:
                    duration = (ended_dt - started_dt).total_seconds()
            except: pass
        
        # Extract name from transcript
        name = extract_name_from_transcript(transcript)
        if not name and 'name is' in sl:
            try: name = summary.split('name is')[1].split('.')[0].split(',')[0].strip()[:30]
            except: pass
        # Don't use "called the bell tower" pattern — it grabs the summary sentence, not a name
        if not name:
            name = fmt_phone(phone) if phone else 'Unknown Caller'
        
        # Generate substantive notes from transcript analysis
        call_notes = analyze_call_outcome(transcript, summary, duration, ended_reason)
        
        # Check post-call SMS status
        sms_status = ''
        if ended_dt and phone:
            sms_status = get_post_call_sms_status(env, phone, ended_dt)
        
        is_non_lead = False
        category, notes = '', ''
        if phone == '+17134094000':
            is_non_lead, category, notes = True, 'Roger — internal', 'Internal test call'
        elif any(w in sl for w in ['vendor', 'photo booth', 'caterer', 'dj']):
            is_non_lead, category, notes = True, 'Vendor', f"Vendor call — {summary[:80]}"
        elif 'photographer' in sl and 'engagement' not in sl and 'portrait' not in sl and 'bridal portrait' not in sl:
            # "photographer" alone might be someone IS a photographer (vendor)
            # But "engagement pictures/portrait" = prospect wanting photos AT the venue
            is_non_lead, category, notes = True, 'Vendor', f"Vendor call — {summary[:80]}"
        elif any(w in sl for w in ['speak with roger', 'speak with the general manager', 'speak with the manager']):
            is_non_lead, category, notes = True, 'Internal', f"Asked for staff — {summary[:80]}"
        elif 'reschedul' in sl or 'existing tour' in sl:
            is_non_lead, category, notes = True, 'Existing Client', f"Rescheduling — {summary[:80]}"
        elif 'returning' in sl and 'call' in sl:
            is_non_lead, category, notes = True, 'Callback', f"Returning a callback — {summary[:80]}"
        else:
            full_notes = call_notes
            if sms_status:
                full_notes += f' · {sms_status}'
            leads.append({
                'name': name,
                'channel': 'Phone Leads',
                'phone': phone, 'email': '',
                'timestamp': started,
                'type': '', 'status': 'responded',
                'notes': full_notes,
                'callId': call_id,
                'recordingUrl': f'/api/recording/{call_id}',
                'inquiry': '',
                'prospectReplied': 'prospect replied' in sms_status,
            })
            continue
        full_notes = notes
        if sms_status:
            full_notes += f' · {sms_status}'
        non_leads.append({
            'name': name if name and name != fmt_phone(phone) else category,
            'category': category,
            'channel': 'Phone Leads',
            'phone': phone,
            'timestamp': started,
            'type': '', 'status': '', 'notes': full_notes,
            'callId': call_id,
            'recordingUrl': f'/api/recording/{call_id}',
            'smsSent': 'Y' if sms_status and 'sent' in sms_status.lower() and 'no post-call' not in sms_status.lower() else 'N',
        })
    return leads, non_leads

def get_sms_conversations(env, start_ct, end_ct):
    sid = env.get('TWILIO_ACCOUNT_SID', '')
    token = env.get('TWILIO_AUTH_TOKEN', '')
    if not sid or not token:
        return {}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json?PageSize=1000"
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode().strip()
    data = curl_json(url, headers={"Authorization": f"Basic {auth}"})
    convos = {}
    bell_numbers = ['+17138682355', '+17138685335']
    for m in data.get('messages', []):
        date_sent = m.get('date_sent', m.get('date_created', ''))
        direction = m.get('direction', '')
        to, frm = m.get('to', ''), m.get('from', '')
        if not date_sent:
            continue
        try:
            ts = parsedate_to_datetime(date_sent)
            ts_ct = ts.astimezone(CT)
            if not (start_ct <= ts_ct <= end_ct):
                continue
        except:
            continue
        if to in bell_numbers: phone = frm
        elif frm in bell_numbers: phone = to
        else: phone = frm if 'outbound' not in direction else to
        phone_norm = re.sub(r'\D', '', phone) if phone else ''
        if phone_norm not in convos:
            convos[phone_norm] = {'in': 0, 'out': 0, 'first': ts_ct, 'last': ts_ct, 'first_out': None}
        if 'outbound' in direction:
            convos[phone_norm]['out'] += 1
            # Track first outbound time
            if convos[phone_norm]['first_out'] is None or ts_ct < convos[phone_norm]['first_out']:
                convos[phone_norm]['first_out'] = ts_ct
        else:
            convos[phone_norm]['in'] += 1
        if ts_ct < convos[phone_norm]['first']: convos[phone_norm]['first'] = ts_ct
        if ts_ct > convos[phone_norm]['last']: convos[phone_norm]['last'] = ts_ct
    return convos

def enrich_with_sms(leads, convos):
    # Load SMS thread enrichment once
    sms_idx = get_sms_thread_enrichment()
    
    for lead in leads:
        phone = re.sub(r'\D', '', lead.get('phone') or '')
        
        # First, try SMS thread enrichment for name and SMS counts
        if phone and phone in sms_idx:
            rec = sms_idx[phone]
            # Update name if thread has a real name and lead has a placeholder
            if rec.get('name') and (not lead.get('name') or lead.get('name') in ('Unknown Caller', 'Talk to someone', 'looking for a quote for an event')):
                lead['name'] = rec['name']
            # Set SMS counts
            lead['smsSent'] = rec.get('sms_sent', 0)
            lead['smsReplies'] = rec.get('sms_replies', 0)
            # Append SMS counts to existing notes (don't replace call notes)
            sms_note = ''
            if rec.get('sms_sent', 0) > 0:
                sms_note = f"{rec.get('sms_sent', 0)} SMS sent"
                if rec.get('sms_replies', 0) > 0:
                    sms_note += f", {rec.get('sms_replies', 0)} prospect repl{'y' if rec.get('sms_replies') == 1 else 'ies'}"
            if sms_note and lead.get('notes'):
                # Append to existing call notes
                lead['notes'] = lead['notes'].rstrip(' ·') + f' · {sms_note}'
            elif sms_note:
                lead['notes'] = sms_note
            # Mark replied if SMS thread shows inbound after outbound
            if rec.get('replied'):
                lead['prospectReplied'] = True
                lead['status'] = 'active'
        
        # Skip SMS enrichment for leads that already have a responseSpeed
        # set by their channel handler (e.g., platform leads from thread files).
        # Only calculate SMS-based speed for GHL leads with phone numbers.
        if lead.get('responseSpeed') and lead['responseSpeed'] != '—':
            # Already has speed from platform thread — just set conversation status
            if not phone or phone not in convos:
                if not lead.get('conversationStatus'):
                    lead['conversationStatus'] = '—'
            continue
        
        # Calculate response speed
        lead_ts = lead.get('timestamp', '')
        if lead_ts:
            try:
                if 'T' in lead_ts:
                    lead_dt = datetime.fromisoformat(lead_ts.replace('Z', '+00:00'))
                else:
                    lead_dt = datetime.strptime(lead_ts[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                lead_ct = lead_dt.astimezone(CT)
                
                if phone in convos:
                    c = convos[phone]
                    # Response speed: time from lead to first outbound
                    first_out = c.get('first_out')
                    if first_out:
                        delta = (first_out - lead_ct).total_seconds()
                        if delta < 60:
                            lead['responseSpeed'] = f"{int(delta)}s"
                        elif delta < 3600:
                            lead['responseSpeed'] = f"{int(delta/60)}m"
                        else:
                            lead['responseSpeed'] = f"{delta/3600:.1f}h"
                    else:
                        lead['responseSpeed'] = "—"
                    
                    # Conversation status
                    if c['in'] > 0:
                        lead['status'] = 'active'
                        lead['conversationStatus'] = f"In conversation — {c['in']} prospect repl{'y' if c['in'] == 1 else 'ies'}"
                        lead['notes'] = f"{c['in']} prospect repl{'y' if c['in'] == 1 else 'ies'}, {c['out']} SMS sent"
                        lead['prospectReplied'] = True
                    else:
                        lead['status'] = 'responded'
                        lead['conversationStatus'] = "No reply yet"
                        lead['notes'] = 'First-touch sent, awaiting reply'
                else:
                    lead['responseSpeed'] = "—"
                    lead['conversationStatus'] = "No SMS history"
            except:
                lead['responseSpeed'] = "—"
                lead['conversationStatus'] = ""
        else:
            lead['responseSpeed'] = "—"
            lead['conversationStatus'] = ""
    return leads

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        # Recording proxy endpoint
        if parsed.path.startswith('/api/recording/'):
            call_id = parsed.path.split('/api/recording/')[1]
            self.serve_recording(call_id)
            return
        
        if parsed.path != '/api/leads':
            self.send_response(404)
            self.end_headers()
            return
        
        start_str = params.get('start', [None])[0]
        end_str = params.get('end', [None])[0]
        
        if not start_str or not end_str:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'start and end required'}).encode())
            return
        
        try:
            start_ct, end_ct = parse_date_range(start_str, end_str)
        except:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid date'}).encode())
            return
        
        env = load_env()
        email_idx, name_idx = get_thread_enrichment()
        ghl = get_ghl_leads(env, start_ct, end_ct, email_idx)
        platform = get_platform_leads(env, start_ct, end_ct, name_idx)
        vapi, non_leads = get_vapi_leads(env, start_ct, end_ct)
        all_leads = ghl + platform + vapi
        convos = get_sms_conversations(env, start_ct, end_ct)
        all_leads = enrich_with_sms(all_leads, convos)
        all_leads.sort(key=lambda x: x.get('timestamp', ''))
        non_leads.sort(key=lambda x: x.get('timestamp', ''))
        reminders = get_reminder_stats(start_ct, end_ct)
        # SENT = outbound sent for GHL + platform leads only (Phone Leads excluded)
        sent = sum(1 for l in all_leads if l['status'] in ('responded', 'active') and l['channel'] != 'Phone Leads')
        replies = sum(1 for l in all_leads if l.get('prospectReplied'))
        summary = {
            'total': len(all_leads),
            'sent': sent,
            'replies': replies,
            'ghl': sum(1 for l in all_leads if l['channel'] == 'GHL Webform'),
            'platform': sum(1 for l in all_leads if l['channel'] in ('WeddingWire', 'TheKnot', 'Zola')),
            'phone': sum(1 for l in all_leads if l['channel'] == 'Phone Leads'),
            'reminders': reminders,
        }
        result = {'leads': all_leads, 'nonLeadCalls': non_leads, 'summary': summary}
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(result, default=str).encode())
    
    def serve_recording(self, call_id):
        """Proxy VAPI recording — fetches presigned URL from VAPI API, streams MP3 to browser"""
        env = load_env()
        vapi_key = env.get('VAPI_PRIVATE_KEY', '')
        if not vapi_key:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'VAPI key not configured'}).encode())
            return
        
        # Get presigned URL from VAPI single-call endpoint
        call_data = curl_json(
            f"https://api.vapi.ai/call/{call_id}",
            headers={"Authorization": f"Bearer {vapi_key}"}
        )
        
        # Try presigned URL first, then raw recordingUrl
        recording_url = ''
        artifact = call_data.get('artifact', {})
        if isinstance(artifact, dict):
            recording_url = artifact.get('presignedMonoUrl', '') or artifact.get('recordingUrl', '')
        if not recording_url:
            recording_url = call_data.get('recordingUrl', '')
        
        if not recording_url:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'No recording found for this call'}).encode())
            return
        
        # Download recording via curl and stream to browser
        result = subprocess.run(
            ['curl', '-s', '-L', '--max-time', '30', recording_url],
            capture_output=True, timeout=35
        )
        
        if not result.stdout or len(result.stdout) < 100:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Recording download failed'}).encode())
            return
        
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Length', str(len(result.stdout)))
        self.send_header('Content-Disposition', f'inline; filename="call-{call_id[:8]}.mp3"')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(result.stdout)
    
    def log_message(self, format, *args):
        pass  # Suppress logs

if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 3979), Handler)
    print("Leads API server running on port 3979")
    server.serve_forever()
