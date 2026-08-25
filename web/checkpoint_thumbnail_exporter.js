import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "CheckpointThumbnailExporter";
const MIN_WIDTH = 470;
const REPORT_HEIGHT = 170;

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function getWidgetValue(node, name, fallback = "") {
    const widget = getWidget(node, name);
    return widget ? widget.value : fallback;
}

function setWidgetValue(node, name, value) {
    const widget = getWidget(node, name);
    if (!widget) return;
    widget.value = value;
    if (typeof widget.callback === "function") {
        try { widget.callback(value, app.canvas, node, null); } catch (_) {}
    }
}

function wrapText(ctx, text, maxWidth) {
    const lines = [];
    const paragraphs = String(text || "").split(/\r?\n/);
    for (const paragraph of paragraphs) {
        if (!paragraph) {
            lines.push("");
            continue;
        }
        let line = "";
        const words = paragraph.split(/(\s+)/);
        for (const word of words) {
            const candidate = line + word;
            if (ctx.measureText(candidate).width <= maxWidth || !line) {
                line = candidate;
            } else {
                lines.push(line.trimEnd());
                line = word.trimStart();
            }
        }
        lines.push(line.trimEnd());
    }
    return lines;
}

function operationLabel(operation) {
    if (operation === "uninstall_managed") return "Managed Thumbnails";
    return "Missing Thumbnails";
}

function buttonLabel(node) {
    const operation = getWidgetValue(node, "operation", "install_missing");
    const runMode = getWidgetValue(node, "run_mode", "dry_run");
    if (operation === "uninstall_managed") {
        return runMode === "execute"
            ? "❌ [Execute!] Uninstall Managed Thumbnails"
            : "❌ [Dry Run] Find Managed Thumbnails";
    }
    return runMode === "execute"
        ? "🎨 [Execute!] Install Missing Thumbnails"
        : "🎨 [Dry Run] Find Missing Thumbnails";
}

function payloadKey(node) {
    return JSON.stringify({
        operation: getWidgetValue(node, "operation", "install_missing"),
        target_format: getWidgetValue(node, "target_format", "OGN-ModelManager"),
    });
}

function syncStatusUI(node) {
    const ui = node.cte_status_ui;
    if (!ui) return;

    const progress = node.cte_progress || { current: 0, total: 0, status: "Ready.", current_name: "" };
    const total = Number(progress.total || 0);
    const current = Number(progress.current || 0);
    const ratio = total > 0
        ? Math.max(0, Math.min(1, current / total))
        : (node.cte_is_running ? 0.25 : 0);
    ui.progressFill.style.width = `${ratio * 100}%`;
    ui.progressFill.style.background = node.cte_is_running ? "#7aa7ff" : "#6f9f6f";
    ui.progressText.textContent = total > 0
        ? `${current} / ${total}`
        : String(progress.status || "Ready.");
    ui.currentName.textContent = progress.current_name && progress.phase !== "scanning_source"
        ? String(progress.current_name)
        : "";

    const reportText = node.cte_is_running
        ? (node.cte_live_report || node.cte_report || "")
        : (node.cte_report || "");
    if (ui.textarea.value !== reportText) {
        ui.textarea.value = reportText;
    }
}

