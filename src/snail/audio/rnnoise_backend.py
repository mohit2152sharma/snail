"""RNNoiseDenoiseBackend — the real ``DenoiseBackend`` (librnnoise via ``pyrnnoise``).

Plugs into :class:`~snail.audio.RNNoiseCleaner` as its injected per-frame denoise kernel:
the framework keeps the rechunk-to-480 / per-consumer policy, this supplies the librnnoise
call. Completes the native audio trio (soxr resample, opus codec, rnnoise denoise).

**Why the load looks unusual.** ``pyrnnoise``'s top-level package pulls in ``audiolab``
(→ ``pyav``), whose API is incompatible with the ``pyav`` in this environment — importing
``pyrnnoise`` at all raises. But the piece we need, ``pyrnnoise/rnnoise.py``, is a
self-contained ``ctypes`` binding over the bundled ``librnnoise`` shared library (stdlib +
numpy only). So we locate the package **without executing its ``__init__``**
(``find_spec``) and load just that file as a standalone module. If ``pyrnnoise`` (and its
bundled lib) isn't installed, constructing this class raises with an actionable message —
the same guarded-import contract as the soxr/opus backends; the core audio layer stays
importable without it.

librnnoise is fixed at **48kHz, 480-sample (10ms) mono** frames — exactly the interior
frame and :class:`RNNoiseCleaner`'s rechunk size, so ``process_480`` is a 1:1 call. The
denoise state is **stateful** (recurrent filter), so one backend instance per stream.
"""

from __future__ import annotations

import importlib.util
import os
from types import ModuleType

import numpy as np


def _load_rnnoise() -> ModuleType:
    """Load ``pyrnnoise/rnnoise.py`` standalone, bypassing the package ``__init__``."""
    spec = importlib.util.find_spec("pyrnnoise")  # locates; does not exec __init__
    if spec is None or not spec.submodule_search_locations:
        raise ModuleNotFoundError(
            "RNNoiseDenoiseBackend needs the 'pyrnnoise' package (bundled librnnoise). "
            "Install it, or use NullCleaner / an InputSource.RAW agent to skip denoise."
        )
    path = os.path.join(list(spec.submodule_search_locations)[0], "rnnoise.py")
    sub = importlib.util.spec_from_file_location("_snail_rnnoise", path)
    module = importlib.util.module_from_spec(sub)
    sub.loader.exec_module(module)  # ctypes + numpy only; loads the bundled shared lib
    return module


class RNNoiseDenoiseBackend:
    """One stateful librnnoise denoiser (the injected :class:`DenoiseBackend`)."""

    def __init__(self) -> None:
        self._lib = _load_rnnoise()
        if self._lib.FRAME_SIZE != 480:
            raise RuntimeError(
                f"librnnoise frame size is {self._lib.FRAME_SIZE}, expected 480 (48k/10ms)"
            )
        self._state = self._lib.create()

    def process_480(self, frame: np.ndarray) -> np.ndarray:
        """Denoise one 480-sample int16 mono frame @48k, in-place-equivalent."""
        out, _speech_prob = self._lib.process_mono_frame(self._state, frame)
        return out

    def close(self) -> None:
        """Free the native denoise state. Idempotent."""
        if self._state is not None:
            self._lib.destroy(self._state)
            self._state = None

    def __del__(self) -> None:  # best-effort native cleanup
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass
