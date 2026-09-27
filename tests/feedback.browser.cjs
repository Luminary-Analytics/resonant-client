/* Send feedback in a real browser (headless Edge): the source app, a real WebSocket and
 * lumi/feedback.py, against a fake Lumi Cloud on loopback; no model or real Lumi Cloud.
 *
 *   node tests/feedback.browser.cjs [absolute-path-to-playwright-module]
 *
 * Run node with LUMI_KEYCHAIN=off and the ordinary USERPROFILE: the fixture
 * server (tests/fixtures/feedback_ui_server.py) isolates its own home, and Edge
 * doesn't start with a temporary one. FEEDBACK_PYTHON and
 * FEEDBACK_BROWSER_EXECUTABLE choose other runtimes. Screenshots and the
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
const post = async (info, route, body = {}) => (await fetch(info.url + route, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
const focusedId = page => page.evaluate(() => {
    const node = document.activeElement;
    return node.id || node.getAttribute('aria-label') || node.tagName;
});

/** The dialog shows a report Send would send: current, not one being updated. */
const reportShown = page => page.waitForFunction(() => window.app._feedbackPreviewId
    && !document.querySelector('#feedback-preview-body').classList.contains('is-stale')
    && document.querySelector('#feedback-preview-body').textContent.includes('"log_tail"'));

