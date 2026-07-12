import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_assistant


def test_reindex_and_search_txt_knowledge(tmp_path, monkeypatch):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "mr-design.md").write_text(
        "MR Design memakai folder FAT, HP COVER, dan DISTRIBUTION CABLE untuk validasi EVA.",
        encoding="utf-8",
    )
    db_path = tmp_path / "knowledge_index.db"
    monkeypatch.setattr(ai_assistant, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(ai_assistant, "KNOWLEDGE_DB_PATH", db_path)
    monkeypatch.setattr(ai_assistant, "BOT_WA_ENV_PATH", tmp_path / "missing-bot.env")

    result = ai_assistant.reindex_knowledge()
    status = ai_assistant.knowledge_status()
    matches = ai_assistant.search_knowledge("folder FAT validasi", top_k=2)

    assert result["indexed_files"] == 1
    assert status["chunks"] == 1
    assert matches
    assert matches[0]["source"] == "mr-design.md"


def test_ai_chat_requires_message():
    with pytest.raises(ValueError):
        ai_assistant.ai_chat({"message": ""})


def test_ai_chat_requires_provider_config(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_assistant, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(ai_assistant, "KNOWLEDGE_DB_PATH", tmp_path / "knowledge_index.db")
    monkeypatch.setattr(ai_assistant, "BOT_WA_ENV_PATH", tmp_path / "missing-bot.env")
    monkeypatch.delenv("XIAOMI_MIMO_API_KEY", raising=False)
    monkeypatch.delenv("XIAOMI_MIMO_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    with pytest.raises(ai_assistant.AssistantConfigError):
        ai_assistant.ai_chat({"message": "cara convert?"})


def test_provider_config_falls_back_to_bot_env(tmp_path, monkeypatch):
    bot_env = tmp_path / "bot.env"
    bot_env.write_text(
        "OPENAI_API_KEY=bot-key\nOPENAI_BASE_URL=https://example.test/v1\nOPENAI_MODEL=bot-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ai_assistant, "BOT_WA_ENV_PATH", bot_env)
    monkeypatch.delenv("XIAOMI_MIMO_API_KEY", raising=False)
    monkeypatch.delenv("XIAOMI_MIMO_BASE_URL", raising=False)
    monkeypatch.delenv("XIAOMI_MIMO_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    assert ai_assistant._provider_config() == ("bot-key", "https://example.test/v1", "bot-model")
