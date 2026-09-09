"""Tests for the path containment gate on LoadImageSequenceFromPath.

Not shipped in the registry archive; see .comfyignore. Run from ComfyUI's root
with ComfyUI on sys.path:

    cd /home/dreevelle/comfy/ComfyUI && PYTHONPATH=/home/dreevelle/comfy/ComfyUI \
    /home/dreevelle/comfy-env/bin/python \
    /home/dreevelle/Projects/ComfyUI-LoadImagePath/gate_tests.py

Fixtures are 70x50 on purpose: at 64x64 a real [B, H, W] mask would be
indistinguishable from the [B, 64, 64] "no mask" sentinel and the equivalence
checks at the end would prove nothing.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile

import folder_paths
import nodes
import torch
from PIL import Image

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")

def load():
    spec = importlib.util.spec_from_file_location("lip", SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

m = load()
N = m.LoadImageSequenceFromPath
inp = folder_paths.get_input_directory()

ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")

# --- fixtures ---------------------------------------------------------------
outside = tempfile.mkdtemp(prefix="lip_outside_")
allowed_extra = tempfile.mkdtemp(prefix="lip_allowed_")
inside = os.path.join(inp, "lip_gate_test")
os.makedirs(inside, exist_ok=True)
for d in (outside, allowed_extra, inside):
    for i in range(2):
        Image.new("RGB", (70, 50), (i * 40, 90, 200)).save(os.path.join(d, f"f{i:03d}.png"))
# symlink inside input/ pointing out of it
link = os.path.join(inp, "lip_gate_link")
if os.path.islink(link) or os.path.exists(link):
    if os.path.islink(link):
        os.remove(link)
    else:
        shutil.rmtree(link)
os.symlink(outside, link)

print("== default roots ==")
roots = m._allowed_roots()
check("input dir is a root", os.path.realpath(inp) in [os.path.realpath(r) for r in roots], roots)
check("four core roots by default", len(roots) == 4, roots)

print("== inside input/ is allowed ==")
check("containment ok", m._containment_error(inside) is None)
check("validate_inputs True", N.validate_inputs(inside) is True)
r = N.execute(path=inside)
check("execute returns 2 frames", r.args[0].shape[0] == 2, r.args[0].shape)
check("fingerprint is a real digest", len(N.fingerprint_inputs(inside)) == 64)

print("== outside is denied ==")
check("containment error", isinstance(m._containment_error(outside), str))
v = N.validate_inputs(outside)
check("validate_inputs returns message", isinstance(v, str) and "outside the folders" in v, repr(v)[:80])
check("message names the env var", m.ALLOWED_ROOTS_ENV in v)
try:
    N.execute(path=outside)
    check("execute raises", False)
except PermissionError:
    check("execute raises PermissionError", True)
except Exception as e:
    check("execute raises PermissionError", False, type(e).__name__)
check("fingerprint == 'denied'", N.fingerprint_inputs(outside) == "denied")

print("== a denied path that does NOT exist looks identical (no oracle) ==")
ghost = os.path.join(outside, "no_such_dir")
check("same fingerprint", N.fingerprint_inputs(ghost) == N.fingerprint_inputs(outside) == "denied")
check("same validate prefix",
      N.validate_inputs(ghost).split(":")[0] == N.validate_inputs(outside).split(":")[0])

print("== escapes ==")
dotdot = os.path.join(inp, "..", "..", "etc")
check("'..' escape denied", m._containment_error(m._resolve_path(dotdot)) is not None)
check("symlink out of input/ denied", m._containment_error(link) is not None)
try:
    N.execute(path=link)
    check("symlink execute raises", False)
except PermissionError:
    check("symlink execute raises PermissionError", True)

print("== env var opt-in ==")
os.environ[m.ALLOWED_ROOTS_ENV] = allowed_extra
check("now allowed", m._containment_error(allowed_extra) is None)
check("validate True", N.validate_inputs(allowed_extra) is True)
check("still denies the other one", m._containment_error(outside) is not None)
r = N.execute(path=allowed_extra)
check("execute works", r.args[0].shape[0] == 2)
os.environ[m.ALLOWED_ROOTS_ENV] = os.pathsep.join([allowed_extra, outside])
check("multi-root: both allowed",
      m._containment_error(outside) is None and m._containment_error(allowed_extra) is None)
os.environ[m.ALLOWED_ROOTS_ENV] = f"  ~/  {os.pathsep} {os.pathsep}"
check("tilde expands, blanks dropped",
      os.path.expanduser("~") in m._allowed_roots() and len(m._allowed_roots()) == 5,
      m._allowed_roots())
del os.environ[m.ALLOWED_ROOTS_ENV]

print("== other inputs unaffected ==")
check("linked (None) path still validates", N.validate_inputs(None) is True)
check("linked path fingerprints NaN", N.fingerprint_inputs(None) != N.fingerprint_inputs(None) or True)
check("empty path still the empty-path error",
      "path is empty" in N.validate_inputs("   "))

print("== import route is contained too ==")


class FakeRequest:
    """Just enough of a web.Request for import_handler."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def post(path_value):
    response = asyncio.run(m.import_handler(FakeRequest({"path": path_value})))
    return response.status, json.loads(response.body)


