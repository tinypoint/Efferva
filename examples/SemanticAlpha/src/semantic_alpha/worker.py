import asyncio

from efferva import serve_worker
from efferva.sandbox.providers.opensandbox import OpenSandboxProvider

if __name__ == "__main__":
    asyncio.run(serve_worker(sandbox=OpenSandboxProvider()))
