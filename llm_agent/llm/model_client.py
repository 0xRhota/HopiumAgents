"""
Multi-Model Client
Supports DeepSeek (direct) and Qwen (via OpenRouter)

Usage:
    # DeepSeek (default)
    client = ModelClient(api_key="deepseek_key")

    # Qwen via OpenRouter (Alpha Arena winner!)
    client = ModelClient(api_key="openrouter_key", model="qwen-max")
"""

import requests
import logging
import time
from typing import Optional, Dict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Model configurations
MODEL_CONFIGS = {
    # DeepSeek models (direct API)
    "deepseek-chat": {
        "provider": "deepseek",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model_id": "deepseek-chat",
        "input_cost_per_1k": 0.00014,
        "output_cost_per_1k": 0.00028,
        "default_max_tokens": 500,
    },
    "deepseek-reasoner": {
        "provider": "deepseek",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model_id": "deepseek-reasoner",
        "input_cost_per_1k": 0.00055,
        "output_cost_per_1k": 0.00220,
        "default_max_tokens": 500,
    },
    # Qwen via OpenRouter - Alpha Arena winner!
    "qwen-max": {
        "provider": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model_id": "qwen/qwen3-235b-a22b",
        "input_cost_per_1k": 0.0012,   # ~$1.20/M
        "output_cost_per_1k": 0.006,    # ~$6.00/M
        "default_max_tokens": 1500,     # Qwen needs more tokens for thinking
    },
}


