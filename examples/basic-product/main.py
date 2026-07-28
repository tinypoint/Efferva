from auth import DemoAuthMiddleware, resolve_principal
from auth import router as auth_router
from fastapi import FastAPI

from efferva import Efferva

app = FastAPI(title="Basic Product")
app.add_middleware(DemoAuthMiddleware)
app.include_router(auth_router)

Efferva(identity=resolve_principal).install(app, prefix="/agent")