/** WCAG contrast of an element's text against its background: the dialog's, or ``on``'s. */
const contrast = (page, selector, on = '.feedback-dialog') => page.evaluate(([target, surface]) => {
    const rgb = value => value.match(/[\d.]+/g).slice(0, 3).map(Number);
    const luminance = ([r, g, b]) => {
        const [R, G, B] = [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
        return 0.2126 * R + 0.7152 * G + 0.0722 * B;
    };
    const text = luminance(rgb(getComputedStyle(document.querySelector(target)).color));
    const back = luminance(rgb(getComputedStyle(document.querySelector(surface)).backgroundColor));
    return (Math.max(text, back) + 0.05) / (Math.min(text, back) + 0.05);
}, [selector, on]);

/** Switch the theme and wait out its color transitions. */
const setTheme = async (page, theme) => {
    await page.evaluate(name => window.LumiAppearance.setTheme(name), theme);
    await page.waitForFunction(name => (document.documentElement.dataset.theme || 'dark') === name, theme);  // dark has none
    await page.waitForTimeout(400);
};

test('Send feedback: opening it, checking it, what it shows, what goes and what waits', {timeout: 180000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-feedback-browser-'));
    const server = spawn(process.env.FEEDBACK_PYTHON || 'python',
        [path.join(__dirname, 'fixtures/feedback_ui_server.py'), output],
        {cwd: output, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
            env: {...process.env, LUMI_KEYCHAIN: 'off', PYTHONDONTWRITEBYTECODE: '1'}});
    let stdout = '', stderr = '', info, browser, page;
    server.stdout.on('data', chunk => { stdout += chunk; });
    server.stderr.on('data', chunk => { stderr += chunk; });
    const exited = new Promise(resolve => server.once('exit', (code, signal) => resolve({code, signal})));
    const steps = [];
    const step = label => steps.push(`${new Date().toISOString()} ${label}`);
    const watchdog = setTimeout(async () => {
        fs.writeFileSync(path.join(output, 'failure.txt'),
            `Stuck after: ${steps.at(-1)}\n\n${steps.join('\n')}\n\nserver stderr:\n${stderr}`);
        if (page) await page.screenshot({path: path.join(output, 'stuck.png')}).catch(() => {});
        console.error(`stuck; evidence: ${output}`);
        server.kill();
        process.exit(1);
    }, 170000);
    try {
        for (let i = 0; i < 300 && !info; i++) {
            const line = stdout.split(/\r?\n/).find(row => row.startsWith('{"url":'));
            if (line) info = JSON.parse(line);
            else if (server.exitCode !== null) throw Error('Fixture server failed: ' + stderr);
            else await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.ok(info, 'Fixture server ready metadata missing: ' + stderr);
        const {token, saved_key: savedKey, home} = await evidence(info);
        browser = await chromium.launch(process.env.FEEDBACK_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.FEEDBACK_BROWSER_EXECUTABLE}
            : {headless: true, channel: 'msedge'});
        const context = await browser.newContext({viewport: {width: 1280, height: 860}});
        await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: info.url});
        page = await context.newPage();
        page.setDefaultTimeout(15000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        // Nothing may leave this machine; loopback (the app and its fake Lumi Cloud) continues.
        await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)
            ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);
        const dialog = page.getByRole('dialog', {name: 'Send feedback'});

        step('Help > Send Feedback…, with the pointer');
        await page.locator('.titlebar-menu-button').click();
        await page.locator('.menubar-item[data-menu="help"]').hover();
        await page.locator('.menubar-action[data-action="send-feedback"]').click();
        await dialog.waitFor();
        assert.equal(await focusedId(page), 'feedback-message');
        await page.locator('#feedback-destination', {hasText: /^It goes to 127\.0\.0\.1:\d+, without your account\.$/}).waitFor();
        assert.match(await page.locator('#feedback-always').textContent(),
            /^Always sent: Lumi \S+ \((stable|beta)\), .+, and a random id for this install\.$/);
        assert.equal(await page.locator('#feedback-preview').isVisible(), false);
        await page.screenshot({path: path.join(output, 'feedback-dark.png')});

        step('The form is checked from the keyboard before anything is sent');
        await page.keyboard.press('Control+Enter');
        const messageError = page.locator('#feedback-message-error');
        await messageError.waitFor();
        assert.match(await messageError.textContent(), /Write what happened/);
        assert.equal(await page.locator('#feedback-message').getAttribute('aria-invalid'), 'true');
        assert.equal(await focusedId(page), 'feedback-message');
        await page.keyboard.type(`The build button does nothing. ${token}`);
        await messageError.waitFor({state: 'hidden'});
        assert.match(await page.locator('#feedback-message-count').textContent(), /^\d+ \/ 5,000$/);
        await page.keyboard.press('Tab');
        assert.equal(await focusedId(page), 'feedback-reply-to');
        await page.keyboard.type('ada@example');
        await page.keyboard.press('Enter');
        const replyError = page.locator('#feedback-reply-to-error');
        await replyError.waitFor();
        assert.match(await replyError.textContent(), /Enter an email address such as you@example\.com/);
        assert.equal(await focusedId(page), 'feedback-reply-to');
        await page.keyboard.type('.com');
        await replyError.waitFor({state: 'hidden'});
        assert.equal((await evidence(info)).received.length, 0, 'nothing was sent while the form was wrong');

        step('Include diagnostics: the report exactly as it will be sent');
        await page.keyboard.press('Tab');
        assert.equal(await focusedId(page), 'feedback-diagnostics');
        await page.keyboard.press('Space');
        const previewBody = page.locator('#feedback-preview-body');
        await reportShown(page);
        const shownText = await previewBody.textContent();
        const shown = JSON.parse(shownText);
        assert.deepEqual(Object.keys(shown).sort(), ['app', 'diagnostics', 'install_id', 'kind', 'message', 'reply_to']);
        assert.equal(shown.message, 'The build button does nothing. [REDACTED GitHub token]');
        assert.equal(shown.reply_to, 'ada@example.com');
        assert.equal(shown.kind, 'bug');
        assert.deepEqual([shown.diagnostics.provider, shown.diagnostics.model, shown.diagnostics.offline_mode],
            ['ollama', 'fixture-native', false]);
        for (const secret of [token, savedKey, home]) assert.ok(!shownText.includes(secret), 'the preview holds a secret or the home folder');
        assert.match(shown.diagnostics.log_tail, /~\\lumi\\gui\\app\.py/);
        assert.match(await page.locator('#feedback-preview-meta').textContent(), /^To 127\.0\.0\.1:\d+, without your account\.$/);
        assert.match(await page.locator('#feedback-preview-notices').textContent(), /^Removed 1 secret \(GitHub token\) from the report\./);
        await page.keyboard.press('Tab');
        assert.equal(await focusedId(page), 'feedback-preview-body', 'the report scrolls from the keyboard');
        // Readable in both themes: the report on its own surface, what's said about it on the dialog's.
        const reportRatios = async () => [await contrast(page, '#feedback-preview-body', '#feedback-preview-body'),
            await contrast(page, '#feedback-preview-meta'), await contrast(page, '#feedback-preview-notices')];
        const reportDark = await reportRatios();
        await setTheme(page, 'light');
        const reportLight = await reportRatios();
        await page.screenshot({path: path.join(output, 'feedback-report-light.png')});
        await setTheme(page, 'dark');
        for (const ratio of [...reportDark, ...reportLight]) assert.ok(ratio >= 4.5, `contrast ${ratio.toFixed(2)}:1`);

        step('Send sends that report');
        await page.locator('#feedback-send').focus();
        await page.keyboard.press('Enter');
        const done = page.locator('#feedback-done-text');
        await done.waitFor();
        assert.match(await done.textContent(), /^Thanks\. Your feedback is in Lumi Cloud \(reference fbk_0001\)\.$/);
        assert.equal(await focusedId(page), 'feedback-done-close');
        let seen = await evidence(info);
        assert.equal(seen.received.length, 1);
        assert.deepEqual(seen.received[0].body, shown, 'what went is what the dialog showed');
        assert.equal(seen.received[0].authorization, false, 'no account token while signed out');
        assert.equal(seen.received[0].content_type, 'application/json');
        assert.deepEqual(seen.audit.map(record => [record.type, record.data.kind, record.data.diagnostics, record.data.queued]),
            [['feedback.sent', 'bug', true, false]]);
        for (const text of ['The build button does nothing', 'ada@example.com', shown.install_id]) {
            assert.ok(!seen.audit_text.includes(text), `the audit log holds ${text}`);
        }

        step('Escape closes; focus returns to the Menu button');
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'hidden'});
        assert.equal(await focusedId(page), 'Menu');

        step('From the command palette, with Lumi Cloud down: the report waits on this computer');
        await post(info, '/__fixture__/cloud-mode', {mode: 'down'});
        await page.locator('#user-input').focus();
        await page.keyboard.press('Control+k');
        await page.locator('#cmd-palette-input').fill('feedback');
        await page.locator('.cmd-palette-item', {hasText: 'Send feedback'}).waitFor();
        await page.keyboard.press('Enter');
        await dialog.waitFor();
        assert.equal(await focusedId(page), 'feedback-message');
        assert.equal(await page.locator('#feedback-message').inputValue(), '', 'a sent report leaves an empty form');
        assert.equal(await page.locator('#feedback-diagnostics').isChecked(), false);
        await page.keyboard.type('Second report, while Lumi Cloud is down.');
        await page.keyboard.press('Control+Enter');
        await page.locator('#feedback-done-text', {hasText: "couldn't be reached"}).waitFor();
        assert.match(await done.textContent(), /saved on this computer\. Lumi will send it when it can\./);
        seen = await evidence(info);
        assert.deepEqual(seen.queue.map(item => [item.reason, item.body.message]),
            [['unreachable', 'Second report, while Lumi Cloud is down.']]);
        assert.equal(seen.received.at(-1).status, 503);
        assert.equal(seen.audit.at(-1).type, 'feedback.queued');
        await page.keyboard.press('Enter');  // Done
        await dialog.waitFor({state: 'hidden'});
        assert.equal(await focusedId(page), 'user-input');

        step('From the profile menu, by keyboard: Send now');
        await post(info, '/__fixture__/cloud-mode', {mode: 'up'});
        await page.locator('#sidebar-account').focus();
        await page.keyboard.press('Enter');
        for (let i = 0; i < 8 && await focusedId(page) !== 'account-feedback'; i++) await page.keyboard.press('ArrowDown');
        assert.equal(await focusedId(page), 'account-feedback');
        await page.keyboard.press('Enter');
        await dialog.waitFor();
        await page.locator('#feedback-queue-text', {hasText: '1 report waiting on this computer.'}).waitFor();
        await page.locator('#feedback-flush').focus();
        await page.keyboard.press('Enter');
        await page.locator('#feedback-progress', {hasText: 'Sent 1 report.'}).waitFor();
        await page.locator('#feedback-queue').waitFor({state: 'hidden'});
        seen = await evidence(info);
        assert.deepEqual(seen.queue, []);
        assert.deepEqual([seen.received.at(-1).status, seen.received.at(-1).body.message],
            [201, 'Second report, while Lumi Cloud is down.']);
        assert.deepEqual([seen.audit.at(-1).type, seen.audit.at(-1).data.queued], ['feedback.sent', true]);
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'hidden'});
        assert.equal(await focusedId(page), 'sidebar-account');

        step('Offline mode refuses, says why, and offers the report to copy');
        await post(info, '/__fixture__/cloud-url', {url: 'https://cloud.example.test'});
        await post(info, '/__fixture__/offline', {enabled: true});
        await page.keyboard.press('Control+Comma');
        const about = page.locator('#settings-nav-about');
        await about.waitFor();
        await about.focus();
        await page.keyboard.press('Enter');
        const aboutButton = page.locator('#about-send-feedback');
        await aboutButton.waitFor();
        await aboutButton.focus();
        await page.keyboard.press('Enter');
        await dialog.waitFor();
        await page.locator('#feedback-destination', {hasText: 'Offline mode: sending feedback needs cloud.example.test'}).waitFor();
        await page.keyboard.type('Offline, and this can’t go.');
        const before = (await evidence(info)).received.length;
        await page.keyboard.press('Control+Enter');
        const alert = page.locator('#feedback-alert');
        await alert.waitFor();
        assert.match(await alert.textContent(), /^Offline mode: sending feedback needs cloud\.example\.test; allow it or turn offline mode off\.$/);
        assert.equal(await focusedId(page), 'feedback-copy');
        await page.keyboard.press('Enter');
        await page.locator('#feedback-progress', {hasText: /Copied\.|Select the text/}).waitFor();
        // Windows' clipboard gives text back with CRLF line breaks.
        const copied = (await page.locator('#feedback-copy-text').isVisible()
            ? await page.locator('#feedback-copy-text').inputValue()
            : await page.evaluate(() => navigator.clipboard.readText())).split('\r\n').join('\n');
        assert.match(copied, /^Lumi feedback: Bug\n\nOffline, and this can’t go\.\n/);
        assert.match(copied, /\nReply to: ada@example\.com\n/);
        seen = await evidence(info);
        assert.equal(seen.received.length, before, 'offline mode sent nothing');
        assert.deepEqual([seen.audit.at(-1).type, seen.audit.at(-1).data.reason], ['feedback.refused', 'offline']);

        step('Offline, diagnostics aren\'t even gathered: the refusal comes first, readable in both themes');
        await page.locator('#feedback-diagnostics').check();
        // The report is never prepared: no empty box to tab to, and the refusal comes back instead.
        await page.waitForFunction(() => !document.querySelector('#feedback-alert').hidden
            && document.querySelector('#feedback-preview-meta').textContent.startsWith('Nothing to show'));
        assert.match(await alert.textContent(), /^Offline mode: sending feedback needs cloud\.example\.test/);
        assert.equal(await page.locator('#feedback-preview-body').textContent(), '');
        assert.equal(await page.locator('#feedback-preview-body').isVisible(), false);
        assert.equal(await page.evaluate(() => window.app._feedbackPreviewId), null);
        await page.locator('#feedback-copy').waitFor();
        seen = await evidence(info);
        assert.equal(seen.received.length, before, 'offline mode sent nothing');
        assert.deepEqual(seen.audit.map(record => [record.type, record.data.reason ?? '']),
            [['feedback.sent', ''], ['feedback.queued', 'unreachable'], ['feedback.sent', ''], ['feedback.refused', 'offline']],
            'one refusal, for Send; nothing for the report that was never prepared');
        const refusalRatios = async () => [await contrast(page, '#feedback-alert'), await contrast(page, '.feedback-count'),
            await contrast(page, '#feedback-destination')];
        const darkRatios = await refusalRatios();
        await page.screenshot({path: path.join(output, 'feedback-refused-dark.png')});
        await setTheme(page, 'light');
        const lightRatios = await refusalRatios();
        await page.screenshot({path: path.join(output, 'feedback-refused-light.png')});
        for (const ratio of [...darkRatios, ...lightRatios]) assert.ok(ratio >= 4.5, `contrast ${ratio.toFixed(2)}:1`);
        await setTheme(page, 'dark');

        step('375 px wide: no sideways scrolling, and the dialog fits');
        await page.setViewportSize({width: 375, height: 740});
        await page.waitForTimeout(200);
        const fit = await page.evaluate(() => {
            const box = document.querySelector('.feedback-dialog').getBoundingClientRect();
            const pre = document.querySelector('#feedback-preview-body').getBoundingClientRect();
            return {page: document.documentElement.scrollWidth, width: window.innerWidth, left: box.left, right: box.right,
                top: box.top, bottom: box.bottom, height: window.innerHeight, preRight: pre.right};
        });
        assert.ok(fit.page <= fit.width, `the page scrolls sideways: ${JSON.stringify(fit)}`);
        assert.ok(fit.left >= 0 && fit.right <= 375.5 && fit.top >= 0 && fit.bottom <= 740.5, JSON.stringify(fit));
        assert.ok(fit.preRight <= fit.right, JSON.stringify(fit));
        for (const id of ['feedback-dialog-close', 'feedback-copy', 'feedback-cancel', 'feedback-send']) {
            const box = await page.locator(`#${id}`).boundingBox();
            assert.ok(box.width >= 24 && box.height >= 24, `${id} is a small target: ${JSON.stringify(box)}`);
        }
        await page.screenshot({path: path.join(output, 'feedback-375.png')});
        // Tab stays in the dialog.
        await page.locator('#feedback-send').focus();
        await page.keyboard.press('Tab');
        assert.equal(await focusedId(page), 'feedback-dialog-close');
        await page.keyboard.press('Shift+Tab');
        assert.equal(await focusedId(page), 'feedback-send');
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'hidden'});
        assert.equal(await focusedId(page), 'about-send-feedback');
        await post(info, '/__fixture__/offline', {enabled: false});

        step('No page errors, and nothing but the reports left the app');
        const final = await evidence(info);
        assert.equal(final.live_cloud_called, false);
        assert.equal(final.received.length, 3);
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output, 'evidence.json'), JSON.stringify({final, steps}, null, 2));
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
