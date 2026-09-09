"""ComfyUI-LoadImagePath.

Load images from a pasted filesystem path instead of hunting through a file
picker, without giving up anything the built-in nodes do.

Two nodes, deliberately shaped differently.

``Load Image From Path`` is a faithful clone of the built-in ``Load Image``: the
same combo widget, the same upload button, the same ``image`` widget name. Paste
a path into its extra box and the file is imported into ``input/`` and selected,
after which *everything downstream is core's code* — the thumbnail, the mask
editor, clipspace, inpainting. That matters more than it sounds. The mask editor
refuses to open on a node with no ``imgs``, and writes its result back into a
widget it finds by the literal name ``image``; a node that only looks like a
loader satisfies neither. Being structurally identical is the only way to get
those for free rather than reimplementing them badly.

``Load Image Sequence From Path`` handles the case the first node deliberately
does not: a whole folder, loaded in place with no copying, with frame selection.
It is entirely server-side with no frontend code.

Both ways in take a filesystem path from an untrusted caller, so both are
contained the same way: reads happen only inside ComfyUI's own directories plus
whatever the operator allows through ``COMFYUI_LOADIMAGEPATH_ROOTS``. See
``_containment_error`` — that fence is why the pack is installable at all.

Both decode through ``VideoFromFile``, the same primitive ``Load Image`` uses, so
pixels and masks match it bit for bit — full-precision 16-bit PNG, paletted
transparency, EXIF rotation, and the ``1.0 - alpha`` mask with its deliberately
undersized 64x64 "no mask here" sentinel.
"""

import hashlib
import logging
import mimetypes
import os
import shutil
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import torch
from aiohttp import web
from PIL import Image, ImageOps, ImageSequence
from typing_extensions import override

import comfy.model_management
import folder_paths
import node_helpers
from comfy.cli_args import args
from comfy.utils import ProgressBar
from comfy_api.latest import ComfyExtension, IO, InputImpl, UI

__version__ = "1.0.2"

# Serves web/loadimagepath.js, which adds the paste-a-path box to the loader.
WEB_DIRECTORY = "./web"

# The frontend clamps integer widgets to their declared max; this is the largest
# value it can represent exactly.
BIGMAX = 2**53 - 1

# Opt-in for running the import route on a non-loopback bind. See _import_denied.
ALLOW_REMOTE_ENV = "COMFYUI_LOADIMAGEPATH_ALLOW_REMOTE"

# Extra directories these nodes may read from, on top of ComfyUI's own.
# os.pathsep-separated, like PATH. See _allowed_roots.
ALLOWED_ROOTS_ENV = "COMFYUI_LOADIMAGEPATH_ROOTS"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _resolve_path(path: str | None) -> str | None:
    """Normalize a pasted path to an absolute one, or raise ValueError.

    Returns None for None, which is what a linked (non-widget) input looks like
    at validation time — see execution.py's mark_missing().
    """
    if path is None:
        return None

    text = path.strip()
    if not text:
        raise ValueError(
            "path is empty. Paste an absolute path to an image file, "
            "or to a folder of images."
        )
    if "\n" in text or "\r" in text:
        lines = len(text.splitlines())
        raise ValueError(
            f"path contains {lines} lines. This node takes exactly one file "
            "or folder path."
        )

    if text.startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    # File managers and shells quote paths containing spaces when you copy them.
    text = text.strip("\"'")

    # expanduser but deliberately not expandvars: on Windows the latter eats
    # %...% tokens, and ComfyUI users habitually paste those as literal text.
    return os.path.abspath(os.path.expanduser(text))


def _allowed_roots() -> list[str]:
    """Directories this package is permitted to read from.

    Governs both read surfaces — the sequence node's `path` and the import
    route — because they are the same primitive and deserve the same knob.

    ComfyUI's own directories always, plus whatever the operator listed in
    COMFYUI_LOADIMAGEPATH_ROOTS. Recomputed per call rather than cached: the
    input and user directories are settable at runtime, and an operator who
    edits the environment should not have to guess whether it took effect.
    """
    roots = [
        folder_paths.get_input_directory(),
        folder_paths.get_output_directory(),
        folder_paths.get_temp_directory(),
        folder_paths.get_user_directory(),
    ]
    extra = os.environ.get(ALLOWED_ROOTS_ENV) or ""
    roots += extra.split(os.pathsep)
    return [
        os.path.abspath(os.path.expanduser(root.strip()))
        for root in roots
        if root and root.strip()
    ]


