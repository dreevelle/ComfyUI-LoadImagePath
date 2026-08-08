# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A ComfyUI custom node package. All Python lives in `__init__.py` and the one
frontend file in `web/loadimagepath.js`; there is no build step, test suite, or
linter config. Two nodes:

- **`LoadImageFromPath`** — a structural clone of core's `LoadImage` (schema is
  literally just an `image` combo with `upload=image`), plus a frontend-only
  paste box that imports a file into `input/` and selects it.
- **`LoadImageSequenceFromPath`** — folder loading in place, with frame
  selection and threaded decode. No frontend code.

`WEB_DIRECTORY = "./web"` is what makes ComfyUI serve the extension.

The node is registered through ComfyUI's **V3 schema API**, not the legacy
`NODE_CLASS_MAPPINGS` dict: `comfy_entrypoint()` returns a `ComfyExtension`
whose `get_node_list()` yields `IO.ComfyNode` subclasses. Inputs are declared in
`define_schema()` and arrive as named kwargs to `execute()`. The V3 names for
the cache and validation hooks are `fingerprint_inputs` and `validate_inputs`,
not `IS_CHANGED` and `VALIDATE_INPUTS`.

## Local environment

- ComfyUI checkout: `/home/dreevelle/comfy/ComfyUI`
- Python venv (has `av`, `torch`, `numpy`, `Pillow`): `/home/dreevelle/comfy-env/bin/python`
- During development this tree is symlinked into
  `/home/dreevelle/comfy/ComfyUI/custom_nodes/ComfyUI-LoadImagePath`, so edits
  are live after a server restart. A registry install would instead be an
  independent copy under the registry id `comfyui-load-image-path`.

Run ComfyUI:

```bash
/home/dreevelle/comfy-env/bin/python /home/dreevelle/comfy/ComfyUI/main.py
```

## Verifying changes

`__init__.py` imports `folder_paths`, `comfy.model_management`, `node_helpers`
and `comfy_api.latest` at module scope, so it cannot be imported standalone —
ComfyUI must be on `sys.path`.

**The project's core invariant is that the `IMAGE` and `MASK` tensors are
bit-identical to the built-in `Load Image` node** for the same file. Any change
to the decode or mask path must be checked against it. Copy a test image into
ComfyUI's `input/` so the built-in node can see it, then:

```bash
cd /home/dreevelle/comfy/ComfyUI && PYTHONPATH=/home/dreevelle/comfy/ComfyUI \
/home/dreevelle/comfy-env/bin/python -c "
import importlib.util, torch, nodes, os, folder_paths
spec = importlib.util.spec_from_file_location('lip', '/home/dreevelle/Projects/ComfyUI-LoadImagePath/__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
name = 'SOME_IMAGE.png'
ni, nm = nodes.LoadImage().load_image(image=name)
oi, om = m.LoadImageFromPath.execute(path=os.path.join(folder_paths.get_input_directory(), name)).args
print('image identical:', torch.equal(ni, oi))
print('mask  identical:', torch.equal(nm, om), tuple(nm.shape), tuple(om.shape))
"
```

Cover, at minimum: 8-bit RGB, 8-bit RGBA, 16-bit, EXIF-rotated JPEG, a width
that is not a multiple of 32, paletted PNG with `tRNS`, animated GIF, animated
WebP, and animated WebP with alpha. Use fixtures whose dimensions are **not**
64x64, or a real `[B, H, W]` mask is indistinguishable from the `[B, 64, 64]`
sentinel and the test proves nothing.

Only animated WebP exercises `_load_animated_webp`. Confirm a fixture actually
reaches that branch rather than assuming it does:

```python
InputImpl.VideoFromFile(path).get_components().images.shape[0]  # 0 => Pillow fallback
```

## Architecture

Four layers in `__init__.py`, in order:

1. **`_resolve_path`** — normalises a pasted string. Strips quotes and
   whitespace, unwraps `file://`, expands `~`. Deliberately does *not* call
   `expandvars`, which would eat `%...%` tokens on Windows. Returns `None` for
   `None`, because a linked (non-widget) input arrives as `None` at validation
   time rather than being omitted.
