from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.groq_service import ask_groq


router = APIRouter(prefix="/api/chatbot", tags=["chatbot"])


class ChatRequest(BaseModel):
    message: str


@router.post("/message")
def chatbot_message(payload: ChatRequest, request: Request):
    message = payload.message.strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    session_token = request.cookies.get("session")
    from main import _get_session_state
    state = _get_session_state(session_token, create=True)

    history = state.setdefault("chat_history", [])

    try:
        reply = ask_groq(message,history)

        state["chat_history"].append({"role": "user", "content": message})
        state["chat_history"].append({"role": "assistant", "content": reply})

        state["chat_history"] = state["chat_history"][-10:]

        return {"reply": reply}
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error))
    except Exception:
        raise HTTPException(status_code=500, detail="Groq chatbot request failed")