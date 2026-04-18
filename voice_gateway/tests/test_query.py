import json
import pytest
from pseudo_llm import pseudo_llm


@pytest.mark.asyncio
async def test_pseudo_llm_returns_all_products():
    result = await pseudo_llm("任意文字")
    items = json.loads(result)
    assert len(items) == 5
    assert items[0]["title"] == "商品1"
    assert items[4]["title"] == "商品5"


@pytest.mark.asyncio
async def test_pseudo_llm_returns_valid_json():
    result = await pseudo_llm("商品四多少钱")
    items = json.loads(result)
    assert all("title" in item and "content" in item for item in items)