function createStatusUI(node) {
    if (node.cte_status_ui || typeof node.addDOMWidget !== "function") return;

    const root = document.createElement("div");
    root.style.display = "flex";
    root.style.flexDirection = "column";
    root.style.gap = "5px";
    root.style.width = "100%";
    root.style.height = "100%";
    root.style.minHeight = "245px";
    root.style.boxSizing = "border-box";
    root.style.padding = "2px 0";

    const progressLabel = document.createElement("div");
    progressLabel.textContent = "Progress";
    progressLabel.style.fontSize = "12px";

    const progressTrack = document.createElement("div");
    progressTrack.style.position = "relative";
    progressTrack.style.width = "100%";
    progressTrack.style.height = "12px";
    progressTrack.style.flex = "0 0 12px";
    progressTrack.style.boxSizing = "border-box";
    progressTrack.style.border = "1px solid #555";
    progressTrack.style.background = "#222";

    const progressFill = document.createElement("div");
    progressFill.style.width = "0%";
    progressFill.style.height = "100%";
    progressFill.style.background = "#6f9f6f";
    progressTrack.append(progressFill);

    const progressLine = document.createElement("div");
    progressLine.style.display = "flex";
    progressLine.style.gap = "8px";
    progressLine.style.minHeight = "16px";
    progressLine.style.fontSize = "11px";
    progressLine.style.color = "#CCC";

    const progressText = document.createElement("span");
    const currentName = document.createElement("span");
    currentName.style.overflow = "hidden";
    currentName.style.textOverflow = "ellipsis";
    currentName.style.whiteSpace = "nowrap";
    progressLine.append(progressText, currentName);

    const reportLabel = document.createElement("div");
    reportLabel.textContent = "Report";
    reportLabel.style.fontSize = "12px";

    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.spellcheck = false;
    textarea.wrap = "soft";
    textarea.setAttribute("aria-label", "Checkpoint Thumbnail Exporter report");
    textarea.style.width = "100%";
    textarea.style.height = "100%";
    textarea.style.minHeight = `${REPORT_HEIGHT}px`;
    textarea.style.resize = "vertical";
    textarea.style.boxSizing = "border-box";
    textarea.style.padding = "7px";
    textarea.style.border = "1px solid #444";
    textarea.style.borderRadius = "2px";
    textarea.style.background = "#161616";
    textarea.style.color = "#E5E5E5";
    textarea.style.fontFamily = "monospace";
    textarea.style.fontSize = "11px";
    textarea.style.lineHeight = "1.3";
    textarea.style.whiteSpace = "pre-wrap";
    textarea.style.overflowWrap = "anywhere";
    textarea.style.overflow = "auto";
    textarea.style.cursor = "text";

    const stopPropagation = (event) => event.stopPropagation();
    for (const eventName of [
        "pointerdown",
        "pointerup",
        "mousedown",
        "mouseup",
        "click",
        "dblclick",
        "contextmenu",
        "wheel",
        "keyup",
    ]) {
        textarea.addEventListener(eventName, stopPropagation);
    }
    textarea.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
            event.preventDefault();
        }
        event.stopPropagation();
    });

    root.append(progressLabel, progressTrack, progressLine, reportLabel, textarea);
    const domWidget = node.addDOMWidget(
        "cte_status_report",
        "checkpoint-thumbnail-exporter-status",
        root,
        {
            serialize: false,
            hideOnZoom: false,
            getValue: () => textarea.value,
            setValue: () => {},
        },
    );
    node.cte_status_ui = {
        root,
        progressFill,
        progressText,
        currentName,
        textarea,
        domWidget,
    };
    syncStatusUI(node);
}

function updateButton(node) {
    const button = node.cte_button_widget;
    if (!button) return;
    button.name = buttonLabel(node);
    button.label = button.name;
    syncStatusUI(node);
    app.graph.setDirtyCanvas(true, true);
}

function resetRunMode(node, reason) {
    const runMode = getWidget(node, "run_mode");
    if (runMode && runMode.value !== "dry_run") {
        runMode.value = "dry_run";
    }
    node.cte_confirm_token = null;
    node.cte_confirm_key = null;
    if (reason) {
        node.cte_report = reason;
        node.cte_live_report = reason;
    }
    updateButton(node);
}

function ensureButtonAfterRunMode(node) {
    if (!node.cte_button_widget || !node.widgets) return;
    const button = node.cte_button_widget;
    const currentIndex = node.widgets.indexOf(button);
    const runModeIndex = node.widgets.findIndex((w) => w.name === "run_mode");
    if (currentIndex < 0 || runModeIndex < 0) return;
    node.widgets.splice(currentIndex, 1);
    const freshRunModeIndex = node.widgets.findIndex((w) => w.name === "run_mode");
    node.widgets.splice(freshRunModeIndex + 1, 0, button);
}

