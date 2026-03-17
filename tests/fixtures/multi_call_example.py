"""Fixture with multiple calls to the same function from one caller."""


async def _internal_request(url: str, data: bytes) -> dict:
    return {"url": url}


async def process_document(content: bytes) -> str:
    """Calls _internal_request twice on different lines."""
    first = await _internal_request("http://localhost/fast", content)
    text = first.get("body", "")
    second = await _internal_request("http://localhost/slow", content)
    return text or second.get("body", "")
