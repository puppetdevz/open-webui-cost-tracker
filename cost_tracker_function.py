"""
title: Cost Tracker for OpenRouter
description: This function is designed to manage and calculate the costs associated with user interactions and model usage in a Open WebUI based on OpenRouters pricings. See https://github.com/bgeneto/open-webui-cost-tracker & https://openwebui.com/f/makimaki/cost_tracker/edit
author: Kkoolldd (maki & bgeneto)
author_url: https://github.com/bgeneto/open-webui-cost-tracker
funding_url: https://github.com/bgeneto/open-webui-cost-tracker
version: 0.3.2
license: MIT
requirements: requests, tiktoken, cachetools, pydantic
environment_variables:
disclaimer: This function is provided as is without any guarantees.
            It is your responsibility to ensure that the function meets your requirements.
            All metrics and costs are approximate and may vary depending on the model and the usage.
"""

import hashlib
import json
import os
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

import requests
import tiktoken
from cachetools import TTLCache, cached
from open_webui.utils.misc import get_last_assistant_message, get_messages_content
from pydantic import BaseModel, Field


class Config:
    DATA_DIR = "data"
    CACHE_DIR = os.path.join(DATA_DIR, ".cache")
    USER_COST_FILE = os.path.join(DATA_DIR, f"costs-{datetime.now().year}.json")
    CACHE_TTL = 432000  # try to keep model pricing json file for 5 days in the cache.
    CACHE_MAXSIZE = 16
    DECIMALS = "0.00000001"
    DEBUG_PREFIX = "DEBUG:    " + __name__ + " -"
    INFO_PREFIX = "INFO:     " + __name__ + " -"
    DEBUG = False


# Initialize cache
cache = TTLCache(maxsize=Config.CACHE_MAXSIZE, ttl=Config.CACHE_TTL)


def get_encoding(model):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        if Config.DEBUG:
            print(
                f"{Config.DEBUG_PREFIX} Encoding for model {model} not found. Using cl100k_base for computing tokens."
            )
        return tiktoken.get_encoding("cl100k_base")


class UserCostManager:
    def __init__(self, cost_file_path):
        self.cost_file_path = cost_file_path
        self._ensure_cost_file_exists()

    def _ensure_cost_file_exists(self):
        if not os.path.exists(self.cost_file_path):
            with open(self.cost_file_path, "w", encoding="UTF-8") as cost_file:
                json.dump({}, cost_file)

    def _read_costs(self):
        with open(self.cost_file_path, "r", encoding="UTF-8") as cost_file:
            return json.load(cost_file)

    def _write_costs(self, costs):
        with open(self.cost_file_path, "w", encoding="UTF-8") as cost_file:
            json.dump(costs, cost_file, indent=4)

    def update_user_cost(
        self,
        user_email: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_cost: Decimal,
    ):
        costs = self._read_costs()
        timestamp = datetime.now().isoformat()

        if user_email not in costs:
            costs[user_email] = []

        costs[user_email].append(
            {
                "model": model,
                "timestamp": timestamp,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_cost": str(total_cost),
            }
        )

        self._write_costs(costs)


