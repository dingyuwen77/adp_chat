"""只规划企业微信输出；保留正式 reply 顺序，不改变 ADP/API/终端内容。"""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass


_URL = re.compile(r"https?://", re.I)
_IMAGE = re.compile(r"(?<!\\)!\[([^\[\]\n]*)\]\(")


def _url_ranges(text: str) -> list[tuple[int, int]]:
    """在 URL 自身平衡括号处结束，不吞入 Markdown 右括号后的正文。"""
    ranges, consumed = [], 0
    for match in _URL.finditer(text):
        if match.start() < consumed:
            continue
        round_depth, square_depth, end = 0, 0, len(text)
        for index in range(match.end(), len(text)):
            char = text[index]
            if char.isspace() or char in '<>"\'。！？；，':
                end = index
                break
            if char == "(":
                round_depth += 1
            elif char == ")":
                if not round_depth:
                    end = index
                    break
                round_depth -= 1
            elif char == "[":
                square_depth += 1
            elif char == "]":
                if not square_depth:
                    end = index
                    break
                square_depth -= 1
        ranges.append((match.start(), end))
        consumed = end
    return ranges


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """代码围栏与反引号游标只向前推进，畸形长串不触发正则尾部回溯。"""
    fences = list(re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)", text))
    ranges, index = [], 0
    while index < len(fences):
        opening = fences[index]
        delimiter = opening.group(1)
        end = len(text)
        index += 1
        while index < len(fences):
            closing = fences[index]
            index += 1
            if (closing.group(1)[0] == delimiter[0] and len(closing.group(1)) >= len(delimiter)
                    and not text[closing.end(1):closing.end()].strip()):
                end = closing.end()
                break
        ranges.append((opening.start(), end))
    runs = list(re.finditer(r"`+", text))
    next_run, next_same = {}, {}
    for index in range(len(runs) - 1, -1, -1):
        length = len(runs[index].group())
        if length in next_run:
            next_same[index] = next_run[length]
        next_run[length] = index
    inline, index, fence_index = [], 0, 0
    while index < len(runs):
        start = runs[index].start()
        while fence_index < len(ranges) and ranges[fence_index][1] <= start:
            fence_index += 1
        if fence_index < len(ranges) and ranges[fence_index][0] <= start:
            index += 1
            continue
        closing = next_same.get(index)
        if closing is not None and (fence_index == len(ranges) or runs[closing].end() <= ranges[fence_index][0]):
            inline.append((start, runs[closing].end()))
            index = closing + 1
        else:
            index += 1
    return sorted(ranges + inline)


@dataclass(frozen=True)
class TextBlock:
    """等待按企业微信字节限制分段的原始文字。"""

    text: str


@dataclass(frozen=True)
class ImageBlock:
    """原位置的图片引用，下载或上传失败时使用安全文字降级。"""

    url: str
    alt: str = ""

    def fallback(self) -> str:
        """保留可打开的 HTTP(S) 原链接，不把危险协议变成可点击内容。"""
        url = self.url if self.url.lower().startswith(("https://", "http://")) else "（图片地址不可用）"
        return f"[图片未能显示，请打开链接查看]\n{url}"


def text_parts(text: str) -> list[str]:
    """完整按 UTF-8 分段，优先段落/换行，能容纳的 URL 不从中切断。"""
    if not isinstance(text, str) or not text:
        raise ValueError("empty notification")
    # 单个 URL 已超平台上限时，明确告知无法展示，避免生成看似可用的半条链接。
    rewritten, cursor = [], 0
    for start, end in _url_ranges(text):
        rewritten.append(text[cursor:start])
        rewritten.append("[链接过长，无法在企业微信文字消息中完整展示]"
                         if len(text[start:end].encode("utf-8")) > 2048 else text[start:end])
        cursor = end
    rewritten.append(text[cursor:])
    text = "".join(rewritten)
    urls = _url_ranges(text)
    url_starts = [a for a, _ in urls]
    raw = text.encode("utf-8")
    parts, start, byte_start = [], 0, 0
    while start < len(text):
        end = start + len(raw[byte_start:byte_start + 2048].decode("utf-8", errors="ignore"))
        if end < len(text):
            url_index = bisect_right(url_starts, end) - 1
            if url_index >= 0 and urls[url_index][0] < end < urls[url_index][1]:
                end = urls[url_index][0]
            # 自然边界过近会产生大量短消息；只在当前容量后半区寻找。
            floor = start + (end - start) // 2
            for separator in ("\n\n", "\n", "。", "！", "？", " "):
                boundary = text.rfind(separator, floor, end)
                cut = boundary + len(separator)
                url_index = bisect_right(url_starts, cut) - 1
                inside_url = url_index >= 0 and urls[url_index][0] < cut < urls[url_index][1]
                if boundary >= floor and not inside_url:
                    end = cut
                    break
        parts.append(text[start:end])
        byte_start += len(parts[-1].encode("utf-8"))
        start = end
    return parts


