from fastapi import FastAPI
from app.routers import orders

App = FastAPI()
App.include_router(orders.Router, prefix="/api")


@App.on_event("startup")
async def Startup():
    pass