class ModelCostManager:
    _best_match_cache = {}

    def __init__(self, cache_dir=Config.CACHE_DIR):
        self.cache_dir = cache_dir
        self.lock = Lock()
        self.url = (
            "https://openrouter.ai/api/v1/models"  # Changed URL to OpenRouter API
        )
        self.cache_file_path = self._get_cache_filename()
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    def _get_cache_filename(self):
        cache_file_name = hashlib.sha256(self.url.encode()).hexdigest() + ".json"
        return os.path.normpath(os.path.join(self.cache_dir, cache_file_name))

    def _is_cache_valid(self, cache_file_path):
        cache_file_mtime = os.path.getmtime(cache_file_path)
        return time.time() - cache_file_mtime < cache.ttl

    @cached(cache=cache)
    def get_cost_data(self):
        """
        Fetches model pricing data from OpenRouter API and transforms it into the required format.
        """
        with self.lock:
            if os.path.exists(self.cache_file_path) and self._is_cache_valid(
                self.cache_file_path
            ):
                with open(self.cache_file_path, "r", encoding="UTF-8") as cache_file:
                    if Config.DEBUG:
                        print(
                            f"{Config.DEBUG_PREFIX} Reading costs json file from disk!"
                        )
                    return json.load(cache_file)
        try:
            if Config.DEBUG:
                print(f"{Config.DEBUG_PREFIX} Downloading model costs json file!")
            response = requests.get(self.url)
            response.raise_for_status()
            raw_data = response.json()

            # Transform OpenRouter data format to match expected format
            transformed_data = {}
            for model in raw_data["data"]:
                model_id = model[
                    "id"
                ].lower()  # Ensure lowercase for consistent matching
                transformed_data[model_id] = {
                    "input_cost_per_token": float(model["pricing"]["prompt"]),
                    "output_cost_per_token": float(model["pricing"]["completion"]),
                    "context_window": model["context_length"],
                }
                if Config.DEBUG:
                    print(f"{Config.DEBUG_PREFIX} Added pricing for model: {model_id}")

            # backup existing cache file
            try:
                if os.path.exists(self.cache_file_path):
                    os.rename(self.cache_file_path, self.cache_file_path + ".bkp")
            except Exception as e:
                print(f"**ERROR: Failed to backup costs json file. Error: {e}")

            with self.lock:
                with open(self.cache_file_path, "w", encoding="UTF-8") as cache_file:
                    if Config.DEBUG:
                        print(f"{Config.DEBUG_PREFIX} Writing costs to json file!")
                    json.dump(transformed_data, cache_file, indent=2)
            return transformed_data

        except Exception as e:
            print(
                f"**ERROR: Failed to download or write to costs json file. Using old cached file if available. Error: {e}"
            )
            with self.lock:
                if os.path.exists(self.cache_file_path + ".bkp"):
                    with open(
                        self.cache_file_path + ".bkp", "r", encoding="UTF-8"
                    ) as cache_file:
                        if Config.DEBUG:
                            print(
                                f"{Config.DEBUG_PREFIX} Reading costs json file from backup!"
                            )
                        return json.load(cache_file)
                else:
                    raise e

    def levenshtein_distance(self, s1, s2):
        m, n = len(s1), len(s2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]

        for i in range(m + 1):
            dp[i][0] = i
        for j in range(n + 1):
            dp[0][j] = j

        for i in range(1, m + 1):
            for j in range(1, n + 1):
                cost = 0 if s1[i - 1] == s2[j - 1] else 1
                dp[i][j] = min(
                    dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost
                )

        return dp[m][n]

    def _find_best_match(self, query: str, json_data) -> str:
        query_lower = query.lower()
        keys_lower = {key.lower(): key for key in json_data.keys()}

        # 1. 完全一致を確認
        if query_lower in keys_lower:
            return keys_lower[query_lower]

        # 2. 後方一致を確認
        for key_lower, original_key in keys_lower.items():
            if query_lower.endswith(key_lower):
                if Config.DEBUG:
                    print(
                        f"{Config.DEBUG_PREFIX} Found suffix match: {original_key} for query {query}"
                    )
                return original_key

        # 3. Levenshtein距離によるフォールバック
        threshold_ratio = 0.6 if len(query) < 15 else 0.3
        min_distance = float("inf")
        best_match = None
        threshold = round(len(query) * threshold_ratio)

        start = time.time()
        distances = (self.levenshtein_distance(query_lower, key) for key in keys_lower)
        for key, dist in zip(keys_lower.values(), distances):
            if dist < min_distance:
                min_distance = dist
                best_match = key
            if dist < 2:  # ほぼ完全一致の場合の早期終了
                return key
        end = time.time()

        if Config.DEBUG:
            print(
                f"{Config.DEBUG_PREFIX} Levenshtein distance search took {end - start:.3f} seconds"
            )

        if min_distance > threshold:
            return None  # しきい値内に一致するものが見つからなかった

        return best_match

    def _sanitize_model_name(self, name: str) -> str:
        """
        Keep the full model identifier for OpenRouter API matching
        Args:
            name (str): model name
        Returns:
            str: sanitized model name
        """
        # Remove only specific suffixes but keep the provider prefix
        suffixes = ["-tuned"]
        name = name.lower().strip()
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name

    def get_model_data(self, model):
        if not model:
            return {}

        model = self._sanitize_model_name(model)
        json_data = self.get_cost_data()

        # Direct match first
        if model in json_data:
            return json_data[model]

        # Try finding in cache
        if model in ModelCostManager._best_match_cache:
            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Using cached costs for model named '{model}'"
                )
            cached_match = ModelCostManager._best_match_cache[model]
            return json_data.get(cached_match, {})

        # Find best match
        if Config.DEBUG:
            print(
                f"{Config.DEBUG_PREFIX} Searching best match in costs file for model named '{model}'"
            )

        # Look for partial matches in model IDs
        for model_id in json_data.keys():
            if model in model_id or model_id in model:
                ModelCostManager._best_match_cache[model] = model_id
                return json_data[model_id]

        print(f"{Config.INFO_PREFIX} Model '{model}' not found in costs json file!")
        return {}


