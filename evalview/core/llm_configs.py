"""LLM provider configurations, pricing, and model aliases.

Extracted from llm_provider.py to keep data declarations separate from
client logic. All names are re-exported from llm_provider.py for backward
compatibility.
"""

import os
import logging
from typing import Dict, List, NamedTuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class LLMProvider(Enum):
    """Supported LLM providers for evaluation."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROK = "grok"
    DEEPSEEK = "deepseek"
    HUGGINGFACE = "huggingface"
    OLLAMA = "ollama"


class AvailableProvider(NamedTuple):
    """Result from detect_available_providers().

    Note: This contains the API key, NOT the model name.
    Use PROVIDER_CONFIGS[provider].default_model to get the default model.
    """

    provider: LLMProvider
    api_key: str


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider."""

    name: str
    env_var: str
    default_model: str
    display_name: str
    api_key_url: str


# Provider configurations
PROVIDER_CONFIGS: Dict[LLMProvider, ProviderConfig] = {
    LLMProvider.OPENAI: ProviderConfig(
        name="openai",
        env_var="OPENAI_API_KEY",
        default_model="gpt-5-mini",
        display_name="OpenAI",
        api_key_url="https://platform.openai.com/api-keys",
    ),
    LLMProvider.ANTHROPIC: ProviderConfig(
        name="anthropic",
        env_var="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        display_name="Anthropic",
        api_key_url="https://console.anthropic.com/settings/keys",
    ),
    LLMProvider.GEMINI: ProviderConfig(
        name="gemini",
        env_var="GEMINI_API_KEY",
        default_model="gemini-2.0-flash",
        display_name="Google Gemini",
        api_key_url="https://aistudio.google.com/app/apikey",
    ),
    LLMProvider.GROK: ProviderConfig(
        name="grok",
        env_var="XAI_API_KEY",
        default_model="grok-2-latest",
        display_name="xAI Grok",
        api_key_url="https://console.x.ai/",
    ),
    LLMProvider.DEEPSEEK: ProviderConfig(
        name="deepseek",
        env_var="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        display_name="DeepSeek",
        api_key_url="https://platform.deepseek.com/api_keys",
    ),
    LLMProvider.HUGGINGFACE: ProviderConfig(
        name="huggingface",
        env_var="HF_TOKEN",
        default_model="meta-llama/Llama-3.1-8B-Instruct",
        display_name="Hugging Face",
        api_key_url="https://huggingface.co/settings/tokens",
    ),
    LLMProvider.OLLAMA: ProviderConfig(
        name="ollama",
        env_var="OLLAMA_HOST",  # Optional - defaults to localhost:11434
        default_model="llama3.2",
        display_name="Ollama (Local)",
        api_key_url="https://ollama.ai/download",  # Download page, no API key needed
    ),
}

# Model aliases for better DX - shortcuts map to full model names
MODEL_ALIASES: Dict[str, str] = {
    # OpenAI GPT-5 family (use simple names - they track latest)
    "gpt-5": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5-nano": "gpt-5-nano",
    "gpt-5.1": "gpt-5.1",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    # OpenAI GPT-4 family (legacy)
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4": "gpt-4-turbo",
    # Anthropic Claude
    "sonnet": "claude-sonnet-4-5-20250929",
    "claude-sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
    "claude-opus": "claude-opus-4-6",
    "opus-4.6": "claude-opus-4-6",
    "opus-4.5": "claude-opus-4-5-20251101",
    "haiku": "claude-haiku-4-5-20251001",
    "claude-haiku": "claude-haiku-4-5-20251001",
    # HuggingFace Llama
    "llama": "meta-llama/Llama-3.1-8B-Instruct",
    "llama-8b": "meta-llama/Llama-3.1-8B-Instruct",
    "llama-70b": "meta-llama/Llama-3.1-70B-Instruct",
    # Google Gemini
    "gemini": "gemini-3.0",
    "gemini-3": "gemini-3.0",
    "gemini-flash": "gemini-2.0-flash",
    "gemini-pro": "gemini-1.5-pro",
    # Ollama (local models)
    "ollama-llama": "llama3.2",
    "llama3.2": "llama3.2",
    "llama3.1": "llama3.1",
    "mistral": "mistral",
    "codellama": "codellama",
    "phi": "phi",
}


def resolve_model_alias(model: str) -> str:
    """Resolve model alias to full model name.

    Args:
        model: Model name or alias (e.g., 'gpt-5', 'sonnet', 'llama-70b')

    Returns:
        Full model name (e.g., 'gpt-5-2025-08-07', 'claude-sonnet-4-5-20250929')
    """
    return MODEL_ALIASES.get(model.lower(), model)


