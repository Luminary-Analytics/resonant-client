'use strict';
// Lumi for VS Code. Adds the selection or files to your message in Lumi (you
// send it from Lumi), and shows what Lumi's latest change-making turn changed
// beside the current files. It talks only to the Lumi app on this computer,
// through the bridge Lumi opens while it runs (lumi/gui/editor_bridge.py).

const path = require('path');
const vscode = require('vscode');
const bridge = require('./bridge');

const BEFORE_SCHEME = 'lumi-before';
const MAX_DIFFS = 20;

function connection() {
    return bridge.readBridge();
}

async function call(method, route, body) {
    try {
        return await bridge.request(connection(), method, route, body);
    } catch (error) {
        vscode.window.showErrorMessage(`Lumi: ${error.message}`);
        return null;
    }
}

/** Offer to save unsaved files first: Lumi reads files from disk. False to stop. */
async function savedFirst(documents) {
    const dirty = documents.filter((document) => document && document.isDirty);
    if (!dirty.length) return true;
    const names = dirty.map((document) => path.basename(document.uri.fsPath)).join(', ');
    const choice = await vscode.window.showWarningMessage(
        `Lumi reads saved files, and ${names} has unsaved changes.`,
        'Save and Send', 'Send the Saved Version',
    );
    if (!choice) return false;
    if (choice === 'Save and Send') {
        for (const document of dirty) {
            if (!(await document.save())) return false;
        }
    }
    return true;
}

async function send(items, text) {
    const result = await call('POST', '/api/editor/context', { items, text, source: 'VS Code' });
    if (!result) return;
    const { info, warning } = bridge.summary(result);
    if (warning) vscode.window.showWarningMessage(warning);
    if (info) vscode.window.showInformationMessage(info);
}

async function sendSelection(ask) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showInformationMessage('Lumi: open a file first.');
        return;
    }
    const document = editor.document;
    if (document.uri.scheme !== 'file') {
        vscode.window.showErrorMessage('Lumi: only files saved on this computer can be sent.');
        return;
    }
    if (!(await savedFirst([document]))) return;
    let text = '';
    if (ask) {
        text = await vscode.window.showInputBox({
            prompt: 'What should Lumi do with the selection?',
            placeHolder: 'Explain this, find the bug, write tests for it...',
        });
        if (text === undefined) return;
    }
    await send(bridge.selectionItems(document.uri.fsPath, editor.selections), text);
}

async function sendFiles(uris) {
    const files = uris.filter((uri) => uri && uri.scheme === 'file');
    if (!files.length) {
        vscode.window.showInformationMessage('Lumi: there are no saved files to send.');
        return;
    }
    const open = vscode.workspace.textDocuments || [];
    const documents = files.map((uri) => open.find((document) => document.uri.toString() === uri.toString()));
    if (!(await savedFirst(documents))) return;
    await send(files.map((uri) => ({ path: uri.fsPath })), '');
}

/** The file a command was run on: the clicked files, else the active editor's file. */
function commandUris(uri, uris) {
    if (Array.isArray(uris) && uris.length) return uris;
    if (uri && uri.scheme) return [uri];
    const editor = vscode.window.activeTextEditor;
    return editor ? [editor.document.uri] : [];
}

function openFileUris() {
    const seen = new Set();
    const uris = [];
    for (const group of vscode.window.tabGroups.all) {
        for (const tab of group.tabs) {
            const uri = tab.input && tab.input.uri;
            if (uri && uri.scheme === 'file' && !seen.has(uri.toString())) {
                seen.add(uri.toString());
                uris.push(uri);
            }
        }
    }
    return uris;
}

/** Earlier versions of files, fetched from Lumi, for the left side of a diff. */
class BeforeProvider {
    async provideTextDocumentContent(uri) {
        const params = new URLSearchParams(uri.query);
        if (params.get('empty')) return '';
        const query = new URLSearchParams({ before: params.get('before') || '', path: uri.path.replace(/^\//, '') });
        try {
            const content = await bridge.request(connection(), 'GET', `/api/editor/before?${query}`, undefined, { raw: true });
            return content.toString('utf8');
        } catch (error) {
            return `Lumi couldn't load the earlier version: ${error.message}`;
        }
    }
}

function beforeUri(relative, before) {
    return vscode.Uri.from({ scheme: BEFORE_SCHEME, path: `/${relative}`, query: new URLSearchParams({ before }).toString() });
}

function emptyUri(relative) {
    return vscode.Uri.from({ scheme: BEFORE_SCHEME, path: `/${relative}`, query: 'empty=1' });
}

async function openDiff(data, file, preview) {
    const left = file.before_path ? beforeUri(file.before_path, data.turn.before) : emptyUri(file.path);
    const right = file.status === 'deleted'
        ? emptyUri(file.path)
        : vscode.Uri.file(path.join(data.project, ...file.path.split('/')));
    const title = `${path.basename(file.path)} (before Lumi ↔ now)`;
    await vscode.commands.executeCommand('vscode.diff', left, right, title, { preview });
}

async function reviewChanges() {
    const data = await call('GET', '/api/editor/changes');
    if (!data) return;
    if (!data.turn || !data.files.length) {
        vscode.window.showInformationMessage("Lumi: the open session hasn't changed any files yet.");
        return;
    }
    const files = data.files.map((file) => ({ label: file.path, description: bridge.statusLabel(file), file }));
    const everything = { label: 'Open all', description: `${data.files.length} file${data.files.length === 1 ? '' : 's'}`, all: true };
    const running = data.turn.finished ? '' : ' (still running)';
    const choice = await vscode.window.showQuickPick(data.files.length > 1 ? [everything, ...files] : files, {
        title: `Lumi's changes${running}: ${data.turn.prompt || 'latest turn'}`,
        placeHolder: `Compared with ${data.turn.compared_with}`,
    });
    if (!choice) return;
    const chosen = choice.all ? data.files.slice(0, MAX_DIFFS) : [choice.file];
    for (const file of chosen) await openDiff(data, file, chosen.length === 1);
    if (choice.all && data.files.length > MAX_DIFFS) {
        vscode.window.showInformationMessage(`Lumi: opened the first ${MAX_DIFFS} of ${data.files.length} files.`);
    }
}

async function status() {
    const data = await call('GET', '/api/editor/status');
    if (!data) return;
    const window = data.window ? '' : " Its window isn't open, so files can't be added to a message.";
    vscode.window.showInformationMessage(`Lumi ${data.version} is running with ${data.project || 'no project'} open.${window}`);
}

function activate(context) {
    context.subscriptions.push(
        vscode.workspace.registerTextDocumentContentProvider(BEFORE_SCHEME, new BeforeProvider()),
        vscode.commands.registerCommand('lumi.sendSelection', () => sendSelection(false)),
        vscode.commands.registerCommand('lumi.askAboutSelection', () => sendSelection(true)),
        vscode.commands.registerCommand('lumi.sendFile', (uri, uris) => sendFiles(commandUris(uri, uris))),
        vscode.commands.registerCommand('lumi.sendOpenFiles', () => sendFiles(openFileUris())),
        vscode.commands.registerCommand('lumi.reviewChanges', reviewChanges),
        vscode.commands.registerCommand('lumi.status', status),
    );
}

function deactivate() {}

module.exports = { activate, deactivate };
