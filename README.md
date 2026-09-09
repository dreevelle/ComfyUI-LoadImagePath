# ComfyUI-LoadImagePath

Load images from a **pasted path** instead of hunting through a file picker —
without giving up the mask editor, inpainting, or anything else the built-in
nodes do.

Most path-loading nodes replace the built-in loader, and quietly lose things in
the process. The mask editor is the clearest example: it refuses to open on a
node with no `imgs`, and writes its result back into a widget it finds by the
literal name `image`. A node that merely *looks* like a loader satisfies
neither, so inpainting stops working and it isn't obvious why.

So this doesn't replace the loader. **Load Image From Path** is the built-in
`Load Image`, with one extra box: paste a path, and the file is imported into
your input folder and selected. From that point everything downstream is core's
own code — the thumbnail, the mask editor, clipspace, inpainting — because the
node is structurally identical to the one those features were written for.

**Load Image Sequence From Path** covers what the first node deliberately
doesn't: a whole folder, loaded in place with no copying, with frame selection.

> **Read this before your first paste.** Out of the box both nodes only touch
> ComfyUI's own `input/`, `output/`, `temp/` and `user/` folders. To use paths
> from anywhere else — which is presumably why you installed this — name those
> folders in `COMFYUI_LOADIMAGEPATH_ROOTS` when you start ComfyUI. One-time
> setup, details in [Security](#security).

## Nodes

### Load Image From Path (category: `image`)

| Input | Default | Notes |
|---|---|---|
| `paste path` | — | Paste a path and press Enter. The file is imported into `input/` and selected below. Not a graph input — it does its job and clears itself. Paths outside ComfyUI's own folders need `COMFYUI_LOADIMAGEPATH_ROOTS` — see [Security](#security). |
| `image` | — | The built-in image combo, with its upload button. Identical to `Load Image`. |

Outputs `IMAGE` and `MASK`. Right-click → **Open in MaskEditor** works, because
this node is a native loader in every respect that matters.

Imports are content-addressed: the filename gets a short hash of the file's
contents, so pasting the same path twice reuses the one copy instead of filling
`input/` with duplicates.

### Load Image Sequence From Path (category: `image`)

| Input | Default | Notes |
|---|---|---|
| `path` | — | Path to a folder of images, or to a single animated file. Folders load every image inside, sorted by filename, as one batch. Subfolders are not searched. Surrounding quotes, a `file://` prefix and a leading `~` are accepted. Confined to ComfyUI's own folders unless you allow more with `COMFYUI_LOADIMAGEPATH_ROOTS` — see [Security](#security). |
| `start_index` *(advanced)* | `0` | Skip this many images — or frames, for a single animated file. |
| `count` *(advanced)* | `0` | How many to load. `0` means all. |
| `select_every_nth` *(advanced)* | `1` | Load only every Nth. |
| `threads` *(advanced)* | `0` | Decoder threads. `0` = auto, `min(8, CPU count)`. |

Outputs `IMAGE` and `MASK`, and previews the first frame after running. Nothing
is copied. Selection is applied as start, then stride, then cap — the same order
VideoHelperSuite uses, so the same three numbers pick the same frames.

Folders sort lexicographically, so zero-pad your filenames: `frame_0010.png`
sorts after `frame_0009.png`, but `frame10.png` sorts *before* `frame9.png`.

## Performance

Folder loads decode in parallel. PyAV releases the GIL while decoding, so this
scales with cores. 24 PNGs at 1920x1080, 69 MiB total, best of three:

| threads | total | per file | speedup |
|---|---:|---:|---:|
| 1 | 1.06 s | 44.2 ms | 1.0x |
| 2 | 0.60 s | 25.0 ms | 1.8x |
| 4 | 0.42 s | 17.3 ms | 2.6x |
| 8 *(default)* | 0.34 s | 14.3 ms | 3.1x |
| 16 | 0.31 s | 13.1 ms | 3.4x |

Output is identical regardless of thread count.

## Security

### Which folders these nodes may read (`COMFYUI_LOADIMAGEPATH_ROOTS`)

**Both nodes read only inside ComfyUI's own `input/`, `output/`, `temp/` and
`user/` folders** unless you say otherwise. To use paths anywhere else — pasting
`~/Pictures/shot.png` into the loader, or pointing the sequence node at a render
folder — list those folders in `COMFYUI_LOADIMAGEPATH_ROOTS` when you start
ComfyUI, separated the way `PATH` is (`:` on Linux and macOS, `;` on Windows):

```bash
COMFYUI_LOADIMAGEPATH_ROOTS=~/Pictures:~/frames:/mnt/renders python main.py
```

Set it once in whatever starts ComfyUI and forget about it. Until you do, a path
outside those folders is refused: the sequence node fails validation on the queue
press with a message naming the variable, and the loader's paste box reports it
in the widget label, with the full list of allowed folders in the server console.
`..` and symlinks are resolved before the check, so neither escapes.

The reason for the fence: both surfaces take a filesystem path from an untrusted
caller. The sequence node's `path` is free text arriving over `/prompt`, and its
`IMAGE` output hands the decoded pixels straight back; the import route copies a
file the browser names into `input/`, where `/view` can fetch it. Without
containment, anything that can reach ComfyUI can name any image on the server's
disk and read it out. Loopback is not a boundary on its own here — a page in your
own browser is on loopback too. Confining reads to folders *you* nominated makes
it a capability you configured rather than one that is simply open, which is why
the allowlist can only be set in the environment ComfyUI was started in, never
from the graph and never from the request.

### The import route

The import route behind the loader's paste box copies a file the browser names,
off the server's filesystem. That is a wider capability than reading one, and it
is fenced in accordingly:

- **The source must be inside the allowed folders**, as above. Everything else
  gets a 403 before the file is so much as stat'd, so the route cannot be used to
  test what exists either.
- **Refused when ComfyUI is not on localhost.** If `--listen` is bound to
  anything other than a loopback address the route returns 403. Set
  `COMFYUI_LOADIMAGEPATH_ALLOW_REMOTE=1` to override, and only do that if you
  trust everyone who can reach the server.
- **The source must really be an image.** Its MIME type must be `image/*`,
  anything a browser executes inline (SVG, HTML, XML) is refused, and Pillow has
  to agree it can parse the header. A renamed `.txt` or a fake `.png` is
  rejected before a byte is copied, so this cannot be turned into a general
  file-read.
- **The destination is never caller-controlled.** It is always `input/`, with
  the basename stripped of anything but alphanumerics, `-_. `, plus a content
  hash, verified with `is_within_directory`. There is no way to steer the write
  out of `input/`.
- Copies go to a `.partial` file and are then renamed, so an interrupted import
  cannot leave a truncated file under a name its hash says is complete.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/dreevelle/ComfyUI-LoadImagePath
```

No extra dependencies; everything it uses ships with ComfyUI.

## Notes

- **Equivalence to `Load Image` is verified, not assumed.** Both nodes produce
  bit-identical `IMAGE` and `MASK` tensors against the built-in node for 8-bit
  RGB, 8-bit RGBA, 16-bit, EXIF-rotated JPEG, a width that is not a multiple of
  32, paletted PNG with `tRNS`, animated GIF, animated WebP, and animated WebP
  with alpha.
- **The mask sentinel.** No alpha gives a `[B, 64, 64]` block of zeros, matching
  the built-in node — that undersized shape is what downstream nodes read as "no
  mask". Alpha gives `1.0 - alpha` at `[B, H, W]`.
- **Mixed alpha in one folder** is the single case that cannot match the
  built-in node, since the two shapes above cannot be concatenated. Files
  without alpha contribute image-sized zeros, which is what the built-in node
  produces for an all-opaque alpha channel. Nothing is masked either way.
- **Mixed sizes raise an error naming both files and both dimensions.** Neither
  node resizes.
- **Only the loader needs the frontend extension** in `web/`. If it fails to
  load you get a plain native image picker — you lose the paste box, not the
  node. The sequence node has no frontend code at all.
- **Recognized extensions** come from the same MIME lookup `Load Image` uses:
  png, jpg, jpeg, webp, gif, bmp, tif, tiff, avif, heic. Not exr, jxl, tga, dds
  or psd.
- **Cache invalidation.** The loader content-hashes its file, matching the
  built-in node. The sequence node uses path, size and mtime instead — hashing a
  500-frame folder would mean gigabytes of reads on every queue press. The
  tradeoff is that an edit preserving both size and mtime is not noticed;
  touching the file forces a reload.
- **Memory.** Images are float32, so 500 frames at 1920x1080 is roughly 12 GB.

## Migrating from VideoHelperSuite

| VideoHelperSuite | Here |
|---|---|
| `Load Image (Path)` | **Load Image From Path**, paste into the box |
| `Load Images (Path)` | **Load Image Sequence From Path** |
| `skip_first_images` | `start_index` |
| `image_load_cap` | `count` |
| `select_every_nth` | `select_every_nth` |
| `frame_count` output | core **Get Image Size**, which returns `batch_size` |

Behavioural differences worth knowing:

- VHS resized mismatched frames to the most common size in the folder, with a
  lanczos filter and a centre crop, silently. These raise instead.
- VHS decoded through Pillow, which truncates 16-bit PNGs to 8-bit.
- VHS decided alpha handling once per directory, so one RGBA file changed the
  channel path for every image in it. These decide per file.
- VHS's path loader did not give you the mask editor. This one does, by not
  replacing the built-in loader in the first place.

## License

[MIT](LICENSE)
