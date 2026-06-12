import json
from datetime import datetime
from pathlib import Path
from random import randint

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter(prefix="/api/grievances", tags=["grievances"])

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_FILE = DATA_DIR / "grievances.json"


class GrievanceCreate(BaseModel):
    module: str
    name: str
    phone: str
    company: str
    reference: str | None = None
    issue_type: str
    description: str
    priority: str


def ensure_data_file():
    DATA_DIR.mkdir(exist_ok=True)

    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]", encoding="utf-8")


def read_grievances():
    ensure_data_file()
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def write_grievances(grievances):
    ensure_data_file()
    DATA_FILE.write_text(json.dumps(grievances, indent=2), encoding="utf-8")


def generate_ticket_id(module: str):
    today = datetime.now().strftime("%Y%m%d")
    number = randint(1000, 9999)
    return f"{module.upper()}-{today}-{number}"


@router.post("/create")
def create_grievance(payload: GrievanceCreate):
    grievances = read_grievances()

    ticket_id = generate_ticket_id(payload.module)

    grievance = {
        "ticket_id": ticket_id,
        "module": payload.module,
        "name": payload.name,
        "phone": payload.phone,
        "company": payload.company,
        "reference": payload.reference,
        "issue_type": payload.issue_type,
        "description": payload.description,
        "priority": payload.priority,
        "status": "New",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    grievances.append(grievance)
    write_grievances(grievances)

    return {
        "message": "Grievance submitted successfully",
        "ticket_id": ticket_id,
        "status": "New",
    }


@router.get("/")
def list_grievances():
    return read_grievances()


@router.get("/{ticket_id}")
def get_grievance(ticket_id: str):
    grievances = read_grievances()

    for grievance in grievances:
        if grievance["ticket_id"] == ticket_id:
            return grievance

    raise HTTPException(status_code=404, detail="Ticket not found")
