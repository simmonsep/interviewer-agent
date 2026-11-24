// F07Z-148: one tiny invalidation event. It carries no data - only "this area changed, re-read
// it from the server". Panels stay the owners of their own queries; nothing here is a state
// machine, a cache or a second source of truth. The rule it protects: a derived surface (rail
// badge, workbench progress, recent sessions) must follow the real record without waiting for a
// user-initiated refresh or a page reload.

export type DataArea = 'interview' | 'materials' | 'application' | 'job' | 'goal';

const EVENT = 'py-data-changed';

export function notifyDataChanged(area: DataArea): void {
  try {
    window.dispatchEvent(new CustomEvent(EVENT, {detail: {area}}));
  } catch {
    /* a blocked window must not break the action that just succeeded */
  }
}

export function onDataChanged(handler: (area: DataArea) => void): () => void {
  const listener = (event: Event) => {
    const area = (event as CustomEvent<{area?: DataArea}>).detail?.area;
    if (area) handler(area);
  };
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}
