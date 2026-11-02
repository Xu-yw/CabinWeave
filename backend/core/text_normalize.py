"""
CabinWeave 文本归一化工具（纯函数，无外部依赖）

语音识别出来的文本常带全角符号、空格、口头词和中文数字，规则匹配前需要先统一格式。
这里的函数被意图识别器的关键词层和快速通道使用，
因此只做通用的字符串变换，不包含任何意图或业务判断。

提供的函数：
  - to_halfwidth：全角字符转半角（全角空格转普通空格）
  - remove_spaces：去掉所有空白字符
  - remove_fillers：去掉"请、帮我、麻烦、给我、一下、吧、呗、啊、呀"等口头词
  - chinese_to_arabic：中文数字转阿拉伯数字（0–9999，含"两""俩"、口语"一百二"、
    "百分之五十"→"50%"、"二十二点五度"→"22.5度"）
  - normalize_command：以上步骤的组合，供快速通道整句匹配使用
"""
import re
from typing import Iterable, Optional, Tuple

# ── 全角 / 空白 ───────────────────────────────────────────────────────────────


def to_halfwidth(text: str) -> str:
    """全角 ASCII 区（U+FF01–U+FF5E）转半角，全角空格（U+3000）转普通空格。"""
    if not text:
        return ""
    chars = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return "".join(chars)


