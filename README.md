# 🎙️ ARIA — Voice Calendar Assistant

A **real-time voice-powered scheduling assistant** built with FastAPI, OpenAI GPT-4o + Whisper + TTS, and Google Calendar API. Speak or type to manage your calendar with natural language.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🎙️ **Voice Input** | Record audio → Whisper transcription → AI response |
| 🔊 **Voice Output** | OpenAI TTS (Nova voice) reads responses aloud |
| 📅 **Google Calendar Sync** | View, create, update, delete events in real-time |
| 🤖 **GPT-4o Intelligence** | Natural language date parsing, context-aware scheduling |
| 🔄 **WebSocket** | Real-time bidirectional communication |
| 💬 **Multi-turn Conversation** | Full conversation memory per session |

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
cd voice-scheduler
pip install -r requirements.txt
```

### 2. Configure API Keys

```bash
cp .env.example .env
# Edit .env with your keys
```

**OpenAI API Key:**
- Get from https://platform.openai.com/api-keys

**Google OAuth Setup:**
1. Go to https://console.cloud.google.com/
2. Create a project → Enable **Google Calendar API**
3. Create **OAuth 2.0 Client ID** (Web Application type)
4. Add redirect URI: `http://localhost:8000/auth/callback`
5. Copy Client ID and Client Secret to `.env`

### 3. Run the Server

```bash
# Load environment and start
export $(cat .env | xargs)
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Or use python-dotenv (install separately):
```bash
pip install python-dotenv
```

Then add to top of `main.py`:
```python
from dotenv import load_dotenv
load_dotenv()
```

### 4. Open the App

Visit: **http://localhost:8000**

1. Click **"Connect Google Calendar"** to authenticate
2. Start speaking or typing!

---

## 💬 Example Commands

```
"What's on my schedule today?"
"Schedule a team standup tomorrow at 9am for 30 minutes"
"Book lunch with Sarah on Friday from 12 to 1pm at Cafe Roma"
"What meetings do I have this week?"
"Cancel my 3pm meeting"
"Move tomorrow's standup to 10am"
"Am I free on Thursday afternoon?"
```

---

## 🏗️ Architecture

```
Browser (HTML/CSS/JS)
    │
    ├── WebSocket ──→ FastAPI /ws/{session_id}
    ├── POST /chat ──→ GPT-4o with Calendar Tools
    ├── POST /transcribe ──→ OpenAI Whisper
    ├── POST /speak ──→ OpenAI TTS (Nova)
    ├── GET  /events ──→ Google Calendar API
    └── GET  /auth/google ──→ Google OAuth 2.0
                                    │
                              Google Calendar
```

## 📁 File Structure

```
voice-scheduler/
├── main.py          # FastAPI app, routes, AI logic, calendar tools
├── static/
│   └── index.html   # Full frontend (single-file SPA)
├── requirements.txt
├── .env.example
└── README.md
```

## 🔒 Production Notes

- Replace in-memory `token_store` with Redis or a database
- Add HTTPS (required for microphone access in most browsers)
- Set `REDIRECT_URI` to your production domain
- Add rate limiting and user authentication
- Use environment secrets manager instead of `.env`

## 🛠️ Tech Stack

- **Backend:** FastAPI, Python 3.11+
- **AI:** OpenAI GPT-4o (chat), Whisper (STT), TTS-1 (TTS)
- **Calendar:** Google Calendar API v3, OAuth 2.0
- **Frontend:** Vanilla HTML/CSS/JS (zero dependencies)
- **Realtime:** WebSockets
