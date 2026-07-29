from pathlib import Path

from efferva.config import load_codex_config, merge_codex_config


def test_codex_config_file_and_python_overrides_are_deep_merged(tmp_path: Path) -> None:
    config_path = tmp_path / "codex.toml"
    config_path.write_text(
        """
model_reasoning_effort = "medium"

[features]
unified_exec = false
multi_agent_v2 = true
""".strip()
    )

    merged = merge_codex_config(
        load_codex_config(config_path),
        {
            "model_reasoning_effort": "high",
            "features": {"unified_exec": True},
        },
    )

    assert merged == {
        "model_reasoning_effort": "high",
        "features": {
            "unified_exec": True,
            "multi_agent_v2": True,
        },
    }
