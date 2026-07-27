from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol, Sequence


class AsrProvider(Protocol):
    def transcribe(self, audio_path: Path) -> tuple[str, str]: ...


class FasterWhisperAsr:
    def __init__(
        self,
        model_name: str = "small",
        *,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        self.model_name = model_name
        self.model_factory = model_factory
        self.model: object | None = None

    def _get_model(self) -> object:
        if self.model is None:
            factory = self.model_factory
            if factory is None:
                from faster_whisper import WhisperModel

                factory = WhisperModel
            self.model = factory(
                self.model_name,
                device="auto",
                compute_type="int8",
            )
        return self.model

    def transcribe(self, audio_path: Path) -> tuple[str, str]:
        segments, info = self._get_model().transcribe(
            str(audio_path),
            language="zh",
            vad_filter=True,
        )
        transcript = "".join(str(segment.text).strip() for segment in segments)
        return transcript, str(info.language)


def build_optional_asr() -> AsrProvider | None:
    return FasterWhisperAsr()


@dataclass(frozen=True)
class VoiceValidationResult:
    available: bool
    passed: bool
    language: str | None
    transcript: str
    missing_numbers: tuple[str, ...] = ()
    missing_phrases: tuple[str, ...] = ()
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class VoiceValidator:
    def __init__(self, asr: AsrProvider | None = None) -> None:
        self.asr = asr

    def validate(
        self,
        audio_path: str | Path,
        *,
        expected_text: str,
        key_phrases: Sequence[str] = (),
        expected_language: str = "zh",
    ) -> VoiceValidationResult:
        if self.asr is None:
            return VoiceValidationResult(
                available=False,
                passed=False,
                language=None,
                transcript="",
                warning="ASR 不可用，未执行旁白可懂度验收",
            )
        try:
            transcript, language = self.asr.transcribe(Path(audio_path))
        except Exception as error:
            return VoiceValidationResult(
                available=False,
                passed=False,
                language=None,
                transcript="",
                warning=f"ASR 不可用，未执行旁白可懂度验收: {error}",
            )
        numbers = _numbers(expected_text)
        transcript_numbers = set(_numbers(transcript))
        missing_numbers = tuple(
            value for value in numbers if value not in transcript_numbers
        )
        phrases = tuple(phrase for phrase in key_phrases if phrase.strip())
        missing_phrases = tuple(
            phrase
            for phrase in phrases
            if _search_text(phrase) not in _search_text(transcript)
        )
        language_ok = language.lower().startswith(expected_language.lower())
        passed = language_ok and not missing_numbers and not missing_phrases
        return VoiceValidationResult(
            available=True,
            passed=passed,
            language=language,
            transcript=transcript,
            missing_numbers=missing_numbers,
            missing_phrases=missing_phrases,
            warning=None if language_ok else f"旁白语种不匹配: {language}",
        )


def _numbers(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text)
    values: list[str] = []
    for match in re.finditer(
        r"(?<!\d)(\d[\d,]*(?:\.\d+)?)([万亿]?)(?!\d)",
        normalized,
    ):
        raw, suffix = match.groups()
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if suffix == "万":
            value *= Decimal(10_000)
        elif suffix == "亿":
            value *= Decimal(100_000_000)
        canonical = format(value.normalize(), "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        if canonical not in values:
            values.append(canonical)
    for match in re.finditer(r"[零〇一二两三四五六七八九十百千万亿]+", normalized):
        raw = match.group()
        if not raw or not any(character in "零〇一二两三四五六七八九" for character in raw):
            continue
        converted = _chinese_integer(raw)
        if converted is not None and str(converted) not in values:
            values.append(str(converted))
    return tuple(values)


def _chinese_integer(text: str) -> int | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1_000, "万": 10_000, "亿": 100_000_000}
    if not text:
        return None
    if not any(character in units for character in text):
        return int("".join(str(digits[character]) for character in text))
    total = section = number = 0
    for character in text:
        if character in digits:
            number = digits[character]
            continue
        unit = units[character]
        if unit < 10_000:
            section += (number or 1) * unit
        elif unit == 10_000:
            total += (section + number or 1) * unit
            section = 0
        else:
            total = (total + section + number or 1) * unit
            section = 0
        number = 0
    return total + section + number


def _search_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(character for character in normalized if character.isalnum())