class CostCalculator:
    def __init__(
        self, user_cost_manager: UserCostManager, model_cost_manager: ModelCostManager
    ):
        self.model_cost_manager = model_cost_manager
        self.user_cost_manager = user_cost_manager

    def calculate_costs(
        self, model: str, input_tokens: int, output_tokens: int, compensation: float
    ) -> Decimal:
        model_pricing_data = self.model_cost_manager.get_model_data(model)
        if not model_pricing_data:
            print(f"{Config.INFO_PREFIX} Model '{model}' not found in costs json file!")
        input_cost_per_token = Decimal(
            str(model_pricing_data.get("input_cost_per_token", 0))
        )
        output_cost_per_token = Decimal(
            str(model_pricing_data.get("output_cost_per_token", 0))
        )

        input_cost = input_tokens * input_cost_per_token
        output_cost = output_tokens * output_cost_per_token
        total_cost = Decimal(float(compensation)) * (input_cost + output_cost)
        total_cost = total_cost.quantize(
            Decimal(Config.DECIMALS), rounding=ROUND_HALF_UP
        )

        return total_cost


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=15, description="Priority level")
        compensation: float = Field(
            default=1.0, description="Compensation for price calculation (percent)"
        )
        elapsed_time: bool = Field(default=True, description="Display the elapsed time")
        number_of_tokens: bool = Field(
            default=True, description="Display total number of tokens"
        )
        tokens_per_sec: bool = Field(
            default=True, description="Display tokens per second metric"
        )
        debug: bool = Field(default=False, description="Display debugging messages")
        pass

    def __init__(self):
        self.valves = self.Valves()
        Config.DEBUG = self.valves.debug
        self.model_cost_manager = ModelCostManager()
        self.user_cost_manager = UserCostManager(Config.USER_COST_FILE)
        self.cost_calculator = CostCalculator(
            self.user_cost_manager, self.model_cost_manager
        )
        self.start_time = None
        self.input_tokens = 0
        pass

    def _sanitize_model_name(self, name: str) -> str:
        """Sanitize model name while preserving provider prefix
        Args:
            name (str): model name
        Returns:
            str: sanitized model name
        """
        suffixes = ["-tuned"]
        name = name.lower().strip()
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name

    def _remove_roles(self, content):
        # Define the roles to be removed
        roles = ["SYSTEM:", "USER:", "ASSISTANT:", "PROMPT:"]

        # Process each line
        def process_line(line):
            for role in roles:
                if line.startswith(role):
                    return line.split(":", 1)[1].strip()
            return line  # Return the line unchanged if no role matches

        return "\n".join([process_line(line) for line in content.split("\n")])

    def _get_model(self, body):
        if "model" in body:
            return self._sanitize_model_name(body["model"])
        return None

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:

        Config.DEBUG = self.valves.debug
        enc = tiktoken.get_encoding("cl100k_base")
        input_content = self._remove_roles(
            get_messages_content(body["messages"])
        ).strip()
        self.input_tokens = len(enc.encode(input_content))

        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": f"Processing {self.input_tokens} input tokens...",
                    "done": False,
                },
            }
        )

        # add user email to payload in order to track costs
        if __user__:
            if "email" in __user__:
                if Config.DEBUG:
                    print(
                        f"{Config.DEBUG_PREFIX} Adding email to request body: {__user__['email']}"
                    )
                body["user"] = __user__["email"]

        self.start_time = time.time()

        return body

    async def outlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:

        end_time = time.time()
        elapsed_time = end_time - self.start_time

        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": "Computing number of output tokens...",
                    "done": False,
                },
            }
        )

        model = self._get_model(body)
        enc = tiktoken.get_encoding("cl100k_base")
        output_tokens = len(enc.encode(get_last_assistant_message(body["messages"])))

        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": "Computing total costs...", "done": False},
            }
        )

        total_cost = self.cost_calculator.calculate_costs(
            model, self.input_tokens, output_tokens, self.valves.compensation
        )

        if __user__:
            if "email" in __user__:
                user_email = __user__["email"]
            else:
                print("**ERROR: User email not found!")
            try:
                self.user_cost_manager.update_user_cost(
                    user_email,
                    model,
                    self.input_tokens,
                    output_tokens,
                    total_cost,
                )
            except Exception as _:
                print("**ERROR: Unable to update user cost file!")
        else:
            print("**ERROR: User not found!")

        tokens = self.input_tokens + output_tokens
        tokens_per_sec = tokens / elapsed_time

        # 获取匹配的模型名称
        matched_model = None
        if model in self.model_cost_manager._best_match_cache:
            matched_model = self.model_cost_manager._best_match_cache[model]

        # 构建统计数组
        stats_array = []

        if self.valves.elapsed_time:
            stats_array.append(f"⌛{elapsed_time:.2f}s")
        if self.valves.tokens_per_sec:
            stats_array.append(f"🚀{tokens_per_sec:.2f}t/s")
        if self.valves.number_of_tokens:
            # 分拆为输入、输出和总tokens，用简洁的emoji表示
            stats_array.append(f"⬇️{self.input_tokens}")  # 输入tokens
            stats_array.append(f"⬆️{output_tokens}")  # 输出tokens
            stats_array.append(f"🔢:{tokens}")  # 总tokens
        # 处理金额显示
        if float(total_cost) <= 0:
            stats_array.append(f"💲0.00")  # 金额小于0时显示0美元
        elif float(total_cost) < float(Config.DECIMALS):
            stats_array.append(f"💲{total_cost:.2f}")
        else:
            stats_array.append(f"💲{total_cost:.10f}")
        # 添加匹配的模型显示
        if matched_model and matched_model != model:
            stats_array.append(f"✅️{matched_model}")  # 显示匹配到的实际模型
        stats = " | ".join(stats_array)
        await __event_emitter__(
            {"type": "status", "data": {"description": stats, "done": True}}
        )

        return body
