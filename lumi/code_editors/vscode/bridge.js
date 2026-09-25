'use strict';
// The Lumi bridge client: finds the running Lumi app and calls it. Kept free
// of the VS Code API so it can be tested with Node alone.
//
// While Lumi runs it writes editor-bridge.json in its state folder (normally
// ~/.lumi) with its local address and a token for this launch. See
// lumi/gui/editor_bridge.py for what the endpoints accept.

const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

class BridgeError extends Error {
    constructor(message, status = 0) {
        super(message);
        this.name = 'BridgeError';
        this.status = status;
    }
}

const NOT_RUNNING = "Lumi isn't running, or its Code editors switch (Settings > Privacy & security) is off. "
    + 'Open Lumi, then try again.';

/** Lumi's state folder, chosen the way lumi/paths.py chooses it. */
function stateHome(env = process.env, home = os.homedir()) {
    if (env.LUMI_STATE_HOME) return env.LUMI_STATE_HOME;
    const current = path.join(home, '.lumi');
    const legacy = path.join(home, '.resonant');
    if (!fs.existsSync(current) && fs.existsSync(legacy)) return legacy;
    return current;
}

function running(pid) {
    try {
        process.kill(pid, 0);  // signal 0 only checks that the process exists
        return true;
    } catch (error) {
        return error.code === 'EPERM';  // exists, but belongs to someone else
    }
}

/** The running app's address and token, or a BridgeError saying why not. */
function readBridge(env = process.env, home = os.homedir()) {
    const file = path.join(stateHome(env, home), 'editor-bridge.json');
    let data;
    try {
        data = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (error) {
        throw new BridgeError(NOT_RUNNING);
    }
    if (!data || typeof data.url !== 'string' || typeof data.token !== 'string' || !data.token) {
        throw new BridgeError("Lumi's bridge file is unreadable. Restart Lumi.");
    }
    // A launch that was killed leaves its file behind; its port may belong to
    // something else now, which must not be sent the old token.
    if (Number.isInteger(data.pid) && !running(data.pid)) throw new BridgeError(NOT_RUNNING);
    let url;
    try {
        url = new URL(data.url);
    } catch (error) {
        throw new BridgeError("Lumi's bridge file is unreadable. Restart Lumi.");
    }
    // The token and your code go only to Lumi on this computer.
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
        throw new BridgeError("Lumi's bridge file doesn't point at this computer.");
    }
    return { url: url.origin, token: data.token };
}

/** Call ``route`` (such as /api/editor/status); resolves to parsed JSON, or a Buffer with raw. */
function request(bridge, method, route, body, { raw = false, timeoutMs = 15000 } = {}) {
    return new Promise((resolve, reject) => {
        const url = new URL(route, bridge.url);
        const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), 'utf8');
        const headers = { Authorization: `Bearer ${bridge.token}`, Accept: 'application/json' };
        if (payload) {
            headers['Content-Type'] = 'application/json';
            headers['Content-Length'] = payload.length;
        }
        const req = http.request(url, { method, headers, timeout: timeoutMs }, (res) => {
            const chunks = [];
            res.on('data', (chunk) => chunks.push(chunk));
            res.on('end', () => {
                const buffer = Buffer.concat(chunks);
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    if (raw) {
                        resolve(buffer);
                        return;
                    }
                    try {
                        resolve(JSON.parse(buffer.toString('utf8')));
                    } catch (error) {
                        reject(new BridgeError('Lumi sent an answer this extension could not read.'));
                    }
                    return;
                }
                let message = `Lumi answered ${res.statusCode}.`;
                try {
                    message = JSON.parse(buffer.toString('utf8')).error || message;
                } catch (error) {
                    // Keep the status line.
                }
                if (res.statusCode === 403 && message === 'Forbidden') {
                    message = 'Lumi refused the request. Restart Lumi and try again.';
                }
                reject(new BridgeError(message, res.statusCode));
            });
        });
        req.on('timeout', () => req.destroy(new BridgeError("Lumi didn't answer in time.")));
        req.on('error', (error) => {
            if (error instanceof BridgeError) reject(error);
            else if (error.code === 'ECONNREFUSED') reject(new BridgeError(NOT_RUNNING));
            else reject(new BridgeError(`Couldn't reach Lumi: ${error.message}`));
        });
        if (payload) req.write(payload);
        req.end();
    });
}

/**
 * Items for a file's selections, as 1-based line ranges. A selection that
 * ends at the start of a line leaves that line out; with nothing selected the
 * whole file is sent.
 */
function selectionItems(fsPath, selections) {
    const items = [];
    const seen = new Set();
    for (const selection of selections || []) {
        if (selection.isEmpty) continue;
        const start = selection.start.line + 1;
        let end = selection.end.line + 1;
        if (selection.end.character === 0 && selection.end.line > selection.start.line) end -= 1;
        const key = `${start}-${end}`;
        if (seen.has(key)) continue;
        seen.add(key);
        items.push({ path: fsPath, start_line: start, end_line: end });
    }
    return items.length ? items : [{ path: fsPath }];
}

/** A short description of a changed file for the review list. */
function statusLabel(file) {
    if (file.status === 'added') return 'new file';
    if (file.status === 'deleted') return 'deleted';
    if (file.status === 'renamed') return `renamed from ${file.before_path}`;
    return 'changed';
}

/** What to tell the person after files were sent: {info, warning}. */
function summary(result) {
    const attached = result.attached || [];
    const skipped = result.skipped || [];
    const names = attached.slice(0, 3).join(', ') + (attached.length > 3 ? ` and ${attached.length - 3} more` : '');
    const info = attached.length ? `Added to your message in Lumi: ${names}. Send it from Lumi.` : '';
    const warning = skipped.length
        ? `Not added: ${skipped.map((item) => `${path.basename(item.path)} (${item.reason})`).join('; ')}`
        : '';
    return { info, warning };
}

module.exports = { BridgeError, NOT_RUNNING, stateHome, readBridge, request, selectionItems, statusLabel, summary };
