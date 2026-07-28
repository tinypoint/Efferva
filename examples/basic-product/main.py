from auth import DemoAuthMiddleware, resolve_principal
from auth import router as auth_router
from fastapi import FastAPI

from agentframe import AgentFrame

app = FastAPI(title="Basic Product")
app.add_middleware(DemoAuthMiddleware)
app.include_router(auth_router)

AgentFrame(identity=resolve_principal).install(app, prefix="/agent")
