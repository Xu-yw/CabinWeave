"""LLM response helpers shared by Anthropic-compatible providers."""
from typing import Any, Dict, Iterable, List

# 关闭思考模式的请求参数（SDK 版本较旧，没有 thinking 形参，经 extra_body 透传）
THINKING_DISABLED: Dict[str, Any] = {"thinking": {"type": "disabled"}}


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)
