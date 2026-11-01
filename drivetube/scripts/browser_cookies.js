// drive-tube: apre YouTube in un Chromium vero (Playwright) sul server e stampa
// in JSON i cookie ottenuti. Lo lancia drivetube/browser.py.
// Variabili: DT_BROWSER_DIR (dove e' installato playwright), DT_UA (user agent),
// DT_VIDEO (video da aprire), DT_YT_BASE (solo per le prove: sito al posto di YouTube).
const path = require('path');
const { chromium } = require(path.join(process.env.DT_BROWSER_DIR, 'node_modules', 'playwright'));

(async () => {
  const base = process.env.DT_YT_BASE || 'https://www.youtube.com';
  const out = { ok: false, steps: [] };
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--mute-audio']
        .concat((process.env.DT_BROWSER_ARGS || '').split('|').filter(Boolean)),   // solo per le prove
    });
    const ctx = await browser.newContext({ userAgent: process.env.DT_UA || undefined, locale: 'it-IT' });
    const page = await ctx.newPage();
    await page.goto(base + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    out.steps.push('aperta ' + new URL(page.url()).hostname);
    // banner del consenso ai cookie (in Europa): "Accetta tutto"
    const sels = ['button[aria-label*="Accetta"]', 'button[aria-label*="Accept"]',
                  'form[action*="consent"] button', 'text=Accetta tutto', 'text=Accept all'];
    for (const sel of sels) {
      const b = page.locator(sel).first();
      if (await b.count()) {
        try { await b.click({ timeout: 5000 }); out.steps.push('consenso accettato'); break; } catch (e) {}
      }
    }
    await page.waitForTimeout(4000);
    await page.goto(base + '/watch?v=' + (process.env.DT_VIDEO || 'jNQXAC9IVRw'),
                    { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(8000);   // il tempo di far girare gli script di YouTube
    out.steps.push('video aperto');
    out.cookies = await ctx.cookies();
    out.ok = true;
  } catch (e) {
    // Playwright mette il motivo vero nelle righe dopo la prima (es. librerie mancanti)
    out.error = String(e && e.message || e).split('\n')
      .map(s => s.replace(/[\u2500-\u257f]/g, '').trim()).filter(Boolean).join(' | ').slice(0, 800);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  process.stdout.write(JSON.stringify(out));
})();
