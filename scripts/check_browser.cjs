// Optional live UI check. WEATHER_DESK_PLAYWRIGHT can point to an installed Playwright module.
const {chromium} = require(process.env.WEATHER_DESK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  let page;
  try {
    page = await browser.newPage({viewport:{width:1600,height:1100},acceptDownloads:true});
    const errors=[], animationFrames=[];
    let measuring=false;
    page.on('pageerror', error=>errors.push(String(error)));
    page.on('websocket', ws=>ws.on('framereceived', ({payload})=>{
      if(measuring) animationFrames.push(typeof payload==='string' ? Buffer.byteLength(payload) : payload.length);
    }));
    const started=Date.now();
    await page.goto(process.env.WEATHER_DESK_URL || 'http://127.0.0.1:5007/app');
    await page.waitForFunction(()=>window.Bokeh?.documents?.length && Bokeh.documents[0].get_model_by_name('satellite_frames')?.data.image.length>=2, null, {timeout:60000});
    await page.waitForFunction(()=>Bokeh.documents[0].get_model_by_name('ifs_msl')?.data.xs.length>0, null, {timeout:60000});
    console.log('Usable satellite + IFS after',Date.now()-started,'ms');
    await page.getByRole('button',{name:'Actualiser le satellite',exact:true}).waitFor();
    await page.waitForFunction(()=>{
      const d=Bokeh.documents[0];
      const f=d.get_model_by_name('satellite_frame_filter');
      return f?.indices.length===1 && Number.isInteger(f.indices[0]);
    });
    // Wait for the background sequence before measuring playback traffic.
    await page.getByRole('button',{name:'Actualiser le satellite',exact:true}).isEnabled().then(async enabled=>{
      if(!enabled) await page.waitForFunction(()=>Array.from(Bokeh.documents[0]._all_models.values()).some(m=>m.label==='Actualiser le satellite' && !m.disabled),null,{timeout:60000});
    });
    const imageCount = await page.evaluate(()=>Bokeh.documents[0].get_model_by_name('satellite_frames').data.image.length);
    measuring=true;
    const switchStart=Date.now();
    await page.locator('button.first').click();
    await page.waitForFunction(()=>{
      const d=Bokeh.documents[0];
      return d.get_model_by_name('satellite_frame_filter').indices[0]===d.get_model_by_name('satellite_frame_order').data.index[0];
    });
    console.log('Frame selection (including Playwright click):',Date.now()-switchStart,'ms');
    const firstIndex=await page.evaluate(()=>Bokeh.documents[0].get_model_by_name('satellite_frame_filter').indices[0]);
    await page.locator('button.play').click();
    await page.waitForFunction(i=>Bokeh.documents[0].get_model_by_name('satellite_frame_filter').indices[0]!==i,firstIndex);
    await page.locator('button.pause').click();
    measuring=false;
    assert(Math.max(0,...animationFrames)<64000,'Playback retransmitted a raster buffer');
    assert.equal(await page.evaluate(()=>Bokeh.documents[0].get_model_by_name('satellite_frames').data.image.length),imageCount);
    // Opacity and visibility must update in the browser.
    await page.getByRole('slider').first().press('ArrowLeft');
    await page.waitForFunction(()=>{
      const d=Bokeh.documents[0],s=d.get_model_by_name('satellite_frames');
      const r=Array.from(d._all_models.values()).find(m=>m.data_source===s);
      return r.glyph.global_alpha.value<0.8;
    });
    await page.getByRole('checkbox',{name:'Isobares IFS — hPa, pas de 4'}).uncheck();
    await page.waitForFunction(()=>{
      const d=Bokeh.documents[0],s=d.get_model_by_name('ifs_msl');
      return !Array.from(d._all_models.values()).find(m=>m.data_source===s).visible;
    });
    await page.getByRole('checkbox',{name:'Isobares IFS — hPa, pas de 4'}).check();
    await page.locator('canvas').last().scrollIntoViewIfNeeded();
    await page.locator('[title="Tracer : Front froid"]').click();
    const box=await page.locator('canvas').last().boundingBox();
    await page.mouse.click(box.x+box.width*0.45,box.y+box.height*0.4,{delay:600});
    await page.mouse.click(box.x+box.width*0.50,box.y+box.height*0.5);
    await page.mouse.click(box.x+box.width*0.55,box.y+box.height*0.6,{delay:600});
    await page.getByText('1 annotation(s) exportable(s).',{exact:true}).waitFor();
    // An IFS change must not reset the map or drawings.
    await page.getByRole('slider').last().press('Home');
    await page.getByText('IFS +0 h · 0,25° · © ECMWF / CC BY 4.0',{exact:true}).waitFor({timeout:60000});
    await page.getByText('1 annotation(s) exportable(s).',{exact:true}).waitFor();
    await page.locator('textarea').first().fill('Analyse synoptique satellite et IFS — test navigateur.');
    await page.locator('textarea').first().press('Tab');
    const downloadEvent=page.waitForEvent('download');
    await page.getByRole('button',{name:"Exporter l'analyse complète (.zip)",exact:true}).click();
    const download=await downloadEvent;
    await download.saveAs('/tmp/weather-desk-live.zip');
    await page.locator('canvas').last().scrollIntoViewIfNeeded();
    await page.screenshot({path:'/tmp/weather-desk-live.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: raster, playback without pixel transfer, opacity, visibility, drawing, IFS step change and ZIP export.');
  } finally {
    if(page) await page.screenshot({path:'/tmp/weather-desk-live.png',fullPage:true}).catch(()=>{});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1)});
