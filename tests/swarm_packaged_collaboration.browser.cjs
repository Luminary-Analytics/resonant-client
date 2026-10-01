/* Two personal conversations in the unmodified frozen GUI; scripted HTTP only. */
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[3]||'playwright');

test('Frozen personal collaboration preserves consent, receiver authority and independent review',{timeout:120000},async()=>{
    const executable=path.resolve(process.argv[2]);assert.ok(fs.existsSync(executable));
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-packaged-collaboration-'));
    const fixture=spawn(process.env.SWARM_MANAGED_PYTHON||process.env.SWARM_PYTHON||'python',
        [path.join(__dirname,'fixtures/swarming_packaged_collaboration_server.py'),output,executable],
        {windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,a,b;
    const errors=[],commands=[],frames=[],interventions=[];
    fixture.stdout.on('data',chunk=>{stdout+=chunk;});fixture.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>fixture.once('exit',(code,signal)=>resolve({code,signal})));
    async function ready(page){await page.waitForFunction(()=>app._swarmState&&!app._swarmPending);}
    async function open(page){await page.getByRole('button',{name:'Team',exact:true}).click();await ready(page);await page.getByText('Share with another personal conversation',{exact:true}).click();}
    async function switchSession(page,id){
        if(await page.getByRole('dialog',{name:'Work together'}).count())await page.keyboard.press('Escape');
        await page.locator(`.agent-row[data-session-id="${id}"]`).click();
        await page.waitForFunction(value=>app.currentSessionId===value,id);
    }
    async function inspect(page){
        await ready(page);
        await page.waitForFunction(()=>document.querySelector('[data-collab="grants"]')?.options.length>1);
        await page.getByLabel('Sharing agreement',{exact:true}).selectOption({index:1});
        await page.getByRole('button',{name:'Inspect agreement',exact:true}).focus();await page.keyboard.press('Enter');await ready(page);
    }
    async function evidence(){return (await fetch(info.api_url+'/__fixture__/evidence')).json();}
    try{
        for(let n=0;n<350;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error('Fixture exited: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Candidate startup metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        const context=await browser.newContext({viewport:{width:1180,height:900}});
        context.on('page',page=>{
            page.on('pageerror',error=>errors.push(error.message));
            page.on('websocket',socket=>{
                socket.on('framesent',frame=>{const value=JSON.parse(frame.payload);if(value.command==='swarm')commands.push(value);});
                socket.on('framereceived',frame=>{const value=JSON.parse(frame.payload);if(value.event==='swarm_state')frames.push(value);});
            });
        });
        await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        a=await context.newPage();a.setDefaultTimeout(20000);await a.goto(info.url);
        await switchSession(a,info.session_id);
        await a.locator('#user-input').fill('Preserve ordinary origin draft');await open(a);
        await a.getByLabel('Enable team preview').check();await ready(a);
        await a.getByLabel('Collaboration objective',{exact:true}).fill('Packaged origin collaboration');
        await a.getByLabel('Collaboration total request allowance').fill('4');
        await a.getByRole('button',{name:'Prepare collaboration team',exact:true}).focus();await a.keyboard.press('Enter');await ready(a);
        await a.getByText('Share with another personal conversation',{exact:true}).click();
        const addressA=await a.getByLabel('This conversation’s team address').inputValue();
        const originPrepared=await a.evaluate(()=>app._swarmState.run);
        await switchSession(a,info.other_session_id);await open(a);
        await a.getByLabel('Collaboration objective',{exact:true}).fill('Packaged receiver collaboration');
        await a.getByLabel('Collaboration total request allowance').fill('6');
        await a.getByRole('button',{name:'Prepare collaboration team',exact:true}).click();await ready(a);
        await a.getByText('Share with another personal conversation',{exact:true}).click();
        const addressB=await a.getByLabel('This conversation’s team address').inputValue();
        const receiverPrepared=await a.evaluate(()=>app._swarmState.run);
        assert.notEqual(originPrepared.run.id,receiverPrepared.run.id);
        assert.equal(originPrepared.run.request_limit,4);assert.equal(receiverPrepared.run.request_limit,6);
        assert.equal(originPrepared.model_requests.length,0);assert.equal(receiverPrepared.model_requests.length,0);
        assert.equal((await evidence()).requests.length,0,'Preparing teams must not invoke a model');
        await switchSession(a,info.session_id);await open(a);
        await a.getByText('Offer a sharing agreement',{exact:true}).click();
        await a.getByLabel('Other conversation’s team address').fill(addressB);
        await a.getByLabel('Sharing purpose').fill('Explicit packaged CSV investigation cooperation');
        await a.getByLabel('Work proposals',{exact:true}).check();
        await a.getByRole('button',{name:'Offer sharing agreement',exact:true}).click();await ready(a);await inspect(a);
        assert.equal(await a.getByRole('button',{name:'Accept sharing agreement',exact:true}).count(),0);
        b=await context.newPage();b.setDefaultTimeout(20000);await b.goto(info.url);
        await switchSession(b,info.other_session_id);await open(b);await inspect(b);
        await b.getByRole('button',{name:'Accept sharing agreement',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await a.waitForFunction(()=>app._swarmState?.collaboration?.detail?.state==='active'&&!app._swarmPending);
        await a.getByText('Compose an explicit message',{exact:true}).click();
        await a.getByLabel('Message type',{exact:true}).selectOption('work_request');
        const proposal='Selected proposal <script>window.collaborationLeak=true</script>: investigate fact.txt';
        await a.getByLabel('Selected message content',{exact:true}).fill(proposal);
        await a.getByRole('button',{name:'Send selected content',exact:true}).click();await ready(a);
        await b.getByRole('button',{name:'Read message',exact:true}).waitFor();
        assert.equal(await b.getByText('Selected proposal',{exact:false}).count(),0);
        assert.equal((await evidence()).requests.length,0,'Consent and delivery must not invoke a model');
        await b.getByRole('button',{name:'Read message',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await b.getByLabel('Accepted investigation',{exact:true}).fill('Inspect fact.txt independently under my own allowance');
        await b.getByLabel('Accepted readable folders').fill('fact.txt');
        await b.getByLabel('Accepted request allowance').fill('3');
        await b.getByLabel('Work acceptance notes').fill('I choose this assignment after explicitly reading the proposal.');
        await b.setViewportSize({width:390,height:844});
        await b.getByLabel('Accepted investigation',{exact:true}).focus();await b.waitForTimeout(1100);
        assert.equal(await b.getByLabel('Accepted investigation',{exact:true}).evaluate(node=>node===document.activeElement),true);
        assert.equal(await b.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        assert.equal(await b.evaluate(()=>window.collaborationLeak),undefined);
        await b.screenshot({path:path.join(output,'personal-consent-compact.png')});
        await b.getByRole('button',{name:'Accept and start my investigation',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await b.waitForFunction(()=>app._swarmState?.run?.model_requests?.some(row=>row.state==='started'));
        let during=await evidence();
        for(let n=0;n<100&&during.requests.length===0;n++){await b.waitForTimeout(50);during=await evidence();}
        assert.equal(during.requests.length,1);
        const children=during.children.filter(child=>child.argv.includes('--swarm-worker'));
        assert.equal(children.length,1);
        assert.equal(path.resolve(children[0].exe).toLowerCase(),executable.toLowerCase());
        const receiverActive=await b.evaluate(()=>app._swarmState.run);
        assert.equal(receiverActive.model_requests.length,1);
        assert.ok(receiverActive.attempts.some(row=>row.id===receiverActive.model_requests[0].attempt_id&&row.run_id===receiverPrepared.run.id));
        assert.ok(receiverActive.process_observations.some(row=>row.pid===children[0].pid&&row.state==='started'));
        await a.getByLabel('Reason to revoke',{exact:true}).fill('Close future sharing while receiver finishes accepted work');
        await a.getByRole('button',{name:'Revoke sharing agreement',exact:true}).click();await ready(a);
        if((await a.locator('[data-swarm="notice"]').textContent()).includes('Run revision changed')){
            interventions.push('Rejected stale revision: refreshed and explicitly retried revoke with a new command.');
            await a.getByRole('button',{name:'Refresh team',exact:true}).click();await ready(a);
            await a.getByRole('button',{name:'Revoke sharing agreement',exact:true}).click();await ready(a);
        }
        await a.waitForFunction(()=>app._swarmState?.collaboration?.detail?.state==='revoked');
        await a.getByRole('button',{name:'Stop team',exact:true}).click();await ready(a);
        const originStopped=await a.evaluate(()=>app._swarmState.run);
        assert.equal(originStopped.model_requests.length,0);
        assert.ok(originStopped.run.stop_requested);
        await b.getByRole('button',{name:'Refresh team',exact:true}).click();await ready(b);
        const receiverAfterOriginStop=await b.evaluate(()=>app._swarmState.run);
        assert.equal(receiverAfterOriginStop.run.state,'running');
        assert.equal(Boolean(receiverAfterOriginStop.run.stop_requested),false);
        assert.ok(receiverAfterOriginStop.workers.some(row=>row.alive));
        await fetch(info.api_url+'/__fixture__/release',{method:'POST'});
        await b.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await inspect(b);
        await b.getByText('Agreement revoked. Receiver approval grants no work execution.',{exact:true}).waitFor();
        await b.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await b.getByText('Packaged collaborative finding: quoted CSV fields preserve commas. Owner review remains required.',{exact:true}).waitFor();
        const beforeReview=await b.evaluate(()=>app._swarmState.run);
        assert.notEqual(beforeReview.run.state,'completed');
        await b.getByLabel('Review notes for Worker 1').fill('Reviewed exact retained file and observed read independently.');
        await b.getByRole('button',{name:'Accept findings',exact:true}).click();await ready(b);
        await b.getByRole('button',{name:'Complete team',exact:true}).click();await ready(b);
        await b.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
        const receiverFinal=await b.evaluate(()=>app._swarmState.run), observed=await evidence();
        assert.equal(observed.requests.length,2);assert.equal(observed.requests.filter(row=>row.observed_file_result).length,1);
        assert.equal(receiverFinal.model_requests.length,2);
        assert.equal(receiverFinal.action_receipts.filter(row=>row.tool_name==='file_read'&&row.state==='completed').length,1);
        assert.ok(receiverFinal.attempts.every(row=>row.process_state==='stopped'));
        assert.equal(observed.children.filter(child=>child.argv.includes('--swarm-worker')).length,0);
        await b.screenshot({path:path.join(output,'personal-receiver-completed.png')});
        await a.keyboard.press('Escape');assert.equal(await a.locator('#user-input').inputValue(),'Preserve ordinary origin draft');
        assert.deepEqual(errors,[]);
        const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
        assert.match(log,/Application startup complete/);assert.match(log,/WebSocket.*accepted/);
        assert.equal(log.includes('Traceback (most recent call last)'),false);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:'frozen-native-scripted-personal-collaboration',candidate:info,
            addresses:[addressA,addressB],originPrepared,receiverPrepared,receiverActive,originStopped,receiverAfterOriginStop,
            beforeReview,receiverFinal,during,observed,commands,interventions,live_providers_called:false},null,2));
        console.log('Frozen personal collaboration evidence:',output);
    }catch(error){
        fs.writeFileSync(path.join(output,'debug.json'),JSON.stringify({errors,commands,frames},null,2));
        for(const [name,page] of [['origin',a],['receiver',b]])if(page){await page.screenshot({path:path.join(output,name+'-failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,name+'-failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('Frozen collaboration failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();fixture.stdin.end('stop\n');
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),12000);})]);clearTimeout(timer);
        if(!result)fixture.kill();fs.writeFileSync(path.join(output,'fixture.log'),stdout+'\n'+stderr);
    }
});