async function runExporter(node) {
    const operation = getWidgetValue(node, "operation", "install_missing");
    const runMode = getWidgetValue(node, "run_mode", "dry_run");
    const key = payloadKey(node);

    if (operation === "uninstall_managed" && runMode === "execute") {
        if (!node.cte_confirm_token || node.cte_confirm_key !== key) {
            node.cte_report = "Uninstall execute requires a fresh dry_run.\n\nRun dry_run for uninstall_managed first.\nNo files were removed.";
            node.cte_progress = { phase: "idle", current: 0, total: 0, status: "Dry run required.", current_name: "" };
            resetRunMode(node, node.cte_report);
            app.graph.setDirtyCanvas(true, true);
            return;
        }
    }

    const payload = {
        node_id: node.id,
        source_image_root: getWidgetValue(node, "source_image_root", ""),
        target_format: getWidgetValue(node, "target_format", "OGN-ModelManager"),
        operation,
        run_mode: runMode,
        max_size: Number(getWidgetValue(node, "max_size", 512)),
        jpeg_quality: Number(getWidgetValue(node, "jpeg_quality", 90)),
        confirm_token: operation === "uninstall_managed" && runMode === "execute" ? node.cte_confirm_token : null,
    };

    node.cte_is_running = true;
    node.cte_report = `Running ${operation} / ${runMode}...`;
    node.cte_live_report = node.cte_report;
    node.cte_progress = { phase: "running", current: 0, total: 0, status: "Starting...", current_name: "" };
    updateButton(node);
    app.graph.setDirtyCanvas(true, true);

    try {
        const response = await api.fetchApi("/checkpoint-thumbnail-exporter/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const result = await response.json();
        node.cte_report = result.report || "Done.";
        node.cte_live_report = node.cte_report;
        node.cte_result = result;

        if (operation === "uninstall_managed" && runMode === "dry_run" && result.confirm_token) {
            node.cte_confirm_token = result.confirm_token;
            node.cte_confirm_key = key;
        } else {
            node.cte_confirm_token = null;
            node.cte_confirm_key = null;
        }

        if (runMode === "execute") {
            const runModeWidget = getWidget(node, "run_mode");
            if (runModeWidget) runModeWidget.value = "dry_run";
            node.cte_report += "\n\nrun_mode has been reset to dry_run.";
        }
    } catch (error) {
        node.cte_report = `Request failed.\n\n${error}`;
        node.cte_live_report = node.cte_report;
        node.cte_confirm_token = null;
        node.cte_confirm_key = null;
    } finally {
        node.cte_is_running = false;
        node.cte_live_report = node.cte_report;
        updateButton(node);
        app.graph.setDirtyCanvas(true, true);
    }
}

api.addEventListener("checkpoint-thumbnail-exporter-progress", (event) => {
    const data = event.detail || event;
    if (!data) return;
    const graph = app.graph;
    if (!graph) return;
    for (const node of graph._nodes || []) {
        if (node.type !== NODE_NAME) continue;
        if (String(node.id) !== String(data.node_id)) continue;
        node.cte_progress = data;
        if (node.cte_is_running && data.report) {
            node.cte_live_report = String(data.report);
        }
        syncStatusUI(node);
        app.graph.setDirtyCanvas(true, true);
        break;
    }
});

app.registerExtension({
    name: "ana.CheckpointThumbnailExporter",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const originalOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = originalOnNodeCreated?.apply(this, arguments);

            this.cte_report = "Ready.\n\nButton behavior = operation + run_mode.\n\n🎨 install_missing\n  dry_run : find missing thumbnails\n  execute : install missing thumbnails\n\n❌ uninstall_managed\n  dry_run : find managed thumbnails\n  execute : uninstall managed thumbnails\n\ndry_run does not modify thumbnails or source images.\nThe internal source index may be updated.\nEmpty source_image_root uses ComfyUI output.";
            this.cte_progress = { phase: "idle", current: 0, total: 0, status: "Ready.", current_name: "" };
            this.cte_confirm_token = null;
            this.cte_confirm_key = null;
            this.cte_is_running = false;
            this.cte_live_report = this.cte_report;

            const operationWidget = getWidget(this, "operation");
            const runModeWidget = getWidget(this, "run_mode");

            if (operationWidget && !operationWidget.cte_wrapped) {
                const originalCallback = operationWidget.callback;
                operationWidget.callback = (value, canvas, node, pos, event) => {
                    const result = originalCallback?.call(operationWidget, value, canvas, node, pos, event);
                    const report = "Operation changed.\n\nrun_mode has been reset to dry_run.";
                    resetRunMode(this, report);
                    return result;
                };
                operationWidget.cte_wrapped = true;
            }

            if (runModeWidget && !runModeWidget.cte_wrapped) {
                const originalCallback = runModeWidget.callback;
                runModeWidget.callback = (value, canvas, node, pos, event) => {
                    const result = originalCallback?.call(runModeWidget, value, canvas, node, pos, event);
                    updateButton(this);
                    return result;
                };
                runModeWidget.cte_wrapped = true;
            }

            this.cte_button_widget = this.addWidget("button", buttonLabel(this), null, () => {
                if (this.cte_is_running) return;
                runExporter(this);
            });
            ensureButtonAfterRunMode(this);
            createStatusUI(this);

            this.size[0] = Math.max(this.size[0], MIN_WIDTH);
            this.size[1] = Math.max(this.size[1], 500);
            updateButton(this);
            return r;
        };

        const originalOnDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            originalOnDrawForeground?.apply(this, arguments);
            if (this.flags?.collapsed) return;
            if (this.cte_status_ui) return;

            const width = this.size[0];
            const margin = 12;
            const widgetCount = this.widgets?.length || 0;
            const widgetHeight = LiteGraph.NODE_WIDGET_HEIGHT || 20;
            const startY = Math.max(160, 28 + widgetCount * widgetHeight + 10);
            const barY = startY + 22;
            const barW = width - margin * 2;
            const barH = 12;
            const reportY = barY + 48;
            const reportH = Math.max(REPORT_HEIGHT, this.size[1] - reportY - 12);

            const progress = this.cte_progress || { current: 0, total: 0, status: "Ready.", current_name: "" };
            const total = Number(progress.total || 0);
            const current = Number(progress.current || 0);
            const ratio = total > 0 ? Math.max(0, Math.min(1, current / total)) : (this.cte_is_running ? 0.25 : 0);

            ctx.save();
            ctx.font = "12px sans-serif";
            ctx.fillStyle = "#DDD";
            ctx.fillText("Progress", margin, startY + 10);

            ctx.fillStyle = "#222";
            ctx.fillRect(margin, barY, barW, barH);
            ctx.fillStyle = this.cte_is_running ? "#7aa7ff" : "#6f9f6f";
            ctx.fillRect(margin, barY, barW * ratio, barH);
            ctx.strokeStyle = "#555";
            ctx.strokeRect(margin, barY, barW, barH);

            ctx.fillStyle = "#CCC";
            const progressText = total > 0 ? `${current} / ${total}` : String(progress.status || "Ready.");
            ctx.fillText(progressText, margin, barY + 28);

            if (progress.current_name && progress.phase !== "scanning_source") {
                const currentName = String(progress.current_name);
                const displayName = currentName.length > 54 ? currentName.slice(0, 51) + "..." : currentName;
                ctx.fillText(displayName, margin + 92, barY + 28);
            }

            ctx.fillStyle = "#DDD";
            ctx.fillText("Report", margin, reportY - 8);
            ctx.fillStyle = "#161616";
            ctx.fillRect(margin, reportY, barW, reportH);
            ctx.strokeStyle = "#444";
            ctx.strokeRect(margin, reportY, barW, reportH);

            ctx.fillStyle = "#E5E5E5";
            ctx.font = "11px monospace";
            const reportText = this.cte_is_running ? (this.cte_live_report || this.cte_report || "") : (this.cte_report || "");
            const lines = wrapText(ctx, reportText, barW - 14);
            const lineHeight = 14;
            const maxLines = Math.floor((reportH - 12) / lineHeight);
            for (let i = 0; i < Math.min(lines.length, maxLines); i++) {
                ctx.fillText(lines[i], margin + 7, reportY + 14 + i * lineHeight);
            }
            if (lines.length > maxLines) {
                ctx.fillStyle = "#AAA";
                ctx.fillText(`... ${lines.length - maxLines} more lines`, margin + 7, reportY + 14 + (maxLines - 1) * lineHeight);
            }
            ctx.restore();
        };
    },
});
