// Records the app footage for the demo with a scripted browser. Three clips, 1920x1080:
//   landing.webm  public landing page -> Launch App -> public PAPER dashboard
//   paper.webm    local paper engine on live mainnet data: typed prompt, propose, execute, two vetoes
//   live.webm     public LIVE dashboard: the real position and its receipt
// Every keystroke / click / verdict is logged with its time so the composition can place sounds.
const { chromium } = require('/Users/mrnetwork/.npm/_npx/db89d7302a373f10/node_modules/playwright');
const fs = require('fs'); const path = require('path');
const OUT = path.resolve(__dirname, '..', 'public', 'footage');
const PUBLIC = 'https://usedeltrapp.vercel.app';
const LOCAL = 'http://127.0.0.1:3012';
const VP = { width: 1920, height: 1080 };

async function clip(name, fn) {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: VP, deviceScaleFactor: 1, recordVideo: { dir: OUT, size: VP } });
  const p = await ctx.newPage();
  const t0 = Date.now(); const events = [];
  const mark = (kind, note = '') => events.push({ t: (Date.now() - t0) / 1000, kind, note });
  const wait = (ms) => p.waitForTimeout(ms);
  const type = async (sel, text) => { await p.click(sel); mark('click', 'focus'); for (const ch of text) { await p.keyboard.type(ch); mark('key', ch); await wait(38 + Math.random() * 70); } };
  const smoothScroll = async (to, ms) => { const from = await p.evaluate(() => window.scrollY); const steps = Math.max(8, Math.round(ms / 40)); for (let i = 1; i <= steps; i++) { const k = i / steps; const e = k < 0.5 ? 2 * k * k : -1 + (4 - 2 * k) * k; await p.evaluate((y) => window.scrollTo(0, y), from + (to - from) * e); await wait(ms / steps); } };
  await fn({ p, mark, wait, type, smoothScroll });
  await wait(600);
  const video = p.video(); await ctx.close(); await b.close();
  const tmp = await video.path(); const dst = path.join(OUT, `${name}.webm`); fs.renameSync(tmp, dst);
  fs.writeFileSync(path.join(OUT, `${name}.events.json`), JSON.stringify({ name, seconds: (Date.now() - t0) / 1000, events }, null, 1));
  console.log(name, 'recorded', ((Date.now() - t0) / 1000).toFixed(1) + 's', events.length, 'events');
}

const ONLY = process.argv[2] || null;
const run = (name, fn) => (!ONLY || ONLY === name) ? clip(name, fn) : Promise.resolve();
(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  await run('landing', async ({ p, mark, wait, smoothScroll }) => {
    await p.goto(PUBLIC + '/', { waitUntil: 'networkidle' }); mark('scene', 'landing'); await wait(3500);
    await smoothScroll(700, 2600); mark('scroll'); await wait(1800);
    await smoothScroll(1500, 2600); mark('scroll'); await wait(5200);           // how-it-works switcher animating
    await smoothScroll(2600, 2600); mark('scroll'); await wait(2600);           // architecture / edge
    await smoothScroll(0, 1800); mark('scroll'); await wait(1200);
    const btn = p.locator('a:has-text("Launch App")').first(); await btn.hover(); await wait(700);
    mark('click', 'launch'); await btn.click();
    await p.waitForURL('**/app/**', { timeout: 20000 }); await wait(9000); mark('scene', 'paper-public');
  });

  await run('paper', async ({ p, mark, wait, type, smoothScroll }) => {
    await p.goto(LOCAL + '/app/', { waitUntil: 'networkidle' }); await wait(3500); mark('scene', 'paper-local');
    const input = 'input[placeholder*="Rebalance"]';
    await p.locator(input).scrollIntoViewIfNeeded(); await wait(800);
    await type(input, 'Rebalance $5,000 USDC into delta-neutral BNB arbitrage.'); await wait(500);
    mark('click', 'propose'); await p.click('button:has-text("propose")');
    await p.waitForSelector('button:has-text("Execute plan")', { timeout: 30000 }); mark('result', 'precheck-approved'); await wait(2200);
    mark('click', 'execute'); await p.click('button:has-text("Execute plan")');
    await p.waitForSelector('a:has-text("receipt rcpt_")', { timeout: 40000 }); mark('result', 'filled'); await wait(1500);
    await p.locator('#trace').scrollIntoViewIfNeeded(); mark('scroll', 'trace'); await wait(5200);
    await p.locator(input).scrollIntoViewIfNeeded(); await wait(600);
    await p.fill(input, ''); await type(input, 'Do it again with $50,000 at 10x.'); await wait(400);
    mark('click', 'propose'); await p.click('button:has-text("propose")');
    await p.waitForSelector('text=VETO LEVERAGE', { timeout: 30000 }); mark('veto', 'LEVERAGE'); await wait(2800);
    await p.fill(input, ''); await type(input, 'Fine, $50,000 at 3x.'); await wait(400);
    mark('click', 'propose'); await p.click('button:has-text("propose")');
    await p.waitForSelector('text=VETO CAPITAL_RISK', { timeout: 30000 }); mark('veto', 'CAPITAL_RISK'); await wait(2400);
    await p.evaluate(() => window.scrollTo(0, 0)); await wait(400);
    await p.locator('section:has(h2:text-is("Risk gate"))').scrollIntoViewIfNeeded(); mark('scroll', 'gate'); await wait(5000);
  });

  await run('live', async ({ p, mark, wait, smoothScroll }) => {
    await p.goto(PUBLIC + '/app/?engine=live', { waitUntil: 'load' });
    await p.waitForSelector('text=REAL FUNDS ARMED', { timeout: 60000 }); await wait(6000); mark('scene', 'live');
    const card = p.locator('section:has(h2:text-is("Positions"))').first(); await card.scrollIntoViewIfNeeded(); mark('scroll', 'position'); await wait(4500);
    await p.locator('#trace').scrollIntoViewIfNeeded(); mark('scroll', 'trace'); await wait(6000);
    await p.evaluate(() => window.scrollTo(0, 0)); await wait(2500);
  });
})().catch((e) => { console.error('CAPTURE FAILED', e.message); process.exit(1); });
