/* SpaceTraders Web UI — minimal JS for cooldown timers and toast auto-dismiss */

// Toast auto-dismiss: watch for new toasts and remove after 4 seconds
const toastObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
            if (node.nodeType === 1 && node.classList.contains("toast")) {
                setTimeout(() => {
                    node.classList.add("toast-fade-out");
                    setTimeout(() => node.remove(), 300);
                }, 4000);
            }
        }
    }
});

const toastContainer = document.getElementById("action-toast");
if (toastContainer) {
    toastObserver.observe(toastContainer, { childList: true });
}

// Cooldown timer: counts down seconds on elements with data-cooldown-seconds
function initCooldownTimers() {
    document.querySelectorAll("[data-cooldown-seconds]").forEach((el) => {
        const total = parseInt(el.dataset.cooldownTotal || el.dataset.cooldownSeconds);
        let remaining = parseInt(el.dataset.cooldownSeconds);
        if (remaining <= 0) return;

        const textEl = el.querySelector(".cooldown-text");
        const fillEl = el.querySelector(".cooldown-fill");

        const tick = () => {
            remaining--;
            if (textEl) textEl.textContent = `${remaining}s`;
            if (fillEl) fillEl.style.width = `${(remaining / total) * 100}%`;

            if (remaining <= 0) {
                el.classList.add("toast-fade-out");
                setTimeout(() => el.remove(), 300);
                // Refresh ship status when cooldown expires
                const shipSymbol = el.dataset.ship;
                if (shipSymbol) {
                    htmx.ajax("GET", `/fleet/${shipSymbol}/status`, {
                        target: "#ship-status-panel",
                        swap: "innerHTML",
                    });
                }
            } else {
                setTimeout(tick, 1000);
            }
        };

        setTimeout(tick, 1000);
    });
}

// Re-initialize cooldown timers after htmx swaps
document.addEventListener("htmx:afterSwap", () => {
    initCooldownTimers();
});

// System map zoom & pan (viewBox manipulation)
function initMapZoom() {
    const svg = document.getElementById("map-svg");
    if (!svg) return;

    const initial = svg.dataset.initialViewbox.split(" ").map(Number);
    let vb = { x: initial[0], y: initial[1], w: initial[2], h: initial[3] };
    const ZOOM_FACTOR = 0.25;
    const MIN_ZOOM = 0.015; // ~67x zoom in
    const MAX_ZOOM = 3.0; // 3x zoom out

    const scalableEls = svg.querySelectorAll(".wp-group, .ship-group");

    // Track cursor position in SVG coords for button zoom
    let cursorSvg = null;
    svg.addEventListener("mousemove", (e) => {
        const rect = svg.getBoundingClientRect();
        const sx = (e.clientX - rect.left) / rect.width;
        const sy = (e.clientY - rect.top) / rect.height;
        cursorSvg = { x: vb.x + sx * vb.w, y: vb.y + sy * vb.h };
    });
    svg.addEventListener("mouseleave", () => { cursorSvg = null; });

    function applyViewBox() {
        svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
        // Keep shapes/labels at constant visual size by inverse-scaling
        const ratio = vb.w / initial[2];
        scalableEls.forEach((el) => {
            const cx = parseFloat(el.dataset.cx);
            const cy = parseFloat(el.dataset.cy);
            el.setAttribute(
                "transform",
                `translate(${cx}, ${cy}) scale(${ratio}) translate(${-cx}, ${-cy})`
            );
        });
    }

    function zoomAt(cx, cy, factor) {
        const newW = Math.max(initial[2] * MIN_ZOOM, Math.min(initial[2] * MAX_ZOOM, vb.w * factor));
        const newH = Math.max(initial[3] * MIN_ZOOM, Math.min(initial[3] * MAX_ZOOM, vb.h * factor));
        // Keep the point (cx, cy) at the same relative position
        vb.x = cx - (cx - vb.x) * (newW / vb.w);
        vb.y = cy - (cy - vb.y) * (newH / vb.h);
        vb.w = newW;
        vb.h = newH;
        applyViewBox();
    }

    function zoomAtCursor(factor) {
        // Zoom at cursor if over the SVG, otherwise fall back to viewport center
        const cx = cursorSvg ? cursorSvg.x : vb.x + vb.w / 2;
        const cy = cursorSvg ? cursorSvg.y : vb.y + vb.h / 2;
        zoomAt(cx, cy, factor);
    }

    // Buttons
    document.getElementById("map-zoom-in")?.addEventListener("click", () => zoomAtCursor(1 - ZOOM_FACTOR));
    document.getElementById("map-zoom-out")?.addEventListener("click", () => zoomAtCursor(1 + ZOOM_FACTOR));
    document.getElementById("map-zoom-reset")?.addEventListener("click", () => {
        vb = { x: initial[0], y: initial[1], w: initial[2], h: initial[3] };
        applyViewBox();
    });

    // Scroll wheel zoom (centered on cursor)
    svg.addEventListener("wheel", (e) => {
        e.preventDefault();
        const rect = svg.getBoundingClientRect();
        // Convert screen coords to SVG coords
        const sx = (e.clientX - rect.left) / rect.width;
        const sy = (e.clientY - rect.top) / rect.height;
        const cx = vb.x + sx * vb.w;
        const cy = vb.y + sy * vb.h;
        const factor = e.deltaY > 0 ? (1 + ZOOM_FACTOR) : (1 - ZOOM_FACTOR);
        zoomAt(cx, cy, factor);
    }, { passive: false });

    // Drag to pan
    let dragging = false;
    let dragStart = { x: 0, y: 0 };
    let vbStart = { x: 0, y: 0 };

    svg.addEventListener("pointerdown", (e) => {
        // Only pan on primary button, not on links
        if (e.button !== 0 || e.target.closest("a")) return;
        dragging = true;
        svg.classList.add("panning");
        svg.setPointerCapture(e.pointerId);
        dragStart = { x: e.clientX, y: e.clientY };
        vbStart = { x: vb.x, y: vb.y };
    });

    svg.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const rect = svg.getBoundingClientRect();
        const dx = (e.clientX - dragStart.x) / rect.width * vb.w;
        const dy = (e.clientY - dragStart.y) / rect.height * vb.h;
        vb.x = vbStart.x - dx;
        vb.y = vbStart.y - dy;
        applyViewBox();
    });

    svg.addEventListener("pointerup", () => {
        dragging = false;
        svg.classList.remove("panning");
    });

    // Double-click to center
    svg.addEventListener("dblclick", (e) => {
        const rect = svg.getBoundingClientRect();
        const sx = (e.clientX - rect.left) / rect.width;
        const sy = (e.clientY - rect.top) / rect.height;
        const cx = vb.x + sx * vb.w;
        const cy = vb.y + sy * vb.h;
        vb.x = cx - vb.w / 2;
        vb.y = cy - vb.h / 2;
        applyViewBox();
    });
}

