/**
 * Plan-graph viewport renderer.
 *
 * Self-contained: ships a depth-based hierarchical layout (no D3), and
 * re-renders on every plan event. Click a node to open the side detail
 * panel; the toolbar drives pause / history / branch / checkpoint flows.
 *
 * Public API (attached to window):
 *   PlanGraphView.render(graphSnapshot)  - draw a fresh snapshot
 *   PlanGraphView.applyEvent(event)      - patch the cached snapshot
 *   PlanGraphView.showCheckpoint(payload) - soft-checkpoint toast w/ countdown
 *   PlanGraphView.hideCheckpoint()
 *   PlanGraphView.onSelect = fn(nodeId)  - hook for the host app
 */

(function (window) {
    'use strict';

    const NODE_W = 200;
    const NODE_H = 64;
    const COL_GAP = 56;
    const ROW_GAP = 18;
    const PADDING = 24;

    // status -> css class
    const STATUS_CLASS = {
        pending: 'pgn-pending',
        running: 'pgn-running',
        done: 'pgn-done',
        blocked: 'pgn-blocked',
        abandoned: 'pgn-abandoned',
    };

    const SPECIALIZATION_ICON = {
        explore: '\u{1F50D}',     // magnifier
        implement: '\u{1F527}',   // wrench
        verify: '\u2713',         // check
        repair: '\u{1F527}',      // wrench (slightly different label)
        research: '\u{1F4DA}',    // books
        plan: '\u{1F5FA}',        // map
    };

    let _snapshot = null;     // last full graph snapshot {intent, intent_id, nodes:[]}
    let _selectedId = null;
    let _checkpointTimer = null;

    // ── Public API ─────────────────────────────────────────────────

    function render(snapshot) {
        _snapshot = _normalizeSnapshot(snapshot);
        _draw();
    }

    function applyEvent(event) {
        if (!event || !_snapshot) return;
        const kind = event.kind || event.event;
        if (kind === 'plan.snapshot' && event.snapshot) {
            render(event.snapshot);
            return;
        }
        const nid = event.node_id;
        const payload = event.payload || {};
        const node = nid ? _snapshot.nodes[nid] : null;

        switch (kind) {
            case 'node.start':
                if (node) {
                    node.status = 'running';
                    if (payload.goal) node.goal = payload.goal;
                    if (payload.specialization) node.specialization = payload.specialization;
                }
                break;
            case 'node.done':
                if (node) {
                    node.status = payload.status || 'done';
                    if (typeof payload.confidence === 'number') node.confidence = payload.confidence;
                    if (payload.summary) node.last_summary = payload.summary;
                }
                break;
            case 'node.confidence':
                if (node && typeof payload.value === 'number') node.confidence = payload.value;
                break;
            case 'plan.rewrite':
                if (Array.isArray(payload.added)) {
                    for (const newId of payload.added) {
                        if (!_snapshot.nodes[newId]) {
                            _snapshot.nodes[newId] = {
                                id: newId,
                                goal: payload.goal || newId,
                                specialization: payload.specialization || 'implement',
                                status: 'pending',
                                confidence: 1.0,
                                parent_id: nid,
                                depends_on: payload.depends_on || [],
                            };
                        }
                    }
                }
                if (Array.isArray(payload.removed)) {
                    for (const rid of payload.removed) delete _snapshot.nodes[rid];
                }
                break;
            case 'plan.complete':
                // Just re-render with current data
                break;
            default:
                return;  // unknown event — ignore
        }
        _draw();
    }

    function showCheckpoint(payload) {
        const el = document.getElementById('plan-graph-checkpoint');
        if (!el) return;
        const countdown = Math.max(0, parseInt(payload?.countdown_seconds, 10) || 5);
        const message = String(payload?.message || 'Plan rewriting...');
        const onPause = typeof payload?.onPause === 'function' ? payload.onPause : null;
        const onShowDiff = typeof payload?.onShowDiff === 'function' ? payload.onShowDiff : null;

        el.innerHTML = `
            <span class="pgcp-msg">${_escape(message)}</span>
            <span class="pgcp-count" id="pgcp-count">${countdown}s</span>
            <button class="pgcp-btn" id="pgcp-pause">Pause</button>
            <button class="pgcp-btn" id="pgcp-diff">Show diff</button>
        `;
        el.style.display = 'flex';

        if (_checkpointTimer) clearInterval(_checkpointTimer);
        let remaining = countdown;
        _checkpointTimer = setInterval(() => {
            remaining -= 1;
            const c = document.getElementById('pgcp-count');
            if (c) c.textContent = `${Math.max(0, remaining)}s`;
            if (remaining <= 0) hideCheckpoint();
        }, 1000);

        document.getElementById('pgcp-pause')?.addEventListener('click', () => {
            hideCheckpoint();
            if (onPause) onPause();
        });
        document.getElementById('pgcp-diff')?.addEventListener('click', () => {
            if (onShowDiff) onShowDiff();
        });
    }

    function hideCheckpoint() {
        const el = document.getElementById('plan-graph-checkpoint');
        if (el) el.style.display = 'none';
        if (_checkpointTimer) {
            clearInterval(_checkpointTimer);
            _checkpointTimer = null;
        }
    }

    // ── Internal: drawing ─────────────────────────────────────────

    function _draw() {
        const canvas = document.getElementById('plan-graph-canvas');
        const intentEl = document.getElementById('plan-graph-intent');
        if (!canvas) return;
        if (!_snapshot || Object.keys(_snapshot.nodes).length === 0) {
            canvas.innerHTML = '<div class="plan-graph-empty">Plan-graph idle &mdash; start an intent to see it populate.</div>';
            if (intentEl) intentEl.textContent = 'No active intent';
            _updateBadge(0);
            return;
        }
        if (intentEl) intentEl.textContent = _snapshot.intent || _snapshot.intent_id || 'Active intent';

        // Layout: depth-based columns, hierarchical layout
        const positions = _layout(_snapshot);
        const totalW = positions.width + PADDING * 2;
        const totalH = positions.height + PADDING * 2;

        // SVG for edges
        let edgesSvg = `<svg class="plan-graph-edges" width="${totalW}" height="${totalH}">`;
        for (const node of Object.values(_snapshot.nodes)) {
            const target = positions.nodes[node.id];
            if (!target) continue;
            // Tree edges (parent_id) — solid
            if (node.parent_id && positions.nodes[node.parent_id]) {
                const src = positions.nodes[node.parent_id];
                edgesSvg += _edgePath(src, target, 'solid');
            }
            // Dep edges (depends_on, not also parent) — dashed
            for (const dep of node.depends_on || []) {
                if (dep === node.parent_id) continue;
                const src = positions.nodes[dep];
                if (src) edgesSvg += _edgePath(src, target, 'dashed');
            }
        }
        edgesSvg += '</svg>';

        // Node cards
        let nodesHtml = '';
        for (const node of Object.values(_snapshot.nodes)) {
            const pos = positions.nodes[node.id];
            if (!pos) continue;
            const isSelected = node.id === _selectedId;
            const cls = ['pgn', STATUS_CLASS[node.status] || 'pgn-pending'];
            if (isSelected) cls.push('pgn-selected');
            const conf = (typeof node.confidence === 'number' ? node.confidence : 1.0).toFixed(2);
            const icon = SPECIALIZATION_ICON[node.specialization] || '\u25CB';
            nodesHtml += `
                <div class="${cls.join(' ')}" data-id="${_escape(node.id)}"
                     style="left:${pos.x}px;top:${pos.y}px;width:${NODE_W}px;height:${NODE_H}px">
                    <div class="pgn-header">
                        <span class="pgn-icon">${icon}</span>
                        <span class="pgn-spec">${_escape(node.specialization || 'implement')}</span>
                        <span class="pgn-status">${_escape(node.status)}</span>
                    </div>
                    <div class="pgn-goal" title="${_escape(node.goal)}">${_escape(node.goal)}</div>
                    <div class="pgn-foot">
                        <span class="pgn-conf">conf ${conf}</span>
                    </div>
                </div>`;
        }

        canvas.innerHTML = edgesSvg + nodesHtml;
        canvas.style.minWidth = `${totalW}px`;
        canvas.style.minHeight = `${totalH}px`;

        // Wire node clicks
        canvas.querySelectorAll('.pgn').forEach((card) => {
            card.addEventListener('click', () => _selectNode(card.dataset.id));
        });

        _updateBadge(Object.keys(_snapshot.nodes).length);
        if (_selectedId) _renderDetail(_selectedId);
    }

    function _layout(snapshot) {
        // Depth = longest dependency chain to a node with no incoming edges.
        const nodes = Object.values(snapshot.nodes);
        const depth = {};
        function computeDepth(id, seen) {
            if (depth[id] !== undefined) return depth[id];
            if (seen.has(id)) return 0;  // cycle defense
            seen.add(id);
            const n = snapshot.nodes[id];
            if (!n) return 0;
            const incoming = (n.depends_on || []).filter((d) => snapshot.nodes[d]);
            if (n.parent_id && snapshot.nodes[n.parent_id]) incoming.push(n.parent_id);
            if (incoming.length === 0) {
                depth[id] = 0;
                return 0;
            }
            const max = Math.max(...incoming.map((d) => computeDepth(d, seen) + 1));
            depth[id] = max;
            return max;
        }
        for (const n of nodes) computeDepth(n.id, new Set());

        // Group by depth → columns
        const columns = {};
        for (const n of nodes) {
            const d = depth[n.id] || 0;
            (columns[d] = columns[d] || []).push(n);
        }
        const sortedDepths = Object.keys(columns).map(Number).sort((a, b) => a - b);

        const positions = {};
        let maxRow = 0;
        for (const d of sortedDepths) {
            const col = columns[d].sort((a, b) => (a.id || '').localeCompare(b.id || ''));
            col.forEach((n, idx) => {
                positions[n.id] = {
                    x: PADDING + d * (NODE_W + COL_GAP),
                    y: PADDING + idx * (NODE_H + ROW_GAP),
                };
                if (idx > maxRow) maxRow = idx;
            });
        }

        const width = (sortedDepths.length || 1) * (NODE_W + COL_GAP);
        const height = (maxRow + 1) * (NODE_H + ROW_GAP);
        return { nodes: positions, width, height };
    }

    function _edgePath(srcPos, dstPos, style) {
        // Source: right edge midpoint. Destination: left edge midpoint.
        const x1 = srcPos.x + NODE_W;
        const y1 = srcPos.y + NODE_H / 2;
        const x2 = dstPos.x;
        const y2 = dstPos.y + NODE_H / 2;
        const cx = (x1 + x2) / 2;
        const dasharray = style === 'dashed' ? 'stroke-dasharray="4 4"' : '';
        return `<path d="M${x1} ${y1} C${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}" ` +
               `class="pg-edge pg-edge-${style}" fill="none" ${dasharray}/>`;
    }

    // ── Internal: side detail ────────────────────────────────────

    function _selectNode(id) {
        _selectedId = id;
        _draw();
    }

    function _renderDetail(id) {
        const detail = document.getElementById('plan-graph-detail');
        if (!detail) return;
        const node = _snapshot.nodes[id];
        if (!node) {
            detail.style.display = 'none';
            return;
        }
        detail.style.display = 'block';
        detail.innerHTML = `
            <div class="pgd-header">
                <span class="pgd-spec">${_escape(node.specialization || '')}</span>
                <span class="pgd-status pgn-status ${STATUS_CLASS[node.status] || ''}">${_escape(node.status)}</span>
                <button class="pgd-close" id="pgd-close">&times;</button>
            </div>
            <div class="pgd-goal">${_escape(node.goal)}</div>
            <div class="pgd-meta">
                <div><b>Confidence:</b> ${(node.confidence ?? 1.0).toFixed(2)}</div>
                ${node.parent_id ? `<div><b>Parent:</b> ${_escape(node.parent_id)}</div>` : ''}
                ${(node.depends_on || []).length ? `<div><b>Depends on:</b> ${node.depends_on.map(_escape).join(', ')}</div>` : ''}
                ${node.last_summary ? `<div><b>Summary:</b> ${_escape(node.last_summary)}</div>` : ''}
            </div>
            <div class="pgd-actions">
                <button class="pgd-btn" id="pgd-restore" title="Restore plan to before this node">Restore from here</button>
                <button class="pgd-btn" id="pgd-rerun" title="Re-run this node">Re-run</button>
            </div>
        `;
        document.getElementById('pgd-close')?.addEventListener('click', () => {
            _selectedId = null;
            detail.style.display = 'none';
            _draw();
        });
        if (typeof PlanGraphView.onAction === 'function') {
            document.getElementById('pgd-restore')?.addEventListener('click', () =>
                PlanGraphView.onAction('restore_from', id));
            document.getElementById('pgd-rerun')?.addEventListener('click', () =>
                PlanGraphView.onAction('rerun', id));
        }
        if (typeof PlanGraphView.onSelect === 'function') {
            PlanGraphView.onSelect(id);
        }
    }

    // ── Utilities ────────────────────────────────────────────────

    function _escape(s) {
        const div = document.createElement('div');
        div.textContent = String(s ?? '');
        return div.innerHTML;
    }

    function _normalizeSnapshot(s) {
        if (!s) return { intent: '', intent_id: '', nodes: {} };
        // Server may send {nodes: [...]} (list); normalize to {nodes: {id: node}} (map).
        const nodes = {};
        const rawNodes = Array.isArray(s.nodes) ? s.nodes : Object.values(s.nodes || {});
        for (const n of rawNodes) {
            if (n && n.id) nodes[n.id] = { ...n };
        }
        return {
            intent: s.intent || '',
            intent_id: s.intent_id || '',
            nodes,
        };
    }

    function _updateBadge(count) {
        const badge = document.getElementById('plan-tab-badge');
        if (!badge) return;
        if (count > 0) {
            badge.style.display = '';
            badge.textContent = String(count);
        } else {
            badge.style.display = 'none';
        }
    }

    // Expose
    window.PlanGraphView = {
        render,
        applyEvent,
        showCheckpoint,
        hideCheckpoint,
        // hook points the host app can override:
        onSelect: null,
        onAction: null,
    };
})(window);
