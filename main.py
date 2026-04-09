import os
import json
import asyncio
import base64
import tempfile
from datetime import datetime, timedelta
from typing import Optional
import httpx

# Load .env file automatically — no need to manually export vars
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("[INFO] Loaded .env file")
except ImportError:
    print("[INFO] python-dotenv not installed, using system environment variables")

# Required for OAuth over HTTP on localhost (remove in production with HTTPS)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Google OAuth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

# OpenAI
from openai import AsyncOpenAI

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Calendar Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = "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"
GOOGLE_CLIENT_ID = "514385349603-e43eh06npjoojgoulinsra2av2sb5u18.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "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"
REDIRECT_URI         = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/callback")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

# ── Startup validation ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_check():
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set! Chat and voice will NOT work.")
    else:
        logger.info(f"OpenAI API key loaded (...{OPENAI_API_KEY[-4:]})")
    if not GOOGLE_CLIENT_ID:
        logger.warning("GOOGLE_CLIENT_ID not set — Google Calendar will not work.")
    else:
        logger.info("Google OAuth credentials loaded")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# In-memory stores (use Redis/DB in production)
token_store: dict = {}
conversation_store: dict = {}
flow_store: dict = {}   # keeps OAuth flow alive between /auth/google and /auth/callback

# ── Models ────────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    session_id: str
    message: str

# ── Google Calendar helpers ───────────────────────────────────────────────────
def get_calendar_service(session_id: str):
    if session_id not in token_store:
        raise HTTPException(status_code=401, detail="Not authenticated with Google Calendar")
    creds_data = token_store[session_id]
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        token_store[session_id]["token"] = creds.token
    return build("calendar", "v3", credentials=creds)

def list_events(session_id: str, max_results: int = 10, time_min: str = None):
    service = get_calendar_service(session_id)
    if not time_min:
        time_min = datetime.utcnow().isoformat() + "Z"
    result = service.events().list(
        calendarId="primary", timeMin=time_min,
        maxResults=max_results, singleEvents=True, orderBy="startTime",
    ).execute()
    return result.get("items", [])

def create_event(session_id: str, event_data: dict):
    service = get_calendar_service(session_id)
    return service.events().insert(calendarId="primary", body=event_data).execute()

def delete_event(session_id: str, event_id: str):
    service = get_calendar_service(session_id)
    service.events().delete(calendarId="primary", eventId=event_id).execute()
    return True