def remove_spaces(text: str) -> str:
    """去掉所有空白字符（空格、制表符、换行）。语音识别常在中文之间插入空格。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", text)


# ── 口头词 ────────────────────────────────────────────────────────────────────

# 默认口头词，按长度从长到短排列，保证"麻烦你"先于"麻烦"被移除
DEFAULT_FILLERS: Tuple[str, ...] = (
    "麻烦你", "麻烦", "帮我", "给我", "一下", "请", "吧", "呗", "啊", "呀",
)

# 含口头词字面但不是口头词的常见词，移除前先保护起来
_FILLER_PROTECTED: Tuple[str, ...] = (
    "申请", "邀请", "请假", "请教", "请求",
    "酒吧", "网吧", "贴吧", "书吧", "氧吧", "吧台",
)

# 用私有区字符做占位符，正常语音文本里不会出现
_PLACEHOLDER_BASE = 0xE000


def remove_fillers(text: str, fillers: Optional[Iterable[str]] = None) -> str:
    """
    去掉口头词。

    fillers 为空时使用 DEFAULT_FILLERS；传入时完全替换默认表。
    "申请""酒吧"这类词会被保护，不会被误删成"申""酒"。
    """
    if not text:
        return ""
    words = sorted(set(fillers if fillers is not None else DEFAULT_FILLERS), key=len, reverse=True)

    # 1. 保护词替换为占位符
    protected = {}
    for idx, word in enumerate(_FILLER_PROTECTED):
        if word in text:
            placeholder = chr(_PLACEHOLDER_BASE + idx)
            protected[placeholder] = word
            text = text.replace(word, placeholder)

    # 2. 删除口头词（长词优先）
    for word in words:
        if word:
            text = text.replace(word, "")

    # 3. 还原保护词
    for placeholder, word in protected.items():
        text = text.replace(placeholder, word)
    return text


# ── 中文数字 ──────────────────────────────────────────────────────────────────

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS)
_CN_RUN_RE = re.compile(f"[{_CN_CHARS}]+")

# 单个数字字后面跟这些量词时才转换，避免"一下""一直""统一"之类被误转
_MEASURE_WORDS: Tuple[str, ...] = (
    "摄氏度", "小时", "公里", "千米", "分钟",
    "度", "℃", "档", "挡", "级", "个", "首", "号", "次", "格", "分", "秒", "点",
    "%", "成", "倍", "层", "排", "楼", "天", "周", "年", "月", "元", "人", "位",
    "辆", "条", "张", "遍", "岁", "米", "台", "页", "集", "章", "期",
)

# 单个数字字后面跟这些字时一律不转换（"一半""一下""一起""两边"等）
_NO_CONVERT_NEXT = "半下些会直起样定般切边路共键旦致同带向"

# 单个数字字前面是这些字时可以转换（"第三""调到五""分成两"）
_CONVERT_PREV = "第到至成为"

# "一点"默认表示"一些"不转换；前面是时间词时按钟点转换（"下午一点"→"下午1点"）
_TIME_PREFIXES: Tuple[str, ...] = (
    "凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "今晚", "明晚",
    "今早", "明早", "夜里", "半夜", "今天", "明天", "后天",
)

# 含数字字面的固定说法，整体保留
_NUMBER_IDIOMS: Tuple[str, ...] = ("一五一十", "三三两两", "一一")

_PERCENT_RE = re.compile(f"百分之([{_CN_CHARS}]+|\\d+(?:\\.\\d+)?)")
_DECIMAL_DEGREE_RE = re.compile(r"(\d+)点(\d+)(?=度|℃|摄氏度)")
_CN_DECIMAL_DEGREE_RE = re.compile(f"([{_CN_CHARS}]+)点([零〇一二三四五六七八九]+)(?=度|℃|摄氏度)")


def parse_chinese_number(run: str) -> Optional[int]:
    """
    把一段纯中文数字解析成整数，支持 0–9999。

    - 不含"十百千"时按逐位读法："二零二六"→2026（电话号码、年份）。
    - 含单位时按位值相加："二十二"→22、"一百零五"→105、"十五"→15。
    - 口语省略末位单位："一百二"→120、"两千五"→2500。
    - 无法解析（如"十十""二三十"）返回 None。
    """
    if not run or any(ch not in _CN_DIGITS and ch not in _CN_UNITS for ch in run):
        return None

    # 逐位读法
    if not any(ch in _CN_UNITS for ch in run):
        return int("".join(str(_CN_DIGITS[ch]) for ch in run))

    total = 0
    current: Optional[int] = None
    last_unit: Optional[int] = None
    zero_seen = False
    prev_is_unit = False
    for ch in run:
        if ch in _CN_DIGITS:
            value = _CN_DIGITS[ch]
            if value == 0:
                zero_seen = True
                current = None
                prev_is_unit = False
                continue
            if current is not None:
                # 单位模式下连续两个数字（如"二三十"）属于约数，不解析
                return None
            current = value
            prev_is_unit = False
        else:
            unit = _CN_UNITS[ch]
            if last_unit is not None and unit >= last_unit:
                return None
            total += (current if current is not None else 1) * unit
            current = None
            last_unit = unit
            zero_seen = False
            prev_is_unit = True

    if current is not None:
        # "一百二"：末位紧跟在百/千之后且中间没有"零"，按下一级单位计
        if last_unit is not None and last_unit >= 100 and not zero_seen and not prev_is_unit:
            total += current * (last_unit // 10)
        else:
            total += current
    return total


def _is_approximate(run: str) -> bool:
    """两个相邻递增数字组成的约数（"两三""五六"）不转换，避免"调高两三度"变成 23 度。"""
    if len(run) != 2 or any(ch not in _CN_DIGITS for ch in run):
        return False
    first, second = _CN_DIGITS[run[0]], _CN_DIGITS[run[1]]
    return first > 0 and second == first + 1


def _should_convert_run(run: str, text: str) -> bool:
    """判断多字数字串是否整体转换。"""
    if text.strip() == run:
        return True
    if _is_approximate(run) or run in _NUMBER_IDIOMS:
        return False
    if not any(ch in _CN_UNITS for ch in run):
        # 逐位读法只用于电话号码、年份这类 3 位以上的串；
        # 两位串（"三五个"）多为约数，含"两/俩"的串（"咱俩一起"）不是逐位读法
        return len(run) >= 3 and "两" not in run and "俩" not in run
    return True


def _convert_single(ch: str, text: str, start: int, end: int) -> bool:
    """判断单个数字字是否应该转换。"""
    before, after = text[:start], text[end:]
    stripped = text.strip()
    if stripped == ch:
        # 整句只有一个数字（如回答"调到几档"时说"五"）
        return True
    if ch in "百千零〇":
        # "百度""千万""零下"等，单独出现时不转换
        return False
    if after[:1] and after[0] in _NO_CONVERT_NEXT:
        return False
    if ch == "十":
        # "十分感谢"不转，"十分钟"转
        return not (after.startswith("分") and not after.startswith("分钟"))
    if after.startswith("点") and ch == "一":
        return before.endswith(_TIME_PREFIXES)
    if after.startswith(_MEASURE_WORDS):
        return True
    return bool(before) and before[-1] in _CONVERT_PREV


def _replace_runs(text: str) -> str:
    """逐段替换中文数字串。"""
    pieces = []
    last = 0
    for match in _CN_RUN_RE.finditer(text):
        run, start, end = match.group(0), match.start(), match.end()
        pieces.append(text[last:start])
        last = end
        replacement = run
        if len(run) == 1:
            if _convert_single(run, text, start, end):
                value = parse_chinese_number(run)
                replacement = str(value) if value is not None else run
        elif _should_convert_run(run, text):
            value = parse_chinese_number(run)
            if value is not None:
                replacement = str(value)
        pieces.append(replacement)
    pieces.append(text[last:])
    return "".join(pieces)


def chinese_to_arabic(text: str) -> str:
    """
    把文本里的中文数字转成阿拉伯数字，其余字符保持不变。

    例："空调调到二十二度"→"空调调到22度"；"座椅加热调到两档"→"座椅加热调到2档"；
    "车窗开到百分之三十"→"车窗开到30%"；"二十二点五度"→"22.5度"；
    "两点半"→"2点半"。"一下""一半""一直""统一"等保持原样。
    """
    if not text:
        return ""

    # 1. "百分之X" / "百分百" → "X%"
    text = text.replace("百分百", "100%")

    def _percent(match: "re.Match[str]") -> str:
        raw = match.group(1)
        if raw[0].isdigit():
            return f"{raw}%"
        value = parse_chinese_number(raw)
        return f"{value}%" if value is not None else match.group(0)

    text = _PERCENT_RE.sub(_percent, text)

    # 2. 中文小数温度："二十二点五度" / "一点五度" → "22.5度" / "1.5度"
    def _cn_decimal(match: "re.Match[str]") -> str:
        integer = parse_chinese_number(match.group(1))
        fraction = "".join(str(_CN_DIGITS[ch]) for ch in match.group(2))
        return f"{integer}.{fraction}" if integer is not None else match.group(0)

    text = _CN_DECIMAL_DEGREE_RE.sub(_cn_decimal, text)

    # 3. 逐段转换中文数字
    text = _replace_runs(text)

    # 4. "22点5度" → "22.5度"
    return _DECIMAL_DEGREE_RE.sub(r"\1.\2", text)


# ── 组合 ──────────────────────────────────────────────────────────────────────


def normalize_command(text: str) -> str:
    """
    指令归一化：全角转半角 → 去空格 → 小写 → 去口头词 → 中文数字转阿拉伯数字。

    例："请帮我把空调调到 二十二度 吧" → "把空调调到22度"。
    """
    text = remove_spaces(to_halfwidth(text or "")).lower()
    text = remove_fillers(text)
    return chinese_to_arabic(text)
