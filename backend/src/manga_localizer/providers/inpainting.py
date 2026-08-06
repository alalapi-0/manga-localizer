"""Provider-layer aliases for the OpenCV imaging implementation."""

from manga_localizer.imaging.inpainting import (
    InpaintingProvider,
    OpenCVInpaintingProvider,
    create_mask,
    inpaint,
)

__all__ = ["InpaintingProvider", "OpenCVInpaintingProvider", "create_mask", "inpaint"]