def _containment_error(resolved: str) -> str | None:
    """Reason `resolved` is out of bounds, or None if it may be read.

    An unbounded filesystem path is an arbitrary-read primitive on either
    surface: through the sequence node, anything that can reach /prompt names a
    file and reads the decoded pixels out of the IMAGE output; through the
    import route, it names a file, gets it copied into input/, and fetches it
    back from /view. Containing reads to directories the *operator* chose is
    what makes that a configured capability rather than an exposed one, which is
    why the allowlist is settable only from the environment ComfyUI was started
    in — never from the graph, and never from the request.

    Containment is checked before any stat of the path, so an out-of-bounds path
    cannot be used as an existence oracle either.
    """
    roots = _allowed_roots()
    for root in roots:
        # realpath on both sides, so neither '..' nor a planted symlink escapes.
        if folder_paths.is_within_directory(root, resolved) or resolved == root:
            return None
    return (
        f"path is outside the folders this node may read: {resolved}\n"
        f"Allowed: {', '.join(roots)}\n"
        f"To read from anywhere else, start ComfyUI with {ALLOWED_ROOTS_ENV} set to "
        f"the folders you want to allow, separated by '{os.pathsep}' — for example "
        f"{ALLOWED_ROOTS_ENV}={os.path.join(os.path.expanduser('~'), 'frames')}"
    )


def _scan_directory(directory: str) -> list[str]:
    """Filenames of the images directly inside `directory`, sorted by name.

    Uses the same MIME-based filter as LoadImage.INPUT_TYPES, so the recognized
    extension set tracks core's rather than being hardcoded here. Not recursive.
    """
    names = [entry.name for entry in os.scandir(directory) if entry.is_file()]
    return sorted(folder_paths.filter_files_content_types(names, ["image"]))


def _select(items: list, start_index: int, count: int, select_every_nth: int) -> list:
    """Apply the start/stride/cap selection, in VideoHelperSuite's order.

    Ordering matters for workflow migration: VHS applies skip_first_images, then
    select_every_nth, then image_load_cap, so the same three numbers pick the
    same frames here.
    """
    return items[start_index:][::select_every_nth][: count or None]


# ---------------------------------------------------------------------------
# Animated-WebP fallback
#
# Copied from nodes.py LoadImage.load_image (ComfyUI v0.31.0, lines 1757-1789).
# PyAV cannot decode animated WebP; core keeps this Pillow path for exactly that
# case. Copied rather than imported so a core refactor cannot silently change
# what these nodes return — diff against that function when updating ComfyUI.
#
# Two deliberate deltas from the original, both load-bearing:
#   * Returns (images, alpha | None) instead of (images, mask), so the caller
#     owns the "no alpha" sentinel decision.
#   * Reads alpha presence from frame 0 rather than per-frame. Core's per-frame
#     check would try to cat a [H,W] mask with a [64,64] one and crash on an
#     animation whose frames disagree about bands; this cannot.
# ---------------------------------------------------------------------------
def _load_animated_webp(path: str) -> tuple[torch.Tensor, torch.Tensor | None]:
    img = node_helpers.pillow(Image.open, path)

    output_images = []
    output_alphas = []
    has_alpha = None
    w, h = None, None

    for i in ImageSequence.Iterator(img):
        i = node_helpers.pillow(ImageOps.exif_transpose, i)

        image = i.convert("RGB")

        if len(output_images) == 0:
            w = image.size[0]
            h = image.size[1]
            has_alpha = "A" in i.getbands()

        if image.size[0] != w or image.size[1] != h:
            continue

        image = np.array(image).astype(np.float32) / 255.0
        output_images.append(torch.from_numpy(image)[None,])

        if has_alpha:
            alpha = np.array(i.getchannel("A")).astype(np.float32) / 255.0
            output_alphas.append(torch.from_numpy(alpha)[None, ..., None])

    if not output_images:
        raise RuntimeError(f"No frames could be decoded from: {path}")

    images = torch.cat(output_images, dim=0)
    alphas = torch.cat(output_alphas, dim=0) if has_alpha else None
    return images, alphas


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def _load_one(path: str) -> tuple[torch.Tensor, torch.Tensor | None]:
    """Decode one image file to (images [F,H,W,3], alpha [F,H,W,1] | None).

    This is the same call LoadImage makes, so the pixels match bit for bit.
    """
    try:
        components = InputImpl.VideoFromFile(path).get_components()
    except Exception as e:
        # PyAV's own errors name neither the file nor the operation, which is
        # useless when one file out of five hundred is the bad one.
        raise RuntimeError(
            f"Failed to decode image: {path}\n  {type(e).__name__}: {e}"
        ) from e

    if components.images.shape[0] > 0:
        return components.images, components.alpha

    return _load_animated_webp(path)


