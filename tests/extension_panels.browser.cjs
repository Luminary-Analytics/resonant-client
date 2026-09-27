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

test('A capability pack panel runs sandboxed and reaches the app only through the bridge', {timeout: 120000}, async () => {
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
    }, 110000);
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
        // Nothing may leave this machine; loopback (the app and the canary) continues.
        await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)
            ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);
        await page.locator('#user-input').fill('Existing draft');

        // ── View > Panels, with the pointer ──────────────────────────────
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
        assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Close Build stats');
        const {handle, frame} = await panelFrame(page);
        assert.equal(await handle.getAttribute('sandbox'), 'allow-scripts');
        assert.equal(await handle.getAttribute('title'), 'Build stats, a panel from the Panel demo pack');
        const token = (await handle.getAttribute('src')).split('/')[2];
        assert.match(token, /^[A-Za-z0-9_-]{32}$/);
        await page.screenshot({path: path.join(output, 'panel-dark.png')});

        // ── What the panel could do from inside its sandbox ────────────────
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
        assert.deepEqual(JSON.parse(probe.context.replace(/^reached: /, '')),
            {project: 'project', session: 'Panel fixture conversation', theme: 'dark'});
        assert.equal(await frame.locator('#context').textContent(), 'project / Panel fixture conversation / dark');
        // The panel's own path serves its files; the bridge was added first.
        assert.ok(await frame.evaluate(() => typeof window.lumi.insert === 'function'));
        assert.equal(await frame.evaluate(() => document.documentElement.dataset.lumiTheme), 'dark');

        // ── Adding to the message: into the draft, never sent ──────────────
        step('Adding to the message: into the draft, never sent');
        const sentBefore = commands.filter(command => ['chat', 'send_message', 'user_message'].includes(command)).length;
        await frame.locator('#insert').click();
        await frame.waitForFunction(() => window.insertResult === 'ok');
        assert.equal(await page.locator('#user-input').inputValue(),
            'Existing draft\nSummarize the failing builds from the panel.');
        await page.locator('#ui-toast-message', {hasText: 'Build stats added text to your message'}).waitFor();
        await frame.locator('#toast').click();
        await frame.waitForFunction(() => window.toastResult === 'ok');
        await page.locator('#ui-toast-message', {hasText: 'Build stats: Saved the build filter'}).waitFor();
        assert.equal(commands.filter(command => ['chat', 'send_message', 'user_message'].includes(command)).length,
            sentBefore, 'the panel sent no message');

        // ── Both themes: the dialog's tokens, and the panel is told ─────────
        step("Both themes: the dialog's tokens, and the panel is told");
        const darkBackground = await page.locator('.extension-panel-dialog').evaluate(node => getComputedStyle(node).backgroundColor);
        await page.evaluate(() => window.LumiAppearance.setTheme('light'));
        await frame.waitForFunction(() => document.documentElement.dataset.lumiTheme === 'light');
        assert.equal(await frame.locator('#context').textContent(), 'project / Panel fixture conversation / light');
        const lightBackground = await page.locator('.extension-panel-dialog').evaluate(node => getComputedStyle(node).backgroundColor);
        assert.notEqual(lightBackground, darkBackground);
        await page.screenshot({path: path.join(output, 'panel-light.png')});
        await page.evaluate(() => window.LumiAppearance.setTheme('dark'));

        // ── Keyboard: Tab stays in the dialog; Escape closes, focus returns ──
        step('Keyboard: Tab stays in the dialog; Escape closes, focus returns');
        await page.getByRole('button', {name: 'Close Build stats'}).focus();
        await page.keyboard.press('Tab');                                // into the panel: its first button
        assert.equal(await page.evaluate(() => document.activeElement.tagName), 'IFRAME');
        assert.equal(await frame.evaluate(() => document.activeElement.id), 'insert');
        // Past the panel's last tab stop (the probe added a frame after the buttons), focus comes back to Close.
        for (let tries = 0; tries < 10 && await page.evaluate(() => document.activeElement.tagName) === 'IFRAME'; tries++) {
            await page.keyboard.press('Tab');
        }
        assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Close Build stats',
            await page.evaluate(() => document.activeElement.outerHTML.slice(0, 200)));
        await page.keyboard.press('Shift+Tab');                          // and backwards, into the panel
        assert.equal(await page.evaluate(() => document.activeElement.tagName), 'IFRAME');
        await page.getByRole('button', {name: 'Close Build stats'}).focus();
        step('keyboard: Escape on Close');
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'detached'});
        step('keyboard: closed');
        // Opened from the menu, whose items take no focus: focus goes back to the message box.
        assert.equal(await page.evaluate(() => document.activeElement.id), 'user-input');
        assert.equal((await fetch(`${info.url}/panels/${token}/index.html`)).status, 404, 'closing withdrew the token');

        // From the command palette; Escape pressed inside the panel closes it too.
        step('palette: open again');
        await openFromPalette(page);
        const again = await panelFrame(page);
        step('palette: Escape inside the panel');
        await again.frame.locator('#toast').focus();
        assert.equal(await page.evaluate(() => document.activeElement.tagName), 'IFRAME');
        await page.keyboard.press('Escape');
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        assert.equal(await page.evaluate(() => document.activeElement.id), 'user-input');

        // ── A panel URL outside the frame Lumi made ──────────────────────
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

        // ── It can't navigate itself to another site ───────────────────────
        step("It can't navigate itself to another site");
        await opened.frame.locator('#navigate').click();
        await page.waitForTimeout(1000);
        const canaryAfterNavigation = (await evidence(info)).canary_hits;
        assert.deepEqual(canaryAfterNavigation, [], 'navigating the frame away reached the network');

        // ── Revoked in Settings: the open panel closes, its files are gone ──
        step('Revoked in Settings: the open panel closes, its files are gone');
        const revoked = await (await fetch(info.url + '/__fixture__/revoke', {method: 'POST'})).json();
        assert.equal(revoked.status, 'needs_approval');
        await page.evaluate(() => window.dispatchEvent(new Event('focus')));
        await page.getByRole('dialog', {name: 'Build stats'}).waitFor({state: 'detached'});
        await page.locator('#ui-toast-message', {hasText: 'Build stats closed.'}).waitFor();
        assert.notEqual((await fetch(`${info.url}/panels/${liveToken}/index.html`)).status, 200);
        await page.locator('.titlebar-menu-button').click();
        await page.locator('.menubar-item[data-menu="view"]').hover();
        await page.waitForFunction(() => Array.isArray(window.app._extensionPanels) && !window.app._extensionPanels.length);
        assert.equal(await page.locator('.extension-panel-menu-item').count(), 0);
        await page.keyboard.press('Escape');

        // ── Compact width ──────────────────────────────────────────────────
        step('Compact width');
        await page.setViewportSize({width: 390, height: 844});
        // Approved again, as Settings > Capability packs would.
        const reapproved = await (await fetch(info.url + '/__fixture__/approve', {method: 'POST'})).json();
        assert.equal(reapproved.status, 'approved');
        await page.evaluate(() => window.app._requestExtensionPanels(true));
        await page.waitForFunction(() => window.app._extensionPanels?.length === 1);
        await page.evaluate(() => window.app.openExtensionPanel('panel-demo', 'build-stats'));
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

        // Nothing got out, from any of it; the page itself raised no errors.
        const final = await evidence(info);
        assert.deepEqual(final.canary_hits, [], 'a request reached the canary');
        assert.equal(final.live_providers_called, false);
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output, 'evidence.json'), JSON.stringify({probe, final}, null, 2));
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
