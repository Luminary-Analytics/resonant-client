/* Panels from capability packs in a real browser (headless Edge): the source app, a
 * real WebSocket, the panel route and the sandboxed frame; no model is called.
 *
 *   node tests/extension_panels.browser.cjs [absolute-path-to-playwright-module]
 *
 * Run node with LUMI_KEYCHAIN=off and the ordinary USERPROFILE: the fixture
 * server (tests/fixtures/extension_panels_server.py) isolates its own home,
 * and Edge doesn't start with a temporary one. PANELS_PYTHON and
 * PANELS_BROWSER_EXECUTABLE choose other runtimes. Screenshots and the
 * server's output land in a folder under the temp directory, named on failure.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

const fixtureLaunch = async info => (await (await fetch(info.url + '/__fixture__/launch')).json()).url;
const evidence = async info => (await fetch(info.url + '/__fixture__/evidence')).json();
const post = async (info, route) => (await fetch(info.url + route, {method: 'POST'})).json();

async function panelFrame(page) {
    const handle = await page.waitForSelector('#extension-panel-dialog iframe.extension-panel-frame');
    const frame = await handle.contentFrame();
    await frame.waitForSelector('body[data-probe="done"]');
    return {handle, frame};
}

async function openFromPalette(page) {
    await page.locator('#user-input').focus();
    await page.keyboard.press('Control+k');
    await page.locator('#cmd-palette-input').fill('Build stats');
    await page.locator('.cmd-palette-item', {hasText: 'Open panel: Build stats'}).waitFor();
    await page.keyboard.press('Enter');
    await page.getByRole('dialog', {name: 'Build stats'}).waitFor();
}

const focusedName = page => page.evaluate(() => {
    const node = document.activeElement;
    return node.id || node.getAttribute('aria-label') || node.tagName;
});

test('A capability pack panel runs sandboxed and reaches the app only through the bridge', {timeout: 150000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-extension-panels-browser-'));
    const server = spawn(process.env.PANELS_PYTHON || 'python',
        [path.join(__dirname, 'fixtures/extension_panels_server.py'), output],
        {cwd: output, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
            env: {...process.env, LUMI_KEYCHAIN: 'off', PYTHONDONTWRITEBYTECODE: '1'}});
    let stdout = '', stderr = '', info, browser, page;
    server.stdout.on('data', chunk => { stdout += chunk; });
    server.stderr.on('data', chunk => { stderr += chunk; });
    const exited = new Promise(resolve => server.once('exit', (code, signal) => resolve({code, signal})));
    // Where the check got to, for failure.txt. node:test's timeout reports a
    // failure but doesn't stop a stuck step (page.evaluate has no timeout), so
    // a watchdog saves the evidence and ends the run.
    const steps = [];
    const step = label => steps.push(`${new Date().toISOString()} ${label}`);
    const watchdog = setTimeout(async () => {
        fs.writeFileSync(path.join(output, 'failure.txt'),
            `Stuck after: ${steps.at(-1)}\n\n${steps.join('\n')}\n\nserver stderr:\n${stderr}`);
        if (page) await page.screenshot({path: path.join(output, 'stuck.png')}).catch(() => {});
        console.error(`stuck; evidence: ${output}`);
        server.kill();
        process.exit(1);
    }, 140000);
    try {
        for (let i = 0; i < 300 && !info; i++) {
            const line = stdout.split(/\r?\n/).find(row => row.startsWith('{"url":'));
            if (line) info = JSON.parse(line);
            else if (server.exitCode !== null) throw Error('Fixture server failed: ' + stderr);
            else await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.ok(info, 'Fixture server ready metadata missing: ' + stderr);
        browser = await chromium.launch(process.env.PANELS_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.PANELS_BROWSER_EXECUTABLE}
            : {headless: true, channel: 'msedge'});
        const context = await browser.newContext({viewport: {width: 1280, height: 860}});
        page = await context.newPage();
        page.setDefaultTimeout(15000);
        const errors = [];
        const commands = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('websocket', socket => socket.on('framesent', frame => {
            try { commands.push(JSON.parse(frame.payload).command); } catch (_) { /* not JSON */ }
        }));
        const sent = () => commands.filter(command => ['chat', 'send_message', 'user_message', 'shell_exec'].includes(command));
        // Nothing may leave this machine; loopback (the app and the canary) continues.
        await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)
            ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);
        await page.locator('#user-input').fill('Existing draft');

        step('View > Panels, with the pointer');
        await page.locator('.titlebar-menu-button').click();
        await page.locator('.menubar-item[data-menu="view"]').hover();
        const item = page.locator('.extension-panel-menu-item', {hasText: 'Build stats'});
        await item.waitFor();
        assert.equal(await page.locator('.extension-panel-menu-heading').textContent(), 'Panels');
        assert.equal(await item.locator('.menubar-shortcut').textContent(), 'Panel demo');
        await item.click();
        const dialog = page.getByRole('dialog', {name: 'Build stats'});
        await dialog.waitFor();
        assert.equal(await focusedName(page), 'Close Build stats');
        const {handle, frame} = await panelFrame(page);
        assert.equal(await handle.getAttribute('sandbox'), 'allow-scripts');
        assert.equal(await handle.getAttribute('title'), 'Build stats, a panel from the Panel demo pack');
        const token = (await handle.getAttribute('src')).split('/')[2];
        assert.match(token, /^[A-Za-z0-9_-]{32}$/);
        await page.screenshot({path: path.join(output, 'panel-dark.png')});

        step('What the panel could do from inside its sandbox');
        const probe = await frame.evaluate(() => window.probe);
        fs.writeFileSync(path.join(output, 'probe.json'), JSON.stringify(probe, null, 2));
        assert.equal(probe.origin, 'ran: null', 'the frame has an opaque origin');
        for (const name of ['localStorage', 'sessionStorage', 'indexedDB', 'cookie', 'parentDocument', 'topDocument',
            'parentApp', 'parentAccessToken', 'parentComposer', 'topNavigation', 'eval', 'newFunction']) {
            assert.match(probe[name], /^blocked: /, `${name}: ${probe[name]}`);
        }
        assert.equal(probe.popup, 'ran: null', 'window.open returns no window');
        for (const name of ['fetchCanary', 'fetchApp', 'fetchOwnFile', 'xhr', 'webSocket', 'appSocket', 'eventSource',
            'image', 'script', 'appScript', 'module', 'worker']) {
            assert.match(probe[name], /^blocked: /, `${name}: ${probe[name]}`);
        }
        assert.equal(probe.inlineScript, 'blocked');
        assert.equal(probe.inlineHandler, 'blocked');
        assert.notEqual(probe.inlineStyle, 'rgb(255, 0, 0)', 'inline styles are refused');
        assert.equal(probe.nativeBridge, 'ran: undefined');
        // The project's name and the theme, and nothing about the conversation.
        assert.deepEqual(JSON.parse(probe.context.replace(/^reached: /, '')), {project: 'project', theme: 'dark'});
        assert.equal(await frame.locator('#context').textContent(), 'project / dark');
        assert.equal(await frame.evaluate(() => document.documentElement.dataset.lumiTheme), 'dark');
        // The private channel reached Lumi's bridge, and none of the panel's traps saw it.
        await page.waitForTimeout(300);
        assert.equal(await frame.evaluate(() => window.stolePort || 'none'), 'none');

        step('A panel can\'t close itself');
        await frame.locator('#forge').click();
        await frame.waitForFunction(() => window.forged === true);
        await page.waitForTimeout(500);
        assert.equal(await page.locator('#extension-panel-dialog').count(), 1, 'a forged close closed the panel');

        step('Adding to the message: checked, into the draft, never sent');
        await frame.locator('#insert').click();
        await frame.waitForFunction(() => window.insertResult === 'ok');
        assert.equal(await page.locator('#user-input').inputValue(),
            'Existing draft\nSummarize the failing builds from the panel.');
        assert.ok(commands.includes('extension_panel_check'), 'the pack is checked before text is added');
        assert.equal(await page.locator('#user-input').evaluate(node => node.selectionStart), 15,
            'the caret is where the added text starts');
        await page.locator('#ui-toast-message', {hasText: 'Build stats (Panel demo pack) added text to your message'}).waitFor();
        assert.notEqual(await focusedName(page), 'user-input');

        step('Text that would run as a command is refused');
        await page.locator('#user-input').fill('');
        await frame.locator('#command').click();
        await frame.waitForFunction(() => typeof window.commandResult === 'string');
        assert.match(await frame.evaluate(() => window.commandResult), /would start with ! and run as a command/);
        assert.equal(await page.locator('#user-input').inputValue(), '');
        await page.locator('#ui-toast-message', {hasText: 'Lumi didn’t add Build stats’s text'}).waitFor();

        step('Padding and mentions, after a long draft');
        const draft = Array.from({length: 30}, (_, i) => `draft line ${i + 1}`).join('\n');
        await page.locator('#user-input').fill(draft);
        await frame.locator('#padded').click();
        await frame.waitForFunction(() => window.paddedResult === 'ok');
        // 3,000 spaces of indentation keep 8; blank lines collapse to one; the mention is split.
        const added = '        Padded start\nxxxxxxxxxxxxxxxxxxxx\n\nsecond paragraph @ file:secrets.txt';
        assert.equal(await page.locator('#user-input').inputValue(), `${draft}\n${added}`);
        await page.locator('#ui-toast-message', {hasText: 'were split apart, so they attach nothing'}).waitFor();
        // The first added line shows in the message box, however long the draft above it.
        const shown = await page.locator('#user-input').evaluate((node, start) => {
            const style = getComputedStyle(node);
            const line = parseFloat(style.lineHeight);
            const top = parseFloat(style.paddingTop) + node.value.slice(0, start).split('\n').length * line - line;
            return {top, line, scrollTop: node.scrollTop, height: node.clientHeight, caret: node.selectionStart};
        }, draft.length + 1);
        assert.equal(shown.caret, draft.length + 1);
        assert.ok(shown.top >= shown.scrollTop && shown.top + shown.line <= shown.scrollTop + shown.height,
            JSON.stringify(shown));
        await page.locator('#user-input').fill('Existing draft');

        step('The panel\'s notice is its own');
        await frame.locator('#toast').click();
        await frame.waitForFunction(() => window.toastResult === 'ok');
        const notice = page.locator('#extension-panel-dialog .extension-panel-notice');
        await notice.waitFor();
        assert.equal(await notice.textContent(), 'Panel · Panel demo: Saved the build filter');
        assert.doesNotMatch(await page.locator('#ui-toast-message').textContent(), /Saved the build filter/);

        step('Both themes: the dialog\'s tokens, and the panel is told');
        const darkBackground = await page.locator('.extension-panel-dialog').evaluate(node => getComputedStyle(node).backgroundColor);
        await page.evaluate(() => window.LumiAppearance.setTheme('light'));
        await frame.waitForFunction(() => document.documentElement.dataset.lumiTheme === 'light');
        assert.equal(await frame.locator('#context').textContent(), 'project / light');
        const lightBackground = await page.locator('.extension-panel-dialog').evaluate(node => getComputedStyle(node).backgroundColor);
        assert.notEqual(lightBackground, darkBackground);
        await page.screenshot({path: path.join(output, 'panel-light.png')});
        await page.evaluate(() => window.LumiAppearance.setTheme('dark'));

        step('Keyboard: Tab stays in the dialog; Escape closes; focus goes back to the menu');
        await page.getByRole('button', {name: 'Close Build stats'}).focus();
        await page.keyboard.press('Tab');                                // into the panel: its first button
        assert.equal(await focusedName(page), 'IFRAME');
        assert.equal(await frame.evaluate(() => document.activeElement.id), 'insert');
        // Past the panel's last tab stop (the probe added a frame after the buttons), focus comes back to Close.
        for (let tries = 0; tries < 12 && await focusedName(page) === 'IFRAME'; tries++) {
            await page.keyboard.press('Tab');
        }
        assert.equal(await focusedName(page), 'Close Build stats');
        await page.keyboard.press('Shift+Tab');                          // and backwards, into the panel
        assert.equal(await focusedName(page), 'IFRAME');
        await page.getByRole('button', {name: 'Close Build stats'}).focus();
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'detached'});
        // Opened from the menu: focus goes back to the menu's button, never the message box.
        assert.equal(await focusedName(page), 'Menu');
        assert.equal((await fetch(`${info.url}/panels/${token}/index.html`)).status, 404, 'closing withdrew the token');

        step('From the command palette; a real Escape pressed inside the panel closes it');
        await openFromPalette(page);
        const again = await panelFrame(page);
        await again.frame.locator('#toast').focus();
        assert.equal(await focusedName(page), 'IFRAME');
        await page.keyboard.press('Escape');
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        assert.equal(await focusedName(page), 'titlebar-command');

        step('A panel URL outside the frame Lumi made');
        await openFromPalette(page);
        const opened = await panelFrame(page);
        const liveToken = (await opened.handle.getAttribute('src')).split('/')[2];
        const panelUrl = `${info.url}/panels/${liveToken}/index.html`;
        // Pasted into the address bar: refused (Fetch Metadata says it's a top-level page).
        const direct = await context.newPage();
        const directResponse = await direct.goto(panelUrl);
        assert.equal(directResponse.status(), 403);
        assert.match(await direct.locator('body').textContent(), /opens inside Lumi/);
        await direct.close();
        // Framed without the sandbox attribute, as a bug in the page might: the
        // response's own policy still sandboxes it, away from the launch token.
        await page.evaluate(url => {
            const probe = document.createElement('iframe');
            probe.id = 'unsandboxed-probe';
            probe.src = url;
            document.body.appendChild(probe);
        }, panelUrl);
        const unsandboxed = await (await page.waitForSelector('#unsandboxed-probe')).contentFrame();
        await unsandboxed.waitForSelector('body[data-probe="done"]', {timeout: 20000});
        const outside = await unsandboxed.evaluate(() => window.probe);
        assert.equal(outside.origin, 'ran: null');
        for (const name of ['localStorage', 'parentDocument', 'parentAccessToken', 'topNavigation', 'fetchApp']) {
            assert.match(outside[name], /^blocked: /, `unsandboxed frame ${name}: ${outside[name]}`);
        }
        await page.evaluate(() => document.getElementById('unsandboxed-probe').remove());

        step('It can\'t navigate itself to another site');
        await opened.frame.locator('#navigate').click();
        await page.waitForTimeout(1000);
        assert.deepEqual((await evidence(info)).canary_hits, [], 'navigating the frame away reached the network');

        step('Revoked while open: its next addition is refused and the panel closes');
        await page.getByRole('button', {name: 'Close Build stats'}).click();
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        assert.equal(await focusedName(page), 'titlebar-command');
        await openFromPalette(page);
        const beforeRevoke = await panelFrame(page);
        const revokedToken = (await beforeRevoke.handle.getAttribute('src')).split('/')[2];
        assert.equal((await post(info, '/__fixture__/revoke')).status, 'needs_approval');
        await page.locator('#user-input').evaluate(node => { node.value = 'Existing draft'; });
        await beforeRevoke.frame.locator('#insert').click();
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        await page.locator('#ui-toast-message', {hasText: 'Build stats closed.'}).waitFor();
        assert.equal(await page.locator('#user-input').inputValue(), 'Existing draft', 'a revoked pack added text');
        assert.notEqual((await fetch(`${info.url}/panels/${revokedToken}/index.html`)).status, 200);
        await page.locator('.titlebar-menu-button').click();
        await page.locator('.menubar-item[data-menu="view"]').hover();
        await page.waitForFunction(() => Array.isArray(window.app._extensionPanels) && !window.app._extensionPanels.length);
        assert.equal(await page.locator('.extension-panel-menu-item').count(), 0);
        await page.keyboard.press('Escape');

        step('Its connection drops: the open panel closes');
        assert.equal((await post(info, '/__fixture__/approve')).status, 'approved');
        await page.evaluate(() => window.app._requestExtensionPanels(true));
        await page.waitForFunction(() => window.app._extensionPanels?.length === 1);
        await openFromPalette(page);
        await panelFrame(page);
        await page.evaluate(() => window.app.ws.close());
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        await page.locator('#ui-toast-message', {hasText: 'connection dropped'}).waitFor();
        await page.waitForFunction(() => window.app.ws?.readyState === 1, null, {timeout: 20000});

        step('Compact width');
        await page.setViewportSize({width: 390, height: 844});
        await page.evaluate(() => window.app._requestExtensionPanels(true));
        await page.waitForFunction(() => window.app._extensionPanels?.length === 1);
        await page.evaluate(() => window.app.openExtensionPanel('panel-demo', 'build-stats', 'menu'));
        const compact = await panelFrame(page);
        const box = await page.locator('.extension-panel-dialog').boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= 390.5 && box.height <= 844.5, JSON.stringify(box));
        const frameBox = await compact.handle.boundingBox();
        assert.ok(frameBox.width >= 380 && frameBox.height >= 600, JSON.stringify(frameBox));
        const closeBox = await page.getByRole('button', {name: 'Close Build stats'}).boundingBox();
        assert.ok(closeBox.width >= 24 && closeBox.height >= 24, 'a reliable close target');
        await page.screenshot({path: path.join(output, 'panel-compact.png')});
        await page.keyboard.press('Escape');
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        assert.notEqual(await focusedName(page), 'user-input');

        step('Nothing got out, and nothing was sent');
        const final = await evidence(info);
        assert.deepEqual(final.canary_hits, [], 'a request reached the canary');
        assert.equal(final.live_providers_called, false);
        assert.deepEqual(sent(), [], 'a panel caused a message or a command to be sent');
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output, 'evidence.json'), JSON.stringify({probe, final, steps}, null, 2));
        console.log(`evidence: ${output}`);
    } catch (error) {
        if (page) await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
        fs.writeFileSync(path.join(output, 'failure.txt'), `${error.stack}\n\n${steps.join('\n')}\n\nserver stderr:\n${stderr}`);
        console.error(`failure evidence: ${output}`);
        throw error;
    } finally {
        clearTimeout(watchdog);
        if (browser) await browser.close();
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        const done = await Promise.race([exited, new Promise(resolve => setTimeout(() => resolve(null), 5000))]);
        if (!done) server.kill();
        fs.writeFileSync(path.join(output, 'server.log'), stdout + '\n' + stderr);
    }
});
