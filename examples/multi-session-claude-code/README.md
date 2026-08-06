# Multi-session Claude Code

This product selects Efferva's Claude Code engine explicitly. Efferva installs the fixed Claude Agent SDK into each Session Sandbox on first access; the product image contains neither Claude Code nor Node.js.

```bash
cd examples/multi-session-claude-code
export ANTHROPIC_API_KEY="your-key"
uv build --wheel --out-dir ../../dist ../..
docker compose up --build
```

Open <http://localhost:8080>. The first Claude request for a Session takes longer while its Sandbox installs `claude-agent-sdk==0.2.131`.

Optional variables: `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, and `EFFERVA_SANDBOX_IMAGE`. The Sandbox image must be Linux x86_64/arm64 with Python 3.11+, pip, venv, and glibc 2.17+; Alpine is unsupported.
