import os
from pathlib import Path

from groq import Groq


BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_FILE = BASE_DIR / "knowledge" / "module_context.txt"

KDS_DIR = BASE_DIR / "kds"

DEFAULT_MODEL = "llama-3.1-8b-instant"


def load_module_context() -> str:
    if KNOWLEDGE_FILE.exists():
        return KNOWLEDGE_FILE.read_text(encoding="utf-8")

    return "You are an SAP assistant. Answer questions about SAP MM and PP clearly."

def load_kds_context() -> str:
    kds_content = ""
    
    if KDS_DIR.exists() and KDS_DIR.is_dir():
        for file_path in KDS_DIR.iterdir():
            # Only read readable text formats
            if file_path.suffix.lower() in ['.txt', '.csv', '.json']:
                try:
                    text = file_path.read_text(encoding="utf-8")
                    kds_content += f"\n--- Data from {file_path.name} ---\n{text}\n"
                except Exception as e:
                    print(f"Skipping {file_path.name}: {e}")
                    
    return kds_content

def get_groq_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

    return Groq(api_key=api_key)


def ask_groq(user_message: str, history: list = None) -> str:
    client = get_groq_client()
    module_context = load_module_context()
    kds_context = load_kds_context()
    model = os.getenv("GROQ_MODEL", DEFAULT_MODEL)

    system_prompt = f"""
You are an SAP module assistant for a Master Data Validator website.

Use the following knowledge/context while answering:

General module context:
{module_context}

KDS Reference data:
{kds_context}

Answer style:
- Keep answers simple,concise but informative.
- Use short paragraphs or bullet points when helpful.
- Focus mainly on SAP MM and SAP PP.
- If the question is unclear, ask only one clarification question.
- Do not say you can access the company's SAP system.
- Do not collect personal data.
- Do not create tickets.
"""
    messages_payload = [
        {"role": "system", "content": system_prompt}
    ]

    if history:
        for msg in history:
            messages_payload.append({
                "role": msg["role"], 
                "content": msg["content"]
            })

    messages_payload.append({"role": "user", "content": user_message})

    completion = client.chat.completions.create(
        model=model,
        messages=messages_payload,
        temperature=0.2,
        max_tokens=700,
    )

    return completion.choices[0].message.content