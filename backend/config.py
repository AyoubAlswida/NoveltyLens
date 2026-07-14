"""Application configuration sourced exclusively from environment variables."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Settings:
    hf_token: str = os.getenv("HF_TOKEN", "")
    serpapi_key: str = os.getenv("SERPAPI_KEY", "")
    epo_key: str = os.getenv("EPO_KEY", os.getenv("EPO_CONSUMER_KEY", ""))
    epo_secret: str = os.getenv("EPO_SECRET", os.getenv("EPO_CONSUMER_SECRET", ""))
    llm_api_url: str = os.getenv("LLM_API_URL", "https://router.huggingface.co/v1/chat/completions")
    llm_model: str = os.getenv("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct:novita")


settings = Settings()
