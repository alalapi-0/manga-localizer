import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium, expect } from '@playwright/test';
import { resolveActiveFrontendRuntime } from './storage-frontend-route.mjs';

const root = path.resolve(import.meta.dirname, '..');
resolveActiveFrontendRuntime();
const { createServer } = await import(pathToFileURL(path.join(root, 'frontend/node_modules/vite/dist/node/index.js')).href);
const receipt = JSON.parse(readFileSync(path.join(root, 'docs/reports/all-projects-governance/manga-retry/live-recovery.json'), 'utf8'));
receipt.cases = receipt.observed_handoffs;
if (!receipt.unchanged || receipt.cases.length !== 3) throw new Error('Three unchanged observed DB handoffs are required; this script proves isolated UI only');
const output = path.join(root, '.agent/audits/manga-retry-ui');
mkdirSync(output, { recursive: true });
const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Isolated retry UI proof</title><div id="root"></div><script type="module">
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from '/src/App.tsx';
import { FinalReviewPage, RepairContextBanner } from '/src/finalReview/FinalReviewPage.tsx';
import { useFinalReviewStore } from '/src/finalReview/store.ts';
import { finalReviewItemFixture, finalReviewBatchFixture } from '/src/finalReview/testFixtures.ts';
import { useWorkbenchStore } from '/src/store/workbench.ts';
import { seedWorkbench } from '/src/test/fixtures.ts';
import { ApiError, api } from '/src/api/client.ts';
import '/src/styles.css';
const cases = ${JSON.stringify(receipt.cases)};
const selected = cases[Number(new URL(location.href).searchParams.get('case') || 0)];
const scenario = new URL(location.href).searchParams.get('scenario') || 'retry';
const handoff = selected.handoff;
const item = finalReviewItemFixture(handoff.itemId, { position: 1, batchId: 'isolated-retry-proof', sourceProjectId: handoff.sourceProjectId, sourceImageId: handoff.sourceImageId, sourceRelativePath: '隔离回归.png', revision: handoff.finalReviewItemRevision, artifactRevision: handoff.artifactRevision, verdict: 'issues', issueCodes: ['mask'], feedback: '隔离界面回归，未生成或审核真实图片', strictEvidence: selected.strict_evidence, formatVersion: selected.strict_evidence ? 2 : 1 });
const batch = {...finalReviewBatchFixture([item]), id: item.batchId, revision: handoff.batchRevision, name: '隔离回归样例'};
useFinalReviewStore.setState({ batches: [batch], batch, items: [item], activeItemId: item.id, draft: { verdict: 'issues', issueCodes: ['mask'], feedback: item.feedback } });
window.proof = { calls: [], expected: handoff, context: () => useFinalReviewStore.getState().repairContext, state: () => useFinalReviewStore.getState(), verdict: () => useFinalReviewStore.getState().items[0].verdict };
api.beginFinalReviewRepair = async (id, body) => { if (id !== item.id || body.expectedRevision !== item.revision || body.expectedBatchRevision !== batch.revision || body.retryFromGenerationId) throw new Error('Unexpected mutation'); window.proof.calls.push(['repair', id]); return structuredClone(handoff); };
let saveAttempts = 0;
api.updateFinalReviewItem = async (id, body) => { window.proof.calls.push(['save', id, structuredClone(body)]); saveAttempts += 1; if (saveAttempts === 1) throw new ApiError('隔离保存失败，请重试', 422); return { item: {...item, verdict: body.verdict, issueCodes: body.issueCodes, feedback: body.feedback, revision: item.revision + 1, reviewedAt: '2026-09-08T12:00:00Z'}, batchRevision: batch.revision + 1, historyCreated: true }; };
useWorkbenchStore.setState({ selectProject: async (id, reload) => { window.proof.calls.push(['project', id, reload]); return id === handoff.repairProjectId && reload; }, selectImage: async (id) => { window.proof.calls.push(['image', id]); return id === handoff.repairImageId; } });
function Proof() { const [opened, setOpened] = React.useState(Boolean(useFinalReviewStore.getState().repairContext)); return opened ? React.createElement('main', {style:{padding:'16px'}}, React.createElement('p', null, '隔离回归：修复上下文已恢复'), React.createElement(RepairContextBanner, {onReturn: () => setOpened(false)})) : React.createElement(FinalReviewPage, {onOpenWorkbench: () => setOpened(true)}); }
if (scenario === 'hash') seedWorkbench({images: [], regions: []});
createRoot(document.getElementById('root')).render(React.createElement(scenario === 'hash' ? App : Proof));
</script></html>`;
const server = await createServer({ root: path.join(root, 'frontend'), server: {host:'127.0.0.1', port:0, strictPort:false}, plugins:[{name:'isolated-retry-proof', configureServer(s) { s.middlewares.use(async (req,res,next) => {if (!req.url?.startsWith('/__retry-proof.html') || req.url.includes('html-proxy')) return next(); try {res.setHeader('Content-Type','text/html');res.end(await s.transformIndexHtml(req.url,html));} catch(e) {next(e);} });}}] });
let browser;
const results = [];
try {
 await server.listen();
 const address = server.httpServer?.address();
 if (!address || typeof address === 'string') throw new Error('Unable to resolve isolated Vite port');
 const baseUrl = `http://127.0.0.1:${address.port}/__retry-proof.html`;
 browser = await chromium.launch({headless:true, executablePath:process.env.MANGA_LOCALIZER_E2E_BROWSER_EXECUTABLE || undefined});
 for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
  for (let index=0;index<receipt.cases.length;index++) {
   const context = await browser.newContext({viewport,locale:'zh-CN',reducedMotion:'reduce'});
   const page = await context.newPage(); const errors=[];const unexpected=[];const requests=[];
   page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
   await page.route('**/api/**',async route=>{const u=new URL(route.request().url()); if (!u.pathname.startsWith('/api/')) return route.continue(); requests.push({method:route.request().method(),path:u.pathname}); if (/\/(artifacts\/final|thumbnail)$/.test(u.pathname)) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800"><rect width="1200" height="1800" fill="#ece7df"/><text x="100" y="200" font-size="72">Isolated fixture</text></svg>'});unexpected.push(u.pathname);await route.abort();});
   await page.goto(`${baseUrl}?case=${index}`);
   if(viewport.width<600)await page.getByRole('button',{name:'审核与导出',exact:true}).click();
   const button=page.getByRole('button',{name:'进入修复工作台',exact:true});try {await expect(button).toBeEnabled();} catch(error) {writeFileSync(path.join(output,'failure.json'),JSON.stringify({errors,unexpected,body:await page.locator('body').innerText()},null,2));await page.screenshot({path:path.join(output,'failure.png')});throw error;}
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
   await page.screenshot({path:path.join(output,`${viewport.width}-${index}-before.png`),fullPage:true});
   await button.focus();await page.keyboard.press('Enter');await expect(page.getByText('隔离回归：修复上下文已恢复')).toBeVisible();
   const proof=await page.evaluate(()=>({calls:window.proof.calls,context:window.proof.context(),verdict:window.proof.verdict()}));
   expect(proof.calls.map(c=>c[0])).toEqual(['repair','project','image']);expect(proof.context.pageGenerationId).toBe(receipt.cases[index].handoff.pageGenerationId);expect(proof.verdict).toBe('issues');
   await page.reload();await expect(page.getByText('隔离回归：修复上下文已恢复')).toBeVisible();
   expect(await page.evaluate(()=>window.proof.context().runId)).toBe(receipt.cases[index].handoff.runId);
   await page.screenshot({path:path.join(output,`${viewport.width}-${index}-resumed.png`),fullPage:true});
   expect(errors).toEqual([]);expect(unexpected).toEqual([]);
   results.push({scenario:'retry-handoff',viewport,index,item_id:receipt.cases[index].item_id,keyboard_native_button:true,session_reload:true,verdict_unchanged:true,reduced_motion:true,console_errors:errors,observed_network:requests,unexpected_network:unexpected});
   await context.close();
  }

  {
   const context = await browser.newContext({viewport,locale:'zh-CN',reducedMotion:'reduce'});
   const page = await context.newPage(); const errors=[];const unexpected=[];const requests=[];
   page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
   await page.route('**/api/**',async route=>{const u=new URL(route.request().url());if (!u.pathname.startsWith('/api/')) return route.continue();requests.push({method:route.request().method(),path:u.pathname});if (/\/(artifacts\/final|thumbnail)$/.test(u.pathname)) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800"><rect width="1200" height="1800" fill="#ece7df"/><text x="100" y="200" font-size="72">Isolated fixture</text></svg>'});unexpected.push(u.pathname);await route.abort();});
   await page.goto(`${baseUrl}?case=0&scenario=save`);
   if(viewport.width<600)await page.getByRole('button',{name:'审核与导出',exact:true}).click();
   const approved=page.getByRole('radio',{name:'完全没问题',exact:true});
   try {await expect(approved).toBeVisible({timeout:5000});} catch(error) {writeFileSync(path.join(output,'failure-save.json'),JSON.stringify({url:page.url(),errors,unexpected,body:await page.locator('body').innerText()},null,2));await page.screenshot({path:path.join(output,'failure-save.png'),fullPage:true});throw error;}
   await approved.click();
   const saveNext=page.getByRole('button',{name:'保存并下一张',exact:true});
   await expect(saveNext).toBeEnabled();
   await saveNext.click();
   await expect(page.getByText('隔离保存失败，请重试',{exact:true})).toBeVisible();
   const failed=await page.evaluate(()=>({draft:window.proof.state().draft,conflict:window.proof.state().conflict,active:window.proof.state().activeItemId,calls:window.proof.calls}));
   expect(failed.draft.verdict).toBe('approved');expect(failed.conflict).toBe(false);expect(failed.active).toBe(receipt.cases[0].handoff.itemId);expect(failed.calls.filter(c=>c[0]==='save')).toHaveLength(1);
   await page.screenshot({path:path.join(output,`${viewport.width}-save-error.png`),fullPage:true});
   await saveNext.click();
   await expect(page.getByText(/已保存/)).toBeVisible();
   const saved=await page.evaluate(()=>({draft:window.proof.state().draft,active:window.proof.state().activeItemId,verdict:window.proof.verdict(),calls:window.proof.calls}));
   expect(saved.draft.verdict).toBe('approved');expect(saved.active).toBe(receipt.cases[0].handoff.itemId);expect(saved.verdict).toBe('approved');expect(saved.calls.filter(c=>c[0]==='save')).toHaveLength(2);
   await page.screenshot({path:path.join(output,`${viewport.width}-save-success.png`),fullPage:true});
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);expect(errors).toEqual([]);expect(unexpected).toEqual([]);
   results.push({scenario:'last-item-save-retry',viewport,error_preserved_draft:true,retry_saved_without_navigation:true,reduced_motion:true,console_errors:errors,observed_network:requests,unexpected_network:unexpected});
   await context.close();
  }

  {
   const context = await browser.newContext({viewport,locale:'zh-CN',reducedMotion:'reduce'});
   const page = await context.newPage(); const errors=[];const unexpected=[];const requests=[];
   page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
   await page.route('**/api/**',async route=>{const u=new URL(route.request().url());if (!u.pathname.startsWith('/api/')) return route.continue();requests.push({method:route.request().method(),path:u.pathname});if (/\/(artifacts\/final|thumbnail)$/.test(u.pathname)) return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800"><rect width="1200" height="1800" fill="#ece7df"/><text x="100" y="200" font-size="72">Isolated fixture</text></svg>'});unexpected.push(u.pathname);await route.abort();});
   await page.goto(`${baseUrl}?case=0&scenario=hash#final-review`);
   if(viewport.width<600)await page.getByRole('button',{name:'审核与导出',exact:true}).click();
   await page.getByRole('radio',{name:'完全没问题',exact:true}).click();
   page.once('dialog',dialog=>dialog.dismiss());
   await page.evaluate(()=>{location.hash='';});
   await expect(page.locator('main.final-review')).toBeVisible();
   await expect.poll(()=>page.evaluate(()=>location.hash)).toBe('#final-review');
   expect(await page.evaluate(()=>window.proof.state().draft.verdict)).toBe('approved');
   await page.screenshot({path:path.join(output,`${viewport.width}-hash-denied.png`),fullPage:true});
   page.once('dialog',dialog=>dialog.accept());
   await page.evaluate(()=>{location.hash='';});
   await expect(page.locator('.workbench-grid')).toBeVisible();
   expect(await page.evaluate(()=>location.hash)).toBe('');
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
   expect(errors).toEqual([]);expect(unexpected).toEqual([]);
   results.push({scenario:'hash-dirty-guard',viewport,decline_preserved_draft_and_url:true,accept_navigated:true,reduced_motion:true,console_errors:errors,observed_network:requests,unexpected_network:unexpected});
   await context.close();
  }
 }
 writeFileSync(path.join(output,'result.json'),JSON.stringify({kind:'isolated real-component browser proof; API and navigation substitutes; no actual workbench or image generation claim',results},null,2)+'\n');
 console.log(JSON.stringify({passed:results.length,output}));
} finally {await browser?.close();await server.close();}
