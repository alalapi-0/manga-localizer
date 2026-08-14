# Third-party notices

Manga Localizer is licensed under Apache-2.0. The direct runtime dependencies used by the v0.2.0
MVP are available under permissive licenses compatible with that choice. The resolved frontend build
graph also contains MPL-2.0 components; MPL-2.0's file-level terms do not require relicensing this
Apache-2.0 project. Their own license texts remain authoritative.

| Component | Purpose | License |
| --- | --- | --- |
| FastAPI, Pydantic Settings, SQLAlchemy | Local API, configuration, persistence | MIT |
| HTTPX | Optional OpenAI-compatible HTTP client | BSD-3-Clause |
| NumPy | Image-array operations | BSD-3-Clause and other permissive notices in its distribution |
| OpenCV / `opencv-python-headless` | Mask processing and local inpainting | Apache-2.0 |
| Pillow | Image decoding and typesetting | MIT-CMU |
| `python-multipart` | Browser image uploads | Apache-2.0 |
| Uvicorn | Local ASGI server | BSD-3-Clause |
| React, React DOM, React Konva, Konva, Zustand | Web workbench and canvas state | MIT |
| Tesseract OCR | Optional system-installed default OCR engine | Apache-2.0 |
| ONNX Runtime | Optional local inference for LaMa and Real-ESRGAN | MIT |
| Real-ESRGAN `RealESRGAN_x4plus_anime_6B` | Optional local anime super-resolution weights | BSD-3-Clause |
| Lightning CSS (transitive build dependency) | CSS transformation used by the frontend toolchain | MPL-2.0 |

Build and test dependencies include Playwright (Apache-2.0), Vite/Vitest, TypeScript, ESLint,
and Testing Library (permissive licenses). Exact resolved versions, transitive packages, and their
authoritative package metadata are recorded in `backend/uv.lock`, `frontend/package-lock.json`, and
the root `package-lock.json`.

No font, OCR language data, model weight, or user image is redistributed by this repository.
Users install Tesseract language packs, optional ONNX models, and fonts already available on their
own system. Optional model setup prints each file's license and verifies a pinned SHA-256 checksum
before the application will use it.
