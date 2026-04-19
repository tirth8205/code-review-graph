"""Mini cloud API clients for MRR eval fixture — identical signatures, body differs."""

import os


class OpenAIClient:
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.base_url = "https://api.openai.com/v1"
        self.default_model = "text-embedding-3-small"


class GeminiClient:
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self.default_model = "gemini-embedding-001"


class MinimaxClient:
    def __init__(self):
        self.api_key = os.environ.get("MINIMAX_API_KEY", "")
        self.base_url = "https://api.minimax.io/v1"
        self.default_model = "embo-01"
