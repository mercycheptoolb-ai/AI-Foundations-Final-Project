"""Paths, API key loading, the model client, and prices."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)

# The .env file is looked for in the repo folder first, then in the folder above it
# (where the teaching staff keep theirs). Values already loaded are never overwritten.
for folder in (ROOT, ROOT.parent):
    if (folder / ".env").exists():
        load_dotenv(folder / ".env", override=False)

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

MODEL_NAME = "gpt-6-luna"
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"

# USD per 1M tokens (OpenAI standard pricing for gpt-6-luna, as used in MGT 409 Lecture 11 / HW6).
PRICES = {"input": 0.10, "cached_input": 0.01, "output": 0.50}

# Spending guard: the server refuses new AI calls once today's spend reaches this.
DAILY_BUDGET_USD = float(os.getenv("KUKUA_DAILY_BUDGET_USD", "2.00"))


def build_model() -> OpenAIResponsesModel:
    key = os.getenv("PORTKEY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("PORTKEY_API_KEY is missing. Put it in a .env file in this folder or the folder above it.")
    client = AsyncOpenAI(api_key=key, base_url=PORTKEY_BASE_URL, default_headers={
        "x-portkey-provider": "openai",
        # Portkey would otherwise replay a stored response for an identical request; OpenAI's own
        # prompt cache (cached input tokens) is separate and still works.
        "x-portkey-cache-force-refresh": "true",
    })
    return OpenAIResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))
