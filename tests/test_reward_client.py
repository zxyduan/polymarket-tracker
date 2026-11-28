"""RewardMarketClient 单测：Gamma 条目匹配与失败隔离。"""

from __future__ import annotations

from framework.sources.reward_client import RewardMarketClient

CID = "0x8a4196617dcb703e49d9bb36d3847bf6a75fd364dbd19f8d408ac944bf1a03d8"


class FakeResp:
    def __init__(self, status: int, data: object, *, headers: dict | None = None,
                 json_error: bool = False) -> None:
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error:
            raise ValueError("not json")
        return self._data


class FakeClient:
    def __init__(self, responses: list[FakeResp]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    async def get(self, url: str, params: dict | None = None) -> FakeResp:
        self.calls.append(params or {})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


def _entry(cid: str) -> dict:
    return {
        "conditionId": cid,
        "question": "q",
        "clobRewards": [
            {"rewardsDailyRate": "750", "assetAddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"}
        ],
    }


def _client(responses: list[FakeResp]) -> RewardMarketClient:
    return RewardMarketClient(
        client=FakeClient(responses), max_retries=2, request_interval_seconds=0
    )


async def test_matches_condition_in_list():
    fake = FakeClient([FakeResp(200, [_entry("0xother"), _entry(CID)])])
    client = RewardMarketClient(client=fake, max_retries=1, request_interval_seconds=0)
    entry = await client.get_reward(CID)
    assert entry is not None
    assert entry["conditionId"] == CID
    assert fake.calls[0] == {"condition_ids": CID}


async def test_data_wrapper_compat():
    client = _client([FakeResp(200, {"data": [_entry(CID)]})])
    entry = await client.get_reward(CID)
    assert entry is not None and entry["conditionId"] == CID


async def test_no_match_returns_none():
    client = _client([FakeResp(200, [_entry("0xother")])])
    assert await client.get_reward(CID) is None


async def test_4xx_returns_none():
    client = _client([FakeResp(404, [])])
    assert await client.get_reward(CID) is None


async def test_429_retries_then_success():
    fake = FakeClient([FakeResp(429, []), FakeResp(200, [_entry(CID)])])
    client = RewardMarketClient(client=fake, max_retries=2, request_interval_seconds=0)
    entry = await client.get_reward(CID)
    assert entry is not None and entry["conditionId"] == CID
    assert len(fake.calls) == 2


async def test_non_json_returns_none():
    client = _client([FakeResp(200, None, json_error=True)])
    assert await client.get_reward(CID) is None


async def test_empty_condition_id_returns_none():
    client = _client([])
    assert await client.get_reward("") is None
