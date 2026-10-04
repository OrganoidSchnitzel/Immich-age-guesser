"""MiVOLO v2 (https://github.com/WildChlamydia/MiVOLO) via Hugging Face transformers.

Needs the "mivolo" extra plus torch. Runs fine on a CPU: roughly 0.1-0.3 s per face on a modern
desktop/server processor. The weights are downloaded on first use into the Hugging Face cache.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_BATCH = 16


class MiVoloEstimator:
    face_margin = 0.1

    def __init__(self, model_id: str = "iitolstykh/mivolo_v2", device: str = "cpu") -> None:
        try:
            import torch
            from transformers import AutoConfig, AutoImageProcessor, AutoModelForImageClassification
        except ImportError as exc:  # pragma: no cover - depends on optional packages
            raise SystemExit(
                f"Cannot load MiVOLO ({exc}). It needs torch and the 'mivolo' extra: "
                "pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu && "
                "pip install '.[mivolo]'"
            ) from exc

        self._torch = torch
        self._device = device
        self._dtype = torch.float16 if device.startswith("cuda") else torch.float32
        self._config = AutoConfig.from_pretrained(model_id, trust_remote_code=True)
        self._model = AutoModelForImageClassification.from_pretrained(
            model_id, trust_remote_code=True, torch_dtype=self._dtype
        ).to(device).eval()
        self._processor = AutoImageProcessor.from_pretrained(model_id, trust_remote_code=True)

    def estimate(self, faces: list[Image.Image]) -> list[float]:
        ages: list[float] = []
        for i in range(0, len(faces), _BATCH):
            ages.extend(self._estimate_batch(faces[i:i + _BATCH]))
        return ages

    def _estimate_batch(self, faces: list[Image.Image]) -> list[float]:
        torch = self._torch
        # The model was trained on OpenCV images, i.e. BGR channel order.
        crops = [np.ascontiguousarray(np.asarray(f.convert("RGB"))[:, :, ::-1]) for f in faces]
        faces_input = self._processor(images=crops)["pixel_values"]
        try:
            # No person detector here: face-only mode, the processor turns None into an empty body.
            body_input = self._processor(images=[None] * len(crops))["pixel_values"]
        except Exception:
            body_input = torch.zeros_like(faces_input)
        faces_input = faces_input.to(dtype=self._dtype, device=self._device)
        body_input = body_input.to(dtype=self._dtype, device=self._device)
        with torch.inference_mode():
            output = self._model(faces_input=faces_input, body_input=body_input)
        age = output["age_output"] if isinstance(output, dict) else output.age_output
        return [float(a) for a in age.reshape(-1).float().cpu()]
