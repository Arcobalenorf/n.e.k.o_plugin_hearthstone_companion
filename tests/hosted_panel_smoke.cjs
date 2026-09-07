// Run with: node tests/hosted_panel_smoke.cjs <NEKO root> [screenshot directory]
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const host = path.resolve(process.argv[2]);
const deps = path.join(host, 'frontend/plugin-manager/node_modules');
const ts = require(path.join(deps, 'typescript'));
const { chromium } = require(path.join(deps, 'playwright'));
const source = path.join(root, 'ui/panel.tsx');
const sdk = path.join(host, 'plugin/sdk/hosted-ui');
const options = {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
  jsx: ts.JsxEmit.React, jsxFactory: 'NekoUiKit.h', jsxFragmentFactory: 'NekoUiKit.Fragment',
  noEmit: true, skipLibCheck: true,
  baseUrl: root, paths: { '@neko/plugin-ui': [path.join(sdk, 'index.d.ts')] },
};
const program = ts.createProgram([source, path.join(sdk, 'globals.d.ts')], {
  ...options, jsx: ts.JsxEmit.Preserve, jsxFactory: undefined, jsxFragmentFactory: undefined,
});
const errors = ts.getPreEmitDiagnostics(program).filter(d => d.category === ts.DiagnosticCategory.Error);
assert.equal(errors.length, 0, errors.map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n')).join('\n'));
const compiled = ts.transpileModule(fs.readFileSync(source, 'utf8'), { compilerOptions: options }).outputText;
const kit = path.join(host, 'frontend/plugin-manager/src/components/plugin/hosted/ui-kit');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.HOSTED_TEST_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined),
  });
  try {
    for (const locale of ['zh-CN', 'en']) {
      for (const width of [390, 1280]) {
        const page = await browser.newPage({ viewport: { width, height: 900 } });
        const pageErrors = [];
        page.on('pageerror', error => pageErrors.push(error.message));
        await page.setContent('<html><head></head><body><div id="root"></div></body></html>');
        await page.addStyleTag({ content: fs.readFileSync(path.join(kit, 'styles.css'), 'utf8') });
        await page.addScriptTag({ content: fs.readFileSync(path.join(kit, 'runtime.js'), 'utf8') });
        await page.addScriptTag({ content: `(function(){const exports={};const require=()=>window.NekoUiKit;${compiled};window.TestPanel=exports.default;})()` });
        const messages = JSON.parse(fs.readFileSync(path.join(root, 'i18n', `${locale}.json`), 'utf8'));
        await page.evaluate(({ messages, locale }) => {
          window.calls = [];
          const props = {
            plugin: { id: 'hearthstone_companion' }, state: {
              runtime: { monitor_running: true, source_state: 'watching', snapshot_revision: 12345, resolved_log_path: 'C:/' + 'long-game-folder/'.repeat(12) + 'Power.log' },
              game: { mode: 'constructed', phase: 'playing', round: 4 },
              settings: { llm_data_consent: true, llm_do_not_disturb: false },
            },
            locale, warnings: [], actions: ['save_settings', 'export_diagnostics', 'start_monitoring'].map(id => ({ id })),
            t: (key, args = {}) => Object.entries(args).reduce((text, [name, value]) => text.replaceAll(`{${name}}`, String(value)), messages[key] || key),
            api: { refresh: async () => {}, call: async (id, args) => { window.calls.push({ id, args }); return { success: true, data: {} }; } },
          };
          window.panelProps = props;
          window.NekoUiKit.render(window.NekoUiKit.h(window.TestPanel, props), document.getElementById('root'));
        }, { messages, locale });
        const accordions = page.locator('.neko-accordion-trigger');
        assert.equal(await accordions.count(), 4);
        for (const trigger of await accordions.all()) assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(await page.getByText(messages['diagnostics.snapshotRevision'], { exact: true }).count(), 0);
        assert.equal(await page.locator('input').count() > 0, true);
        for (const [mode, phase, fresh, stale] of [
          ['constructed', 'playing', true, false],
          ['constructed', 'playing', false, true],
          ['constructed', 'mulligan', false, true],
          ['constructed', 'starting', false, true],
          ['battlegrounds', 'hero_select', false, true],
          ['battlegrounds', 'recruit', false, true],
          ['battlegrounds', 'combat', false, true],
          ['constructed', 'playing', undefined, false],
          ['constructed', 'ended', false, false],
          ['battlegrounds', 'ended', false, false],
          ['constructed', 'spectator', false, false],
          ['battlegrounds', 'spectator', false, false],
          ['constructed', 'idle', false, false],
          ['unknown', 'unknown', false, false],
        ]) {
          await page.evaluate(({ mode, phase, fresh }) => {
            window.panelProps.state.game = { mode, phase, round: 4, battlegrounds: { round: 7 } };
            window.panelProps.state.diagnostics = { log: { fresh } };
            window.NekoUiKit.render(window.NekoUiKit.h(window.TestPanel, window.panelProps), document.getElementById('root'));
          }, { mode, phase, fresh });
          assert.equal(await page.getByText(messages['warnings.staleGame'], { exact: true }).count(), stale ? 1 : 0, `${mode}/${phase}/${fresh}`);
          assert.equal(await page.getByText(messages['status.staleGame'], { exact: true }).count(), stale ? 1 : 0);
          const roundLabel = messages[stale ? 'metrics.lastObservedRound' : 'metrics.round'];
          await page.getByText(`${roundLabel}: ${mode === 'battlegrounds' ? 7 : 4}`, { exact: true }).waitFor();
          for (const trigger of await accordions.all()) assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        }
        await page.evaluate(() => {
          window.panelProps.state.game = { mode: 'constructed', phase: 'playing', round: 4 };
          window.panelProps.state.diagnostics = { log: { fresh: true } };
          window.NekoUiKit.render(window.NekoUiKit.h(window.TestPanel, window.panelProps), document.getElementById('root'));
        });
        if (process.argv[3]) {
          fs.mkdirSync(process.argv[3], { recursive: true });
          await page.screenshot({ path: path.join(process.argv[3], `panel-${locale}-${width}.png`), fullPage: true, animations: 'disabled' });
        }
        const diagnostics = page.getByRole('button', { name: messages['sections.diagnostics.title'], exact: true });
        await diagnostics.click();
        assert.equal(await diagnostics.getAttribute('aria-expanded'), 'true');
        await page.getByText(messages['diagnostics.snapshotRevision'], { exact: true }).waitFor();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        if (process.argv[3]) await page.screenshot({ path: path.join(process.argv[3], `diagnostics-${locale}-${width}.png`), fullPage: true, animations: 'disabled' });
        await page.evaluate(() => window.NekoUiKit.render(window.NekoUiKit.h(window.TestPanel, window.panelProps), document.getElementById('root')));
        assert.equal(await diagnostics.getAttribute('aria-expanded'), 'true');
        await diagnostics.click();
        await page.locator('input[type="text"]').first().fill('Test role');
        await page.getByRole('button', { name: messages['actions.save_settings.label'], exact: true }).click();
        assert.equal(await page.evaluate(() => window.calls.some(c => c.id === 'save_settings' && c.args.target_lanlan === 'Test role')), true);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.deepEqual(pageErrors, []);
        await page.close();
      }
    }
    console.log('Hosted panel: typecheck, four collapsed sections, settings, refresh persistence and responsive layout passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
