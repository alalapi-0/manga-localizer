# Provider system

Providers expose capability metadata and health independently from project workflows. Registries select
providers by stable identifiers; settings contain no secret values.

## OCR provider

```python
detect_text_regions(image)
recognize_region(image, region)
recognize_image(image)
health_check()
get_capabilities()
```

The default Tesseract adapter invokes the installed CLI directly, parses TSV word/line data into
regions, supports cropped recognition, and reports installed languages. MangaOCR and PaddleOCR are
planned adapters and are not included in the MVP; their downloads must never be required for startup.

## Translation provider

```python
translate_text(text, context)
translate_batch(items)
health_check()
get_capabilities()
```

Manual preserves the current reviewed translation without automatic mutation. Mock is deterministic.
Dictionary is a local non-LLM exact/glossary translator. OpenAI-compatible sends the current text plus
bounded preceding/following regions by reading order on the same page to a user-configured endpoint.
Provider errors are normalized without echoing request headers, remote response bodies, or credentials.

The compatibility adapter uses the widely implemented Chat Completions contract: bearer
authentication, `model` plus developer/user `messages`, and text from the first response choice. See
the official [Create chat completion API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

## Inpainting provider

```python
create_mask(image, regions)
inpaint(image, mask)
health_check()
```

OpenCV is the baseline: persisted rectangular text regions are rasterized into a grayscale mask,
optionally expanded/dilated, then processed with Telea, Navier-Stokes, or solid-color fill. The MVP
does not expose a manual mask brush or eraser; users adjust the text box and region mask settings.

## Adding a provider

Implement the protocol, declare stable capabilities, add it to the registry, test success and failure,
and document install/privacy/licensing implications. Optional import failures must degrade to an
unavailable health result rather than prevent the API from starting.