def _markdown(text: str) -> list[TextBlock | ImageBlock]:
    """识别行内图片及平衡括号，代码/转义语法原样保留，普通来源不转换。"""
    blocks, cursor, code_index = [], 0, 0
    codes = _code_ranges(text)
    matches = list(_IMAGE.finditer(text))
    whitespace = list(re.finditer(r"\s+", text))
    space_starts = [m.start() for m in whitespace]
    space_ends = [m.end() for m in whitespace]
    angles = [i for i, char in enumerate(text) if char == ">"]
    candidates = {m.end() - 1 for m in matches}
    stack, closing, escaped = [], {}, False
    # 只扫描一次括号关系；每个坏的图片开头不能重新读取整段尾部。
    for index, char in enumerate(text):
        if not escaped:
            if char == "(":
                stack.append(index)
            elif char == ")" and stack:
                opening = stack.pop()
                if opening in candidates:
                    closing[opening] = index
        escaped = char == "\\" and not escaped
    for match in matches:
        while code_index < len(codes) and codes[code_index][1] <= match.start():
            code_index += 1
        if (match.start() < cursor or code_index < len(codes)
                and codes[code_index][0] <= match.start() < codes[code_index][1]):
            continue
        end = closing.get(match.end() - 1)
        if end is None:
            continue
        syntax_end = end + 1
        start = match.end()
        # 空白游程只扫描/索引一次；嵌套候选共享空白区时不能重复逐字符遍历。
        space_index = bisect_right(space_starts, start) - 1
        if space_index >= 0 and start < space_ends[space_index]:
            start = min(end, space_ends[space_index])
        space_index = bisect_right(space_starts, end - 1) - 1
        if space_index >= 0 and end <= space_ends[space_index]:
            end = max(start, space_starts[space_index])
        if start == end:
            continue
        space_index = bisect_left(space_starts, start)
        first_space = space_starts[space_index] if space_index < len(space_starts) else len(text)
        # CommonMark 的 <URL> 及可选 title；管道等查询参数不得按分隔符截断。
        if text[start] == "<":
            angle_index = bisect_left(angles, start)
            if angle_index == len(angles) or angles[angle_index] >= end:
                continue
            url_start, url_end = start + 1, angles[angle_index]
        else:
            url_start, url_end = start, min(first_space, end)
            title = url_end
            space_index = bisect_right(space_starts, title) - 1
            if space_index >= 0 and title < space_ends[space_index]:
                title = min(end, space_ends[space_index])
            if title < end and text[title] not in "\"'":
                continue
        # 先用索引判断无效嵌套目的地，再复制；不能为每个候选复制同一长后缀。
        if url_start == url_end or first_space < url_end:
            continue
        destination = text[url_start:url_end]
        if match.start() > cursor:
            blocks.append(TextBlock(text[cursor:match.start()]))
        blocks.append(ImageBlock(destination, match.group(1)))
        cursor = syntax_end
    if cursor < len(text):
        blocks.append(TextBlock(text[cursor:]))
    return blocks


def reply_plan(record: dict) -> list[TextBlock | ImageBlock]:
    """优先正式持久化内容；未知/忙碌通知只使用其已保存的风险说明。"""
    result = record.get("adp_result") or {}
    messages = result.get("reply_contents") if record.get("adp_state") == "ADP_SUCCEEDED" else None
    blocks, pending = [], []
    if messages and any(messages):
        for message in messages:
            if blocks or pending:
                pending.append("\n")
            for part in message:
                kind = part.get("Type")
                if kind in ("text", "json_text"):
                    pending.append(part["Text"])
                    continue
                if pending:
                    blocks.extend(_markdown("".join(pending)))
                    pending.clear()
                if kind == "image":
                    image = part.get("Image")
                    url = image.get("Url") if isinstance(image, dict) else None
                    blocks.append(ImageBlock(url) if isinstance(url, str) and url else
                                  TextBlock("[图片未能显示：缺少可用地址]"))
                else:
                    blocks.append(TextBlock("[本回复含当前渠道未能呈现的媒体内容]"))
        if pending:
            blocks.extend(_markdown("".join(pending)))
            pending.clear()
    else:
        blocks = _markdown(record["notification"])
    merged = []
    for block in blocks:
        if isinstance(block, TextBlock):
            pending.append(block.text)
            continue
        if pending:
            merged.append(TextBlock("".join(pending)))
            pending.clear()
        merged.append(block)
    if pending:
        merged.append(TextBlock("".join(pending)))
    return merged or [TextBlock(record["notification"])]
