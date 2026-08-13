import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  readFileSync,
} from 'node:fs';
import path from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';

function checksum(file: string): string {
  return createHash('sha256').update(readFileSync(file)).digest('hex');
}

async function waitForJob(page: Page, jobId: string): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const response = await page.request.get(`/api/jobs/${jobId}`);
    expect(response.ok()).toBe(true);
    const job = await response.json() as Record<string, unknown>;
    const status = String(job.status);
    if (['completed', 'failed', 'cancelled'].includes(status)) return job;
    await page.waitForTimeout(250);
  }
  throw new Error(`Job ${jobId} did not reach a terminal state`);
}

async function runOnlyStage(
  page: Page,
  projectId: string,
  label: string,
  endpoint: string,
): Promise<Record<string, unknown>> {
  const dialog = page.getByRole('dialog', { name: '批处理与导出' });
  await selectOnlyStage(dialog, label);
  const queueButton = dialog.getByRole('button', { name: /加入队列/ });
  await expect(queueButton).toBeEnabled();
  const queuedResponse = page.waitForResponse(
    (response) => response.request().method() === 'POST'
      && response.url().endsWith(`/api/projects/${projectId}/${endpoint}`),
  );
  await queueButton.click();
  const response = await queuedResponse;
  expect(response.status()).toBe(202);
  const queued = await response.json() as { id: string };
  const completed = await waitForJob(page, queued.id);
  expect(completed, JSON.stringify(completed)).toMatchObject({
    status: 'completed',
    completed: completed.total,
  });
  const items = completed.items as Array<{ status: string; error?: string }>;
  expect(items.every((item) => item.status === 'completed' && !item.error)).toBe(true);
  await expect(
    dialog.locator('.job-card').filter({ hasText: label }).first(),
  ).toContainText('已完成');
  return completed;
}

async function selectOnlyStage(
  dialog: Locator,
  label: string,
): Promise<void> {
  const checkboxes = dialog.locator('.pipeline-steps input[type="checkbox"]');
  for (let index = 0; index < await checkboxes.count(); index += 1) {
    const checkbox = checkboxes.nth(index);
    if (await checkbox.isChecked()) await checkbox.uncheck();
  }
  await dialog.locator('.pipeline-steps label').filter({ hasText: label }).getByRole('checkbox').check();
}

async function reviewVisualStage(
  page: Page,
  modeLabel: '增强' | '擦除' | '成品',
  stage: 'preprocess' | 'inpaint' | 'typeset',
  action: '接受' | '拒绝' | '撤回复核',
  expectedState: '已接受' | '已拒绝' | '待复核',
): Promise<void> {
  await page.getByRole('button', { name: modeLabel, exact: true }).click();
  const controls = page.getByRole('group', { name: '当前视觉阶段复核' });
  if (stage === 'inpaint' && action !== '撤回复核') {
    const maskReview = controls.getByRole('checkbox', { name: '复核蒙版' });
    if (!(await maskReview.isChecked())) await maskReview.check();
  }
  const actionButton = controls.getByRole('button', { name: action, exact: true });
  await expect(actionButton).toBeEnabled();
  const reviewed = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && response.url().includes('/api/images/')
      && response.url().endsWith(`/stage-reviews/${stage}`),
  );
  await actionButton.click();
  const response = await reviewed;
  expect(response.status()).toBe(200);
  const body = response.request().postDataJSON() as Record<string, unknown>;
  if (action === '撤回复核') {
    expect(body).not.toHaveProperty('observedArtifactChecksum');
    expect(body).not.toHaveProperty('observedMaskChecksum');
  } else {
    expect(body.observedArtifactChecksum).toMatch(/^[0-9a-f]{64}$/);
    if (stage === 'inpaint') {
      expect(body.observedMaskChecksum).toMatch(/^[0-9a-f]{64}$/);
    } else {
      expect(body).not.toHaveProperty('observedMaskChecksum');
    }
  }
  await expect(controls.getByRole('status')).toHaveText(expectedState);
}

