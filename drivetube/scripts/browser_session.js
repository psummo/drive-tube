// drive-tube: browser del server con profilo persistente, comandato riga per
// riga da drivetube/browser.py (JSON su stdin, una risposta JSON per riga su stdout).
// Serve per fare una volta il login a un account Google dalla pagina admin e per
// rinnovare poi i cookie con lo stesso profilo.
//
// Comandi: {"cmd":"goto","url":...}  {"cmd":"shot","path":...}  {"cmd":"click","x":..,"y":..}
//          {"cmd":"type","text":...}  {"cmd":"key","key":"Enter"}  {"cmd":"cookies"}
//          {"cmd":"addcookies","cookies":[...]}  {"cmd":"close"}
// Variabili: DT_BROWSER_DIR, DT_PROFILE (cartella del profilo), DT_UA, DT_BROWSER_ARGS (prove).
const path = require('path');
const readline = require('readline');
const { chromium } = require(path.join(process.env.DT_BROWSER_DIR, 'node_modules', 'playwright'));

const W = 1000, H = 800;

function reply(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

(async () => {
  let ctx, page;
  try {
    const opts = {
      headless: true,
      viewport: { width: W, height: H },
      locale: 'it-IT',
      userAgent: process.env.DT_UA || undefined,
      args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--mute-audio']
        .concat((process.env.DT_BROWSER_ARGS || '').split('|').filter(Boolean)),
    };
    try {
      // headless "nuovo" (Chromium completo): meno riconoscibile dai controlli di Google
      ctx = await chromium.launchPersistentContext(process.env.DT_PROFILE, Object.assign({ channel: 'chromium' }, opts));
    } catch (e) {
      ctx = await chromium.launchPersistentContext(process.env.DT_PROFILE, opts);
    }
    page = ctx.pages()[0] || await ctx.newPage();
    reply({ ok: true, ready: true });
  } catch (e) {
    reply({ ok: false, error: String(e && e.message || e).split('\n')
      .map(s => s.replace(/[─-╿]/g, '').trim()).filter(Boolean).join(' | ').slice(0, 800) });
    process.exit(1);
  }

  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    let c;
    try { c = JSON.parse(line); } catch (e) { reply({ ok: false, error: 'comando non valido' }); continue; }
    try {
      if (c.cmd === 'goto') {
        await page.goto(c.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
        await page.waitForTimeout(c.wait || 1500);
        reply({ ok: true, url: page.url() });
      } else if (c.cmd === 'shot') {
        await page.screenshot({ path: c.path, type: 'jpeg', quality: 70 });
        reply({ ok: true, url: page.url(), title: await page.title() });
      } else if (c.cmd === 'click') {
        await page.mouse.click(Math.max(0, Math.min(W - 1, c.x)), Math.max(0, Math.min(H - 1, c.y)));
        await page.waitForTimeout(700);
        reply({ ok: true, url: page.url() });
      } else if (c.cmd === 'type') {
        await page.keyboard.type(String(c.text || ''), { delay: 40 });
        reply({ ok: true });
      } else if (c.cmd === 'key') {
        await page.keyboard.press(String(c.key || 'Enter'));
        await page.waitForTimeout(1200);
        reply({ ok: true, url: page.url() });
      } else if (c.cmd === 'cookies') {
        reply({ ok: true, url: page.url(), cookies: await ctx.cookies() });
      } else if (c.cmd === 'addcookies') {
        await ctx.addCookies(c.cookies || []);
        reply({ ok: true, added: (c.cookies || []).length });
      } else if (c.cmd === 'close') {
        reply({ ok: true });
        break;
      } else {
        reply({ ok: false, error: 'comando sconosciuto: ' + c.cmd });
      }
    } catch (e) {
      reply({ ok: false, error: String(e && e.message || e).split('\n')[0].slice(0, 300), url: page.url() });
    }
  }
  await ctx.close().catch(() => {});
  process.exit(0);
})();