// System map detail sidebar (hover)
function initMapSidebar() {
    const svg = document.getElementById("map-svg");
    const sidebar = document.getElementById("map-sidebar");
    if (!svg || !sidebar) return;

    const emptyEl = document.getElementById("map-sidebar-empty");
    const contentEl = document.getElementById("map-sidebar-content");
    const titleEl = document.getElementById("sidebar-title");
    const detailsEl = document.getElementById("sidebar-details");
    const relEl = document.getElementById("sidebar-relationship");

    function addRow(dt, dd) {
        const dtEl = document.createElement("dt");
        dtEl.textContent = dt;
        const ddEl = document.createElement("dd");
        ddEl.innerHTML = dd;
        detailsEl.appendChild(dtEl);
        detailsEl.appendChild(ddEl);
    }

    function addRelTag(text, cls) {
        const tag = document.createElement("div");
        tag.className = `sidebar-rel-tag ${cls}`;
        tag.textContent = text;
        relEl.appendChild(tag);
    }

    function showWaypoint(el) {
        const d = el.dataset;
        titleEl.textContent = d.symbol;
        detailsEl.innerHTML = "";
        relEl.innerHTML = "";

        addRow("Type", d.wpType);
        addRow("Coords", d.coords);
        if (d.orbits) addRow("Orbits", d.orbits);
        if (d.traits) addRow("Traits", d.traits);
        if (d.market === "true") addRow("Market", "Yes");
        if (d.shipyard === "true") addRow("Shipyard", "Yes");

        // Relationships
        if (d.isHq === "true") {
            addRelTag("\u2302 Headquarters", "sidebar-rel-hq");
        }
        if (d.isDelivery === "true" && d.deliveries) {
            d.deliveries.split("; ").forEach((info) => {
                if (info) addRelTag(`\u25CF Deliver: ${info}`, "sidebar-rel-delivery");
            });
        }
        if (d.shipsHere) {
            d.shipsHere.split(", ").forEach((s) => {
                if (s) addRelTag(`\u25B2 ${s} here`, "sidebar-rel-ship");
            });
        }
        if (d.inbound) {
            d.inbound.split(", ").forEach((s) => {
                if (s) addRelTag(`\u2192 ${s} en route`, "sidebar-rel-dest");
            });
        }

        emptyEl.style.display = "none";
        contentEl.style.display = "block";
    }

    function showShip(el) {
        const d = el.dataset;
        titleEl.textContent = d.symbol;
        detailsEl.innerHTML = "";
        relEl.innerHTML = "";

        addRow("Role", d.role);
        addRow("Status", d.status);
        addRow("At", d.waypoint);
        if (d.destination) addRow("Destination", d.destination);

        addRelTag("Your ship", "sidebar-rel-mine");
        if (d.destination) {
            addRelTag(`\u2192 ${d.destination}`, "sidebar-rel-dest");
        }

        emptyEl.style.display = "none";
        contentEl.style.display = "block";
    }

    function clearSidebar() {
        emptyEl.style.display = "";
        contentEl.style.display = "none";
    }

    svg.querySelectorAll(".wp-group").forEach((el) => {
        el.addEventListener("mouseenter", () => showWaypoint(el));
    });

    svg.querySelectorAll(".ship-group").forEach((el) => {
        el.addEventListener("mouseenter", () => showShip(el));
    });

    svg.addEventListener("mouseleave", clearSidebar);
}

// System map theme switcher
function initMapTheme() {
    const mapEl = document.getElementById("system-map");
    const selectEl = document.getElementById("map-theme-select");
    if (!mapEl || !selectEl) return;

    // Restore saved theme
    const saved = localStorage.getItem("map-theme") || "";
    if (saved) {
        mapEl.classList.add(saved);
        selectEl.value = saved;
    }

    selectEl.addEventListener("change", () => {
        // Remove all theme-* classes, then add the selected one
        mapEl.className = mapEl.className.replace(/\btheme-\S+/g, "").trim();
        if (selectEl.value) {
            mapEl.classList.add(selectEl.value);
        }
        localStorage.setItem("map-theme", selectEl.value);
    });
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
    initCooldownTimers();
    initMapTheme();
    initMapZoom();
    initMapSidebar();
});
