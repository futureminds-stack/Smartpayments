/* ═══════════════════════════════════════════════════════════════════
   Hex Network — shared rendering module for the referral constellation
   visualization. Pure rendering + interaction helpers; page-specific
   state (drill-down path, polling, search) lives in the pages that use
   this (network.html, the dashboard widget).
   ═══════════════════════════════════════════════════════════════════ */
const HexNetwork = (function () {
    "use strict";

    // Same palette already used elsewhere in the app (dashboard level
    // badges, admin status borders) - reused here, not reinvented, so the
    // network view reads as part of the same product rather than a
    // bolted-on widget.
    const STATUS_COLOR = { approved: "#10b981", pending: "#f59e0b", rejected: "#ef4444" };
    const STATUS_LABEL = { approved: "Approved", pending: "Pending", rejected: "Rejected" };
    const LEVEL_COLOR = {
        Starter: "#94a3b8", Bronze: "#f59e0b", Silver: "#cbd5e1",
        Gold: "#fbbf24", Diamond: "#22d3ee", Legend: "#f97316",
    };

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    function shortLabel(name) {
        if (!name) return "?";
        const first = name.trim().split(/\s+/)[0];
        return first.length > 9 ? first.slice(0, 8) + "…" : first;
    }

    function initials(name) {
        if (!name) return "?";
        const parts = name.trim().split(/\s+/);
        return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
    }

    function hexPoints(cx, cy, r) {
        const pts = [];
        for (let i = 0; i < 6; i++) {
            const a = (Math.PI / 180) * (60 * i);
            pts.push(`${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`);
        }
        return pts.join(" ");
    }

    function satellitePos(cx, cy, dist, i, n) {
        const a = (Math.PI / 180) * (-90 + (360 / Math.max(n, 1)) * i);
        return { x: cx + dist * Math.cos(a), y: cy + dist * Math.sin(a) };
    }

    /**
     * Build the SVG markup for one hex + its ring of satellites.
     * center / satellites are plain node objects: {id,name,status,level,referrals,earned,joined,hasChildren}
     */
    function constellationSVG(center, satellites, opts) {
        opts = Object.assign({
            w: 280, h: 240, hexR: 40, spokeLen: 84, satR: 15,
            interactive: true, showSatLabels: false,
        }, opts);
        const cx = opts.w / 2, cy = opts.h / 2;
        const lvlColor = LEVEL_COLOR[center.level] || LEVEL_COLOR.Starter;
        const n = satellites.length;

        let svg = `<svg viewBox="0 0 ${opts.w} ${opts.h}" class="hexnet-svg" preserveAspectRatio="xMidYMid meet">`;

        satellites.forEach((s, i) => {
            const p = satellitePos(cx, cy, opts.spokeLen, i, n);
            svg += `<line x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" class="hexnet-spoke"/>`;
        });

        svg += `<g class="hexnet-node hexnet-hex" data-id="${esc(center.id)}" ${opts.interactive ? 'tabindex="0" role="button"' : ""} aria-label="${esc(center.name)}">
            <polygon points="${hexPoints(cx, cy, opts.hexR)}" class="hexnet-hexagon" style="--lvl:${lvlColor}"/>
            <text x="${cx}" y="${cy}" class="hexnet-hex-label">${esc(shortLabel(center.name))}</text>
        </g>`;

        satellites.forEach((s, i) => {
            const p = satellitePos(cx, cy, opts.spokeLen, i, n);
            const color = STATUS_COLOR[s.status] || STATUS_COLOR.approved;
            svg += `<g class="hexnet-node hexnet-satellite" data-id="${esc(s.id)}" ${opts.interactive ? 'tabindex="0" role="button"' : ""} aria-label="${esc(s.name)}, ${STATUS_LABEL[s.status] || s.status}">`;
            if (s.hasChildren) {
                svg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${opts.satR + 5}" class="hexnet-dot-ring"/>`;
            }
            svg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${opts.satR}" fill="${color}" class="hexnet-dot"/>`;
            if (opts.showSatLabels) {
                const anchor = p.x > cx + 4 ? "start" : (p.x < cx - 4 ? "end" : "middle");
                const dx = p.x > cx + 4 ? opts.satR + 6 : (p.x < cx - 4 ? -(opts.satR + 6) : 0);
                const dy = Math.abs(p.x - cx) < 4 ? (p.y < cy ? -(opts.satR + 8) : opts.satR + 16) : 4;
                svg += `<text x="${(p.x + dx).toFixed(1)}" y="${(p.y + dy).toFixed(1)}" text-anchor="${anchor}" class="hexnet-sat-label">${esc(shortLabel(s.name))}</text>`;
            }
            svg += `</g>`;
        });

        svg += `</svg>`;
        return svg;
    }

    function emptySVG(center, opts) {
        opts = Object.assign({ w: 280, h: 240, hexR: 40 }, opts);
        const cx = opts.w / 2, cy = opts.h / 2;
        const lvlColor = LEVEL_COLOR[center.level] || LEVEL_COLOR.Starter;
        return `<svg viewBox="0 0 ${opts.w} ${opts.h}" class="hexnet-svg">
            <g class="hexnet-node hexnet-hex" data-id="${esc(center.id)}" tabindex="0" role="button" aria-label="${esc(center.name)}">
                <polygon points="${hexPoints(cx, cy, opts.hexR)}" class="hexnet-hexagon" style="--lvl:${lvlColor}"/>
                <text x="${cx}" y="${cy}" class="hexnet-hex-label">${esc(shortLabel(center.name))}</text>
            </g>
            <text x="${cx}" y="${cy + opts.hexR + 26}" class="hexnet-empty-label">No referrals yet</text>
        </svg>`;
    }

    // ── Tooltip: one floating element, reused for every constellation on the page ──
    let tipEl = null;
    function ensureTooltip() {
        if (tipEl) return tipEl;
        tipEl = document.createElement("div");
        tipEl.className = "hexnet-tooltip";
        document.body.appendChild(tipEl);
        return tipEl;
    }

    function tooltipHTML(node) {
        const statusColor = STATUS_COLOR[node.status] || STATUS_COLOR.approved;
        const statusLabel = STATUS_LABEL[node.status] || node.status;
        return `
            <div class="hexnet-tooltip-name">${esc(node.name)}</div>
            <div class="hexnet-tooltip-row"><span class="hexnet-tooltip-dot" style="background:${statusColor}"></span>${esc(statusLabel)} &middot; ${esc(node.level)}</div>
            <div class="hexnet-tooltip-row">${node.referrals} approved referral${node.referrals === 1 ? "" : "s"} &middot; ${Math.min(node.referrals, 6)}/6 sides &middot; \u20b9${Number(node.earned).toFixed(2)} earned</div>
            ${node.joined ? `<div class="hexnet-tooltip-row hexnet-tooltip-muted">Joined ${esc(node.joined)}</div>` : ""}
            ${node.hasChildren ? `<div class="hexnet-tooltip-row hexnet-tooltip-hint">Click to explore their network →</div>` : ""}
        `;
    }

    function positionTooltip(el, evt) {
        const pad = 14;
        let x = evt.clientX + pad, y = evt.clientY + pad;
        const rect = el.getBoundingClientRect();
        if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
        if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
        el.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
    }

    /**
     * Wire up hover/click/keyboard interaction for every .hexnet-node inside `root`.
     * `nodesById` maps id -> node data (center + satellites combined) so the
     * tooltip and click handler can look up full details from just the id
     * stored in the SVG's data-id attribute.
     */
    function attachInteractivity(root, nodesById, { onSelect } = {}) {
        const tip = ensureTooltip();
        root.querySelectorAll(".hexnet-node").forEach((el) => {
            const node = nodesById[el.getAttribute("data-id")];
            if (!node) return;

            el.addEventListener("mouseenter", (evt) => {
                tip.innerHTML = tooltipHTML(node);
                tip.classList.add("visible");
                positionTooltip(tip, evt);
            });
            el.addEventListener("mousemove", (evt) => positionTooltip(tip, evt));
            el.addEventListener("mouseleave", () => tip.classList.remove("visible"));

            const activate = () => {
                tip.classList.remove("visible");
                if (onSelect) onSelect(node);
            };
            el.addEventListener("click", activate);
            el.addEventListener("keydown", (evt) => {
                if (evt.key === "Enter" || evt.key === " ") { evt.preventDefault(); activate(); }
            });
        });
    }

    function nodeMap(center, satellites) {
        const map = {};
        if (center) map[center.id] = center;
        (satellites || []).forEach((s) => (map[s.id] = s));
        return map;
    }

    // ═════════════════════════════════════════════════════════════════
    // Referral hexagons
    //   • every admin-APPROVED referral fills one side of the hexagon
    //   • 6 approved referrals = one completed (solid) hexagon
    //   • a hexagon nobody has been referred into yet shows a padlock
    //   • referrals still waiting for admin show as amber dashed sides
    // ═════════════════════════════════════════════════════════════════
    const HEXP_R = 40, HEXP_C = 50;

    function hexpVertex(i) {
        const a = (Math.PI / 180) * (-90 + 60 * i);
        return [HEXP_C + HEXP_R * Math.cos(a), HEXP_C + HEXP_R * Math.sin(a)];
    }
    const HEXP_VERTS = [0, 1, 2, 3, 4, 5].map(hexpVertex);
    const HEXP_POINTS = HEXP_VERTS.map((v) => `${v[0].toFixed(2)},${v[1].toFixed(2)}`).join(" ");

    /** Shared gradients/clip, injected once per page (userSpaceOnUse so they also
        work on perfectly horizontal/vertical <line>s, which have a zero-size bbox). */
    function ensureHexDefs() {
        if (document.getElementById("hexpDefs")) return;
        const holder = document.createElement("div");
        holder.id = "hexpDefs";
        holder.setAttribute("aria-hidden", "true");
        holder.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;";
        holder.innerHTML = `<svg width="0" height="0" focusable="false"><defs>
            <linearGradient id="hexpGrad" gradientUnits="userSpaceOnUse" x1="10" y1="10" x2="90" y2="90">
                <stop offset="0%" stop-color="#25d366"/><stop offset="100%" stop-color="#00a884"/></linearGradient>
            <linearGradient id="hexpFill" gradientUnits="userSpaceOnUse" x1="10" y1="10" x2="90" y2="90">
                <stop offset="0%" stop-color="#0b8f6f"/><stop offset="100%" stop-color="#1fbf6a"/></linearGradient>
            <linearGradient id="hexpShine" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100" y2="0">
                <stop offset="0%" stop-color="#fff" stop-opacity="0"/><stop offset="50%" stop-color="#fff" stop-opacity=".55"/><stop offset="100%" stop-color="#fff" stop-opacity="0"/></linearGradient>
            <clipPath id="hexpClip"><polygon points="${HEXP_POINTS}"/></clipPath>
        </defs></svg>`;
        document.body.appendChild(holder);
    }

    function lockIconSVG() {
        // padlock, ~26px in the 100-unit hexagon box, centered
        return `<g class="hexp-lock-icon" transform="translate(36.5 35.5) scale(1.1)">
            <path class="hexp-lock" d="M8 11V8a4 4 0 0 1 8 0v3"/>
            <rect class="hexp-lock-body" x="5" y="11" width="14" height="10" rx="2.2"/>
            <circle cx="12" cy="16" r="1.5" fill="#111b21"/></g>`;
    }

    /**
     * One referral hexagon.
     *   filled  – approved referrals (0‥6): number of lit sides
     *   pending – referrals awaiting admin approval: amber dashed sides after the lit ones
     *   status  – optional ring colour ("approved" | "pending" | "rejected") for a person's hexagon
     *   active  – pulse ring around the hexagon currently being filled
     * Returns an <svg> string (viewBox 0 0 100 100).
     */
    function hexProgressSVG(o) {
        ensureHexDefs();
        o = Object.assign({ filled: 0, pending: 0, status: null, active: false, showCount: true, label: "" }, o);
        const filled = Math.max(0, Math.min(6, o.filled | 0));
        const pending = Math.max(0, Math.min(6 - filled, o.pending | 0));
        const done = filled >= 6;
        const locked = filled === 0 && pending === 0;
        const cls = ["hexp-svg", done ? "hexp-done" : "", locked ? "hexp-locked" : "", o.active ? "hexp-active" : ""].join(" ");
        const aria = done ? "Hexagon complete, 6 of 6 referrals approved"
            : locked ? "Locked hexagon, no referrals yet"
            : `${filled} of 6 sides filled` + (pending ? `, ${pending} awaiting approval` : "");

        let svg = `<svg viewBox="0 0 100 100" class="${cls}" role="img" aria-label="${esc(o.label ? o.label + ": " + aria : aria)}">`;
        svg += `<g class="hexp-glow">`;
        svg += `<polygon class="hexp-base" points="${HEXP_POINTS}" ${done ? 'fill="url(#hexpFill)"' : ""}/>`;
        if (done) {
            svg += `<g clip-path="url(#hexpClip)"><polygon class="hexp-shine" fill="url(#hexpShine)" points="${HEXP_POINTS}"/></g>`;
        }
        for (let i = 0; i < 6; i++) {
            const a = HEXP_VERTS[i], b = HEXP_VERTS[(i + 1) % 6];
            const coords = `x1="${a[0].toFixed(2)}" y1="${a[1].toFixed(2)}" x2="${b[0].toFixed(2)}" y2="${b[1].toFixed(2)}"`;
            svg += `<line class="hexp-track" ${coords}/>`;
            if (i < filled) {
                svg += `<line class="hexp-edge-on" stroke="url(#hexpGrad)" style="--i:${i}" ${coords}/>`;
            } else if (i < filled + pending) {
                svg += `<line class="hexp-edge-pend" ${coords}/>`;
            }
        }
        svg += `</g>`;
        if (o.status) {
            const col = STATUS_COLOR[o.status] || STATUS_COLOR.approved;
            svg += `<polygon class="hexnet-status-ring" points="${HEXP_VERTS.map((v) => {
                const dx = v[0] - HEXP_C, dy = v[1] - HEXP_C, k = 1.17;
                return `${(HEXP_C + dx * k).toFixed(1)},${(HEXP_C + dy * k).toFixed(1)}`;
            }).join(" ")}" stroke="${col}" opacity=".85"/>`;
        }
        if (o.active) svg += `<polygon class="hexp-ring" points="${HEXP_POINTS}"/>`;

        if (done) {
            svg += `<path class="hexp-check" d="M34 52l11 11 21-23"/>`;
        } else if (locked) {
            svg += lockIconSVG();
        } else if (o.showCount) {
            if (filled === 0) {
                // only pending referrals so far: hourglass, still no filled side
                svg += `<g transform="translate(38 38) scale(1)"><path d="M2 0h20M2 24h20M4 0c0 8 16 9 16 12S4 16 4 24M20 0c0 8-16 9-16 12s16 4 16 12" fill="none" stroke="#f5a623" stroke-width="2.2" stroke-linecap="round" transform="translate(0 0)"/></g>`;
            } else {
                svg += `<text x="50" y="48" class="hexp-count">${filled}/6</text><text x="50" y="64" class="hexp-count-sub">SIDES</text>`;
            }
        }
        svg += `</svg>`;
        return svg;
    }

    /**
     * The user's own progression as a row of hexagons: finished ones (solid),
     * the one currently being filled (large, pulsing), then two locked ones.
     * `approved` / `pending` are the counts of direct referrals by status.
     */
    function hexChainHTML(approved, pending) {
        approved = Math.max(0, approved | 0); pending = Math.max(0, pending | 0);
        const completed = Math.floor(approved / 6);
        const curFilled = approved % 6;
        const curPending = Math.min(pending, 6 - curFilled);
        const startDone = Math.max(0, completed - 2);

        const cell = (k, kind) => {
            let svg, cap, sub, cls = "hexp";
            if (kind === "done") {
                svg = hexProgressSVG({ filled: 6, label: `Hexagon ${k + 1}` });
                cap = "Completed"; sub = "6 / 6";
            } else if (kind === "current") {
                svg = hexProgressSVG({ filled: curFilled, pending: curPending, active: true, label: `Hexagon ${k + 1}` });
                cls += " hexp-current";
                cap = curFilled === 0 && curPending === 0 ? "Refer someone to unlock" : "In progress";
                sub = `${curFilled} / 6${curPending ? ` · ${curPending} pending` : ""}`;
            } else {
                svg = hexProgressSVG({ filled: 0, label: `Hexagon ${k + 1}` });
                cap = "Locked"; sub = "Fill the previous one";
            }
            return `<div class="${cls}">${svg}<div class="hexp-caption"><strong>Hexagon ${k + 1}</strong>${cap}<br>${sub}</div></div>`;
        };

        let html = `<div class="hexchain">`;
        if (startDone > 0) html += `<div class="hexchain-more">+${startDone} more<br>completed</div>`;
        for (let k = startDone; k < completed; k++) html += cell(k, "done");
        html += cell(completed, "current");
        html += cell(completed + 1, "locked");
        html += cell(completed + 2, "locked");
        html += `</div>`;
        return html;
    }

    const SLOTS_PER_LEVEL = 5;
    const LEVEL_COUNT = 5;

    /**
     * Build the 5-levels x 5-hexagons grid. `levels` is an array of
     * { depth, total, people } from /api/my-network-levels, where `people`
     * is the real, individual referred users at that depth (never an
     * aggregate/graph node - one hexagon is always exactly one person).
     * Unfilled slots render as dull placeholder hexagons; filled slots are
     * highlighted and colored by that person's referral status.
     *
     * Progression: the first level that isn't completely filled (5/5) is
     * the "active" level and renders larger/brighter to draw the eye to
     * where to focus next. Levels before it that are already full render
     * as "completed" (normal size, checkmarked). Levels after it are
     * "locked" (smaller/dimmer) until the level ahead of them fills up.
     */
    function levelGridHTML(levels) {
        let activeIdx = LEVEL_COUNT; // default: every level full -> none "active"
        for (let i = 0; i < LEVEL_COUNT; i++) {
            if (((levels[i] || {}).total || 0) < SLOTS_PER_LEVEL) { activeIdx = i; break; }
        }

        let html = `<div class="hexnet-levels-grid">`;
        for (let i = 0; i < LEVEL_COUNT; i++) {
            const lvl = levels[i] || { depth: i + 1, total: 0, people: [] };
            const people = lvl.people || [];
            const state = i < activeIdx ? "completed" : i === activeIdx ? "active" : "locked";
            const stateBadge = state === "completed" ? '<span class="hexnet-level-badge hexnet-level-badge-done"><i class="bi bi-check-circle-fill"></i> Full</span>'
                : state === "active" ? '<span class="hexnet-level-badge hexnet-level-badge-active">Active</span>'
                : '<span class="hexnet-level-badge hexnet-level-badge-locked"><i class="bi bi-lock-fill"></i> Next up</span>';

            const remaining = Math.max(0, SLOTS_PER_LEVEL - lvl.total);

            html += `<div class="hexnet-level-row hexnet-level-${state}" data-depth="${lvl.depth}">
                <div class="hexnet-level-label">
                    <span class="hexnet-level-num">Level ${lvl.depth}</span>
                    <span class="hexnet-level-count">${lvl.total} referred</span>
                    ${lvl.pending ? `<span class="hexnet-level-pending"><i class="bi bi-hourglass-split"></i> ${lvl.pending} pending</span>` : ""}
                    ${remaining > 0 ? `<span class="hexnet-level-remaining">${remaining} more to fill this level</span>` : ""}
                    ${stateBadge}
                </div>
                <div class="hexnet-level-slots">`;
            for (let s = 0; s < SLOTS_PER_LEVEL; s++) {
                const person = people[s];
                if (person) {
                    // one hexagon = one real person; its sides = THAT person's approved referrals
                    html += `<div class="hexnet-node hexnet-slot hexnet-slot-filled" data-id="${esc(person.id)}"
                        tabindex="0" role="button"
                        aria-label="${esc(person.name)}, ${STATUS_LABEL[person.status] || person.status}, ${Math.min(person.referrals || 0, 6)} of 6 sides filled">
                        ${hexProgressSVG({ filled: Math.min(person.referrals || 0, 6), status: person.status, label: person.name })}
                    </div>`;
                } else {
                    html += `<div class="hexnet-slot hexnet-slot-empty hexnet-slot-add" tabindex="0" role="button"
                        aria-label="Locked slot - click to copy your referral link">
                        ${hexProgressSVG({ filled: 0, label: "Empty slot" })}
                    </div>`;
                }
            }
            if (lvl.total > SLOTS_PER_LEVEL) {
                html += `<div class="hexnet-level-more">+${lvl.total - SLOTS_PER_LEVEL} more</div>`;
            }
            html += `</div></div>`;
        }
        html += `</div>`;
        return html;
    }

    let _refPopup = null;
    function closeRefPopup() {
        if (_refPopup) { _refPopup.remove(); _refPopup = null; }
        document.removeEventListener("click", onDocClickCloseRefPopup, true);
    }
    function onDocClickCloseRefPopup(evt) {
        if (_refPopup && !_refPopup.contains(evt.target)) closeRefPopup();
    }

    /**
     * Wire every empty "+" hexagon in a levels grid so clicking it opens a
     * small popup beside that hexagon with the user's referral link,
     * copies it to the clipboard immediately, and offers a manual copy
     * button too. `copyFn` should be the app's existing copyToClipboard(text).
     */
    function attachLevelGridAddSlots(root, refLink, copyFn) {
        root.querySelectorAll(".hexnet-slot-add").forEach((el) => {
            const open = (evt) => {
                evt.stopPropagation();
                closeRefPopup();

                const rect = el.getBoundingClientRect();
                const popup = document.createElement("div");
                popup.className = "hexnet-ref-popup";
                popup.innerHTML = `
                    <div class="hexnet-ref-popup-title"><i class="bi bi-link-45deg"></i> Your referral link</div>
                    <div class="hexnet-ref-popup-copied"><i class="bi bi-check-circle-fill"></i> Copied to clipboard</div>
                    <div class="hexnet-ref-popup-row">
                        <input type="text" readonly value="${esc(refLink)}">
                        <button type="button" class="hexnet-ref-popup-copy"><i class="bi bi-clipboard"></i></button>
                    </div>
                    <div class="hexnet-ref-popup-hint">Share it - new sign-ups fill the next open slot automatically.</div>
                `;
                document.body.appendChild(popup);

                // Position aside the hexagon: prefer to the right, flip to
                // the left if it would run off the viewport edge.
                const pw = popup.offsetWidth, ph = popup.offsetHeight;
                let left = rect.right + 10;
                if (left + pw > window.innerWidth - 8) left = rect.left - pw - 10;
                if (left < 8) left = Math.max(8, Math.min(window.innerWidth - pw - 8, rect.left));
                let top = rect.top + rect.height / 2 - ph / 2;
                top = Math.max(8, Math.min(window.innerHeight - ph - 8, top));
                popup.style.left = `${left + window.scrollX}px`;
                popup.style.top = `${top + window.scrollY}px`;

                popup.querySelector(".hexnet-ref-popup-copy").addEventListener("click", () => copyFn(refLink));
                _refPopup = popup;
                if (typeof copyFn === "function") copyFn(refLink);
                requestAnimationFrame(() => document.addEventListener("click", onDocClickCloseRefPopup, true));
            };
            el.addEventListener("click", open);
            el.addEventListener("keydown", (evt) => {
                if (evt.key === "Enter" || evt.key === " ") { evt.preventDefault(); open(evt); }
            });
        });
    }

    /** Flat id -> node map built from every person across all 5 levels, for tooltip/click lookups. */
    function levelNodeMap(levels) {
        const map = {};
        (levels || []).forEach((lvl) => (lvl.people || []).forEach((p) => (map[p.id] = p)));
        return map;
    }

    /** Midpoint of each of the 6 edges of a regular hexagon centered at (cx,cy) with circumradius r. */
    function hexEdgeMidpoints(cx, cy, r) {
        const verts = [];
        for (let i = 0; i < 6; i++) {
            const a = (Math.PI / 180) * (60 * i);
            verts.push({ x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
        }
        return verts.map((v, i) => {
            const n = verts[(i + 1) % 6];
            return { x: (v.x + n.x) / 2, y: (v.y + n.y) / 2 };
        });
    }

    /**
     * Draws the user's own hexagon with one status-colored marker sitting
     * directly on each of its 6 sides - each marker is one real direct
     * referral (never an aggregate), matching how the rest of this module
     * treats every node as an actual person. Only the first 6 satellites
     * get a side; any beyond that are summarized by the caller (there's
     * no 7th side to put them on).
     */
    function hexSidesSVG(center, satellites, opts) {
        opts = Object.assign({ w: 300, h: 260, hexR: 70, markerR: 15, interactive: true }, opts);
        const cx = opts.w / 2, cy = opts.h / 2;
        const lvlColor = LEVEL_COLOR[center.level] || LEVEL_COLOR.Starter;
        const mids = hexEdgeMidpoints(cx, cy, opts.hexR);
        const shown = (satellites || []).slice(0, 6);
        const filled = shown.filter(Boolean).length;
        const complete = filled >= 6;

        let svg = `<svg viewBox="0 0 ${opts.w} ${opts.h}" class="hexnet-svg hexnet-sides-svg" preserveAspectRatio="xMidYMid meet">`;
        svg += `<g class="hexnet-node hexnet-hex ${complete ? "hexnet-hex-complete" : ""}" data-id="${esc(center.id)}" ${opts.interactive ? 'tabindex="0" role="button"' : ""} aria-label="${esc(center.name)}, ${filled} of 6 referral slots filled">
            <polygon points="${hexPoints(cx, cy, opts.hexR)}" class="hexnet-hexagon" style="--lvl:${lvlColor}"/>
            <text x="${cx}" y="${cy - 10}" class="hexnet-hex-label">${esc(shortLabel(center.name))}</text>
            ${complete
                ? `<text x="${cx}" y="${cy + 16}" class="hexnet-hex-count hexnet-hex-count-done">&#10003;</text>`
                : `<text x="${cx}" y="${cy + 16}" class="hexnet-hex-count">${filled}/6</text>`}
        </g>`;

        for (let i = 0; i < 6; i++) {
            const p = mids[i];
            const s = shown[i];
            if (s) {
                const color = STATUS_COLOR[s.status] || STATUS_COLOR.approved;
                svg += `<g class="hexnet-node hexnet-side-marker" data-id="${esc(s.id)}" ${opts.interactive ? 'tabindex="0" role="button"' : ""}
                             aria-label="${esc(s.name)}, ${STATUS_LABEL[s.status] || s.status}">
                    <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${opts.markerR}" fill="${color}" class="hexnet-side-dot"/>
                </g>`;
            } else {
                svg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${opts.markerR}" class="hexnet-side-dot hexnet-side-dot-empty"/>`;
            }
        }
        svg += `</svg>`;
        return svg;
    }

    function legendHTML() {
        const statuses = Object.keys(STATUS_COLOR).map((k) =>
            `<span class="hexnet-legend-item"><span class="hexnet-legend-dot" style="background:${STATUS_COLOR[k]}"></span>${STATUS_LABEL[k]}</span>`
        ).join("");
        const levels = Object.keys(LEVEL_COLOR).map((k) =>
            `<span class="hexnet-legend-item"><span class="hexnet-legend-hex" style="--lvl:${LEVEL_COLOR[k]}"></span>${k}</span>`
        ).join("");
        return `<div class="hexnet-legend-group"><span class="hexnet-legend-title">Status</span>${statuses}</div>
                <div class="hexnet-legend-group"><span class="hexnet-legend-title">Level</span>${levels}</div>`;
    }

    function statusLegendHTML() {
        const statuses = Object.keys(STATUS_COLOR).map((k) =>
            `<span class="hexnet-legend-item"><span class="hexnet-legend-dot" style="background:${STATUS_COLOR[k]}"></span>${STATUS_LABEL[k]}</span>`
        ).join("");
        return `<div class="hexnet-legend-group">${statuses}</div>`;
    }

    /**
     * Render a single tree node's chip (name + status dot), used inside the
     * org-chart. Not a hexagon here — a variable-branching, variable-depth
     * tree with true hexagons and hand-drawn connector lines would need a
     * full layout engine; a chip + CSS org-chart connector is the same
     * "parent → child, colored by status" information at any depth/width
     * without that complexity.
     */
    function treeChipHTML(node) {
        const color = STATUS_COLOR[node.status] || STATUS_COLOR.approved;
        return `<div class="hexnet-node hexnet-tree-node" data-id="${esc(node.id)}" style="--slot-color:${color}"
                     tabindex="0" role="button" aria-label="${esc(node.name)}, ${STATUS_LABEL[node.status] || node.status}">
            <span class="hexnet-tree-dot"></span>
            <span class="hexnet-tree-name">${esc(shortLabel(node.name))}</span>
            ${node.referrals ? `<span class="hexnet-tree-count">${node.referrals}</span>` : ""}
        </div>`;
    }

    /** Recursively render one tree ({node, children}) as nested <ul><li> for the CSS org-chart. */
    function treeNodeHTML(tn) {
        const kids = tn.children || [];
        return `<li>
            ${treeChipHTML(tn.node)}
            ${kids.length ? `<ul>${kids.map(treeNodeHTML).join("")}</ul>` : ""}
        </li>`;
    }

    /** `forest` is an array of {node, children} root trees, e.g. from /admin/api/network overview. */
    function orgChartForestHTML(forest) {
        if (!forest.length) return "";
        return forest.map((tn) => `<div class="hexnet-orgchart"><ul>${treeNodeHTML(tn)}</ul></div>`).join("");
    }

    /** Flat id -> node map across an entire forest, for tooltip/click lookups. */
    function forestNodeMap(forest) {
        const map = {};
        const walk = (tn) => { map[tn.node.id] = tn.node; (tn.children || []).forEach(walk); };
        forest.forEach(walk);
        return map;
    }

    /**
     * Compact side panel for users nobody referred and who haven't referred
     * anyone themselves - kept out of the main tree canvas so it isn't
     * dozens of single, childless hexagons cluttering the view.
     */
    function soloPanelHTML(solo) {
        if (!solo.length) {
            return `<div class="hexnet-solo-empty">Everyone here is part of a referral chain.</div>`;
        }
        return solo.map((n) => {
            const color = STATUS_COLOR[n.status] || STATUS_COLOR.approved;
            return `<div class="hexnet-node hexnet-solo-dot" data-id="${esc(n.id)}" style="--slot-color:${color}"
                         tabindex="0" role="button" aria-label="${esc(n.name)}, ${STATUS_LABEL[n.status] || n.status}, no referrer">
                <span class="hexnet-tree-dot"></span>${esc(shortLabel(n.name))}
            </div>`;
        }).join("");
    }

    function soloNodeMap(solo) {
        const map = {};
        (solo || []).forEach((n) => (map[n.id] = n));
        return map;
    }

    return {
        STATUS_COLOR, STATUS_LABEL, LEVEL_COLOR,
        esc, initials, constellationSVG, emptySVG, attachInteractivity, nodeMap, legendHTML, statusLegendHTML,
        levelGridHTML, levelNodeMap, attachLevelGridAddSlots,
        orgChartForestHTML, forestNodeMap, soloPanelHTML, soloNodeMap,
        hexSidesSVG, hexEdgeMidpoints, hexProgressSVG, hexChainHTML,
    };
})();
