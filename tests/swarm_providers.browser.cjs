/* Which models a team runs on, in the full source app's Team panel: real WebSocket, runtime and store.
 * A Codex conversation's panel says a team can't run on Codex, names the models a team runs on
 * and keeps Start unavailable; an Anthropic conversation's panel offers workers only models a
 * team runs on (never Codex, Claude Code, a connection that signs in, or Claude on Bedrock
 * without its Bedrock API key).
 * node tests/swarm_providers.browser.cjs [absolute-path-to-playwright-module]
 * Optional SWARM_PYTHON and SWARM_BROWSER_EXECUTABLE select local runtimes.
 */
// The app page needs a one-time launch code (lumi/gui/local_access.py); the
// fixture server mints one per page load.
const fixtureLaunch=async info=>(await (await fetch(info.url+'/__fixture__/launch')).json()).url;
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

async function withPanel(sessionModel, check) {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-providers-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output,
        '--providers', '--session-model', sessionModel], {cwd:output, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='', info, browser, page;
    server.stdout.on('data', chunk=>{stdout+=chunk;});
    server.stderr.on('data', chunk=>{stderr+=chunk;});
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for (let i=0;i<150;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Fixture server failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture server ready metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ? {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE} : {headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(20000);
        const errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        // Open the panel from the keyboard.
        await page.getByRole('button',{name:'Team',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByRole('dialog',{name:'Work together'}).waitFor();
        await page.waitForFunction(()=>Array.isArray(app._swarmState?.team_providers));
        await check(page, output);
        assert.deepEqual(errors,[]);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
        }
        console.error('Browser failure evidence:',output);
        throw error;
    } finally {
        if(browser)await browser.close();
        if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let cleanupTimer;
        const result=await Promise.race([exited,new Promise(resolve=>{cleanupTimer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(cleanupTimer);
        if(!result)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
}

test('A Codex conversation\'s Team panel names the models a team runs on', {timeout: 90000}, async () => {
    await withPanel('codex:gpt-5-codex', async (page, output) => {
        await page.getByText('codex · gpt-5-codex',{exact:true}).waitFor();
        const notice=await page.locator('[data-swarm="notice"]').innerText();
        assert.match(notice,/^Team can't run on Codex: Codex runs its own tool loop/);
        for (const part of ['Anthropic, OpenAI, OpenRouter, Ollama, EXO, Kimi and SONN','OpenAI-compatible (such as NVIDIA NIM)',
            'Azure OpenAI','Claude on Bedrock with a Bedrock API key','Switch this conversation to one of them to start a team.'])
            assert.ok(notice.includes(part),notice);
        // Nothing can start here, and nothing claims otherwise.
        assert.equal(await page.locator('[data-swarm="start"]').isDisabled(),true);
        assert.equal(await page.getByLabel('Enable team preview').isDisabled(),true);
        // The refusal wraps inside the panel at phone width.
        await page.setViewportSize({width:390,height:844});
        const overflow=await page.evaluate(()=>{
            const body=document.querySelector('.swarm-body');
            return {scroll:body.scrollWidth,client:body.clientWidth};
        });
        assert.ok(overflow.scroll<=overflow.client+1,'Team panel scrolls sideways at phone width: '+JSON.stringify(overflow));
        await page.screenshot({path:path.join(output,'codex-refusal.png'),fullPage:false});
        // Escape closes it.
        await page.keyboard.press('Escape');
        await page.getByRole('dialog',{name:'Work together'}).waitFor({state:'detached'});
    });
});

test('An Anthropic conversation\'s Team panel offers workers only models a team runs on', {timeout: 90000}, async () => {
    await withPanel('anthropic:claude-sonnet-5', async (page, output) => {
        await page.getByText('anthropic · claude-sonnet-5',{exact:true}).waitFor();
        assert.match(await page.locator('[data-swarm="notice"]').innerText(),/^Workers share scoped findings/);
        assert.equal(await page.locator('[data-swarm="start"]').isDisabled(),true);  // Until the preview is on.
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        const options=await page.getByLabel('Worker model').locator('option').allTextContents();
        // Claude on Bedrock only with its Bedrock API key; never Vertex, Codex or Claude Code.
        assert.deepEqual(options,['Same as this session','Anthropic · claude-haiku-4-5-20251001','OpenAI · gpt-5','OpenAI · gpt-5-mini',
            'NVIDIA NIM · nvidia/nemotron-3-super-120b-a12b','Bedrock with its key · us.anthropic.claude-haiku-4-5-v1:0',
            'ollama · fixture-native']);
        // Chosen from the keyboard, and kept across the panel's own refreshes.
        await page.getByLabel('Worker model').focus();
        await page.keyboard.press('ArrowDown');
        await page.keyboard.press('ArrowDown');
        assert.equal(await page.getByLabel('Worker model').inputValue(),JSON.stringify({provider:'openai',model:'gpt-5'}));
        await page.getByRole('button',{name:'Refresh team'}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.getByLabel('Worker model').inputValue(),JSON.stringify({provider:'openai',model:'gpt-5'}));
        await page.screenshot({path:path.join(output,'worker-models.png'),fullPage:false});
    });
});