# ── OpenAI Tool definitions ───────────────────────────────────────────────────
CALENDAR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_upcoming_events",
            "description": "List upcoming calendar events. Use when user asks about schedule or what is on their calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "days_ahead":  {"type": "integer", "default": 7},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new calendar event. Use when user wants to schedule or book something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary":        {"type": "string"},
                    "description":    {"type": "string"},
                    "start_datetime": {"type": "string", "description": "ISO 8601 e.g. 2024-01-15T14:00:00"},
                    "end_datetime":   {"type": "string"},
                    "location":       {"type": "string"},
                    "attendees":      {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "start_datetime", "end_datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Delete or cancel a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id":      {"type": "string"},
                    "event_summary": {"type": "string"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todays_schedule",
            "description": "Get today's full schedule.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SYSTEM_PROMPT = """You are ARIA — an intelligent, warm AI scheduling assistant with full access to the user's Google Calendar.

Capabilities:
- View, create, update, and delete calendar events
- Understand natural language dates (next Tuesday, tomorrow at 3pm, in two weeks)
- Help plan schedules and find free time slots

Today's date and time: {current_datetime}

Guidelines:
- Be conversational and concise
- Always confirm before deleting events
- Infer reasonable durations if not given (meetings: 1hr, calls: 30min)
- After completing an action, briefly summarize what was done
- Format event lists clearly with dates, times, and titles 
- You are to automatically schedule in my google calendar


"""


async def process_tool_call(tool_name: str, tool_args: dict, session_id: str) -> str:
    try:
        if tool_name == "list_upcoming_events":
            days     = tool_args.get("days_ahead", 7)
            max_r    = tool_args.get("max_results", 10)
            time_min = datetime.utcnow().isoformat() + "Z"
            time_max = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
            service  = get_calendar_service(session_id)
            result   = service.events().list(
                calendarId="primary", timeMin=time_min, timeMax=time_max,
                maxResults=max_r, singleEvents=True, orderBy="startTime",
            ).execute()
            events = result.get("items", [])
            if not events:
                return "No upcoming events found in this period."
            lines = []
            for e in events:
                start = e["start"].get("dateTime", e["start"].get("date", ""))
                try:
                    dt        = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    formatted = dt.strftime("%a %b %d, %Y at %I:%M %p")
                except Exception:
                    formatted = start
                lines.append(f"[{e['id']}] {e.get('summary', 'Untitled')} — {formatted}")
            return "\n".join(lines)

        elif tool_name == "create_calendar_event":
            event_body = {
                "summary":     tool_args["summary"],
                "description": tool_args.get("description", ""),
                "start": {"dateTime": tool_args["start_datetime"], "timeZone": "UTC"},
                "end":   {"dateTime": tool_args["end_datetime"],   "timeZone": "UTC"},
            }
            if tool_args.get("location"):
                event_body["location"] = tool_args["location"]
            if tool_args.get("attendees"):
                event_body["attendees"] = [{"email": a} for a in tool_args["attendees"]]
            event = create_event(session_id, event_body)
            return f"Event created! ID: {event['id']} | Link: {event.get('htmlLink', 'N/A')}"

        elif tool_name == "delete_calendar_event":
            delete_event(session_id, tool_args["event_id"])
            return f"Event '{tool_args.get('event_summary', tool_args['event_id'])}' deleted."

        elif tool_name == "get_todays_schedule":
            today    = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = today + timedelta(days=1)
            service  = get_calendar_service(session_id)
            result   = service.events().list(
                calendarId="primary",
                timeMin=today.isoformat() + "Z",
                timeMax=tomorrow.isoformat() + "Z",
                singleEvents=True, orderBy="startTime",
            ).execute()
            events = result.get("items", [])
            if not events:
                return "Your calendar is clear today."
            lines = [f"Today ({today.strftime('%A, %B %d')}):"]
            for e in events:
                start = e["start"].get("dateTime", e["start"].get("date", ""))
                try:
                    time_str = datetime.fromisoformat(start.replace("Z", "+00:00")).strftime("%I:%M %p")
                except Exception:
                    time_str = start
                lines.append(f"  {time_str} — {e.get('summary', 'Untitled')}")
            return "\n".join(lines)

        return f"Unknown tool: {tool_name}"

    except HTTPException as e:
        return f"Calendar error: {e.detail}"
    except Exception as e:
        logger.error(f"Tool '{tool_name}' error: {e}", exc_info=True)
        return f"Error running {tool_name}: {str(e)}"


async def chat_with_ai(session_id: str, user_message: str) -> str:
    if not OPENAI_API_KEY:
        return ("OpenAI API key is not configured. "
                "Please add OPENAI_API_KEY=sk-... to your .env file and restart the server.")

    if session_id not in conversation_store:
        conversation_store[session_id] = []

    history = conversation_store[session_id]
    history.append({"role": "user", "content": user_message})

    is_authenticated = session_id in token_store
    system   = SYSTEM_PROMPT.format(current_datetime=datetime.now().strftime("%Y-%m-%d %H:%M (%A)"))
    if not is_authenticated:
        system += ("\n\nNOTE: User has NOT connected Google Calendar yet. "
                   "If they ask about calendar actions, remind them to click 'Connect Google Calendar' first.")

    messages = [{"role": "system", "content": system}] + history[-20:]
    tools    = CALENDAR_TOOLS if is_authenticated else None

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=tools,
        tool_choice="auto" if tools else None,
        temperature=0.7,
    )
    msg = response.choices[0].message

    # Resolve tool calls
    while msg.tool_calls:
        tool_results = []
        for tc in msg.tool_calls:
            args   = json.loads(tc.function.arguments)
            result = await process_tool_call(tc.function.name, args, session_id)
            logger.info(f"Tool '{tc.function.name}' result: {result[:120]}")
            tool_results.append({"tool_call_id": tc.id, "role": "tool", "content": result})

        messages.append(msg)
        messages.extend(tool_results)

        response = await openai_client.chat.completions.create(
            model="gpt-4o", messages=messages,
            tools=tools, tool_choice="auto", temperature=0.7,
        )
        msg = response.choices[0].message

    assistant_reply = msg.content or "I could not generate a response."
    history.append({"role": "assistant", "content": assistant_reply})
    conversation_store[session_id] = history[-40:]
    return assistant_reply


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()

@app.get("/health")
async def health():
    """Quick check — visit /health to see if keys are loaded."""
    return {
        "status": "ok",
        "openai_configured": bool(OPENAI_API_KEY),
        "google_configured": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
    }

@app.get("/auth/google")
async def auth_google(session_id: str = "default"):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env."
        )
    flow = Flow.from_client_config(
        {"web": {
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }},
        scopes=SCOPES, redirect_uri=REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        state=session_id, prompt="consent",
    )
    # Persist the flow so the callback can reuse it (fixes invalid_grant / missing code verifier)
    flow_store[state] = flow
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str, state: str = "default"):
    # Retrieve the original flow (carries code_verifier and state)
    flow = flow_store.pop(state, None)
    if flow is None:
        # Fallback: rebuild flow without PKCE (older Google accounts)
        flow = Flow.from_client_config(
            {"web": {
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }},
            scopes=SCOPES, redirect_uri=REDIRECT_URI,
        )
    # Use the full authorization_response URL — most reliable method
    authorization_response = str(request.url)
    # Force http for localhost (oauthlib requires this matches what was sent)
    if "localhost" in authorization_response:
        authorization_response = authorization_response.replace("https://", "http://")
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    session_id = state
    token_store[session_id] = {"token": creds.token, "refresh_token": creds.refresh_token}
    return RedirectResponse(f"/?session_id={session_id}&auth=success")

@app.get("/auth/status")
async def auth_status(session_id: str = "default"):
    return {"authenticated": session_id in token_store}

@app.post("/chat")
async def chat(msg: ChatMessage):
    try:
        reply = await chat_with_ai(msg.session_id, msg.message)
        return {"reply": reply}
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        # Return a real error message instead of generic frontend failure
        return JSONResponse(
            status_code=200,  # keep 200 so frontend renders it as a message
            content={"reply": f"Server error: {str(e)} — check your server terminal for details."}
        )

@app.post("/transcribe")
async def transcribe_audio(request: Request):
    try:
        data      = await request.json()
        audio_b64 = data.get("audio")
        if not audio_b64:
            raise HTTPException(status_code=400, detail="No audio data provided")
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

        audio_bytes = base64.b64decode(audio_b64)

        # Use mkstemp for Windows-safe temp file handling
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".webm")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(audio_bytes)
            with open(tmp_path, "rb") as f:
                transcript = await openai_client.audio.transcriptions.create(
                    model="whisper-1", file=f, response_format="text",
                )
            return {"text": transcript}
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass  # Non-critical cleanup

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

@app.post("/speak")
async def text_to_speech(request: Request):
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="No text provided")
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

        clean    = text.replace("**", "").replace("*", "").replace("`", "").replace("\n", " ")
        response = await openai_client.audio.speech.create(
            model="tts-1", voice="nova", input=clean[:4096]
        )
        return {"audio": base64.b64encode(response.content).decode()}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}")

@app.get("/events")
async def get_events(session_id: str = "default"):
    try:
        return {"events": list_events(session_id, max_results=20)}
    except HTTPException:
        return {"events": [], "error": "Not authenticated"}
    except Exception as e:
        logger.error(f"Events error: {e}", exc_info=True)
        return {"events": [], "error": str(e)}

@app.delete("/events/{event_id}")
async def delete_event_route(event_id: str, session_id: str = "default"):
    try:
        delete_event(session_id, event_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info(f"WS connected: {session_id}")
    try:
        while True:
            data     = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "chat":
                try:
                    reply = await chat_with_ai(session_id, data["message"])
                    await websocket.send_json({"type": "reply", "text": reply})
                except Exception as e:
                    await websocket.send_json({"type": "error", "text": str(e)})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info(f"WS disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WS error: {e}")
