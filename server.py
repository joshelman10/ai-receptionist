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
    """A simple dashboard showing every captured lead. The owner's view."""
    rows = read_leads()
    rows.reverse()  # newest first
    total = sum(int(r["value"]) for r in rows if r.get("value", "").isdigit())

    body = "".join(
        f"<tr><td>{r['captured_at']}</td><td>{r['name']}</td>"
        f"<td>{r['phone']}</td><td>{r['issue']}</td>"
        f"<td>{r['preferred_time']}</td><td class='v'>${int(r['value']):,}</td></tr>"
        for r in rows if r.get("value", "").isdigit()
    ) or "<tr><td colspan='6' class='empty'>No leads captured yet — go book one in the chat.</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>RapidFlow — Captured Leads</title>
<style>
  body{{margin:0;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;background:#f6f8fc;color:#1a2b42;padding:32px}}
  .wrap{{max-width:980px;margin:0 auto}}
  h1{{font-size:24px;margin:0 0 4px}}
  .sub{{color:#7c8ba3;margin:0 0 24px;font-size:14px}}
  .cards{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
  .card{{background:#fff;border:1px solid #e6ebf2;border-radius:14px;padding:18px 22px;box-shadow:0 8px 24px -12px rgba(16,33,61,.15)}}
  .card .n{{font-size:26px;font-weight:800}}
  .card .l{{color:#7c8ba3;font-size:13px;margin-top:2px}}
  .green{{color:#10b981}}
  table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6ebf2;border-radius:14px;overflow:hidden}}
  th,td{{text-align:left;padding:12px 16px;font-size:14px;border-bottom:1px solid #eef2f8}}
  th{{background:#0d1b2a;color:#fff;font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.04em}}
  td.v{{font-weight:700;color:#10b981}}
  td.empty{{text-align:center;color:#7c8ba3;padding:30px}}
  a{{color:#1763e6;text-decoration:none;font-size:14px}}
</style></head><body><div class="wrap">
  <h1>Captured Leads</h1>
  <p class="sub">Auto-refreshes every 10 seconds · saved to leads.csv · <a href="/">← back to site</a></p>
  <div class="cards">
    <div class="card"><div class="n">{len(rows)}</div><div class="l">Jobs booked</div></div>
    <div class="card"><div class="n green">${total:,}</div><div class="l">Revenue captured</div></div>
  </div>
  <table>
    <tr><th>Captured</th><th>Name</th><th>Phone</th><th>Problem</th><th>Preferred time</th><th>Est. value</th></tr>
    {body}
  </table>
</div></body></html>"""


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  WARNING: ANTHROPIC_API_KEY is not set. The chat will fail until you set it.\n")
    print("  RapidFlow receptionist running at  http://localhost:5000\n")
    app.run(port=5000, debug=True)
