"""
PyTorch backend implementation for STT (Whisper).

JFW-1: Die Qwen3-TTS-Klasse (`PyTorchTTSBackend`) ist entfernt — das
jf-whisper-Profil schließt TTS aus. Der Dispatch in `backends/__init__.py`
ist fail-closed; der Whisper-STT-Pfad bleibt unverändert.
"""

from typing import Optional
import asyncio
import logging
import torch

logger = logging.getLogger(__name__)

from . import WHISPER_HF_REPOS
from .base import (
    is_model_cached,
    get_torch_device,
    empty_device_cache,
    model_load_progress,
)
from ..utils.audio import load_audio


class PyTorchSTTBackend:
    """PyTorch-based STT backend using Whisper."""

    def __init__(self, model_size: str = "base"):
        self.model = None
        self.processor = None
        self.model_size = model_size
        self.device = self._get_device()

    def _get_device(self) -> str:
        """Get the best available device."""
        return get_torch_device(allow_xpu=True, allow_directml=True)

    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self.model is not None

    def _is_model_cached(self, model_size: str) -> bool:
        hf_repo = WHISPER_HF_REPOS.get(model_size, f"openai/whisper-{model_size}")
        return is_model_cached(hf_repo)

    async def load_model_async(self, model_size: Optional[str] = None):
        """
        Lazy load the Whisper model.

        Args:
            model_size: Model size (tiny, base, small, medium, large)
        """
        if model_size is None:
            model_size = self.model_size

        if self.model is not None and self.model_size == model_size:
            return

        await asyncio.to_thread(self._load_model_sync, model_size)

    # Alias for compatibility
    load_model = load_model_async

    def _load_model_sync(self, model_size: str):
        """Synchronous model loading."""
        progress_model_name = f"whisper-{model_size}"
        is_cached = self._is_model_cached(model_size)

        with model_load_progress(progress_model_name, is_cached):
            from transformers import WhisperProcessor, WhisperForConditionalGeneration

            model_name = WHISPER_HF_REPOS.get(model_size, f"openai/whisper-{model_size}")
            logger.info("Loading Whisper model %s on %s...", model_size, self.device)

            self.processor = WhisperProcessor.from_pretrained(model_name)
            self.model = WhisperForConditionalGeneration.from_pretrained(model_name)

        self.model.to(self.device)
        self.model_size = model_size
        logger.info("Whisper model %s loaded successfully", model_size)

    def unload_model(self):
        """Unload the model to free memory."""
        if self.model is not None:
            del self.model
            del self.processor
            self.model = None
            self.processor = None

            empty_device_cache(self.device)

            logger.info("Whisper model unloaded")

    async def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
        model_size: Optional[str] = None,
    ) -> str:
        """
        Transcribe audio to text.

        Args:
            audio_path: Path to audio file
            language: Optional language hint
            model_size: Optional model size override

        Returns:
            Transcribed text
        """
        await self.load_model_async(model_size)

        def _transcribe_sync():
            """Run synchronous transcription in thread pool."""
            # Load audio
            audio, sr = load_audio(audio_path, sample_rate=16000)

            # Whisper verarbeitet exakt 30-s-Fenster (3000 Feature-Frame).
            # Laengere Aufnahmen wurden bisher still auf das erste Fenster
            # trunciert — der Rest ging verloren. Deshalb: in 30-s-Chunks
            # transkribieren und die Teilergebnisse verketten. Kurze Audios
            # (<= 30 s) laufen unveraendert als Einzel-Chunk durch.
            chunk_samples = 30 * sr
            if len(audio) <= chunk_samples:
                chunks = [audio]
            else:
                chunks = [
                    audio[i : i + chunk_samples]
                    for i in range(0, len(audio), chunk_samples)
                ]
                logger.info(
                    "Transkribiere %d Chunk(e) a 30s (%.1fs Audio)",
                    len(chunks),
                    len(audio) / sr,
                )

            # Inference runs with the process's default HF_HUB_OFFLINE
            # state — forcing offline here (issue #462) broke online users
            # whose `get_decoder_prompt_ids` / tokenizer calls issue
            # legitimate metadata lookups.
            parts = []
            for chunk in chunks:
                inputs = self.processor(
                    chunk,
                    sampling_rate=sr,
                    return_tensors="pt",
                )
                inputs = inputs.to(self.device)

                # If language is provided, force it; otherwise auto-detect.
                generate_kwargs = {}
                if language:
                    forced_decoder_ids = self.processor.get_decoder_prompt_ids(
                        language=language,
                        task="transcribe",
                    )
                    generate_kwargs["forced_decoder_ids"] = forced_decoder_ids

                with torch.no_grad():
                    predicted_ids = self.model.generate(
                        inputs["input_features"],
                        **generate_kwargs,
                    )

                text = self.processor.batch_decode(
                    predicted_ids,
                    skip_special_tokens=True,
                )[0].strip()
                if text:
                    parts.append(text)

            return " ".join(parts).strip()

        # Run blocking transcription in thread pool
        return await asyncio.to_thread(_transcribe_sync)
