import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from spark_coach.clients.slack import send_webhook

router = APIRouter(prefix="/v1/slack", tags=["slack"])


class NotifyRequest(BaseModel):
    text: str


@router.post("/notify")
async def notify(req: NotifyRequest) -> dict[str, str]:
    try:
        await send_webhook(text=req.text)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:  # configuration or other errors
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "sent"}
