import asyncio

from efferva import serve_worker

from semantic_alpha.sandbox import create_sandbox_provider

if __name__ == "__main__":
    asyncio.run(serve_worker(sandbox=create_sandbox_provider()))
