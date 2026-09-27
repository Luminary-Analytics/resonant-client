/* Real source GUI/WS/native Session with a scripted provider; no live account. */
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[2]||'playwright');

test('Explicit bilateral personal collaboration through two captured conversation panels', {timeout:90000}, async()=>{
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-collaboration-browser-'));
    const server=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_ui_server.py'),output,'--hold-first-workers','--collaboration'],
        {cwd:output,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,a,b;
    const errors=[],commands=[],responses=[],interventions=[];
    server.stdout.on('data',chunk=>{stdout+=chunk;});server.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    async function ready(page){await page.waitForFunction(()=>app._swarmState&&!app._swarmPending);}
    async function open(page){await page.getByRole('button',{name:'Team',exact:true}).click();await ready(page);await page.getByText('Share with another personal conversation',{exact:true}).click();}
    async function switchSession(page,title,id){
        if(await page.getByRole('dialog',{name:'Work together'}).count())await page.keyboard.press('Escape');
        await page.getByText(title,{exact:true}).first().click();
        await page.waitForFunction(value=>app.currentSessionId===value,id);
    }
    async function inspect(page){
        await ready(page);
        const options=page.getByLabel('Sharing agreement',{exact:true});
        await page.waitForFunction(()=>document.querySelector('[data-collab="grants"]')?.options.length>1);
        await options.selectOption({index:1});
        await page.getByRole('button',{name:'Inspect agreement',exact:true}).focus();await page.keyboard.press('Enter');await ready(page);
    }
    try{
        for(let n=0;n<150;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}if(server.exitCode!==null)throw Error(stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture startup failed: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        const context=await browser.newContext({viewport:{width:1180,height:900}});
        context.on('page',page=>{
            page.on('pageerror',error=>errors.push(error.message));
            page.on('websocket',socket=>{
                socket.on('framesent',frame=>{const value=JSON.parse(frame.payload);if(value.command==='swarm')commands.push(value);});
                socket.on('framereceived',frame=>{const value=JSON.parse(frame.payload);if(value.event==='swarm_state')responses.push(value);});
            });
        });
        await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        a=await context.newPage();a.setDefaultTimeout(12000);await a.goto(info.url);
        await a.waitForFunction(id=>window.app?.currentSessionId===id,info.session_id);
        await a.locator('#user-input').fill('Preserve ordinary origin draft');await open(a);
        await a.getByLabel('Enable team preview').check();await ready(a);
        await a.getByLabel('Collaboration objective',{exact:true}).fill('Origin collaboration');
        await a.getByRole('button',{name:'Prepare collaboration team',exact:true}).focus();await a.keyboard.press('Enter');await ready(a);
        await a.getByText('Share with another personal conversation',{exact:true}).click();
        const addressA=await a.getByLabel('This conversation’s team address').inputValue();
        await switchSession(a,'Other fixture conversation',info.other_session_id);await open(a);
        await a.getByLabel('Collaboration objective',{exact:true}).fill('Receiver collaboration');
        await a.getByRole('button',{name:'Prepare collaboration team',exact:true}).click();await ready(a);
        await a.getByText('Share with another personal conversation',{exact:true}).click();
        const addressB=await a.getByLabel('This conversation’s team address').inputValue();
        let evidence=await(await fetch(info.url+'/__fixture__/evidence')).json();
        assert.equal(evidence.backend_instances,0);assert.equal(evidence.runs.length,2);
        await switchSession(a,'Team fixture conversation',info.session_id);await open(a);
        assert.equal(await a.getByLabel('This conversation’s team address').inputValue(),addressA);
        await a.getByText('Offer a sharing agreement',{exact:true}).click();
        await a.getByLabel('Other conversation’s team address').fill(addressB);
        await a.getByLabel('Sharing purpose').fill('Explicit CSV investigation cooperation');
        await a.getByLabel('Work proposals',{exact:true}).check();
        await a.getByRole('button',{name:'Offer sharing agreement',exact:true}).click();await ready(a);await inspect(a);
        assert.equal(await a.getByRole('button',{name:'Accept sharing agreement',exact:true}).count(),0);
        b=await context.newPage();b.setDefaultTimeout(12000);await b.goto(info.url);
        await b.waitForFunction(()=>window.app?.currentSessionId);
        await switchSession(b,'Other fixture conversation',info.other_session_id);await open(b);await inspect(b);
        await b.getByRole('button',{name:'Accept sharing agreement',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await a.waitForFunction(()=>app._swarmState?.collaboration?.detail?.state==='active'&&!app._swarmPending);
        await a.getByText('Compose an explicit message',{exact:true}).click();
        await a.getByLabel('Message type',{exact:true}).selectOption('work_request');
        await a.getByLabel('Selected message content',{exact:true}).fill('Selected proposal <script>window.collaborationLeak=true</script>: investigate fact.txt');
        await a.getByRole('button',{name:'Send selected content',exact:true}).click();await ready(a);
        await b.getByRole('button',{name:'Read message',exact:true}).waitFor();
        assert.equal(await b.getByText('Selected proposal',{exact:false}).count(),0);
        await b.getByRole('button',{name:'Read message',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await b.getByLabel('Accepted investigation',{exact:true}).fill('Inspect fact.txt independently under my own allowance');
        await b.getByLabel('Accepted readable folders').fill('fact.txt');
        await b.getByLabel('Work acceptance notes').fill('I choose this read-only assignment after reading the proposal.');
        await b.setViewportSize({width:390,height:844});
        await b.getByLabel('Accepted investigation',{exact:true}).focus();await b.waitForTimeout(1200);
        assert.equal(await b.getByLabel('Accepted investigation',{exact:true}).evaluate(node=>node===document.activeElement),true);
        assert.equal(await b.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        assert.equal(await b.evaluate(()=>window.collaborationLeak),undefined);
        await b.screenshot({path:path.join(output,'collaboration-compact.png')});
        await b.getByRole('button',{name:'Accept and start my investigation',exact:true}).focus();await b.keyboard.press('Enter');await ready(b);
        await b.waitForFunction(()=>app._swarmState?.run?.workers.some(row=>row.alive));
        // Sidebar attempts cannot move the active receiver. Origin remains
        // controllable from its already captured separate panel.
        await b.setViewportSize({width:1180,height:900});
        await b.keyboard.press('Escape');
        await b.getByText('Team fixture conversation',{exact:true}).first().click();
        await b.waitForTimeout(250);assert.equal(await b.evaluate(()=>app.currentSessionId),info.other_session_id);
        await a.getByLabel('Reason to revoke',{exact:true}).fill('Close future sharing while receiver finishes its accepted work');
        await a.getByRole('button',{name:'Revoke sharing agreement',exact:true}).click();await ready(a);
        if((await a.locator('[data-swarm="notice"]').textContent()).includes('Run revision changed')){
            interventions.push('Known rejected stale revision: explicit Refresh team, then new revoke click.');
            await a.getByRole('button',{name:'Refresh team',exact:true}).click();await ready(a);
            await a.getByRole('button',{name:'Revoke sharing agreement',exact:true}).click();await ready(a);
        }
        await a.waitForFunction(()=>app._swarmState?.collaboration?.detail?.state==='revoked');
        await a.getByRole('button',{name:'Stop team',exact:true}).click();await ready(a);
        evidence=await(await fetch(info.url+'/__fixture__/evidence')).json();
        const receiver=evidence.runs.find(item=>item.run.run.id===addressB.split('/')[1]);
        assert.equal(receiver.run.run.state,'running');assert.ok(receiver.run.workers.some(row=>row.alive));
        await fetch(info.url+'/__fixture__/release-workers',{method:'POST'});await open(b);
        await b.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await inspect(b);assert.equal(await b.getByText('Agreement revoked. Receiver approval grants no work execution.',{exact:true}).count(),1);
        await b.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await b.getByLabel('Review notes for Worker 1').fill('Read the actual retained fact and matching finding.');
        await b.getByRole('button',{name:'Accept findings',exact:true}).click();await ready(b);
        await b.getByRole('button',{name:'Complete team',exact:true}).click();await ready(b);
        await b.keyboard.press('Escape');await switchSession(b,'Team fixture conversation',info.session_id);
        assert.equal(await a.locator('#user-input').inputValue(),'Preserve ordinary origin draft');
        evidence=await(await fetch(info.url+'/__fixture__/evidence')).json();
        assert.equal(evidence.backend_instances,1);assert.equal(evidence.backend_requests,2);
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:'source-native-scripted-personal-collaboration',live_providers_called:false,
            two_session_addresses:[addressA,addressB],keyboard:true,compact:true,explicit_delivery:true,origin_stop_isolated:true,evidence,commands,interventions},null,2));
        console.log('Personal collaboration browser:',output);
    }catch(error){
        fs.writeFileSync(path.join(output,'debug.json'),JSON.stringify({errors,commands,responses},null,2));
        console.error('Browser errors:',errors);
        for(const [name,page] of [['origin',a],['receiver',b]])if(page){await page.screenshot({path:path.join(output,name+'-failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,name+'-failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('Collaboration evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);clearTimeout(timer);
        if(!result)server.kill();fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
