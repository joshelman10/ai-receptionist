# RapidFlow AI Receptionist — demo

A demo AI receptionist for a (fictional) plumbing company. A customer chats on
the webpage; behind the scenes, a small Python server calls Claude to answer
questions and book appointments.

## How it fits together

```
Browser (index.html)  →  server.py (holds the secret key)  →  Claude API
```

The browser never sees the API key. That's the whole reason the server exists.

## One-time setup

1. **Get an Anthropic API key**
   - Go to https://console.anthropic.com
   - Sign up / log in → **Settings → API keys → Create Key**
   - Add a few dollars of credit under **Billing** (this demo costs fractions of a cent per message)
   - Copy the key (it starts with `sk-ant-...`)

2. **Dependencies are already installed** (`anthropic`, `flask`).

## Running it

Open **PowerShell**, then:

```powershell
cd C:\Users\Admin\projects\ai-receptionist
$env:ANTHROPIC_API_KEY = "sk-ant-...paste-your-key-here..."
python server.py
```

Then open your browser to:  **http://localhost:5000**

Chat with the receptionist. When you give it a plumbing problem and answer its
questions, it books the appointment and the dashboard at the bottom updates.

Press **Ctrl+C** in PowerShell to stop the server.

## Notes

- The key only stays set for that PowerShell window. Open a new window and you'll
  need to set it again (we can make this permanent later).
- Model: `claude-opus-4-8`. For a real high-volume deployment you could switch to
  a cheaper model (e.g. Claude Haiku) — that's a one-line change in `server.py`.
