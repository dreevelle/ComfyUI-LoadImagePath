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
- The package is published, and `ComfyUI/custom_nodes/comfyui-load-image-path`
  (lowercase, the registry id) is a **ComfyUI-Manager install** that tracks
  registry releases. This working tree is not wired into ComfyUI.

To test a change before releasing it, symlink this tree in under its CamelCase
name so it doesn't collide with the Manager install:

```bash
ln -sfn /home/dreevelle/Projects/ComfyUI-LoadImagePath \
        /home/dreevelle/comfy/ComfyUI/custom_nodes/ComfyUI-LoadImagePath
```

**Remove that symlink before doing anything in ComfyUI-Manager for this
package.** Manager's remove/replace path does `shutil.rmtree` on the *resolved*
path, so it deletes the symlink target's contents instead of unlinking — on
2026-08-09 that wiped a sibling package's working tree, `.git` included, and it
had to be restored by re-cloning. Commit and push before any Manager operation,
and never leave a symlinked dev copy in place while installing or updating the
published version.

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
   time rather than being omitted. `_allowed_roots` / `_containment_error` sit
   beside it — see "Path containment" below.
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

Copying a caller-named file is a wider primitive than reading one. Five
constraints fence it in; `gate_tests.py` covers the first:

- **`_containment_error`** refuses with 403 for any source outside
  `_allowed_roots()`, before the path is stat'd. See "Path containment".
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

### Path containment

**Both** read surfaces go through `_containment_error`, against the same
`_allowed_roots()`: ComfyUI's `input/`, `output/`, `temp/` and `user/`
directories, plus anything the operator listed in `COMFYUI_LOADIMAGEPATH_ROOTS`
(`os.pathsep`-separated, like `PATH`).

- `LoadImageSequenceFromPath` — checked in `execute` (`PermissionError`),
  `validate_inputs` (the full multi-line message) and `fingerprint_inputs`.
- `import_handler` — checked before the `os.path.exists` call, 403. The response
  body carries **one line**, because `web/loadimagepath.js` renders `data.error`
  into a widget label; the full reason, allowed roots included, goes to the
  server log instead.

One env var governs both on purpose. They are the same primitive — name a file,
get its pixels back — and two knobs would mean one of them ends up misconfigured.

**This is not optional polish — it is why the pack is installable.** It is also
the reason the loader refuses most paths on a fresh install until the operator
sets the variable; that first-run cost is deliberate and the README leads with
it. Version 1.0.1 was banned from the Comfy Registry on review, verbatim:

> `policy-v0.2: ARBITRARY_FILE_READ` — The STRING widget `path` flows through
> `_resolve_path` (`os.path.abspath(os.path.expanduser(...))`, no containment, no
> `'..'` rejection) into `os.scandir`/`_load_one` media decoders, and the decoded
> pixels are returned as the IMAGE output and rendered via `UI.PreviewImage`. An
> attacker driving the unauthenticated, CSRF-reachable `/prompt` route can read
> and exfiltrate any image/animated file on the server's filesystem.

Check the live status with
`curl -s 'https://api.comfy.org/nodes/comfyui-load-image-path/versions?include_status_reason=true'`
— `status_reason` is omitted unless you pass that query parameter, and it carries
both the admin verdict and the automated scan history under `statusHistory`.
(The YARA scanner also flags `os.environ.get` as "environment variable
manipulation" at severity *info*; that flag is noise and both 1.0.0 and 1.0.1
carry it.)

The verdict named only the sequence node, but the import route is the same
primitive reached a different way — it copies a caller-named file into `input/`,
where `/view` serves it — so it is gated too rather than waiting to be found in
the next review. `_import_denied`'s loopback check is not a substitute: a page in
the operator's own browser is on loopback, which is precisely how CSRF to
127.0.0.1 works.

Four properties are load-bearing, all covered by `gate_tests.py`:

- **The allowlist is environment-only, never a graph input.** A root settable
  from the prompt is not a containment boundary, it is a second path widget.
- **Containment is checked before any `stat`.** Otherwise "does this path exist"
  leaks through the error message, and `fingerprint_inputs` returns the constant
  `"denied"` for the same reason — a per-path digest would leak it through the
  cache key instead.
- **`folder_paths.is_within_directory` does the comparison**, because it
  `realpath`s both sides, so a `..` segment and a symlink planted inside `input/`
  both fail closed.
- **The route answers 403 for a denied path whether or not it exists**, with a
  byte-identical body, for the same no-oracle reason.

Do not relax this to "warn and continue", do not exempt the import route because
it "already checks loopback", and do not add `$HOME` or `folder_paths.base_path`
to the defaults — a default that already covers the interesting files is not
containment, and it is the exact finding that got the version banned.

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

**A banned version stays banned.** 1.0.1 is `NodeVersionStatusBanned` and cannot
be republished over (`400 The node version already exists`); only a registry
admin can lift it. 1.0.2 is the containment fix — see "Path containment". 1.0.0
carries the same unbounded read and was approved before policy-v0.2, so treat its
Active status as an oversight rather than a precedent.

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

`assets/icon.png` is the registry icon, pointed at by `[tool.comfy] Icon` as a
raw GitHub URL on `main`. The registry stores the *URL*, not the bytes, and the
Manager hotlinks it, so **replacing this file on `main` restyles every card with
no republish**. Only changing the URL needs a version bump, because `icon` is
node-level metadata written as a side effect of publishing a version.

The background is **transparent on purpose**. The frontend's `PackBanner` draws
the icon twice into a 7:3 box — once as a backdrop, `bg-cover` plus `blur(10px)`
at `opacity-30`, and once `object-contain` on top. An opaque icon therefore shows
as a hard rectangle: `bg-cover` crops to the centre and zooms ~2.3x, so it samples
mostly glow and lifts the surround to ~47, while the contained copy still shows
the icon's own near-black corners at ~17. Deriving alpha from luminance removes
the edge completely, and it is the right derivation because the artwork is
additive glow on near-black. The cost, accepted deliberately, is that the pale
mark washes out on ComfyUI's light theme.

Regenerate from the brand original, capped at the registry's 400x400 square
maximum. Downscale in linear light; the naive sRGB-space resize dulls the ring
stroke and blurs the bar separations inside the D:

```bash
magick ~/Projects/alora/dev/assets/brand/dreevelle_pfp.png \
  -colorspace RGB -filter Lanczos -resize 400x400 -colorspace sRGB \
  \( +clone -grayscale Rec601Luma -level 7.45%,100% \) \
  -alpha off -compose CopyOpacity -composite \
  -background none -alpha Background -strip assets/icon.png
oxipng -o max --strip safe assets/icon.png
```

Do not set `[tool.comfy] Banner`. `PackBanner` resolves `banner_url || icon`, so
a banner *replaces* the mark rather than sitting behind it.

Check what's live:

```bash
curl -s https://api.comfy.org/nodes/comfyui-load-image-path/versions
```