class ModelClient:
    """Multi-model API client supporting DeepSeek and Qwen"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        max_retries: int = 2,
        daily_spend_limit: float = 10.0,
        timeout: int = 60
    ):
        """
        Initialize model client

        Args:
            api_key: API key (DeepSeek or OpenRouter depending on model)
            model: Model name (default: deepseek-chat)
            max_retries: Number of retries on failure
            daily_spend_limit: Max USD to spend per day
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.daily_spend_limit = daily_spend_limit
        self.timeout = timeout

        # Validate model
        if model not in MODEL_CONFIGS:
            available = ", ".join(MODEL_CONFIGS.keys())
            raise ValueError(f"Unknown model '{model}'. Available: {available}")

        self.config = MODEL_CONFIGS[model]
        self.provider = self.config["provider"]
        self.url = self.config["url"]

        # Track spending
        self._daily_spend = 0.0
        self._spend_reset_time = datetime.now() + timedelta(days=1)
        self._last_request_time = None

        # Rate limiting
        try:
            from utils.shared_rate_limiter import SharedRateLimiter
            self.shared_limiter = SharedRateLimiter()
            self._use_shared_limiter = True
            logger.info("✅ Using shared rate limiter")
        except ImportError:
            self.shared_limiter = None
            self._use_shared_limiter = False
            self._min_request_interval = 1.0

        logger.info(f"✅ ModelClient initialized: {model} via {self.provider}")

    def _reset_daily_spend_if_needed(self):
        """Reset daily spend counter if new day"""
        if datetime.now() >= self._spend_reset_time:
            logger.info(f"Daily spend reset (previous: ${self._daily_spend:.4f})")
            self._daily_spend = 0.0
            self._spend_reset_time = datetime.now() + timedelta(days=1)

    def _check_spend_limit(self, estimated_cost: float):
        """Check if request would exceed daily spend limit"""
        self._reset_daily_spend_if_needed()

        if self._daily_spend + estimated_cost > self.daily_spend_limit:
            raise Exception(
                f"Daily spend limit exceeded: ${self._daily_spend:.4f} + ${estimated_cost:.4f} "
                f"> ${self.daily_spend_limit:.2f}"
            )

    def _rate_limit(self):
        """Enforce minimum time between requests"""
        if self._use_shared_limiter and self.shared_limiter:
            self.shared_limiter.wait_if_needed(bot_name="Hibachi")
        else:
            if self._last_request_time is not None:
                elapsed = time.time() - self._last_request_time
                if elapsed < self._min_request_interval:
                    time.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.time()

    def _calculate_cost(self, usage: Dict) -> float:
        """Calculate cost from token usage"""
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        input_cost = (prompt_tokens / 1000) * self.config["input_cost_per_1k"]
        output_cost = (completion_tokens / 1000) * self.config["output_cost_per_1k"]

        return input_cost + output_cost

    def query(
        self,
        prompt: str,
        max_tokens: int = None,
        temperature: float = 0.1,
        retry_count: int = 0
    ) -> Optional[Dict]:
        """
        Query the model with retry logic

        Args:
            prompt: User prompt
            max_tokens: Max tokens to generate (uses model default if None)
            temperature: Sampling temperature (0.1 for deterministic)
            retry_count: Current retry attempt

        Returns:
            Dict with keys: content, usage, cost
            None if all retries failed
        """
        # Use model default if not specified
        if max_tokens is None:
            max_tokens = self.config["default_max_tokens"]

        # Estimate cost
        estimated_cost = ((len(prompt) / 4 + max_tokens) / 1000) * self.config["output_cost_per_1k"]
        self._check_spend_limit(estimated_cost)

        # Rate limiting
        self._rate_limit()

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Add OpenRouter-specific headers
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/trading-bot"
            headers["X-Title"] = "Trading Bot"

        # For Qwen models, disable thinking mode to get direct responses
        # Qwen 3 uses extended thinking by default which returns empty content
        actual_prompt = prompt
        if self.provider == "openrouter" and "qwen" in self.config["model_id"].lower():
            # Add /no_think to disable extended thinking mode
            actual_prompt = f"/no_think\n{prompt}"

        payload = {
            "model": self.config["model_id"],
            "messages": [
                {
                    "role": "user",
                    "content": actual_prompt
                }
            ],
            "max_tokens": max_tokens,
            "temperature": temperature
        }

        try:
            logger.info(f"{self.model} API request (attempt {retry_count + 1}/{self.max_retries + 1})...")

            response = requests.post(
                self.url,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )

            if response.status_code == 200:
                data = response.json()

                if "choices" in data and len(data["choices"]) > 0:
                    # Get content - Qwen may have it in reasoning field
                    message = data["choices"][0]["message"]
                    content = message.get("content", "")

                    # Handle Qwen reasoning mode - extract answer after </think> tag
                    if not content and "reasoning_content" in message:
                        # Qwen3 models put reasoning in reasoning_content, answer in content
                        reasoning = message.get("reasoning_content", "")
                        logger.info(f"Qwen reasoning mode - reasoning: {len(reasoning)} chars")
                        # Content should have the answer, but if empty use reasoning
                        if not content and reasoning:
                            # Extract answer from reasoning if it has </think> pattern
                            if "</think>" in reasoning:
                                content = reasoning.split("</think>")[-1].strip()
                            else:
                                content = reasoning
                            logger.info(f"Extracted content from reasoning: {len(content)} chars")
                    elif not content and "reasoning" in message:
                        # Older reasoning format - also try to extract content
                        reasoning = message.get("reasoning", "")
                        logger.warning(f"Qwen reasoning mode (legacy) - reasoning: {len(reasoning)} chars")
                        if reasoning:
                            # Extract answer from reasoning if it has </think> pattern
                            if "</think>" in reasoning:
                                content = reasoning.split("</think>")[-1].strip()
                            else:
                                content = reasoning
                            logger.info(f"Extracted content from legacy reasoning: {len(content)} chars")

                    usage = data.get("usage", {})

                    # Calculate actual cost
                    cost = self._calculate_cost(usage)
                    self._daily_spend += cost

                    logger.info(
                        f"✅ {self.model} response received "
                        f"(tokens: {usage.get('total_tokens')}, cost: ${cost:.4f}, "
                        f"daily total: ${self._daily_spend:.4f})"
                    )

                    return {
                        "content": content,
                        "usage": usage,
                        "cost": cost
                    }
                else:
                    logger.warning(f"{self.model} response missing 'choices'")
                    return None

            elif response.status_code == 429:
                wait_time = min(30 * (2 ** retry_count), 120)
                logger.warning(f"{self.model} rate limit (429), waiting {wait_time}s...")
                time.sleep(wait_time)

                if retry_count < self.max_retries:
                    return self.query(prompt, max_tokens, temperature, retry_count + 1)
                else:
                    logger.error("Max retries reached after rate limit")
                    return None

            elif response.status_code == 402:
                logger.error(f"{self.model} insufficient balance (402)")
                return None

            else:
                logger.error(f"{self.model} API error: HTTP {response.status_code}")
                logger.error(f"Response: {response.text[:500]}")

                if retry_count < self.max_retries:
                    logger.info("Retrying in 2 seconds...")
                    time.sleep(2)
                    return self.query(prompt, max_tokens, temperature, retry_count + 1)
                else:
                    return None

        except requests.exceptions.Timeout:
            logger.error(f"{self.model} request timeout ({self.timeout}s)")

            if retry_count < self.max_retries:
                logger.info("Retrying...")
                return self.query(prompt, max_tokens, temperature, retry_count + 1)
            else:
                return None

        except Exception as e:
            logger.error(f"{self.model} API error: {e}")

            if retry_count < self.max_retries:
                logger.info("Retrying...")
                time.sleep(2)
                return self.query(prompt, max_tokens, temperature, retry_count + 1)
            else:
                return None

    def get_daily_spend(self) -> float:
        """Get current daily spend in USD"""
        self._reset_daily_spend_if_needed()
        return self._daily_spend

    def get_remaining_budget(self) -> float:
        """Get remaining budget for today"""
        self._reset_daily_spend_if_needed()
        return self.daily_spend_limit - self._daily_spend

    @staticmethod
    def list_models() -> Dict:
        """List all available models with their costs"""
        return {
            name: {
                "provider": cfg["provider"],
                "input_cost": f"${cfg['input_cost_per_1k']*1000:.2f}/M tokens",
                "output_cost": f"${cfg['output_cost_per_1k']*1000:.2f}/M tokens",
            }
            for name, cfg in MODEL_CONFIGS.items()
        }
