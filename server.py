"""
RapidFlow AI Receptionist — local server.

What this does:
  1. Serves the webpage (index.html) at http://localhost:5000
  2. Exposes /api/chat, which the webpage calls for every customer message
  3. Talks to Claude with your SECRET API key (which never leaves this server)

The browser never sees your API key — that's the whole point of having a server.

Run it with:   python server.py
(Make sure ANTHROPIC_API_KEY is set first — see the README.)
"""

import os
import csv
import json
import datetime
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
import anthropic

# The Anthropic() client automatically reads your key from the
# ANTHROPIC_API_KEY environment variable. We never hard-code it.
client = anthropic.Anthropic()

MODEL = "claude-opus-4-8"

# Absolute path to this file's folder, so we find index.html / leads.csv no
# matter what directory the server is launched from (local vs. cloud host).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Password for the owner dashboard (/leads). Set DASHBOARD_PASSWORD in your
# environment / on Render. Falls back to a default so local dev still works.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "ownerskey123")

# ---------------------------------------------------------------------------
# The receptionist's "personality" and rules. This is the single most
# important piece — it's what turns a generic AI into RapidFlow's receptionist.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are the virtual receptionist for RapidFlow Plumbing, a licensed, insured
plumbing company that operates 24/7 across the metro area.

Your job is to help customers and book service appointments. Be warm, calm,
and concise — one or two short sentences per reply. You are speaking with a
real customer who may be stressed (e.g. a burst pipe).

What you can tell customers:
- We are available 24/7, including nights, weekends, and holidays.
- We are licensed and insured, with same-day service.
- Rough price ranges (always say these are estimates, final price confirmed on site):
    drain / clog clearing: $150-$400
    faucet or minor leak repair: $200-$600
    toilet repair: $250-$500
    water heater repair or install: $1,800-$3,000
    burst pipe / emergency: varies, dispatched immediately

How to handle a service request:
- Treat anything that sounds urgent (burst pipe, flooding, no water, gas smell)
  as an emergency and reassure them you can dispatch someone fast.
- To book, you need four things: the customer's NAME, PHONE NUMBER, a short
  DESCRIPTION of the problem, and their PREFERRED TIME.
- Ask for these naturally, one or two at a time — do not interrogate.
- As soon as you have all four, call the `book_appointment` tool. Do not claim
  the appointment is booked until you have actually called the tool.

