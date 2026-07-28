from __future__ import annotations

import asyncio


async def command(*argv: str, check: bool = True) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if check and process.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed ({process.returncode}): {stderr}")
    return process.returncode or 0, stdout, stderr
