"""Deterministic, conservative paragraph classification, shared by style and retrieval."""
import json
import re
import unicodedata
from functools import lru_cache

from langdetect import DetectorFactory, LangDetectException
from langdetect.detector_factory import PROFILES_DIRECTORY


LANGUAGES = {"en": "English", "zh-CN": "简体中文"}
AGENT_LANGUAGES = {"en": "English", "zh-CN": "Simplified Chinese"}

INITIAL_STYLES = {
    "en": "# Translation Style\n\nPreserve the original meaning. Use natural English and consistent terminology. "
          "Retain the document's headings and paragraph structure.\n",
    "zh-CN": "# 翻译风格\n\n忠实原意，使用自然的简体中文，术语一致。保留原文层级与段落结构。\n",
}


@lru_cache(maxsize=128)
def guidance_language_error(text, target, required=False):
    """Conservative prose check, not a ban on foreign names or quoted examples."""
    prose = re.sub(r"```.*?```|~~~.*?~~~", " ", text, flags=re.S)
    prose = re.sub(r"`[^`\n]*`|https?://\S+", " ", prose)
    prose = re.sub(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』', " ", prose)
    prose = re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", prose)
    prose = re.sub(r"^\s*>.*$", " ", prose, flags=re.M)
    if not any(c.isalpha() for c in prose):
        return "缺少可校验的说明正文" if required else None
    for paragraph in prose.splitlines():
        han = len(re.findall(r"[\u3400-\u9fff]", paragraph))
        latin = len(re.findall(r"[A-Za-z]", paragraph))
        letters = [c for c in paragraph if c.isalpha()]
        foreign = sum(not ('\u3400' <= c <= '\u9fff') and 'LATIN' not in unicodedata.name(c, '') for c in letters)
        if foreign >= 4 and foreign / max(len(letters), 1) > .3:
            return "说明正文包含其他语言；外语引用或名称请使用引号或反引号标注"
        if not han and not latin:
            continue
        if target == "en":
            # Short embedded names are allowed; Chinese instructions are not.
            if han and (not latin or re.search(r"[\u3400-\u9fff]{8,}", paragraph)
                        or han >= 4 and han / (han + latin) > .3):
                return "说明正文包含中文规则"
            if latin >= 40:
                try:
                    detector = detector_factory().create()
                    detector.append(paragraph)
                    detected = detector.get_probabilities()[0]
                    if detected.lang != "en" and detected.prob >= .9:
                        return "说明正文被识别为非英语"
                except LangDetectException:
                    pass
        elif latin >= 40 and han < 8 and han / (han + latin) < .15:
            return "说明正文以英文为主"
        elif not han and len(re.findall(r"[A-Za-z]+", paragraph)) >= 3:
            return "说明正文包含英文规则"
        elif not han and latin and not re.fullmatch(r"[\W\d_A-Z]+", paragraph):
            return "说明正文缺少中文；外语名称或示例请使用引号或反引号标注"
    return None


@lru_cache(maxsize=1)
def detector_factory():
    factory = DetectorFactory()
    factory.load_profile(PROFILES_DIRECTORY)
    factory.seed = 0
    return factory


@lru_cache(maxsize=4096)
def paragraph_language(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4 or re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text):
        return "unknown"
    # Short headings and identifiers are intentionally excluded when uncertain.
    try:
        detector = detector_factory().create()
        detector.append(text[:10000])
        result = detector.get_probabilities()[0]
    except LangDetectException:
        return "unknown"
    if result.prob < .85:
        return "unknown"
    if result.lang in ("zh-cn", "zh-tw"):
        return "zh-CN"
    return "en" if result.lang == "en" else "unknown"


def target_paragraphs(work, files, target):
    records = []
    counts = {"included": 0, "excluded": 0}
    for file in files:
        path = (work / (file["path"] + ".json")).resolve()
        if not path.is_relative_to(work.resolve()):
            raise ValueError("无效语料路径")
        for index, text in enumerate(json.loads(path.read_text(encoding="utf-8"))):
            if paragraph_language(text) == target:
                records.append({"file_id": file["id"], "source": file["name"],
                                "paragraph": index + 1, "language": target, "text": text})
                counts["included"] += 1
            else:
                counts["excluded"] += 1
    return records, counts