2. **`_load_animated_webp`** — copied from `nodes.py` `LoadImage.load_image`
   (v0.31.0, lines 1757-1789). PyAV cannot decode animated WebP and core keeps a
   Pillow path for exactly that case. Copied rather than imported so a core
   refactor cannot silently change results; **diff it against `nodes.py` when
   updating ComfyUI**. Two intentional deltas are commented in place: it returns
   alpha rather than a finished mask, and it reads alpha presence from frame 0
   instead of per frame (core's per-frame check would try to concatenate a
   `[H,W]` mask with a `[64,64]` one).
3. **`_load_one` / `_load_many`** — `InputImpl.VideoFromFile(path).get_components()`
   is the same primitive the built-in node uses, which is what buys the
   equivalence. `_load_many` uses `pool.map` specifically because it preserves
   input order, keeping frames in filename order.
4. **`LoadImageFromPath`** — selection, the size check, and the mask assembly.

### The mask contract

This is the load-bearing part of the whole project.

- No alpha anywhere: `torch.zeros((B, 64, 64))`. **Never widen this to
  image-sized.** The undersized shape is a sentinel that downstream nodes read
  as "no mask"; making it image-sized turns "no mask" into "mask everything" and
  breaks inpainting. It looks like a bug and is not.
- Alpha present: `1.0 - alpha[..., -1]`, shape `[B, H, W]`.
- A folder with mixed alpha is the one case that cannot match core, since the
  two shapes above cannot be concatenated. Files without alpha contribute
  image-sized zeros, which is what core emits for an all-opaque alpha channel.

### Why `LoadImageFromPath` is a clone and not a replacement

This is the single most important thing to understand before changing it, and it
was arrived at the hard way — two earlier designs failed on it.

The frontend hard-codes assumptions about what a loader looks like:

- **MaskEditor gate:** `if (!node.imgs?.length && node.previewMediaType !== 'image')`
  → it logs `[MaskEditor] Node has no images` and refuses to open. A custom DOM
  widget drawing an `<img>` does not set `node.imgs`, so the mask editor never
  opens on it. That was design attempt #2's fatal flaw.
- **Mask writeback:** clipspace does
  `node.widgets.findIndex(w => w.name === 'image')` and writes
  `"clipspace/... [input]"` into that widget. A node with no widget *literally
  named `image`* cannot receive a painted mask at all.

Both are satisfied for free by having the schema be exactly core's: one combo
named `image` carrying `upload=IO.UploadType.image`. Everything else —
thumbnail, upload button, mask editor, clipspace round-trip, inpainting — is
core's code running against a normal `input/` file.

**Do not "improve" this node by adding a path input to it.** The path box is
frontend-only and intentionally not a graph input: the combo must remain the
single source of truth for what gets loaded, or the widget the mask editor
writes into stops matching what executes. Folder loading lives on the other node
for the same reason — importing 500 frames into `input/` is not a copy, it's a
mess.

### The import route (`web/loadimagepath.js` + `/loadimagepath/import`)

`web/loadimagepath.js` **renders nothing and owns no preview.** It adds a text
widget, POSTs the pasted path to `/loadimagepath/import`, then sets
`imageWidget.value` and calls `imageWidget.callback(...)`. That callback is
core's: it clears `node.imgs` and repopulates the preview from `input/`, which is
also what makes the mask editor willing to open. Keeping the JS this thin is the
point — it is the only part that can break on a frontend update, and its failure
mode is "you get a plain native picker".

The paste widget is created with `serialize: false`, so it never enters the saved
workflow, and it clears itself after a successful import so it cannot look like
the source of truth once the combo is set.

`_register_routes` uses `getattr(PromptServer, "instance", None)`, **not**
`PromptServer.instance`. The attribute is assigned inside the server's
`__init__`, so it does not exist on the class at all until a server is
constructed — touching it directly raises `AttributeError` and breaks every
standalone import, including the test harnesses and `comfy node validate`.