def _load_many(paths: list[str], threads: int) -> list[tuple[torch.Tensor, torch.Tensor | None]]:
    """Decode files in parallel. PyAV releases the GIL, so this scales with cores."""
    if len(paths) == 1:
        return [_load_one(paths[0])]

    num_workers = threads if threads > 0 else min(8, os.cpu_count() or 1)
    pbar = ProgressBar(len(paths))
    parts = []
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        # map preserves input order, so frames stay in filename order.
        for part in pool.map(_load_one, paths):
            parts.append(part)
            pbar.update(1)
    return parts


def _to_image_and_mask(parts, names, where, dtype, device):
    """Concatenate decoded parts into (IMAGE, MASK), enforcing equal sizes."""
    height, width = parts[0][0].shape[1:3]
    for name, (part_images, _) in zip(names, parts):
        if part_images.shape[1:3] != (height, width):
            part_h, part_w = part_images.shape[1:3]
            raise ValueError(
                f"Mixed image sizes in {where}:\n"
                f"  {names[0]} is {width}x{height}\n"
                f"  {name} is {part_w}x{part_h}\n"
                "All images loaded together must have the same width and height, "
                "because they are batched into a single IMAGE tensor. Move the odd "
                "file out of the folder, resize it on disk, or narrow the selection "
                "with start_index / count / select_every_nth.\n"
                "(This node will not resize for you. VideoHelperSuite silently "
                "lanczos-resized and centre-cropped mismatched frames to the most "
                "common size, which is what this is here to avoid.)"
            )

    images = torch.cat([p[0] for p in parts], dim=0).to(device=device, dtype=dtype)

    if all(alpha is None for _, alpha in parts):
        # The 64x64 sentinel is what tells downstream nodes "no mask here".
        # Never widen it to image-sized; inpainting reads the shape.
        mask = torch.zeros((images.shape[0], 64, 64), dtype=dtype, device=device)
    else:
        # A file with no alpha is fully opaque, so alpha 1 and mask 1-1 = 0 —
        # the same thing core emits for an all-opaque alpha channel.
        mask = torch.cat(
            [
                (1.0 - alpha[..., -1]) if alpha is not None
                else torch.zeros((part_images.shape[0], height, width))
                for part_images, alpha in parts
            ],
            dim=0,
        ).to(device=device, dtype=dtype)

    return images, mask


# ---------------------------------------------------------------------------
# Import into input/
#
# Copying a file the caller names is a wider primitive than reading one, so it
# is fenced in on every side that costs nothing:
#
#   * Refused outright when the server is bound to a non-loopback address,
#     unless ALLOW_REMOTE_ENV is set. A file-copy endpoint reachable from the
#     network is a different proposition to one reachable from your own machine.
#   * The source must be inside _allowed_roots(). Loopback is not a trust
#     boundary on its own: a page in the operator's own browser is on loopback
#     too, and CSRF to 127.0.0.1 is exactly how that gets abused. Containment is
#     what makes "copy me that file" a capability the operator configured.
#   * The source must be a real image: its MIME type must be image/*, anything a
#     browser executes inline (SVG, HTML, XML) is refused, and Pillow must agree
#     it can parse the header. Extension alone is not trusted.
#   * The destination is never caller-controlled. It is always input/, with a
#     sanitised basename and a content hash appended, checked with
#     is_within_directory afterwards. There is no way to steer the write.
#   * Identical content is imported once. Re-pasting the same path reuses the
#     existing copy instead of growing input/ without bound.
# ---------------------------------------------------------------------------
def _import_denied() -> str | None:
    """Reason the import route must refuse, or None if it may run."""
    if os.environ.get(ALLOW_REMOTE_ENV) == "1":
        return None
    listen = (getattr(args, "listen", "") or "").strip()
    hosts = {h.strip() for h in listen.split(",") if h.strip()}
    if hosts.issubset({"127.0.0.1", "localhost", "::1"}):
        return None
    return (
        f"Importing by path is disabled because ComfyUI is listening on {listen!r} "
        "rather than localhost, and this endpoint copies files off the server's "
        f"filesystem. Set {ALLOW_REMOTE_ENV}=1 to enable it anyway."
    )


