/* Actual source UI/WebSocket routes, retained owner history and immutable text.
 * Inference is scripted; older retained fixtures are explicitly seeded history.
 */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const {chromium}=require(process.argv[2]||'playwright');

test('Retained teams and verified text pages use scoped owner routes', {timeout:90000}, async()=>{
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-swarm-history-browser-'));
    const server=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_ui_server.py'),output,'--history'],
        {cwd:output,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    server.stdout.on('data',chunk=>{stdout+=chunk;});server.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try{
        for(let n=0;n<150;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Fixture server failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Missing fixture readiness: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});page.setDefaultTimeout(15000);
        const errors=[],commands=[];
        page.on('pageerror',error=>{errors.push(error.message);console.error(error.message);});
        page.on('websocket',socket=>socket.on('framesent',frame=>{const data=JSON.parse(frame.payload);if(data.command==='swarm')commands.push(data);}));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);await page.waitForFunction(id=>window.app?.currentSessionId===id,info.session_id);
        await page.locator('#user-input').fill('Keep my ordinary conversation draft');
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmHistory?.items.length===20&&!app._swarmPending);
        await page.getByText('Saved teams in this conversation',{exact:true}).click();
        assert.equal(await page.locator('[data-swarm="history-select"] option').count(),21);
        assert.equal(await page.getByText('Other conversation private team',{exact:false}).count(),0);
        await page.getByLabel('Saved team',{exact:true}).selectOption('history-22');
        await page.getByRole('button',{name:'Open saved team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.run?.id==='history-22'&&!app._swarmPending);
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled&&!app._swarmPending);
        await page.getByLabel('Team objective').fill('Keep a new team setup and review draft');
        await page.locator('[data-task-objective]').nth(0).fill('Inspect saved quoted fields');
        await page.locator('[data-task-objective]').nth(1).fill('Inspect saved delimiter facts');
        await page.getByRole('button',{name:'Start read-only team',exact:true}).click();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await page.getByLabel('Review notes for Worker 1').fill('Keep this exact owner review draft while inspecting older evidence.');
        const activeRun=await page.evaluate(()=>app._swarmScope.run_id);
        await page.waitForFunction(()=>!app._swarmPending);
        await page.getByRole('button',{name:'Older teams',exact:true}).click();
        await page.waitForFunction(()=>app._swarmHistoryPage===1&&!app._swarmPending);
        assert.equal(await page.getByLabel('Review notes for Worker 1').inputValue(),'Keep this exact owner review draft while inspecting older evidence.');
        assert.equal(await page.getByRole('button',{name:'Older teams',exact:true}).isEnabled(),false);
        await page.getByLabel('Saved team',{exact:true}).selectOption('history-00');
        await page.getByRole('button',{name:'Open saved team',exact:true}).focus();await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmScope.run_id==='history-00'&&app._swarmState?.run?.run?.id==='history-00'&&!app._swarmPending);
        await page.getByText('Worker 1 · Stopped',{exact:true}).click();
        const expected=(await(await fetch(info.url+'/__fixture__/evidence')).json()).history_fixture;
        await page.getByLabel('Retained evidence for Worker 1').selectOption(expected.artifact_id);
        await page.getByRole('button',{name:'Read evidence for Worker 1',exact:true}).click();
        await page.waitForFunction(()=>app._swarmArtifactState?.page?.offset===0&&!app._swarmPending);
        let retained=await page.getByLabel('Verified retained evidence text',{exact:true}).textContent();
        assert.ok(retained.startsWith('Retained first page: 🪷 café <script>'));
        assert.equal(await page.evaluate(()=>window.evidenceExecuted),undefined);
        await page.setViewportSize({width:390,height:844});
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.getByLabel('Verified retained evidence text',{exact:true}).focus();
        await page.waitForTimeout(1100);
        assert.equal(await page.getByLabel('Verified retained evidence text',{exact:true}).evaluate(node=>node===document.activeElement),true);
        await page.screenshot({path:path.join(output,'retained-text-compact.png')});
        while(await page.getByRole('button',{name:'Next evidence page',exact:true}).isVisible()){
            const prior=await page.evaluate(()=>app._swarmArtifactState.page.offset);
            await page.getByRole('button',{name:'Next evidence page',exact:true}).focus();await page.keyboard.press('Enter');
            await page.waitForFunction(offset=>app._swarmArtifactState.page.offset>offset&&!app._swarmPending,prior);
            retained+=await page.getByLabel('Verified retained evidence text',{exact:true}).textContent();
        }
        assert.equal(retained,expected.text);
        assert.equal(crypto.createHash('sha256').update(retained).digest('hex'),expected.sha256);
        await page.getByRole('button',{name:'Previous evidence page',exact:true}).click();
        await page.waitForFunction(()=>app._swarmArtifactState.page.next_offset!==null&&!app._swarmPending);
        await page.getByLabel('Retained evidence for Worker 1').selectOption(expected.image_id);
        await page.getByRole('button',{name:'Read evidence for Worker 1',exact:true}).click();
        await page.getByText('This retained artifact is not text. No text preview or visual interpretation is available.',{exact:true}).waitFor();
        assert.equal(await page.getByLabel('Verified retained evidence text',{exact:true}).isVisible(),false);
        await page.getByRole('button',{name:'Newer teams',exact:true}).click();
        await page.waitForFunction(()=>app._swarmHistoryPage===0&&!app._swarmPending);
        await page.getByLabel('Saved team',{exact:true}).selectOption(activeRun);
        await page.getByRole('button',{name:'Open saved team',exact:true}).click();
        await page.waitForFunction(id=>app._swarmState?.run?.run?.id===id&&!app._swarmPending,activeRun);
        assert.equal(await page.getByLabel('Review notes for Worker 1').inputValue(),'Keep this exact owner review draft while inspecting older evidence.');
        assert.equal(await page.getByLabel('Team objective').inputValue(),'Keep a new team setup and review draft');
        assert.equal(await page.locator('[data-task-objective]').nth(0).inputValue(),'Inspect saved quoted fields');
        // Directly send rejected owner requests through the real WebSocket;
        // this cannot forge the server's captured tenant/owner/conversation.
        const denied=await page.evaluate(async({project,session,artifact})=>{
            async function request(payload){
                const request_id=crypto.randomUUID();
                const response=new Promise(resolve=>{
                    const listener=event=>{const value=JSON.parse(event.data);if(value.request_id===request_id){app.ws.removeEventListener('message',listener);resolve(value);}};
                    app.ws.addEventListener('message',listener);
                });
                app.send({command:'swarm',project,session_id:session,request_id,...payload});return response;
            }
            return [await request({action:'history',before_run_id:'foreign-history-team',limit:20}),
                await request({action:'read_artifact',run_id:'foreign-history-team',artifact_id:artifact,offset:0,limit:8000})];
        },{project:info.workspace,session:info.session_id,artifact:expected.artifact_id});
        assert.ok(denied.every(result=>result.error&&!result.artifact_page&&!result.history));
        assert.equal(await page.evaluate(()=>app._swarmScope.run_id),activeRun);
        await page.getByRole('button',{name:'Stop team',exact:true}).click();
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(),'Keep my ordinary conversation draft');
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:'source-app-scripted-native-history',live_providers_called:false,
            history_pages:2,retained_sha256:expected.sha256,retained_characters:Array.from(retained).length,denied_scope_requests:denied,
            scope_preserved:true,review_draft_preserved:true,commands},null,2));
        console.log('Retained owner evidence browser:',output);
    }catch(error){
        if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('History browser failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();
        if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);clearTimeout(timer);
        if(!result)server.kill();fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