test('creates, edits, renders, exports, and reopens a local project', async ({ page }) => {
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const generatedRoot = path.resolve('tests/e2e/.generated');
  const fixtureDirectory = path.join(generatedRoot, `input-${runId}`);
  const fixtureImage = path.join(fixtureDirectory, 'chapter-01', '001.png');
  const projectRoot = path.join(generatedRoot, `project-${runId}`);
  const importedRelative = 'chapter-01';
  const projectName = `端到端项目 ${runId}`;
  const sourceText = 'こんにちは、せかい';
  const translatedText = '你好，世界';

  mkdirSync(path.dirname(fixtureImage), { recursive: true });
  execFileSync(
    'uv',
    ['run', '--project', 'backend', 'python', 'scripts/generate_test_image.py', fixtureImage],
    { cwd: process.cwd(), stdio: 'inherit' },
  );
  const originalChecksum = checksum(fixtureImage);

  await page.goto('/');
  await expect(page.getByRole('button', { name: '新建项目' })).toBeVisible();
  await page.getByRole('button', { name: '新建项目' }).click();

  const createDialog = page.getByRole('dialog', { name: '新建本地项目' });
  await createDialog.getByLabel('项目名称').fill(projectName);
  await createDialog.getByLabel('输出目录（可选）').fill(projectRoot);
  const createResponsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && response.url().endsWith('/api/projects'),
  );
  await createDialog.getByRole('button', { name: '创建项目' }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(201);
  const project = await createResponse.json() as { id: string };
  await expect(createDialog).toBeHidden();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);

  const folderInput = page.locator('.sidebar__imports input[webkitdirectory]');
  await folderInput.setInputFiles(fixtureDirectory);
  await expect(page.locator('.image-row__name')).toHaveText('001.png');
  await expect(page.locator('.image-group h3')).toContainText('chapter-01');

  await page.getByRole('button', { name: '在中央快速新建文本框' }).click();
  await page.getByLabel('日文原文').fill(sourceText);
  await page.getByLabel('中文译文').fill(translatedText);
  await page.getByLabel('文本方向').selectOption('horizontal');
  await page.getByLabel('确认此文本框').click();
  await expect(page.getByLabel('确认此文本框')).toBeChecked();
  await page.keyboard.press('ControlOrMeta+S');
  await expect(page.locator('.save-status')).toContainText(/已保存|已同步/);

  await page.getByRole('button', { name: '批处理与导出' }).click();
  const batchDialog = page.getByRole('dialog', { name: '批处理与导出' });
  await batchDialog.locator('.choice-cards label').filter({ hasText: '当前页' }).getByRole('radio').check();
  await runOnlyStage(page, project.id, '擦字修复', 'inpaint');
  await runOnlyStage(page, project.id, '嵌字排版', 'typeset');
  await batchDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  const reviewResponse = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && response.url().includes('/api/images/')
      && response.url().endsWith('/review'),
  );
  await page.getByRole('button', { name: '标记本页已检查' }).click();
  expect((await reviewResponse).status()).toBe(200);
  await page.getByRole('button', { name: '批处理与导出' }).click();
  await selectOnlyStage(batchDialog, '安全导出');
  await expect(batchDialog.getByText('所选图像版本尚未全部通过视觉复核')).toBeVisible();
  await expect(batchDialog.getByText(/1 页排版图未接受/)).toBeVisible();
  await expect(batchDialog.getByText(/1 页无字底图未接受/)).toBeVisible();
  await expect(batchDialog.getByRole('button', { name: /加入队列/ })).toBeDisabled();
  await batchDialog.getByLabel('导出内容').selectOption('json');
  await expect(batchDialog.getByText('仅文本 JSON 不受页面复核门禁')).toBeVisible();
  await expect(batchDialog.getByRole('button', { name: /加入队列/ })).toBeEnabled();
  await batchDialog.getByLabel('导出内容').selectOption('both');
  await batchDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await reviewVisualStage(page, '擦除', 'inpaint', '接受', '已接受');
  await reviewVisualStage(page, '成品', 'typeset', '拒绝', '已拒绝');
  await page.getByRole('button', { name: '批处理与导出' }).click();
  await selectOnlyStage(batchDialog, '安全导出');
  await expect(batchDialog.getByText(/1 页排版图未接受/)).toBeVisible();
  await expect(batchDialog.getByText(/无字底图未接受/)).toBeHidden();
  await expect(batchDialog.getByRole('button', { name: /加入队列/ })).toBeDisabled();
  await batchDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await reviewVisualStage(page, '成品', 'typeset', '接受', '已接受');
  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await page.getByRole('button', { name: '擦除', exact: true }).click();
  await expect(page.getByRole('group', { name: '当前视觉阶段复核' }).getByRole('status')).toHaveText('已接受');
  await page.getByRole('button', { name: '成品', exact: true }).click();
  await expect(page.getByRole('group', { name: '当前视觉阶段复核' }).getByRole('status')).toHaveText('已接受');
  await page.getByRole('button', { name: '批处理与导出' }).click();
  await runOnlyStage(page, project.id, '安全导出', 'export');

  const translatedImage = path.join(projectRoot, 'translated', importedRelative, '001.png');
  const originalJson = path.join(projectRoot, 'original-text', importedRelative, '001.json');
  const translatedJson = path.join(projectRoot, 'translated-text', importedRelative, '001.json');
  const mask = path.join(projectRoot, 'masks', importedRelative, '001.png');
  await expect.poll(() => existsSync(translatedImage), { timeout: 20_000 }).toBe(true);
  expect(existsSync(originalJson)).toBe(true);
  expect(existsSync(translatedJson)).toBe(true);
  expect(existsSync(mask)).toBe(true);
  expect(existsSync(path.join(projectRoot, 'project', 'project.json'))).toBe(true);
  expect(existsSync(path.join(projectRoot, 'project', 'project.sqlite3'))).toBe(true);
  expect(readFileSync(translatedJson, 'utf8')).toContain(translatedText);
  expect(checksum(fixtureImage)).toBe(originalChecksum);

  await batchDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await page.getByRole('button', { name: '成品', exact: true }).click();
  await expect(page.getByRole('application', { name: '成品画布' })).toBeVisible();

  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await expect(page.locator('.image-row__name')).toHaveText('001.png');
  await page.locator('.region-index button').first().click();
  await expect(page.getByLabel('日文原文')).toHaveValue(sourceText);
  await expect(page.getByLabel('中文译文')).toHaveValue(translatedText);

  await page.getByRole('button', { name: '新建项目' }).click();
  const secondProjectDialog = page.getByRole('dialog', { name: '新建本地项目' });
  await secondProjectDialog.getByLabel('项目名称').fill(`临时切换项目 ${runId}`);
  await secondProjectDialog.getByLabel('输出目录（可选）').fill(`${projectRoot}-second`);
  await secondProjectDialog.getByRole('button', { name: '创建项目' }).click();
  await expect(page.locator('.topbar__project-name')).toHaveText(`临时切换项目 ${runId}`);

  await page.getByRole('button', { name: '打开本地项目' }).click();
  const openDialog = page.getByRole('dialog', { name: '打开已有项目' });
  await openDialog
    .getByLabel('项目清单路径')
    .fill(path.join(projectRoot, 'project', 'project.json'));
  await openDialog.getByRole('button', { name: '打开项目' }).click();
  await expect(openDialog).toBeHidden();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await page.locator('.region-index button').first().click();
  await expect(page.getByLabel('中文译文')).toHaveValue(translatedText);
});

