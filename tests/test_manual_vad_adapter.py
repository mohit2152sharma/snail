import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("examples/multi-agent").resolve()))

from backend.adapter import ManualVadGeminiAdapter  # noqa: E402

from snail.vendor import Backend, ResponseModality, SetupParam  # noqa: E402


def test_manual_vad_disables_automatic_detection():
    a = ManualVadGeminiAdapter(backend=Backend.GEMINI_DEV, model="gemini-2.5-flash-live")
    setup = SetupParam(
        model="gemini-2.5-flash-live", response_modality=ResponseModality.AUDIO
    )
    cfg = a.build_setup(setup)
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is True
