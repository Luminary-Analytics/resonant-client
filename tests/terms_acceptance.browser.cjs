/* Lumi's terms in the source app, through the real WebSocket and real browser events: the dialog
 * at first launch, the locked message box, a script's click and a raw socket message that get
 * nothing through, the keyboard path (focus starts in the text, Tab stays in the dialog, Escape
 * declines, Review terms reopens, Enter on Accept accepts), the recorded acceptance, the unlocked
 * box reaching the model, a second window unlocking with the first, a message refused after the
 * acceptance vanished (the running state ends, the text comes back, its card reads Not sent), About
 * Lumi's texts read offline, an organization's machine policy, the dialog at 375 px in both themes,
 * the dialog opening at the top of its text again, a machine policy that can't be read (its error
 * instead of the terms, nothing to accept, nothing recorded),
 * and a model chosen while the terms wait getting no warm-up (a recording Ollama: nothing reaches it
 * until the terms are accepted). Inference is scripted; nothing leaves the loopback
 * (tests/fixtures/terms_ui_server.py).
 * node tests/terms_acceptance.browser.cjs [absolute-path-to-playwright-module]
 * Optional TERMS_PYTHON selects the Python that runs the fixture.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

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

const active = page => page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName);

// Accept from the keyboard: Tab from the text to Accept, then Enter (a trusted key press).
async function acceptWithKeyboard(page) {
    await page.locator('#terms-dialog').waitFor();
    await page.locator('#terms-dialog-body').focus();
    for (let i = 0; i < 8 && (await active(page)) !== 'terms-dialog-accept'; i++) await page.keyboard.press('Tab');
    assert.equal(await active(page), 'terms-dialog-accept');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.app?.termsStatus?.pending === false);
    await page.locator('#terms-dialog').waitFor({state: 'hidden'});
}

// The running state and the last card, as the person sees them.
function turnState() {
    const cards = [...document.querySelectorAll('.task-card')];
    const card = cards[cards.length - 1];
    const stop = document.getElementById('stop-btn');
    return {
        running: Boolean(app.isRunning), stopShown: Boolean(stop && getComputedStyle(stop).display !== 'none'),
        composer: document.getElementById('user-input').value,
        label: card?.querySelector('.task-run-label')?.textContent || '',
        retry: Boolean(card?.querySelector('[data-recovery]')),
    };
}

test('Lumi’s terms lock the app until the dialog’s own Accept, from the keyboard too', {timeout: 150000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-terms-browser-'));
    const server = spawn(process.env.TERMS_PYTHON || 'python', [path.join(__dirname, 'fixtures/terms_ui_server.py'), output],
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
        browser = await chromium.launch(process.env.TERMS_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.TERMS_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
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
        await page.waitForFunction(() => window.app?.termsStatus?.pending === true);

        // First launch: the dialog, with the agreement and (a development build) the test terms.
        const dialog = page.getByRole('dialog', {name: 'Lumi’s terms'});
        await dialog.waitFor();
        const body = page.locator('#terms-dialog-body');
        await body.getByRole('heading', {name: 'Lumi End User License Agreement'}).waitFor();
        await body.getByRole('heading', {name: 'Lumi Alpha and Beta Test Terms'}).waitFor();
        record.consent = await page.locator('#terms-dialog-consent').innerText();
        assert.match(record.consent, /^By choosing Accept, you agree to the Lumi End User License Agreement \(version 1\.0, published .+\) and the Lumi Alpha and Beta Test Terms \(version 1\.0/);
        assert.equal(await active(page), 'terms-dialog-body', 'reading starts in the text, never on Accept');
        assert.equal(await page.locator('#user-input').isDisabled(), true);
        assert.equal(await page.locator('#user-input').getAttribute('placeholder'), 'Accept Lumi’s terms to start');
        await page.screenshot({path: path.join(output, 'first-launch.png')});

        // Nothing gets through: a script's click on Accept, the page's own send, a raw socket message.
        await page.evaluate(() => document.getElementById('terms-dialog-accept').click());
        await page.waitForTimeout(300);
        assert.equal(sent.filter(message => message.command === 'terms_accept').length, 0);
        await page.evaluate(() => { app.userInput.value = 'sneak past the terms'; app.sendMessage(); });
        assert.equal(sent.filter(message => message.command === 'message').length, 0);
        await page.evaluate(() => app.send({command: 'message', text: 'a raw socket message'}));
        for (let i = 0; i < 50 && !received.some(event => event.code === 'terms_not_accepted'); i++) await page.waitForTimeout(100);
        const refusal = received.find(event => event.code === 'terms_not_accepted');
        assert.ok(refusal && /accept its terms/.test(refusal.message), 'the server refuses the message');
        assert.deepEqual((await evidence(info)).requests, [], 'nothing reached the model');
        assert.equal((await evidence(info)).record, null);
        await page.evaluate(() => { app.userInput.value = ''; });

        // A second window of the app, open while the terms wait: it unlocks when the first accepts.
        const second = await browser.newPage({viewport: {width: 900, height: 700}});
        second.on('pageerror', error => errors.push('second window: ' + error.message));
        await second.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await second.goto((await (await fetch(info.url + '/__fixture__/launch')).json()).url);
        await second.waitForFunction(() => window.app?.termsStatus?.pending === true);
        assert.equal(await second.locator('#user-input').isDisabled(), true);

        // Tab stays in the dialog and reaches Accept; Escape declines and focus goes to Review terms.
        const visited = new Set();
        for (let i = 0; i < 8; i++) {
            await page.keyboard.press('Tab');
            visited.add(await active(page));
            assert.equal(await page.evaluate(() => document.getElementById('terms-dialog').contains(document.activeElement)), true,
                `focus left the dialog for ${[...visited].join(', ')}`);
        }
        record.tabStops = [...visited];
        assert.ok(visited.has('terms-dialog-accept') && visited.has('terms-dialog-decline') && visited.has('terms-dialog-privacy'));
        // Read down to the end from the keyboard before closing: the dialog opens at the top again (the re-review).
        await page.locator('#terms-dialog-body').focus();
        await page.keyboard.press('End');
        await page.waitForFunction(() => document.getElementById('terms-dialog-body').scrollTop > 200);
        await page.keyboard.press('Escape');
        await page.locator('#terms-dialog').waitFor({state: 'hidden'});
        assert.equal(await active(page), 'terms-notice-review');
        assert.equal(await page.locator('#terms-notice').isVisible(), true);
        assert.equal(await page.locator('#user-input').isDisabled(), true);
        assert.equal(sent.filter(message => message.command === 'terms_accept').length, 0);

        // Review terms, from the keyboard, opens it again, at the top of the text and with the text
        // focused; the privacy notice is one link away.
        await page.keyboard.press('Enter');
        await dialog.waitFor();
        record.reopened = await page.evaluate(() => ({body: document.getElementById('terms-dialog-body').scrollTop,
            card: document.querySelector('.terms-dialog').scrollTop}));
        assert.deepEqual(record.reopened, {body: 0, card: 0});
        assert.equal(await active(page), 'terms-dialog-body');
        await page.locator('#terms-dialog-privacy').click();
        await body.getByRole('heading', {name: 'Lumi Privacy Notice'}).waitFor();
        await page.locator('#terms-dialog-back').click();
        await body.getByRole('heading', {name: 'Lumi End User License Agreement'}).waitFor();

        // Tab to Accept (with a visible focus ring) and press Enter: that accepts the versions shown,
        // and the box unlocks and takes the focus.
        await page.locator('#terms-dialog-body').focus();
        for (let i = 0; i < 6 && (await active(page)) !== 'terms-dialog-accept'; i++) await page.keyboard.press('Tab');
        assert.equal(await active(page), 'terms-dialog-accept');
        const ring = await page.evaluate(() => {
            const style = getComputedStyle(document.activeElement);
            return {style: style.outlineStyle, width: style.outlineWidth, visible: document.activeElement.matches(':focus-visible')};
        });
        assert.deepEqual(ring, {style: 'solid', width: '2px', visible: true});
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => !document.getElementById('user-input').disabled);
        await page.locator('#terms-dialog').waitFor({state: 'hidden'});
        assert.equal(await active(page), 'user-input');
        assert.equal(await page.locator('#terms-notice').isVisible(), false);
        const accepted = sent.filter(message => message.command === 'terms_accept');
        assert.deepEqual(accepted.map(message => message.documents), [{eula: '1.0', alpha_terms: '1.0'}]);
        const kept = await evidence(info);
        const people = Object.values(kept.record.people);
        assert.equal(people.length, 1);
        assert.deepEqual(Object.keys(people[0]).sort(), ['alpha_terms', 'eula']);
        assert.equal(people[0].eula.surface, 'app');
        assert.match(people[0].eula.sha256, /^[0-9a-f]{64}$/);
        record.acceptance = people[0];
        // The other window unlocked too, without a reload, and its dialog closed.
        await second.waitForFunction(() => window.app?.termsStatus?.pending === false);
        await second.waitForFunction(() => !document.getElementById('user-input').disabled);
        assert.equal(await second.locator('#terms-dialog').isVisible(), false);
        assert.equal(await second.locator('#terms-notice').isVisible(), false);
        await second.close();
        // A run's events go to the newest window (gui/chat_loop.py attach): make this one it again.
        await page.goto((await (await fetch(info.url + '/__fixture__/launch')).json()).url);
        await page.waitForFunction(() => window.app?.termsStatus?.pending === false);
        assert.equal(await page.locator('#terms-dialog').isVisible(), false);

        // Now a message reaches the model.
        await page.locator('#user-input').fill('hello after accepting');
        await page.keyboard.press('Enter');
        await page.getByText('Scripted reply after the terms.').first().waitFor();
        assert.ok((await evidence(info)).requests.includes('hello after accepting'));
        await page.waitForFunction(() => !app.isRunning);

        // The acceptance vanishes on the server (as a new version would) while the page still shows it:
        // the message is refused before any turn starts. The review saw Stop stay, the turn say Failed
        // and the draft lost; now the running state ends, the text comes back and the card reads Not sent.
        await fetch(info.url + '/__fixture__/forget', {method: 'POST'});
        received.length = 0;
        await page.locator('#user-input').fill('a message after the acceptance vanished');
        await page.keyboard.press('Enter');
        for (let i = 0; i < 50 && !received.some(event => event.refused); i++) await page.waitForTimeout(100);
        const refused = received.find(event => event.refused);
        assert.equal(refused?.code, 'terms_not_accepted');
        await page.waitForFunction(() => !app.isRunning);
        record.refusedTurn = await page.evaluate(turnState);
        assert.deepEqual(record.refusedTurn, {running: false, stopShown: false,
            composer: 'a message after the acceptance vanished', label: 'Not sent', retry: false});
        // The terms come back, and accepting them again sends the text that waited in the box.
        await page.locator('#terms-dialog').waitFor();
        assert.ok(!(await evidence(info)).requests.includes('a message after the acceptance vanished'));
        await acceptWithKeyboard(page);
        assert.equal(await active(page), 'user-input');
        assert.equal(await page.locator('#user-input').inputValue(), 'a message after the acceptance vanished');
        await page.keyboard.press('Enter');
        for (let i = 0; i < 80 && !(await evidence(info)).requests.includes('a message after the acceptance vanished'); i++) {
            await page.waitForTimeout(100);
        }
        assert.ok((await evidence(info)).requests.includes('a message after the acceptance vanished'));
        await page.waitForFunction(() => !app.isRunning);

        // About Lumi: who accepted, and every text, read from the copies in the app.
        await page.keyboard.press('Control+Comma');
        await page.locator('#settings-nav-about').click();
        const status = page.locator('#about-terms-status');
        await status.waitFor();
        record.aboutAccepted = await status.innerText();
        assert.match(record.aboutAccepted, /^You accepted the Lumi End User License Agreement 1\.0 on .+ and the Lumi Alpha and Beta Test Terms 1\.0 on .+\.$/);
        await page.locator('#about-legal-privacy').click();
        await page.getByRole('dialog', {name: 'Lumi Privacy Notice'}).waitFor();
        await page.locator('#terms-dialog-body').getByText('Update checks', {exact: true}).waitFor();
        assert.equal(await page.locator('#terms-dialog-accept').isVisible(), false);
        await page.keyboard.press('Escape');
        await page.locator('#terms-dialog').waitFor({state: 'hidden'});
        assert.equal(await active(page), 'about-legal-privacy', 'focus returns to the button that opened it');
        await page.locator('#about-legal-notices').click();
        await page.locator('#terms-dialog-body').getByText('Installed copies of Lumi include the third-party notices').waitFor();
        await page.locator('#terms-dialog-done').click();

        // An organization's machine policy accepts for everyone: no dialog, and About says who.
        await fetch(info.url + '/__fixture__/forget', {method: 'POST'});
        await fetch(info.url + '/__fixture__/organization', {method: 'POST'});
        await page.goto(info.url + '/');
        await page.waitForFunction(() => window.app?.termsStatus && window.app.termsStatus.pending === false);
        assert.equal(await page.locator('#terms-dialog').isVisible(), false);
        assert.equal(await page.locator('#user-input').isDisabled(), false);
        await page.keyboard.press('Control+Comma');
        await page.locator('#settings-nav-about').click();
        record.aboutOrganization = await page.locator('#about-terms-status').innerText();
        assert.equal(record.aboutOrganization, 'Accepted for everyone who uses Lumi on this computer by Acme Corp, through its machine policy.');
        await page.screenshot({path: path.join(output, 'about-organization.png')});

        // 375 px, both themes: no policy and nothing accepted again, so the dialog is back; it fits and reads.
        await fetch(info.url + '/__fixture__/personal', {method: 'POST'});
        await page.setViewportSize({width: 375, height: 812});
        await page.goto(info.url + '/');
        await page.waitForFunction(() => window.app?.termsStatus?.pending === true);
        record.compact = {};
        await dialog.waitFor();
        await body.getByRole('heading', {name: 'Lumi End User License Agreement'}).waitFor();
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => window.LumiAppearance.setTheme(value), theme);
            const layout = await page.evaluate(() => {
                const accept = document.getElementById('terms-dialog-accept').getBoundingClientRect();
                const decline = document.getElementById('terms-dialog-decline').getBoundingClientRect();
                const box = document.querySelector('.terms-dialog').getBoundingClientRect();
                return {scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                    width: window.innerWidth, height: window.innerHeight, box: [box.left, box.right, box.top, box.bottom],
                    accept: [accept.left, accept.right, accept.bottom], decline: [decline.left, decline.right]};
            });
            assert.ok(layout.scroll <= 0, `no horizontal scroll in ${theme} (${layout.scroll})`);
            assert.ok(layout.box[0] >= 0 && layout.box[1] <= layout.width && layout.box[3] <= layout.height, `the dialog fits in ${theme}`);
            assert.ok(layout.accept[0] >= 0 && layout.accept[1] <= layout.width && layout.accept[2] <= layout.height, `Accept is on screen in ${theme}`);
            record.compact[theme] = {
                intro: await page.evaluate(contrastOf, '#terms-dialog-intro'),
                consent: await page.evaluate(contrastOf, '#terms-dialog-consent'),
                body: await page.evaluate(contrastOf, '#terms-dialog-body'),
                decline: await page.evaluate(contrastOf, '#terms-dialog-decline'),
                accept: await page.evaluate(contrastOf, '#terms-dialog-accept'),
                link: await page.evaluate(contrastOf, '#terms-dialog-privacy'),
            };
            for (const [part, ratio] of Object.entries(record.compact[theme])) assert.ok(ratio >= 4.5, `${part} contrast ${ratio} in ${theme}`);
            await page.screenshot({path: path.join(output, `dialog-375-${theme}.png`)});
        }
        await page.keyboard.press('Escape');
        await page.locator('#terms-notice').waitFor();
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => window.LumiAppearance.setTheme(value), theme);
            const fits = await page.evaluate(() => {
                const box = document.getElementById('terms-notice-review').getBoundingClientRect();
                return box.left >= 0 && box.right <= window.innerWidth
                    && document.documentElement.scrollWidth <= document.documentElement.clientWidth;
            });
            assert.ok(fits, `the notice fits in ${theme}`);
            record.compact[`notice-${theme}`] = {
                text: await page.evaluate(contrastOf, '#terms-notice-text'),
                button: await page.evaluate(contrastOf, '#terms-notice-review'),
            };
            for (const [part, ratio] of Object.entries(record.compact[`notice-${theme}`])) assert.ok(ratio >= 4.5, `notice ${part} contrast ${ratio} in ${theme}`);
            await page.screenshot({path: path.join(output, `notice-375-${theme}.png`)});
        }

        // An administrator's policy Lumi can't read (the re-review: a PolicyFile out of reach opened the
        // terms, and accepting recorded a personal acceptance). Its error shows instead, with nothing to
        // accept; the server records nothing and refuses messages with the policy's code.
        record.policyError = await fetch(info.url + '/__fixture__/policy-error', {method: 'POST'}).then(r => r.json());
        received.length = 0;
        await page.goto(info.url + '/');
        await page.waitForFunction(() => Boolean(window.app?.termsStatus?.policy_error));
        await page.locator('#terms-notice').waitFor();
        await page.waitForTimeout(500);
        assert.equal(await page.locator('#terms-dialog').isVisible(), false, 'no terms dialog');
        assert.equal(await page.locator('#terms-notice-text').innerText(), record.policyError.error);
        assert.equal(await page.locator('#terms-notice-review').isVisible(), false);
        // The message box stays open, as for the policy's other refusals.
        assert.equal(await page.locator('#user-input').isDisabled(), false);
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => window.LumiAppearance.setTheme(value), theme);
            const fits = await page.evaluate(() => {
                const box = document.getElementById('terms-notice').getBoundingClientRect();
                return box.left >= 0 && box.right <= window.innerWidth
                    && document.documentElement.scrollWidth <= document.documentElement.clientWidth;
            });
            assert.ok(fits, `the policy notice fits in ${theme}`);
            record.compact[`policy-${theme}`] = await page.evaluate(contrastOf, '#terms-notice-text');
            assert.ok(record.compact[`policy-${theme}`] >= 4.5, `policy notice contrast in ${theme}`);
            await page.screenshot({path: path.join(output, `policy-error-375-${theme}.png`)});
        }
        // A hand-made acceptance gets the policy's answer; a message typed and sent is refused with it,
        // leaves the running state and comes back to the box. Nothing is recorded or sent.
        await page.evaluate(() => app.send({command: 'terms_accept', documents: {eula: '1.0', alpha_terms: '1.0'}}));
        await page.locator('#user-input').fill('a message under a broken policy');
        await page.keyboard.press('Enter');
        for (let i = 0; i < 50 && received.filter(event => event.code === 'policy_blocked').length < 2; i++) await page.waitForTimeout(100);
        record.policyRefusals = received.filter(event => event.code === 'policy_blocked').map(event => event.message);
        assert.equal(record.policyRefusals.length, 2);
        assert.match(record.policyRefusals[0], /records no acceptance/);
        await page.waitForFunction(() => !app.isRunning);
        record.policyTurn = await page.evaluate(turnState);
        assert.deepEqual(record.policyTurn, {running: false, stopShown: false,
            composer: 'a message under a broken policy', label: 'Not sent', retry: false});
        await page.locator('#user-input').fill('');
        const underPolicy = await evidence(info);
        assert.equal(underPolicy.record, null);
        assert.ok(!underPolicy.requests.includes('a message under a broken policy'));
        // Readable again, the policy doesn't accept for anyone here: the person is asked, as before.
        await fetch(info.url + '/__fixture__/personal', {method: 'POST'});
        await page.goto(info.url + '/');
        await page.waitForFunction(() => window.app?.termsStatus?.pending === true && !window.app.termsStatus.policy_error);
        await dialog.waitFor();
        assert.equal(await page.locator('#terms-dialog-accept').isVisible(), true);

        // Choosing a model while the terms wait, as the setup screen's row does: no warm-up reaches it (the
        // review saw Ollama's "hi" leave). Once they're accepted, choosing it warms it up.
        await fetch(info.url + '/__fixture__/clear', {method: 'POST'});
        received.length = 0;
        await page.evaluate(model => app.selectBackend('ollama', model), info.ollama_model);
        for (let i = 0; i < 50 && !received.some(event => event.event === 'status_msg'); i++) await page.waitForTimeout(100);
        await page.waitForTimeout(1500);
        const chosen = await evidence(info);
        record.pendingModelChoice = {backend: chosen.backend, ollama: chosen.ollama_requests,
            warmups: received.filter(event => String(event.event || '').startsWith('model_warmup')).map(event => event.event)};
        assert.equal(chosen.backend, `ollama:${info.ollama_model}`);
        assert.deepEqual(chosen.ollama_model_requests, [], 'nothing reached the model before the terms');
        assert.deepEqual(record.pendingModelChoice.warmups, []);
        await page.keyboard.press('Escape');  // the dialog came back when the page reconnected
        await page.locator('#terms-notice-review').click();
        await acceptWithKeyboard(page);
        await page.evaluate(model => app.selectBackend('ollama', model), info.ollama_model);
        for (let i = 0; i < 80 && !(await evidence(info)).ollama_model_requests.length; i++) await page.waitForTimeout(100);
        const warmed = await evidence(info);
        record.acceptedModelChoice = warmed.ollama_model_requests.map(body => body.messages);
        assert.deepEqual(record.acceptedModelChoice, [[{role: 'user', content: 'hi'}]], 'the warm-up, once accepted');
        await fetch(info.url + '/__fixture__/scripted', {method: 'POST'});

        // At 375 px in both themes, a refused message's card fits and reads.
        await fetch(info.url + '/__fixture__/forget', {method: 'POST'});
        received.length = 0;
        await page.locator('#user-input').fill('refused at 375 px');
        await page.keyboard.press('Enter');
        for (let i = 0; i < 50 && !received.some(event => event.refused); i++) await page.waitForTimeout(100);
        await page.waitForFunction(() => !app.isRunning);
        assert.equal((await page.evaluate(turnState)).label, 'Not sent');
        await page.keyboard.press('Escape');
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => window.LumiAppearance.setTheme(value), theme);
            const card = await page.evaluate(() => {
                const cards = [...document.querySelectorAll('.task-card')];
                const box = cards[cards.length - 1].getBoundingClientRect();
                return {left: box.left, right: box.right, width: window.innerWidth,
                    scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth};
            });
            assert.ok(card.left >= 0 && card.right <= card.width && card.scroll <= 0, `the card fits in ${theme}`);
            await page.evaluate(() => {
                const cards = [...document.querySelectorAll('.task-card')];
                cards[cards.length - 1].querySelector('.task-run-label').id = 'refused-label';
                cards[cards.length - 1].querySelector('.task-run-detail').id = 'refused-detail';
            });
            record.compact[`refused-${theme}`] = {label: await page.evaluate(contrastOf, '#refused-label'),
                detail: await page.evaluate(contrastOf, '#refused-detail')};
            for (const [part, ratio] of Object.entries(record.compact[`refused-${theme}`])) assert.ok(ratio >= 4.5, `refused ${part} contrast ${ratio} in ${theme}`);
            await page.screenshot({path: path.join(output, `refused-375-${theme}.png`)});
        }
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
