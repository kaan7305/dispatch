/* render media/reddit_video.mp4 out of the hero demo in site/index.html.

   this renders the animation rather than recording it. site/index.html drives
   the demo on setTimeout, requestAnimationFrame and css transitions, and all
   three hang off chromium's clock, so:

     - Emulation.setVirtualTimePolicy puts that clock under our control and we
       advance it exactly one frame per step, never in real time;
     - HeadlessExperimental.beginFrame then composites that exact moment and
       hands back the pixels. Page.captureScreenshot cannot: it waits for the
       display compositor to commit a frame, which a paused clock never does,
       so it deadlocks as soon as a rAF callback is pending.

   every frame is present, none are dropped, and the same source gives the same
   video every time.

   usage, from the repo root:

     python -m http.server 8777 --bind 127.0.0.1 --directory site &
     npm i playwright ffmpeg-static && npx playwright install chromium
     node scripts/render_demo_video.js          # -> frames/ + render.json

   then encode with the crop it printed:

     ffmpeg -framerate 30 -i 'frames/%05d.png'
            -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 -shortest
            -filter_complex '[0:v]crop=<CROP>[v]' -map '[v]' -map 1:a
            -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -profile:v high
            -movflags +faststart -c:a aac -b:a 64k media/reddit_video.mp4

   the silent aac track is there because reddit's uploader wants one. */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const FPS = Number(process.env.FPS || 30);
/* beginFrame composites at the css viewport size and ignores deviceScaleFactor,
   so the way to more pixels is to zoom the page and grow the viewport to match:
   layout stays 1920x1080 css, the desk is just drawn ZOOM times bigger */
const ZOOM = Number(process.env.ZOOM || 1.5);
const HOLD_S = Number(process.env.HOLD_S || 2.5);
const LEAD_S = Number(process.env.LEAD_S || 0.8);
const MAX_FRAMES = Number(process.env.MAX_FRAMES || 3000);
const OUT = process.env.OUT || path.join(__dirname, 'frames');

const STEP_MS = 1000 / FPS;
const ARGS = [
  '--enable-begin-frame-control',
  '--run-all-compositor-stages-before-draw',
  '--disable-new-content-rendering-timeout',
];
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const withTimeout = (p, ms, what) => Promise.race([
  p, new Promise((_, rej) => setTimeout(() => rej(new Error('stalled on ' + what)), ms)),
]);

(async () => {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({ headless: true, args: ARGS });
  const ctx = await browser.newContext({
    viewport: { width: Math.round(1920 * ZOOM), height: Math.round(1080 * ZOOM) },
  });
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);

  await page.goto((process.env.URL || 'http://127.0.0.1:8777/index.html'), { waitUntil: 'networkidle' });
  await page.addStyleTag({ content: `
    html { zoom: ${ZOOM}; scroll-behavior: auto !important; }
    .hero-demo .demo-play { display: none !important; }
  ` });

  /* the page starts the demo itself once the desk scrolls into view. stop that
     run so ours is the only thing on the clock */
  await page.evaluate(() => {
    runToken++;
    clearTimeout(startTimer);
    demoObs.disconnect();
    resetScene();
  });
  /* instant, not smooth: a smooth scroll is driven by animation frames, and
     under begin-frame control none are produced until we ask for them — so the
     page would still be gliding into place while the first frames are captured */
  await page.evaluate(() => {
    const hero = document.querySelector('.hero-demo');
    window.scrollTo({ top: hero.getBoundingClientRect().top + window.scrollY - 40, behavior: 'instant' });
  });
  await page.waitForTimeout(600);

  await cdp.send('Emulation.setVirtualTimePolicy', { policy: 'pause' });

  /* the starvation count has to stay small: the page never runs out of tasks —
     rAF loops and timers keep queueing — so a big one makes the clock wait for
     a quiet moment that never arrives */
  const step = async (ms) => {
    const done = new Promise((res) => cdp.once('Emulation.virtualTimeBudgetExpired', res));
    await cdp.send('Emulation.setVirtualTimePolicy', {
      policy: 'pauseIfNetworkFetchesPending', budget: ms, maxVirtualTimeTaskStarvationCount: 100,
    });
    await withTimeout(done, 15000, 'virtual time budget');
  };

  let n = 0, ticks = 1e6;
  const shoot = async (save = true) => {
    ticks += STEP_MS;
    const res = await withTimeout(cdp.send('HeadlessExperimental.beginFrame', {
      frameTimeTicks: ticks, interval: STEP_MS, noDisplayUpdates: false,
      screenshot: save ? { format: 'png', optimizeForSpeed: true } : undefined,
    }), 20000, 'beginFrame');
    if (!save) return;
    if (!res.screenshotData) throw new Error('beginFrame returned no pixels at frame ' + n);
    fs.writeFileSync(path.join(OUT, String(n).padStart(5, '0') + '.png'), Buffer.from(res.screenshotData, 'base64'));
    n++;
  };

  /* pump a few frames with nothing saved so scroll, fonts and the fit scale all
     land before anything is measured or captured */
  for (let i = 0; i < 12; i++) { await shoot(false); await step(STEP_MS); }

  /* beginFrame returns the whole viewport, so the desk's rect is where to cut.
     measured here, on the settled layout — zoom is already baked into it */
  const r = await page.evaluate(() => {
    const b = document.querySelector('.stage-desk').getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  });
  const ev = (n2) => { n2 = Math.round(n2); return n2 % 2 ? n2 - 1 : n2; };
  const crop = { w: ev(r.w), h: ev(r.h), x: ev(r.x), y: ev(r.y) };
  log('desk', JSON.stringify(r), '-> crop', `${crop.w}:${crop.h}:${crop.x}:${crop.y}`);

  for (let i = 0; i < Math.round(LEAD_S * FPS); i++) { await shoot(); await step(STEP_MS); }

  log('rolling');
  await page.evaluate(() => { runDemo(); });

  let doneAt = -1;
  while (n < MAX_FRAMES) {
    await shoot();
    await step(STEP_MS);
    if (doneAt < 0 && n % 5 === 0) {
      const txt = await page.evaluate(() => document.getElementById('demo-status').textContent);
      if (txt && txt.includes('Completed')) { doneAt = n; log('demo finished at frame', n); }
    }
    if (doneAt > 0 && n - doneAt >= HOLD_S * FPS) break;
    if (n % 120 === 0) log('frame', n);
  }

  /* the crop is one rect for the whole clip, so prove the desk never moved */
  const r2 = await page.evaluate(() => {
    const b = document.querySelector('.stage-desk').getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y) };
  });
  if (Math.abs(r2.x - r.x) > 1 || Math.abs(r2.y - r.y) > 1) {
    throw new Error(`desk drifted mid-render: ${JSON.stringify(r)} -> ${JSON.stringify(r2)}`);
  }
  log('desk held still at', JSON.stringify(r2));

  await browser.close();
  fs.writeFileSync(path.join(__dirname, 'render.json'), JSON.stringify({ frames: n, fps: FPS, crop }, null, 1));
  log('rendered', n, 'frames at', FPS + 'fps ->', OUT);
  console.log('CROP=' + `${crop.w}:${crop.h}:${crop.x}:${crop.y}`);
})();
