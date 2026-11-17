from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from app.database import Db
from app.services import pricing

Router = APIRouter(prefix="/orders")


@Router.post("/")
async def create_order(payload: dict):
    total = pricing.compute_total(payload["items"], payload.get("method"))
    row = await run_in_threadpool(lambda: Db.table("orders").insert({"total": total}).execute())
    return row


@Router.get("/{order_id}")
def get_order(order_id: str):
    return Db.table("orders").select("*").eq("id", order_id).execute()