class JudgeCostTracker:
    """Track LLM-as-judge API costs across all evaluations."""

    # Pricing per 1M tokens (input, output)
    PRICING = {
        "openai": {
            "gpt-5.4": (2.00, 8.00),
            "gpt-5.4-mini": (0.10, 0.40),
            "gpt-5": (2.00, 8.00),
            "gpt-5-mini": (0.10, 0.40),
            "gpt-5-nano": (0.05, 0.20),
            "gpt-4o": (2.50, 10.00),
            "gpt-4o-mini": (0.15, 0.60),
            "gpt-4-turbo": (10.00, 30.00),
        },
        "anthropic": {
            "claude-opus-4-6": (5.00, 25.00),
            "claude-opus-4-5-20251101": (5.00, 25.00),
            "claude-sonnet-4-5-20250929": (3.00, 15.00),
            "claude-haiku-4-5-20251001": (0.25, 1.25),
            "claude-3-5-haiku-latest": (0.25, 1.25),
        },
        "gemini": {
            "gemini-2.0-flash": (0.10, 0.40),
            "gemini-1.5-pro": (1.25, 5.00),
        },
        "deepseek": {
            "deepseek-chat": (0.14, 0.28),
            "deepseek-reasoner": (0.55, 2.19),
        },
        "ollama": {},  # Free - local
        "huggingface": {
            "meta-llama/Llama-3.1-8B-Instruct": (0.05, 0.05),
        },
    }

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0

    def add_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int):
        """Track token usage and calculate cost."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.call_count += 1

        # Calculate cost
        pricing = self.PRICING.get(provider, {})
        model_pricing = None

        # Try exact match first, then partial match
        for model_name, prices in pricing.items():
            if model_name in model or model in model_name:
                model_pricing = prices
                break

        if model_pricing:
            input_cost = (input_tokens / 1_000_000) * model_pricing[0]
            output_cost = (output_tokens / 1_000_000) * model_pricing[1]
            self.total_cost += input_cost + output_cost

    def get_summary(self) -> str:
        """Get a summary string of costs."""
        if self.call_count == 0:
            return "No judge calls yet"

        total_tokens = self.total_input_tokens + self.total_output_tokens

        if self.total_cost > 0:
            # Paid API - show cost prominently
            return f"${self.total_cost:.4f} | {total_tokens:,} tokens ({self.call_count} calls)"
        else:
            # Free (Ollama) - just show tokens
            return f"FREE | {total_tokens:,} tokens ({self.call_count} calls)"

    def get_detailed_summary(self) -> str:
        """Get detailed breakdown of costs."""
        if self.call_count == 0:
            return "No judge calls yet"

        lines = []
        lines.append("Judge LLM Usage:")
        lines.append(f"  Calls:         {self.call_count}")
        lines.append(f"  Input tokens:  {self.total_input_tokens:,}")
        lines.append(f"  Output tokens: {self.total_output_tokens:,}")
        lines.append(f"  Total tokens:  {self.total_input_tokens + self.total_output_tokens:,}")

        if self.total_cost > 0:
            lines.append(f"  Total cost:    ${self.total_cost:.4f}")
        else:
            lines.append("  Total cost:    FREE (local model)")

        return "\n".join(lines)

    def reset(self):
        """Reset all counters."""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.call_count = 0


# Global cost tracker instance
judge_cost_tracker = JudgeCostTracker()


def is_ollama_running() -> bool:
    """Check if Ollama is running locally.

    Returns:
        True if Ollama is accessible at localhost:11434
    """
    import socket

    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    # Parse host and port from URL
    host = ollama_host.replace("http://", "").replace("https://", "")
    if ":" in host:
        host, port_str = host.split(":", 1)
        port = int(port_str)
    else:
        port = 11434

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect((host, port))
            return True
    except (socket.timeout, socket.error, OSError):
        return False


def detect_available_providers() -> List[AvailableProvider]:
    """Detect which LLM providers have API keys configured.

    For most providers, checks if the environment variable is set.
    For Ollama, checks if the server is running locally (no API key needed).

    Returns:
        List of AvailableProvider(provider, api_key) for available providers.

        IMPORTANT: The second field is the API key, NOT the model name.
        To get the default model, use: PROVIDER_CONFIGS[provider].default_model

    Example:
        >>> available = detect_available_providers()
        >>> for p in available:
        ...     print(f"{p.provider}: key={p.api_key[:8]}...")
        ...     model = PROVIDER_CONFIGS[p.provider].default_model
        ...     print(f"  default model: {model}")
    """
    available: List[AvailableProvider] = []
    for provider, config in PROVIDER_CONFIGS.items():
        if provider == LLMProvider.OLLAMA:
            # Ollama doesn't need an API key - check if it's running
            if is_ollama_running():
                available.append(AvailableProvider(provider, "ollama"))  # Placeholder "key"
        else:
            api_key = os.getenv(config.env_var)
            if api_key:
                available.append(AvailableProvider(provider, api_key))
    return available


def get_provider_from_env() -> "LLMProvider | None":
    """Get the user-selected provider from EVAL_PROVIDER env var."""
    provider_name = os.getenv("EVAL_PROVIDER", "").lower()
    if not provider_name:
        return None

    for provider in LLMProvider:
        if provider.value == provider_name:
            return provider
    return None
