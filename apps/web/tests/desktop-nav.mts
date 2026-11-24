// Shared navigation helper. F07Z-82: the rail is the only navigation surface again - the
// artifacts page had copied all seven detail entries, and that copy is gone. These suites
// were written when the rail held three entries and the details lived in the artifacts view,
// so they call this helper by page title; it now clicks the rail entry with that title.
import type {Page} from '@playwright/test';

export async function openDetail(page: Page, title: string): Promise<void> {
  const railEntry = page.locator(`.desktop-rail .d-nav-item[title="${title}"]`);
  if (!(await railEntry.count())) {
    throw new Error(`no rail entry titled "${title}" - the page may have been renamed or moved`);
  }
  await railEntry.first().click();
}

// 收尾 02-2: one readiness criterion for "the goal is really loaded".
// The header carries `data-goal-state` (ready / loading / error / empty), so a suite no longer
// has to exclude Chinese copy by hand - the previous `hasNotText:'尚未创建'` broke the moment the
// loading surface started saying "正在读取目标".
export async function waitGoalReady(page: Page, timeout = 60000): Promise<void> {
  await page.locator('[data-goal-state="ready"]').first().waitFor({timeout});
}