Only discuss plumbing and RapidFlow's services. If asked about anything
unrelated, politely steer back to how you can help with their plumbing needs.
"""

# The one tool the receptionist can use. When Claude decides it has everything
# it needs, it "calls" this tool instead of replying with text. Our server
# catches that call and records the lead.
TOOLS = [
    {
        "name": "book_appointment",
        "description": (
            "Record a confirmed service booking. Only call this once you have "
            "collected the customer's name, phone number, a description of the "
            "plumbing problem, and their preferred time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer's name"},
                "phone": {"type": "string", "description": "Best contact phone number"},
                "issue": {"type": "string", "description": "Short description of the plumbing problem"},
                "preferred_time": {"type": "string", "description": "When the customer wants the visit"},
            },
            "required": ["name", "phone", "issue", "preferred_time"],
        },
    }
]

# Rough job-value estimate so the dashboard can show "revenue rescued".
JOB_VALUES = {
    "burst": 1800, "pipe": 1800, "water heater": 2500, "heater": 2500,
    "clog": 350, "drain": 350, "sink": 350, "toilet": 400, "leak": 600, "faucet": 250,
}

def estimate_value(issue: str) -> int:
    t = (issue or "").lower()
    for keyword, value in JOB_VALUES.items():
        if keyword in t:
            return value
    return 450


# Every booking is appended here so a lead is NEVER lost — even if the
# customer closes the page. The owner can open this file in Excel.
LEADS_FILE = os.path.join(BASE_DIR, "leads.csv")
LEAD_FIELDS = ["captured_at", "name", "phone", "issue", "preferred_time", "value"]

def save_lead(booking: dict) -> None:
    new_file = not os.path.exists(LEADS_FILE)
    row = {
        "captured_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "name": booking.get("name", ""),
        "phone": booking.get("phone", ""),
        "issue": booking.get("issue", ""),
        "preferred_time": booking.get("preferred_time", ""),
        "value": booking.get("value", ""),
    }
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEAD_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)

def read_leads() -> list:
    if not os.path.exists(LEADS_FILE):
        return []
    with open(LEADS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


app = Flask(__name__)


@app.route("/")
def home():
    # Serve the webpage from this file's folder (absolute path — works anywhere).
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    """The webpage sends the whole conversation here; we stream Claude's answer back.

    The response is a stream of newline-separated JSON "events":
      {"type": "text", "text": "..."}      one chunk of the reply as it's typed
      {"type": "booking", "booking": {...}} sent once, if an appointment was booked
    The browser reads these as they arrive and types the reply out live.
    """
    data = request.get_json(force=True)
    messages = data.get("messages", [])

    def event(obj):
        return json.dumps(obj) + "\n"

    def generate():
        try:
            # Stream Claude's first reply, with the booking tool available.
            with client.messages.stream(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield event({"type": "text", "text": text})
                final = stream.get_final_message()

            # If Claude decided to book, record the lead and stream the confirmation.
            if final.stop_reason == "tool_use":
                tool_use = next(b for b in final.content if b.type == "tool_use")
                booking = dict(tool_use.input)
                booking["value"] = estimate_value(booking.get("issue", ""))
                save_lead(booking)

                followup_messages = messages + [
                    {"role": "assistant", "content": final.content},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": "Booking recorded and sent to dispatch. Confirm to the customer.",
                            }
                        ],
                    },
                ]
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=followup_messages,
                ) as s2:
                    for text in s2.text_stream:
                        yield event({"type": "text", "text": text})

                yield event({"type": "booking", "booking": booking})

        except anthropic.AuthenticationError:
            yield event({"type": "text", "text": "(Server error: the ANTHROPIC_API_KEY is missing or invalid.)"})
        except Exception as e:
            yield event({"type": "text", "text": f"(Server error: {e})"})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/leads")
def leads():
    """The owner's private dashboard — password-protected so customers can't see it."""
    # Simple password gate. The browser shows a native login popup; the owner
    # types the password (any username). Real accounts come later with a database.
    auth = request.authorization
    if not auth or auth.password != DASHBOARD_PASSWORD:
        return Response(
            "Owner login required.",
            401,
            {"WWW-Authenticate": 'Basic realm="RapidFlow Owner Dashboard"'},
        )

    rows = read_leads()
    rows.reverse()  # newest first
    valid = [r for r in rows if r.get("value", "").isdigit()]
    count = len(valid)
    total = sum(int(r["value"]) for r in valid)
    avg = total // count if count else 0
    month_prefix = datetime.datetime.now().strftime("%Y-%m")
    this_month = sum(1 for r in valid if r.get("captured_at", "").startswith(month_prefix))

    body = "".join(
        f"<tr><td class='dt'>{r['captured_at']}</td><td class='nm'>{r['name']}</td>"
        f"<td>{r['phone']}</td><td>{r['issue']}</td>"
        f"<td>{r['preferred_time']}</td><td class='v'>${int(r['value']):,}</td></tr>"
        for r in valid
    ) or "<tr><td colspan='6' class='empty'>No leads yet — book one in the chat and it appears here instantly.</td></tr>"

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="15">
<title>Relay — Owner Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='6' fill='%231763e6'/><path d='M12 4.5c3.4 4.3 5 6.8 5 9.1a5 5 0 0 1-10 0c0-2.3 1.6-4.8 5-9.1z' fill='white'/></svg>">
<script>(function(){{try{{var s=localStorage.getItem('relay-theme');if(s==='dark')document.documentElement.setAttribute('data-theme','dark');}}catch(e){{}}}})();</script>
<style>
  :root{{--ink:#13203a;--body:#48566c;--muted:#7c8ba3;--line:#e7ecf4;--bg:#f4f7fc;--card:#fff;
    --brand:#1763e6;--brand-d:#0f4fbf;--green:#10b981;--indigo:#6366f1;--amber:#f59e0b;
    --shadow:0 1px 2px rgba(13,27,42,.05),0 12px 30px -14px rgba(13,27,42,.18)}}
  html[data-theme="dark"]{{--ink:#eaf1ff;--body:#a9b8d0;--muted:#7486a0;--line:#1d2c46;--bg:#091221;--card:#111e33;
    --brand:#4a90ff;--brand-d:#7badff;--green:#34d399;--indigo:#8b8ff6;--amber:#fbbf24;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 14px 34px -14px rgba(0,0,0,.6)}}
  .theme-btn{{width:38px;height:38px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--body);display:grid;place-items:center;cursor:pointer;transition:all .15s;margin-left:14px}}
  .theme-btn:hover{{color:var(--brand);border-color:var(--brand)}}
  .theme-btn svg{{width:17px;height:17px}}
  .i-sun{{display:none}}
  html[data-theme="dark"] .i-sun{{display:block}}
  html[data-theme="dark"] .i-moon{{display:none}}
  html[data-theme="dark"] .topbar{{background:var(--card)}}
  html[data-theme="dark"] th{{background:#0d1a2e}}
  html[data-theme="dark"] th,html[data-theme="dark"] td{{border-bottom-color:#1a2840}}
  html[data-theme="dark"] tr:last-child td{{border-bottom:none}}
  html[data-theme="dark"] .ic.b{{background:#13243f}}
  html[data-theme="dark"] .ic.g{{background:#10301f}}
  html[data-theme="dark"] .ic.i{{background:#1c1f3a}}
  html[data-theme="dark"] .ic.a{{background:#2e2410}}
  html[data-theme="dark"] .biz .live{{background:#10301f}}
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--body);transition:background .25s,color .25s}}
  a{{text-decoration:none;color:var(--brand)}}
  .topbar{{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}}
  .topbar-in{{max-width:1100px;margin:0 auto;padding:0 28px;height:66px;display:flex;align-items:center;gap:13px}}
  .logo{{display:flex;align-items:center;gap:10px;font-weight:800;color:var(--ink);font-size:19px;letter-spacing:-.02em}}
  .logo .mark{{width:34px;height:34px;border-radius:9px;background:linear-gradient(150deg,#3b8bff,#0f4fbf);display:grid;place-items:center;color:#fff;box-shadow:0 7px 16px -5px rgba(23,99,230,.55)}}
  .logo .tag{{font-size:11px;font-weight:700;color:var(--brand-d);background:#eaf1ff;padding:3px 9px;border-radius:999px;letter-spacing:.03em;margin-left:4px}}
  .biz{{margin-left:auto;display:flex;align-items:center;gap:9px;font-size:14px;color:var(--ink);font-weight:600}}
  .biz .live{{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--green);background:#e7faf2;padding:5px 11px;border-radius:999px}}
  .biz .live .d{{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(16,185,129,.22)}}
  .main{{max-width:1100px;margin:0 auto;padding:34px 28px 60px}}
  h1{{font-size:25px;color:var(--ink);margin:0 0 4px;letter-spacing:-.02em}}
  .sub{{color:var(--muted);font-size:14px;margin:0 0 28px}}
  .kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-bottom:30px}}
  @media(max-width:820px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
  .kpi{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:var(--shadow)}}
  .kpi .ic{{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;margin-bottom:14px}}
  .kpi .n{{font-size:30px;font-weight:800;color:var(--ink);line-height:1;letter-spacing:-.02em}}
  .kpi .l{{font-size:13px;color:var(--muted);margin-top:7px}}
  .ic.b{{background:#eaf1ff;color:var(--brand-d)}} .ic.g{{background:#e7faf2;color:var(--green)}}
  .ic.i{{background:#eef0ff;color:var(--indigo)}} .ic.a{{background:#fef3e2;color:var(--amber)}}
  .panel{{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}}
  .panel-head{{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}}
  .panel-head h2{{font-size:16px;color:var(--ink);margin:0;letter-spacing:-.01em}}
  .panel-head .meta{{font-size:12.5px;color:var(--muted)}}
  table{{width:100%;border-collapse:collapse}}
  th,td{{text-align:left;padding:14px 22px;font-size:14px;border-bottom:1px solid #f0f3f9}}
  tr:last-child td{{border-bottom:none}}
  th{{background:#fafbfe;color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em}}
  td.dt{{color:var(--muted);font-size:13px;white-space:nowrap}}
  td.nm{{font-weight:600;color:var(--ink)}}
  td.v{{font-weight:700;color:var(--green);white-space:nowrap}}
  td.empty{{text-align:center;color:var(--muted);padding:44px}}
  .foot{{margin-top:22px;font-size:12.5px;color:var(--muted)}}
</style></head><body>
  <div class="topbar"><div class="topbar-in">
    <div class="logo"><span class="mark"><svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor"><path d="M12 2.5c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z"/></svg></span> Relay <span class="tag">OWNER DASHBOARD</span></div>
    <div class="biz">RapidFlow Plumbing <span class="live"><span class="d"></span> Live</span></div>
    <button class="theme-btn" onclick="toggleTheme()" aria-label="Toggle dark mode">
      <svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
      <svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
    </button>
  </div></div>
  <div class="main">
    <h1>Welcome back</h1>
    <p class="sub">Here's what your AI receptionist captured. Updates automatically every 15 seconds.</p>
    <div class="kpis">
      <div class="kpi"><div class="ic b"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.1-8.6A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg></div><div class="n">{count}</div><div class="l">Leads captured</div></div>
      <div class="kpi"><div class="ic g"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg></div><div class="n">${total:,}</div><div class="l">Revenue captured</div></div>
      <div class="kpi"><div class="ic i"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg></div><div class="n">${avg:,}</div><div class="l">Avg job value</div></div>
      <div class="kpi"><div class="ic a"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></div><div class="n">{this_month}</div><div class="l">Booked this month</div></div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>Recent leads</h2><span class="meta">Newest first</span></div>
      <table>
        <tr><th>Captured</th><th>Name</th><th>Phone</th><th>Problem</th><th>Preferred time</th><th>Est. value</th></tr>
        {body}
      </table>
    </div>
    <div class="foot">Every lead above was captured automatically by your AI receptionist — calls you would otherwise have lost to voicemail. · <a href="/">← View customer site</a></div>
  </div>
<script>function toggleTheme(){{var el=document.documentElement;var now=el.getAttribute('data-theme')==='dark'?'light':'dark';if(now==='dark')el.setAttribute('data-theme','dark');else el.removeAttribute('data-theme');try{{localStorage.setItem('relay-theme',now);}}catch(e){{}}}}</script>
</body></html>"""


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  WARNING: ANTHROPIC_API_KEY is not set. The chat will fail until you set it.\n")
    print("  RapidFlow receptionist running at  http://localhost:5000\n")
    app.run(port=5000, debug=True)
