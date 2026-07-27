from pathlib import Path

from types import SimpleNamespace

from aicf.voice_validation import FasterWhisperAsr, VoiceValidator


class FakeAsr:
    def __init__(self, text: str, language: str = "zh-CN") -> None:
        self.text = text
        self.language = language

    def transcribe(self, _audio_path: Path) -> tuple[str, str]:
        return self.text, self.language


def test_unavailable_asr_warns_without_faking_pass(tmp_path: Path) -> None:
    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")

    result = VoiceValidator().validate(
        audio,
        expected_text="2026年增长30%",
        key_phrases=("内容工厂",),
    )

    assert result.available is False
    assert result.passed is False
    assert result.warning == "ASR 不可用，未执行旁白可懂度验收"


def test_available_asr_checks_language_numbers_and_key_phrases(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")

    passed = VoiceValidator(
        FakeAsr("内容工厂将在2026年增长30%")
    ).validate(
        audio,
        expected_text="内容工厂将在2026年增长30%",
        key_phrases=("内容工厂",),
    )
    failed = VoiceValidator(FakeAsr("内容将在今年增长")).validate(
        audio,
        expected_text="内容工厂将在2026年增长30%",
        key_phrases=("内容工厂",),
    )

    assert passed.available is passed.passed is True
    assert failed.passed is False
    assert failed.missing_numbers == ("2026", "30")
    assert failed.missing_phrases == ("内容工厂",)


def test_numeric_acceptance_normalizes_fullwidth_grouping_and_decimal_zeros(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")

    result = VoiceValidator(
        FakeAsr("营收达到一千万元，增长率为百分之三十")
    ).validate(
        audio,
        expected_text="营收达到１,０００万元，增长率为30.0％",
    )

    assert result.passed is True
    assert result.missing_numbers == ()


def test_faster_whisper_adapter_joins_segments_and_reports_language(
    tmp_path: Path,
) -> None:
    class FakeModel:
        def transcribe(self, path: str, **options: object):
            assert path.endswith("voiceover.wav")
            assert options["language"] == "zh"
            return (
                [SimpleNamespace(text="第一句。"), SimpleNamespace(text="第二句。")],
                SimpleNamespace(language="zh"),
            )

    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")
    provider = FasterWhisperAsr(model_factory=lambda *_args, **_kwargs: FakeModel())

    assert provider.transcribe(audio) == ("第一句。第二句。", "zh")


def test_faster_whisper_model_is_initialized_lazily(tmp_path: Path) -> None:
    created: list[str] = []

    class FakeModel:
        def transcribe(self, _path: str, **_options: object):
            return ([SimpleNamespace(text="就绪")], SimpleNamespace(language="zh"))

    def factory(model_name: str, **_options: object) -> FakeModel:
        created.append(model_name)
        return FakeModel()

    provider = FasterWhisperAsr(model_factory=factory)
    assert created == []

    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")
    assert provider.transcribe(audio) == ("就绪", "zh")
    assert created == ["small"]


def test_asr_runtime_failure_is_reported_as_unavailable(tmp_path: Path) -> None:
    class BrokenAsr:
        def transcribe(self, _audio_path: Path) -> tuple[str, str]:
            raise RuntimeError("模型初始化失败")

    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")

    result = VoiceValidator(BrokenAsr()).validate(
        audio,
        expected_text="测试文本",
    )

    assert result.available is False
    assert result.passed is False
    assert "模型初始化失败" in (result.warning or "")


def test_numeric_acceptance_applies_wan_and_yi_multipliers(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"wav")

    result = VoiceValidator(
        FakeAsr("营收一千万元，估值十二亿五千万元")
    ).validate(
        audio,
        expected_text="营收1000万元，估值12.5亿元",
    )

    assert result.passed is True
    assert result.missing_numbers == ()