def _is_safe_image(path: str) -> bool:
    """True if `path` is an image a browser will not execute inline."""
    content_type = mimetypes.guess_type(path)[0]
    if (
        not content_type
        or not content_type.startswith("image/")
        or folder_paths.is_dangerous_content_type(content_type)
    ):
        return False
    try:
        # Header-only parse; catches a non-image wearing an image extension.
        with Image.open(path) as probe:
            probe.verify()
    except Exception:
        return False
    return True


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_to_input(source: str) -> str:
    """Copy `source` into input/ and return its filename there.

    Content-addressed, so importing the same file twice is a no-op.
    """
    stem, extension = os.path.splitext(os.path.basename(source))
    # Basename only, and only characters that cannot confuse a path.
    stem = "".join(c for c in stem if c.isalnum() or c in "-_. ").strip()
    # Leading dots would make it a hidden file, which the input listing skips.
    stem = stem.lstrip(".").strip() or "image"
    filename = f"{stem}_{_file_digest(source)[:12]}{extension.lower()}"

    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)
    destination = os.path.abspath(os.path.join(input_dir, filename))
    if not folder_paths.is_within_directory(input_dir, destination):
        raise ValueError("Refusing to write outside the input directory.")

    if not os.path.exists(destination):
        # Copy beside the target then rename, so a crash mid-copy cannot leave a
        # truncated file sitting at a name the hash claims is complete.
        partial = destination + ".partial"
        shutil.copyfile(source, partial)
        os.replace(partial, destination)

    return filename


