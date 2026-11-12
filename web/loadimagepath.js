// Adds a "paste path" box to the Load Image From Path node.
//
// This renders nothing and owns no preview. It imports the pasted file into
// input/ via /loadimagepath/import, then sets the node's `image` combo to the
// result and fires its callback — from that point on the thumbnail, the mask
// editor, clipspace and inpainting are all core's code running against a normal
// input/ file, exactly as on the built-in Load Image node.
//
// That is the whole design: the less this file does, the less there is to break
// when the frontend changes. If it fails to load, the node degrades to a plain
// native image picker rather than breaking.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "LoadImageFromPath";
const WIDGET_NAME = "paste path";
const PLACEHOLDER = "paste a path here, then press Enter";

app.registerExtension({
  name: "dreevelle.LoadImagePath.Import",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      const node = this;

      const imageWidget = node.widgets?.find((w) => w.name === "image");
      if (!imageWidget) return result;

      let busy = false;

      const setStatus = (text) => {
        pasteWidget.label = text ? `paste path — ${text}` : WIDGET_NAME;
        node.setDirtyCanvas?.(true, false);
      };

      const importPath = async (raw) => {
        const path = (raw ?? "").trim();
        if (!path || busy) return;
        busy = true;
        setStatus("importing…");

        try {
          const response = await fetch(api.apiURL("/loadimagepath/import"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path }),
          });
          const data = await response.json().catch(() => ({}));

          if (!response.ok) {
            setStatus(data.error || `failed (${response.status})`);
            return;
          }

          // Make the imported file selectable, then select it. Firing the
          // widget's own callback is what hands control back to core: it
          // clears node.imgs and repopulates the preview from input/, which
          // is also what makes the mask editor willing to open.
          const values = imageWidget.options?.values;
          if (Array.isArray(values) && !values.includes(data.filename)) {
            values.push(data.filename);
            values.sort();
          }
          imageWidget.value = data.filename;
          imageWidget.callback?.(data.filename);

          // The path has done its job; leaving it set would imply it is still
          // the source of truth when the combo now is.
          pasteWidget.value = "";
          setStatus("");
        } catch (error) {
          setStatus(error?.message ? `failed: ${error.message}` : "failed");
        } finally {
          busy = false;
        }
      };

      const pasteWidget = node.addWidget(
        "text",
        WIDGET_NAME,
        "",
        (value) => importPath(value),
        { serialize: false, multiline: false, placeholder: PLACEHOLDER }
      );

      // Put it directly above the combo it feeds.
      const widgets = node.widgets;
      const from = widgets.indexOf(pasteWidget);
      const to = widgets.indexOf(imageWidget);
      if (from > -1 && to > -1 && from !== to) {
        widgets.splice(from, 1);
        widgets.splice(to, 0, pasteWidget);
      }

      // Saved workflows restore widget values by position, and before the crop
      // and pad inputs existed they were [paste path, image, upload]. Core adds
      // the upload button after every schema input, so without this the
      // button's value lands in crop_x. Directly after `image` keeps old
      // workflows lined up and the new widgets on their defaults.
      const uploadWidget = widgets.find((w) => w.name === "upload");
      const imageAt = widgets.indexOf(imageWidget);
      if (uploadWidget && imageAt > -1 && widgets.indexOf(uploadWidget) !== imageAt + 1) {
        widgets.splice(widgets.indexOf(uploadWidget), 1);
        widgets.splice(widgets.indexOf(imageWidget) + 1, 0, uploadWidget);
      }

      return result;
    };
  },
});