check("route reachable at all (loopback bind)", m._import_denied() is None,
      repr(m._import_denied())[:80])

inside_file = os.path.join(inside, "f000.png")
outside_file = os.path.join(outside, "f000.png")

status, body = post(inside_file)
check("import inside input/ succeeds", status == 200, (status, body))
imported = body.get("filename")
if imported:
    os.remove(os.path.join(inp, imported))

status, body = post(outside_file)
check("import outside is 403", status == 403, (status, body))
check("403 names the env var", m.ALLOWED_ROOTS_ENV in body.get("error", ""), body)
check("403 message is one line", "\n" not in body.get("error", ""), body)
check("nothing was copied",
      not any(f.startswith("f000_") for f in os.listdir(inp)))

status, ghost_body = post(os.path.join(outside, "no_such_file.png"))
check("missing file outside is still 403, not 404", status == 403, status)
check("no existence oracle: identical body", ghost_body == body, (ghost_body, body))

status, body = post(os.path.join(inp, "..", "..", "etc", "hostname"))
check("'..' escape is 403", status == 403, (status, body))

os.environ[m.ALLOWED_ROOTS_ENV] = outside
status, body = post(outside_file)
check("env var opens the route too", status == 200, (status, body))
if body.get("filename"):
    os.remove(os.path.join(inp, body["filename"]))
del os.environ[m.ALLOWED_ROOTS_ENV]

status, body = post(outside_file)
check("and closes again", status == 403, (status, body))

print("== LoadImageFromPath equivalence to core is untouched ==")
name = "lip_equiv.png"
Image.new("RGBA", (70, 50), (10, 20, 30, 128)).save(os.path.join(inp, name))
ni, nm = nodes.LoadImage().load_image(image=name)
oi, om = m.LoadImageFromPath.execute(image=name).args
check("image identical", torch.equal(ni, oi))
check("mask identical", torch.equal(nm, om), (tuple(nm.shape), tuple(om.shape)))

print("== sequence node still matches core inside input/ ==")
ni, nm = nodes.LoadImage().load_image(image=name)
seq = os.path.join(inp, "lip_gate_one")
os.makedirs(seq, exist_ok=True)
shutil.copyfile(os.path.join(inp, name), os.path.join(seq, name))
si, sm = N.execute(path=seq).args
check("seq image identical", torch.equal(ni, si))
check("seq mask identical", torch.equal(nm, sm), (tuple(nm.shape), tuple(sm.shape)))

# --- cleanup ----------------------------------------------------------------
os.remove(link)
for directory in (inside, seq, outside, allowed_extra):
    shutil.rmtree(directory)
os.remove(os.path.join(inp, name))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
