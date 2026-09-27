/* Organization oversight in the source app, through the real WebSocket: the locked
 * message box, the notice's keyboard path, the signed confirmation that unlocks it,
 * and the notice at 375 px in both themes. Inference is scripted; nothing leaves
 * the loopback (tests/fixtures/oversight_ui_server.py).
 * node tests/oversight_notice.browser.cjs [absolute-path-to-playwright-module]
 * Optional OVERSIGHT_PYTHON selects the Python that runs the fixture.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

const LOCKED = ['#user-input', '#send-btn', '#add-context-btn', '#mic-btn', '#composer-autonomous-btn'];

async function evidence(info) {
    return (await fetch(info.url + '/__fixture__/evidence')).json();
}

// WCAG contrast of an element's text on the nearest opaque background behind it.
function contrastOf(selector) {
    const parse = value => {
        const numbers = (value.match(/[\d.]+/g) || []).map(Number);
        if (value.startsWith('color(')) return {rgb: numbers.slice(0, 3).map(n => n * 255), alpha: numbers[3] ?? 1};
        return {rgb: numbers.slice(0, 3), alpha: numbers[3] ?? 1};
    };
    const luminance = ([r, g, b]) => {
        const channel = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const element = document.querySelector(selector);
    let node = element, background = null;
    while (node && node.nodeType === 1) {
        const color = parse(getComputedStyle(node).backgroundColor);
        if (color.alpha >= 0.99) { background = color.rgb; break; }
        node = node.parentElement;
    }
    background = background || parse(getComputedStyle(document.body).backgroundColor).rgb;
    const text = parse(getComputedStyle(element).color).rgb;
    const [light, dark] = [luminance(text), luminance(background)].sort((a, b) => b - a);
    return Math.round(((light + 0.05) / (dark + 0.05)) * 10) / 10;
}

test('the oversight notice locks the message box until its button confirms it', {timeout: 150000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-oversight-browser-'));
    const server = spawn(process.env.OVERSIGHT_PYTHON || 'python', [path.join(__dirname, 'fixtures/oversight_ui_server.py'), output],
        {cwd: output, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '', stderr = '', info, browser, page;
    server.stdout.on('data', chunk => { stdout += chunk; });
    server.stderr.on('data', chunk => { stderr += chunk; });
    const exited = new Promise(resolve => server.once('exit', (code, signal) => resolve({code, signal})));
    const record = {output};
    try {
        for (let i = 0; i < 300 && !info; i++) {
            const line = stdout.split(/\r?\n/).find(item => item.startsWith('{"url":'));
            if (line) info = JSON.parse(line);
            else if (server.exitCode !== null) throw Error('Fixture server failed: ' + stderr);
            else await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.ok(info, 'Fixture server ready metadata missing: ' + stderr);
        browser = await chromium.launch(process.env.OVERSIGHT_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.OVERSIGHT_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
        page = await browser.newPage({viewport: {width: 1180, height: 860}});
        page.setDefaultTimeout(15000);
        const errors = [], sent = [], received = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('websocket', socket => {
            socket.on('framesent', frame => { try { sent.push(JSON.parse(frame.payload)); } catch {} });
            socket.on('framereceived', frame => { try { received.push(JSON.parse(frame.payload)); } catch {} });
        });
        await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await page.goto((await (await fetch(info.url + '/__fixture__/launch')).json()).url);
        await page.waitForFunction(() => window.app?.oversightStatus?.required === true);

        // Locked: the notice, why, and its button; the message box and its controls disabled.
        await page.locator('#oversight-notice').waitFor({state: 'visible'});
        assert.equal(await page.locator('#oversight-notice-confirm').isVisible(), true);
        assert.match(await page.locator('#oversight-notice-lock').innerText(), /won’t send anything to a model until you confirm/);
        const shown = await page.locator('#oversight-notice-text').innerText();
        assert.match(shown, /^Acme receives your sessions and what they did/);
        for (const selector of LOCKED) assert.equal(await page.locator(selector).isDisabled(), true, selector);
        assert.equal(await page.locator('#user-input').getAttribute('placeholder'), 'Confirm the notice above to start');
        await page.screenshot({path: path.join(output, 'locked-desktop.png')});

        // Nothing gets through: the page's own send, and a raw socket message the server refuses.
        const before = sent.length;
        await page.evaluate(() => { app.userInput.value = 'sneak past the lock'; app.sendMessage(); });
        assert.equal(sent.slice(before).filter(message => message.command === 'message').length, 0);
        await page.evaluate(() => app.send({command: 'message', text: 'a raw socket message'}));
        await page.waitForFunction(() => true);
        for (let i = 0; i < 50 && !received.some(event => event.code === 'oversight_notice'); i++) await page.waitForTimeout(100);
        const refusal = received.find(event => event.code === 'oversight_notice');
        assert.ok(refusal && /oversight notice/.test(refusal.message), 'the server refuses the message');
        assert.deepEqual((await evidence(info)).requests, [], 'nothing reached the model while locked');
        // Said where the notice is, not as a failed turn to retry.
        assert.equal(await page.locator('.error-block, .task-card').count(), 0);
        await page.evaluate(() => { app.userInput.value = ''; });

        // Keyboard: Shift+Tab from the permission mode reaches I've read this, with a visible focus ring; so
        // does Tab from What's shared. Focus is never put on the button by the page itself.
        assert.notEqual(await page.evaluate(() => document.activeElement?.id), 'oversight-notice-confirm');
        await page.locator('#permission-toggle').focus();
        await page.keyboard.press('Shift+Tab');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'oversight-notice-confirm');
        const ring = await page.evaluate(() => {
            const style = getComputedStyle(document.activeElement);
            return {style: style.outlineStyle, width: style.outlineWidth, visible: document.activeElement.matches(':focus-visible')};
        });
        assert.deepEqual(ring, {style: 'solid', width: '2px', visible: true});
        await page.locator('#oversight-notice-details').focus();
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'oversight-notice-confirm');
        record.confirmContrastDark = await page.evaluate(contrastOf, '#oversight-notice-confirm');
        await page.screenshot({path: path.join(output, 'locked-focus.png')});

        // Enter confirms it: the page sends the fingerprint and the text it showed, the box unlocks and takes focus.
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => !document.getElementById('user-input').disabled);
        for (const selector of LOCKED) assert.equal(await page.locator(selector).isDisabled(), false, selector);
        assert.equal(await page.evaluate(() => document.activeElement.id), 'user-input');
        assert.equal(await page.locator('#oversight-notice-confirm').isVisible(), false);
        const confirmation = sent.find(message => message.command === 'oversight_notice_shown');
        assert.equal(confirmation.notice, shown);
        const confirmed = await evidence(info);
        assert.equal(confirmed.notice.record.kind, 'lumi.oversight-acknowledgment/v1');
        assert.equal(confirmed.notice.record.surface, 'app');
        assert.equal(confirmed.notice.record.device_id, 'dev_fixture');
        assert.equal(confirmed.signature_verifies, true, 'signed with the enrolled device key');
        assert.equal(confirmed.upload.state, 'pending', 'queued for Lumi Cloud in the background');
        record.acknowledgment = confirmed.notice.record;

        // Now a message reaches the model and is recorded as the app's.
        await page.locator('#user-input').fill('hello after confirming');
        await page.keyboard.press('Enter');
        let after;
        for (let i = 0; i < 100; i++) {
            after = await evidence(info);
            if (after.records.some(item => item.type === 'turn')) break;
            await page.waitForTimeout(100);
        }
        assert.ok(after.requests.includes('hello after confirming'));
        const turn = after.records.find(item => item.type === 'turn');
        assert.equal(turn.trigger, 'app');
        assert.equal(turn.unattended, false);
        await page.getByText('Scripted reply after the notice.').first().waitFor();

        // 375 px, both themes: forgotten (as on signing out), the notice locks the box again and fits.
        await fetch(info.url + '/__fixture__/forget', {method: 'POST'});
        await page.setViewportSize({width: 375, height: 812});
        await page.goto(info.url + '/');
        await page.waitForFunction(() => window.app?.oversightStatus?.required === true);
        record.compact = {};
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => window.LumiAppearance.setTheme(value), theme);
            await page.locator('#oversight-notice-confirm').scrollIntoViewIfNeeded();
            const layout = await page.evaluate(() => {
                const box = document.getElementById('oversight-notice-confirm').getBoundingClientRect();
                return {scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                    right: box.right, left: box.left, width: window.innerWidth,
                    locked: document.getElementById('user-input').disabled};
            });
            assert.ok(layout.scroll <= 0, `no horizontal scroll in ${theme} (${layout.scroll})`);
            assert.ok(layout.left >= 0 && layout.right <= layout.width, `the button fits in ${theme}`);
            assert.equal(layout.locked, true);
            record.compact[theme] = {
                text: await page.evaluate(contrastOf, '#oversight-notice-text'),
                lock: await page.evaluate(contrastOf, '#oversight-notice-lock'),
                button: await page.evaluate(contrastOf, '#oversight-notice-confirm'),
            };
            for (const [part, ratio] of Object.entries(record.compact[theme])) assert.ok(ratio >= 4.5, `${part} contrast ${ratio} in ${theme}`);
            await page.screenshot({path: path.join(output, `locked-375-${theme}.png`)});
        }
        await page.locator('#oversight-notice-confirm').click();
        await page.waitForFunction(() => !document.getElementById('user-input').disabled);
        assert.deepEqual(errors, []);
        record.ok = true;
    } catch (error) {
        if (page) await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
        fs.writeFileSync(path.join(output, 'failure.txt'), String(error?.stack || error));
        throw error;
    } finally {
        fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(record, null, 2));
        fs.writeFileSync(path.join(output, 'server.log'), stdout + '\n' + stderr);
        if (browser) await browser.close().catch(() => {});
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        const done = await Promise.race([exited, new Promise(resolve => setTimeout(() => resolve(null), 10000))]);
        if (!done) server.kill();
        console.log('evidence folder: ' + output);
    }
});
