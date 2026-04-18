"""Pseudo LLM stub. Simulates LLM latency, returns all products."""
import asyncio
import json
import logging

log = logging.getLogger(__name__)

PRODUCTS = [
    {"title": "商品1", "content": "价格：100元，库存充足"},
    {"title": "商品2", "content": "价格：200元，库存充足"},
    {"title": "商品3", "content": "价格：300元，限时优惠"},
    {"title": "商品4", "content": "价格：400元，需要预订"},
    {"title": "商品5", "content": "价格：500元，新品上市"},
]


async def pseudo_llm(text: str) -> str:
    """Simulate LLM call: wait 2s then return all products as JSON."""
    log.info(f"pseudo_llm called with: '{text[:80]}'")
    await asyncio.sleep(2)
    result = json.dumps(PRODUCTS, ensure_ascii=False)
    log.info(f"pseudo_llm returning {len(PRODUCTS)} products")
    return result