### Import route security

Copying a caller-named file is a wider primitive than reading one. Four
constraints fence it in, all covered by `import_tests.py`:

- **`_import_denied`** refuses with 403 when `args.listen` is not a loopback
  address, unless `COMFYUI_LOADIMAGEPATH_ALLOW_REMOTE=1`. A file-copy endpoint
  reachable from the network is a different proposition to a local one.
- **`_is_safe_image`** requires an `image/*` MIME type, rejects anything
  `folder_paths.is_dangerous_content_type` flags (SVG, HTML, XML — stored XSS
  vectors), *and* makes Pillow parse the header. Extension alone is not trusted,
  so a renamed `.txt` and a fake `.png` are both refused before any copy.
- **The destination is never caller-controlled.** Always `input/`, basename
  reduced to alphanumerics plus `-_. `, leading dots stripped, a content hash
  appended, then `is_within_directory` verified. There is no way to steer it.
- **Content-addressed, so imports are idempotent.** Re-pasting a path reuses the
  existing copy. Copies land on `.partial` and are `os.replace`d, so an
  interrupted import cannot leave a truncated file under a complete-looking hash.

### Why the sequence node cannot just call `nodes.LoadImage`

`folder_paths.get_annotated_filepath` runs an `is_within_directory` containment
check and raises `ValueError` for any path outside `input/`. Absolute paths used
to slip through `os.path.join`'s absolute-second-argument behaviour; that guard
closed it. The decode therefore has to be driven directly. (`LoadImageFromPath`
*does* use `get_annotated_filepath`, because its file is always in `input/` by
then — which is also what lets clipspace's `[input]`-annotated names resolve.)

### Hook signatures

On `LoadImageSequenceFromPath`, both `fingerprint_inputs` and `validate_inputs`
take **only `path`**, and that is deliberate in both cases:

- `validate_inputs`: the executor emits one error per validated input, so a
  second parameter duplicates every message in the UI.
- `fingerprint_inputs`: the other widgets are already part of the engine's cache
  key. Including them would rescan the folder on every knob turn for no benefit.

`fingerprint_inputs` never raises — if it did, the engine would substitute `NaN`
and log a warning, forcing a re-run every time. It fingerprints folders by path,
size and mtime rather than content, because hashing a 500-frame folder would
mean gigabytes of reads before every execution. `LoadVideo` in core sets the
same precedent for the same reason.

`LoadImageFromPath` deliberately does the opposite and content-hashes its single
file, because there it matches core's `LoadImage` exactly, which is that node's
whole reason for existing.

## Conventions

- Version is duplicated in `__version__` (`__init__.py`) and `[project].version`
  (`pyproject.toml`) — bump both.
- `dependencies` stays empty: `av`, `numpy`, `torch` and `Pillow` ship with ComfyUI.
- The node never resizes. Mixed sizes raise an error naming both files and both
  dimensions. Adding a silent resize would reintroduce the exact behaviour this
  project exists to avoid.
- Folder scans go through `folder_paths.filter_files_content_types`, so the
  recognized extension set tracks core's instead of being hardcoded.

## Releasing

Published on the Comfy Registry as `comfyui-load-image-path` under publisher
`dreevelle`, which is what makes it installable from ComfyUI-Manager.

**Changing `[project].version` in `pyproject.toml` on `main` publishes a
release.** `.github/workflows/publish_action.yml` watches that file's path and
runs `Comfy-Org/publish-node-action`, authenticating with the
`REGISTRY_ACCESS_TOKEN` repo secret. Don't touch the version field unless a
release is intended; republishing an existing version fails with
`400 The node version already exists`.

`.comfyignore` keeps dev-only files (`CLAUDE.md`, `.github/`) out of the
published archive. `comfy node validate` runs the registry's checks locally and
`comfy node pack` produces the exact archive that would be uploaded — inspect it
before releasing if the file list may have changed.

Check what's live:

```bash
curl -s https://api.comfy.org/nodes/comfyui-load-image-path/versions
```