async def import_handler(request: web.Request) -> web.Response:
    denied = _import_denied()
    if denied is not None:
        return web.json_response({"error": denied}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Expected a JSON body."}, status=400)

    try:
        resolved = _resolve_path(payload.get("path"))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    # Containment before any stat, so a path outside the allowlist cannot be
    # probed for existence by telling 403 and 404 apart.
    denied = None if resolved is None else _containment_error(resolved)
    if denied is not None:
        # The console gets the full reason, including which folders are allowed.
        # The response gets one line, because web/loadimagepath.js renders it
        # into a widget label where a paragraph would be unreadable.
        logging.warning("[LoadImagePath] refused import: %s", denied)
        return web.json_response(
            {"error": f"Outside the folders this may import from — set "
                      f"{ALLOWED_ROOTS_ENV} on the server to allow it."},
            status=403,
        )

    if resolved is None or not os.path.exists(resolved):
        return web.json_response({"error": f"path does not exist: {resolved}"}, status=404)
    if os.path.isdir(resolved):
        return web.json_response(
            {"error": "That is a folder. Use the Load Image Sequence From Path node "
                      "for folders — it loads them in place, without copying."},
            status=400,
        )
    if not _is_safe_image(resolved):
        return web.json_response(
            {"error": f"Not a readable image file: {os.path.basename(resolved)}"},
            status=415,
        )

    try:
        filename = import_to_input(resolved)
    except OSError as e:
        return web.json_response({"error": f"Could not import: {e}"}, status=500)

    return web.json_response({"filename": filename, "source": resolved})


def _register_routes() -> None:
    """Attach routes, if a server exists.

    Guarded so the module stays importable without one — the test harnesses load
    it directly, and `comfy node validate` imports it outside a running server.
    """
    try:
        from server import PromptServer
    except ImportError:
        return
    # getattr, not PromptServer.instance: the attribute is assigned in the
    # server's __init__, so it does not exist at all until one is constructed.
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return
    server.routes.post("/loadimagepath/import")(import_handler)


_register_routes()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class LoadImageFromPath(IO.ComfyNode):
    """A structural clone of core's LoadImage, plus a paste-a-path box.

    The schema is intentionally just `image`, matching the built-in node exactly.
    Everything the frontend does for a loader — the thumbnail, the upload button,
    the mask editor, clipspace round-tripping — keys off that combo being present
    and named `image`. The paste box is added by web/loadimagepath.js and is not
    a graph input: it imports the file, selects it here, and gets out of the way.
    """

    @classmethod
    def define_schema(cls):
        input_dir = folder_paths.get_input_directory()
        files = [
            f for f in os.listdir(input_dir)
            if os.path.isfile(os.path.join(input_dir, f))
        ]
        files = folder_paths.filter_files_content_types(files, ["image"])
        return IO.Schema(
            node_id="LoadImageFromPath",
            search_aliases=[
                "load image", "load image path", "paste path", "path",
                "open image", "import image", "vhs",
            ],
            display_name="Load Image From Path",
            description=(
                "Loads an image, with a box for pasting a filesystem path. "
                "Pasting a path imports that file into the input folder and "
                "selects it, so the mask editor, inpainting and previews all "
                "behave exactly as they do on the built-in Load Image node."
            ),
            category="image",
            inputs=[
                IO.Combo.Input(
                    "image",
                    options=sorted(files),
                    upload=IO.UploadType.image,
                    tooltip=(
                        "The image to load, from the input folder.\n"
                        "Paste a path into the box above to import a file from "
                        "anywhere on disk and select it here."
                    ),
                ),
            ],
            outputs=[IO.Image.Output(), IO.Mask.Output()],
        )

    @classmethod
    def execute(cls, image: str) -> IO.NodeOutput:
        image_path = folder_paths.get_annotated_filepath(image)
        dtype = comfy.model_management.intermediate_dtype()
        device = comfy.model_management.intermediate_device()

        parts = [_load_one(image_path)]
        images, mask = _to_image_and_mask(
            parts, [os.path.basename(image_path)], image_path, dtype, device
        )
        return IO.NodeOutput(images, mask)

    @classmethod
    def fingerprint_inputs(cls, image):
        # Content hash, matching core's LoadImage rather than the cheaper
        # mtime check the sequence node uses: this is a single file, usually
        # small, and exact agreement with the built-in node is the point.
        image_path = folder_paths.get_annotated_filepath(image)
        return _file_digest(image_path)

    @classmethod
    def validate_inputs(cls, image):
        if not folder_paths.exists_annotated_filepath(image):
            return f"Invalid image file: {image}"
        return True


class LoadImageSequenceFromPath(IO.ComfyNode):
    """Folder loading, in place. No copying and no frontend code."""

    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="LoadImageSequenceFromPath",
            search_aliases=[
                "load images from directory", "load folder", "image sequence",
                "load frames", "batch", "path", "vhs",
            ],
            display_name="Load Image Sequence From Path",
            description=(
                "Loads every image in a folder as one batch, sorted by filename, "
                "from a typed or pasted path. Nothing is copied. Images must all "
                "be the same size; mismatches raise rather than being resized. "
                "Reads outside ComfyUI's own folders need "
                f"{ALLOWED_ROOTS_ENV} set on the server."
            ),
            category="image",
            inputs=[
                IO.String.Input(
                    "path",
                    multiline=False,
                    default="",
                    placeholder="/path/to/frames/",
                    tooltip=(
                        "Path to a folder of images, or to a single animated file.\n"
                        "  A folder loads every image inside it, sorted by filename, "
                        "as one batch. Subfolders are not searched.\n"
                        "  A file loads all of its frames.\n"
                        "Surrounding quotes, a file:// prefix and a leading ~ are accepted.\n"
                        "Reads are confined to ComfyUI's own folders unless the server "
                        f"was started with {ALLOWED_ROOTS_ENV} listing the others."
                    ),
                ),
                IO.Int.Input(
                    "start_index", default=0, min=0, max=BIGMAX, advanced=True,
                    tooltip=(
                        "Skip this many images from the start — or this many frames, "
                        "if the path is a single animated file."
                    ),
                ),
                IO.Int.Input(
                    "count", default=0, min=0, max=BIGMAX, advanced=True,
                    tooltip=(
                        "How many images to load. 0 = all of them. Applied after "
                        "start_index and select_every_nth."
                    ),
                ),
                IO.Int.Input(
                    "select_every_nth", default=1, min=1, max=BIGMAX, advanced=True,
                    tooltip=(
                        "Load only every Nth image. 1 = every one, 2 = every other, "
                        "and so on. Applied after start_index."
                    ),
                ),
                IO.Int.Input(
                    "threads", default=0, min=0, max=64, advanced=True,
                    tooltip=(
                        "Decoder threads. 0 = auto (min(8, CPU count)). Decoding runs "
                        "through PyAV, which releases the GIL, so this scales with cores."
                    ),
                ),
            ],
            outputs=[IO.Image.Output(), IO.Mask.Output()],
        )

    @classmethod
    def execute(cls, path: str, start_index: int = 0, count: int = 0,
                select_every_nth: int = 1, threads: int = 0) -> IO.NodeOutput:
        resolved = _resolve_path(path)
        if resolved is not None:
            denied = _containment_error(resolved)
            if denied is not None:
                raise PermissionError(denied)
        if resolved is None or not os.path.exists(resolved):
            raise FileNotFoundError(f"path does not exist: {resolved}")

        dtype = comfy.model_management.intermediate_dtype()
        device = comfy.model_management.intermediate_device()

        if os.path.isdir(resolved):
            all_names = _scan_directory(resolved)
            if not all_names:
                raise FileNotFoundError(
                    f"No image files in folder: {resolved}\n"
                    "Files are matched by extension through their image MIME type "
                    "(png, jpg, jpeg, webp, gif, bmp, tif, tiff, avif and so on). "
                    "Subfolders are not searched."
                )
            names = _select(all_names, start_index, count, select_every_nth)
            if not names:
                raise ValueError(
                    f"start_index={start_index}, count={count}, "
                    f"select_every_nth={select_every_nth} selected 0 of "
                    f"{len(all_names)} images in {resolved}."
                )
            parts = _load_many([os.path.join(resolved, n) for n in names], threads)
        else:
            names = [os.path.basename(resolved)]
            images, alpha = _load_one(resolved)
            keep = _select(list(range(images.shape[0])), start_index, count, select_every_nth)
            if not keep:
                raise ValueError(
                    f"start_index={start_index}, count={count}, "
                    f"select_every_nth={select_every_nth} selected 0 of "
                    f"{images.shape[0]} frames in {resolved}."
                )
            if len(keep) != images.shape[0]:
                index = torch.tensor(keep)
                images = images[index]
                alpha = alpha[index] if alpha is not None else None
            parts = [(images, alpha)]

        images, mask = _to_image_and_mask(parts, names, resolved, dtype, device)

        # First frame only. Enough to confirm the right folder loaded, and it
        # costs one encode whether the batch is 1 frame or 500. The executor
        # emits ui for any node it runs, not just output nodes, and replays it
        # from cache on a re-queue.
        return IO.NodeOutput(
            images, mask, ui=UI.PreviewImage(images[:1], cls=None)
        )

    @classmethod
    def fingerprint_inputs(cls, path):
        # Only `path` is taken: the other widgets are already part of the engine's
        # cache key, and including them would rescan the folder on every knob turn.
        if not isinstance(path, str):
            return float("nan")  # linked input; its value is unknowable here
        try:
            resolved = _resolve_path(path)
        except ValueError:
            return "invalid"
        if resolved is None:
            return "missing"
        if _containment_error(resolved) is not None:
            # A constant, so an out-of-bounds path cannot be probed for existence
            # by watching the cache key change.
            return "denied"
        if not os.path.exists(resolved):
            return "missing"

        if os.path.isfile(resolved):
            stat = os.stat(resolved)
            return f"{resolved}|{stat.st_mtime_ns}|{stat.st_size}"

        # mtime + size rather than content hashes: hashing a 500-frame folder
        # would be gigabytes of reads on every queue press, before execution.
        # LoadVideo in core sets the same precedent.
        digest = hashlib.sha256(resolved.encode())
        for name in _scan_directory(resolved):
            stat = os.stat(os.path.join(resolved, name))
            digest.update(f"\0{name}\0{stat.st_mtime_ns}\0{stat.st_size}".encode())
        return digest.hexdigest()

    @classmethod
    def validate_inputs(cls, path):
        # Exactly one argument: execution.py emits one error per validated input,
        # so a second parameter would duplicate every message in the UI.
        if path is None:
            return True  # linked input; only checkable at execute time
        try:
            resolved = _resolve_path(path)
        except ValueError as e:
            return str(e)
        denied = _containment_error(resolved)
        if denied is not None:
            return denied
        if not os.path.exists(resolved):
            return f"path does not exist: {resolved}"
        if os.path.isdir(resolved):
            # Deliberately not scanning here — validation runs on every queue
            # press and has to stay cheap. An empty folder is caught at execute.
            return True
        if not folder_paths.filter_files_content_types([os.path.basename(resolved)], ["image"]):
            return f"not a recognized image file: {resolved}"
        return True


class LoadImagePathExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[IO.ComfyNode]]:
        return [LoadImageFromPath, LoadImageSequenceFromPath]


async def comfy_entrypoint() -> LoadImagePathExtension:
    return LoadImagePathExtension()