test('runs real local detection and Japanese OCR before review and export', async ({ page }) => {
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const generatedRoot = path.resolve('tests/e2e/.generated');
  const fixtureDirectory = path.join(generatedRoot, `ocr-input-${runId}`);
  const fixtureImage = path.join(fixtureDirectory, '日本語 章', '001.png');
  const projectRoot = path.join(generatedRoot, `ocr-project-${runId}`);
  const projectName = `真实 OCR 项目 ${runId}`;
  const reviewedTranslation = '经过人工校对的中文';
  const importedRelative = '日本語 章';

  mkdirSync(path.dirname(fixtureImage), { recursive: true });
  execFileSync(
    'uv',
    ['run', '--project', 'backend', 'python', 'scripts/generate_test_image.py', fixtureImage],
    { cwd: process.cwd(), stdio: 'inherit' },
  );
  const originalChecksum = checksum(fixtureImage);

  const configResponse = await page.request.get('/api/config');
  expect(configResponse.ok()).toBe(true);
  const config = await configResponse.json() as {
    providers: { ocr: { tesseract: { available: boolean } } };
    capabilities: { fonts: { available: boolean } };
  };
  expect(config.providers.ocr.tesseract.available).toBe(true);
  expect(config.capabilities.fonts.available).toBe(true);

  await page.goto('/');
  await page.getByRole('button', { name: '新建项目' }).click();
  const createDialog = page.getByRole('dialog', { name: '新建本地项目' });
  await createDialog.getByLabel('项目名称').fill(projectName);
  await createDialog.getByLabel('输出目录（可选）').fill(projectRoot);
  const createResponsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && response.url().endsWith('/api/projects'),
  );
  await createDialog.getByRole('button', { name: '创建项目' }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(201);
  const project = await createResponse.json() as { id: string };

  const inspector = page.getByRole('complementary', { name: '属性检查器' });
  await inspector.getByRole('tab', { name: '项目' }).click();
  const settingsResponse = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && response.url().endsWith(`/api/projects/${project.id}`),
  );
  await inspector.getByRole('combobox', { name: '翻译' }).selectOption('mock');
  await page.keyboard.press('ControlOrMeta+S');
  expect((await settingsResponse).status()).toBe(200);
  await expect(page.locator('.save-status')).toContainText(/已保存|已同步/);

  await page.locator('.sidebar__imports input[webkitdirectory]').setInputFiles(fixtureDirectory);
  await expect(page.getByRole('heading', { name: /日本語 章/ })).toBeVisible();
  await expect(page.locator('.image-row__name')).toHaveText('001.png');

  await page.getByRole('button', { name: '批处理与导出' }).click();
  const batchDialog = page.getByRole('dialog', { name: '批处理与导出' });
  await batchDialog.locator('.choice-cards label').filter({ hasText: '当前页' }).getByRole('radio').check();

  await runOnlyStage(page, project.id, '图片增强', 'preprocess');
  await batchDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await page.getByRole('button', { name: '增强', exact: true }).click();
  await expect(page.getByRole('application', { name: '增强画布' })).toBeVisible();
  await reviewVisualStage(page, '增强', 'preprocess', '接受', '已接受');
  await page.getByRole('button', { name: '批处理与导出' }).click();

  const detected = await runOnlyStage(page, project.id, '文字检测', 'detect');
  const detectedOutput = (detected.items as Array<{ output: { count: number } }>)[0]?.output;
  expect(detectedOutput?.count).toBeGreaterThan(0);
  await runOnlyStage(page, project.id, '日文 OCR', 'ocr');
  const recognizedImagesResponse = await page.request.get(`/api/projects/${project.id}/images`);
  const recognizedImages = await recognizedImagesResponse.json() as Array<{ id: string }>;
  const recognizedRegionsResponse = await page.request.get(
    `/api/images/${recognizedImages[0]?.id}/regions`,
  );
  const recognizedRegions = await recognizedRegionsResponse.json() as Array<{
    id: string;
    sourceText: string;
    order: number;
    revision: number;
    ignored: boolean;
    trustDisposition: 'review' | 'trusted' | 'ignored';
  }>;
  const recognizedRegion = recognizedRegions.find((region) => region.sourceText.trim().length > 0);
  expect(recognizedRegion, JSON.stringify(recognizedRegions)).toBeDefined();
  expect(recognizedRegion?.trustDisposition).toBe('review');
  expect(JSON.stringify(detected)).not.toContain('"regionId"');

  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await page.getByRole('button', { name: '批处理与导出' }).click();
  const trustDialog = page.getByRole('dialog', { name: '批处理与导出' });
  await trustDialog.locator('.choice-cards label').filter({ hasText: '当前页' }).getByRole('radio').check();
  await selectOnlyStage(trustDialog, '翻译');
  await expect(trustDialog.getByText(/还有 \d+ 个 OCR 文本框待信任确认/)).toBeVisible();
  await expect(trustDialog.getByRole('button', { name: /加入队列/ })).toBeDisabled();
  await trustDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();

  await inspector.getByRole('tab', { name: '文本' }).click();
  await inspector
    .locator('.region-index button')
    .filter({ hasText: recognizedRegion!.sourceText })
    .first()
    .click();
  const trustStatus = inspector.getByRole('region', { name: 'OCR 信任状态' });
  await expect(trustStatus).toContainText('OCR 待信任');
  await expect(trustStatus).toContainText('OCR 已完成，置信度不能代替人工确认');
  await expect(trustStatus).toContainText(/检测 .* · OCR .*/);
  const confirmTrustResponse = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && response.url().endsWith(`/api/regions/${recognizedRegion!.id}`),
  );
  await inspector.getByLabel('确认此文本框').click();
  expect((await confirmTrustResponse).status()).toBe(200);
  await expect(inspector.getByLabel('确认此文本框')).toBeChecked();
  await expect(trustStatus).toContainText('OCR 已信任');

  const pendingRegionsResponse = await page.request.get(
    `/api/images/${recognizedImages[0]?.id}/regions`,
  );
  const pendingRegions = await pendingRegionsResponse.json() as Array<{
    id: string;
    sourceText: string;
    revision: number;
    trustDisposition: 'review' | 'trusted' | 'ignored';
  }>;
  for (const region of pendingRegions.filter((entry) => entry.trustDisposition === 'review')) {
    const response = await page.request.patch(`/api/regions/${region.id}`, {
      data: region.sourceText.trim()
        ? { confirmed: true, ignored: false, expectedRevision: region.revision }
        : { confirmed: false, ignored: true, expectedRevision: region.revision },
    });
    expect(response.ok()).toBe(true);
  }

  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await page.getByRole('button', { name: '批处理与导出' }).click();
  const translatedDialog = page.getByRole('dialog', { name: '批处理与导出' });
  await translatedDialog.locator('.choice-cards label').filter({ hasText: '当前页' }).getByRole('radio').check();
  await runOnlyStage(page, project.id, '翻译', 'translate');

  await translatedDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await inspector.getByRole('tab', { name: '文本' }).click();
  await inspector
    .locator('.region-index button')
    .filter({ hasText: recognizedRegion!.sourceText })
    .first()
    .click();
  await expect(inspector.getByLabel('日文原文')).not.toHaveValue('');
  await expect(inspector.getByLabel('中文译文')).not.toHaveValue('');
  await inspector.getByLabel('中文译文').fill(reviewedTranslation);
  await inspector.getByLabel('确认此文本框').click();
  await expect(inspector.getByLabel('确认此文本框')).toBeChecked();
  await page.keyboard.press('ControlOrMeta+S');
  await expect(page.locator('.save-status')).toContainText(/已保存|已同步/);

  const latestRegionsResponse = await page.request.get(
    `/api/images/${recognizedImages[0]?.id}/regions`,
  );
  const latestRegions = await latestRegionsResponse.json() as Array<{
    id: string;
    ignored: boolean;
    confirmed: boolean;
    revision: number;
  }>;
  for (const region of latestRegions.filter((entry) => !entry.ignored && !entry.confirmed)) {
    const response = await page.request.patch(`/api/regions/${region.id}`, {
      data: { confirmed: true, ignored: false, expectedRevision: region.revision },
    });
    expect(response.ok()).toBe(true);
  }
  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  await inspector.getByRole('tab', { name: '文本' }).click();
  await inspector
    .locator('.region-index button')
    .filter({ hasText: recognizedRegion!.sourceText })
    .first()
    .click();

  await page.getByRole('button', { name: '批处理与导出' }).click();
  const renderDialog = page.getByRole('dialog', { name: '批处理与导出' });
  await renderDialog.locator('.choice-cards label').filter({ hasText: '当前页' }).getByRole('radio').check();
  await runOnlyStage(page, project.id, '擦字修复', 'inpaint');
  await renderDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await inspector.getByRole('tab', { name: '修复' }).click();
  const maskResponsePromise = page.waitForResponse(
    (response) => response.request().method() === 'GET'
      && response.url().includes('/generated/mask'),
  );
  await inspector.getByLabel('显示实际蒙版').check();
  await expect(inspector.getByLabel('显示实际蒙版')).toBeChecked();
  await reviewVisualStage(page, '擦除', 'inpaint', '接受', '已接受');
  expect((await maskResponsePromise).status()).toBe(200);
  await page.getByRole('button', { name: '批处理与导出' }).click();
  await runOnlyStage(page, project.id, '嵌字排版', 'typeset');
  await renderDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await reviewVisualStage(page, '成品', 'typeset', '接受', '已接受');
  const reviewResponse = page.waitForResponse(
    (response) => response.request().method() === 'PATCH'
      && response.url().includes('/api/images/')
      && response.url().endsWith('/review'),
  );
  await inspector.getByRole('button', { name: '标记本页已检查' }).click();
  expect((await reviewResponse).status()).toBe(200);
  await page.getByRole('button', { name: '批处理与导出' }).click();
  await runOnlyStage(page, project.id, '安全导出', 'export');

  const translatedImage = path.join(projectRoot, 'translated', importedRelative, '001.png');
  const translatedJson = path.join(
    projectRoot,
    'translated-text',
    importedRelative,
    '001.json',
  );
  expect(existsSync(translatedImage)).toBe(true);
  expect(existsSync(translatedJson)).toBe(true);
  expect(existsSync(path.join(projectRoot, 'generated', 'preprocessed', importedRelative, '001.png'))).toBe(true);
  expect(readFileSync(translatedJson, 'utf8')).toContain(reviewedTranslation);
  expect(checksum(fixtureImage)).toBe(originalChecksum);

  const imagesResponse = await page.request.get(`/api/projects/${project.id}/images`);
  const images = await imagesResponse.json() as Array<{
    status: Record<string, string>;
    stageReviews: Record<string, { state: string }>;
    preprocessingProvider?: string;
    ocrProvider?: string;
  }>;
  expect(images[0]?.preprocessingProvider).toBe('opencv-pillow');
  expect(images[0]?.ocrProvider).toBe('tesseract');
  expect(images[0]?.status).toMatchObject({
    preprocess: 'done',
    detection: 'done',
    ocr: 'done',
    translation: 'done',
    inpaint: 'done',
    typeset: 'done',
    export: 'done',
  });
  expect(images[0]?.stageReviews).toMatchObject({
    preprocess: { state: 'accepted' },
    inpaint: { state: 'accepted' },
    typeset: { state: 'accepted' },
  });

  await renderDialog.getByRole('button', { name: '关闭批处理抽屉' }).click();
  await page.reload();
  await expect(page.locator('.topbar__project-name')).toHaveText(projectName);
  for (const mode of ['增强', '擦除', '成品'] as const) {
    await page.getByRole('button', { name: mode, exact: true }).click();
    await expect(page.getByRole('group', { name: '当前视觉阶段复核' }).getByRole('status')).toHaveText('已接受');
  }
  await inspector.getByRole('tab', { name: '文本' }).click();
  await inspector
    .locator('.region-index button')
    .filter({ hasText: recognizedRegion!.sourceText })
    .first()
    .click();
  await expect(inspector.getByLabel('中文译文')).toHaveValue(reviewedTranslation);
});
