"""JFW-13: lokaler LLM-Provider (Zielsystem-Seam, fail-closed).

``Qwen/Qwen2.5-7B-Instruct`` ueber die vorhandene Transformers-/PyTorch-Laufzeit,
Decode-Profil ``jfw13-minutes-greedy-v1`` (``do_sample=false``). Laden nur mit
gueltigem Installationsmanifest (``minutes-artifact.json``, fail-closed ueber
``backend/minutes/artifacts.py``) — ohne Artefakt ``waiting_for_local_artifact``,
ohne Laufzeit ``provider_runtime_missing``, NIE ein automatischer Download.
Der echte Modelllauf ist die Zielsystem-Seam dieses Blocks.
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import MINUTES_ARTIFACTS, ArtifactGateError, verify_artifact

MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
DECODE_PROFILE = "jfw13-minutes-greedy-v1"


class MinutesProviderError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class LocalLLMProvider:
    """Injizierbare Modell-Seam: Vorschlaege fuer Kandidaten/Aufgaben/Zusammenfassung."""

    def __init__(self, artifact_dir: str | Path, model_id: str = MODEL_ID):
        self.artifact_dir = Path(artifact_dir)
        self.model_id = model_id
        self.provenance: dict | None = None
        self._model = None
        self._tokenizer = None

    def load(self) -> dict:
        """Fail-closed Artefakt-/Lizenz-Gate vor jeder Modellinitialisierung."""
        spec = MINUTES_ARTIFACTS.get(self.model_id)
        if spec is None:
            raise ArtifactGateError("artifact_missing")
        self.provenance = verify_artifact(self.model_id, self.artifact_dir)
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # lazy
        except ImportError as exc:  # pragma: no cover - Zielsystem-Seam
            raise MinutesProviderError("provider_runtime_missing") from exc
        self._tokenizer = AutoTokenizer.from_pretrained(str(self.artifact_dir))
        self._model = AutoModelForCausalLM.from_pretrained(str(self.artifact_dir))
        return self.provenance

    def _generate(self, prompt: str) -> str:  # pragma: no cover - Zielsystem-Seam
        if self._model is None or self._tokenizer is None:
            raise MinutesProviderError("provider_runtime_missing")
        inputs = self._tokenizer(prompt, return_tensors="pt").to(self._model.device)
        generated = self._model.generate(**inputs, max_new_tokens=4096, do_sample=False)
        return self._tokenizer.batch_decode(
            generated[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]

    def detect_candidates(self, text: str) -> list[dict]:  # pragma: no cover
        raise MinutesProviderError("provider_runtime_missing")

    def propose_tasks(self, text: str, turns) -> list[dict]:  # pragma: no cover
        raise MinutesProviderError("provider_runtime_missing")

    def propose_summary(self, text: str, turns) -> dict:  # pragma: no cover
        raise MinutesProviderError("provider_runtime_missing")

    def propose_datum(self, text: str):  # pragma: no cover
        return None
