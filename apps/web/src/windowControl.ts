// Window controls for the undecorated desktop window.
//
// The product window draws its own top bar, so the app has to provide what the
// native Windows title bar used to: moving the window, minimising it, maximising
// it and closing it. These calls only reach this process's own window.
//
// In a normal browser tab there is no Tauri bridge, so every helper reports
// "unavailable" instead of throwing: the web entry must keep working unchanged.

// The bridge is injected by the host (withGlobalTauri). It is absent in a browser.
type TauriBridge = {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
};

function bridge(): TauriBridge | null {
  const candidate = (globalThis as { __TAURI__?: TauriBridge }).__TAURI__;
  return candidate && typeof candidate.core?.invoke === 'function' ? candidate : null;
}

/**
 * Whether the host bridge is present *right now*.
 *
 * The host injects `__TAURI__` after the page starts loading, which can be after
 * the first React render. Callers that need to re-render once the bridge shows
 * up must poll this instead of reading it once at module load.
 */
export function hasWindowControl(): boolean {
  return bridge() !== null;
}

/** Ask the host to move the window. Returns false when there is no host. */
export async function dragWindow(): Promise<boolean> {
  const api = bridge();
  if (!api) return false;
  try {
    await api.core.invoke('window_control', { action: 'drag' });
    return true;
  } catch {
    return false;
  }
}

export async function windowAction(action: 'minimize' | 'toggle_maximize' | 'close'): Promise<boolean> {
  const api = bridge();
  if (!api) return false;
  try {
    await api.core.invoke('window_control', { action });
    return true;
  } catch {
    return false;
  }
}

/**
 * The window's current state, or null when there is no host.
 *
 * The top bar uses this to show "maximise" or "restore" on the same button.
 * Without it the button looks identical in both states, and a working toggle
 * reads as "nothing happened".
 */
export async function windowState(): Promise<{maximized: boolean; minimized: boolean} | null> {
  const api = bridge();
  if (!api) return null;
  try {
    return await api.core.invoke<{maximized: boolean; minimized: boolean}>('window_state');
  } catch {
    return null;
  }
}

/**
 * Props for a whole surface (the top bar): press anywhere that is not a control to drag.
 *
 * F07Z-105: only the title text and a 24px gap were drag regions, so in a 1280px bar
 * exactly one sampled point out of 32 moved the window. The surface now accepts the press
 * itself and skips interactive descendants, so the empty parts of the bar behave like a
 * title bar while buttons, inputs, menus and links keep working.
 *
 * F07Z-107: the attribute is deliberately NOT used. It was relied on for presses on the
 * container itself, but Tauri's built-in drag-region handler does not run on the remote
 * page the product loads (http://127.0.0.1:3100), so the padding around the omnibox was
 * dead while only the title text worked. Every press now goes through the explicit invoke,
 * which is the path that does work on that origin.
 */
const INTERACTIVE = 'button,input,textarea,select,a,summary,label,form,details,[data-no-drag]';
// F07Z-133: the double-click surface is wider than the drag surface. The omnibox is the largest
// part of the bar, so excluding its form/input left most of the bar dead - the user reported the
// top bar "not maximising". Buttons keep their own double-click (theme toggle, account, window
// controls, submit), and a press-drag still ignores the input so text selection is not stolen.
const CAPTION_EXCEPTIONS = 'button,[data-no-drag]';

export function dragSurfaceProps(): {
  onMouseDown: (event: { button: number; target: EventTarget | null }) => void;
  onDoubleClick: (event: { target: EventTarget | null }) => void;
} {
  return {
    onMouseDown: event => {
      if (event.button !== 0) return;
      const target = event.target as Element | null;
      if (target?.closest?.(INTERACTIVE)) return;
      void dragWindow();
    },
    // F07Z-108: a title bar maximises on double click; the custom bar has to provide it.
    // F07Z-133: only real buttons are excluded, so the omnibox counts as caption.
    onDoubleClick: event => {
      const target = event.target as Element | null;
      if (target?.closest?.(CAPTION_EXCEPTIONS)) return;
      void windowAction('toggle_maximize');
    },
  };
}

/**
 * Props for the sidebar: double-click anywhere on it maximises or restores the window.
 *
 * F07Z-122: a user double-clicks the sidebar expecting the window to maximise, the way a
 * title bar behaves, and the sidebar is the app's outermost edge. Unlike the top bar this
 * surface does NOT skip interactive descendants, and the difference is deliberate: the
 * top bar holds the address/omnibox form, appearance switch and window controls, where a
 * second click is meaningful, while every entry in the sidebar is a navigation button whose
 * double-click does nothing on its own. Excluding those buttons would leave only the 9px
 * padding and the 4px gaps between entries as a target, which is where "double-clicking the
 * sidebar does nothing" comes from. Navigation is unaffected: a single click still switches
 * the pane, and the double-click only adds the maximise.
 *
 * Dragging stays on the top bar. The sidebar scrolls (`overflow:auto`), and a press on the
 * scrollbar thumb targets the container itself, so a drag handler here would move the window
 * instead of the thumb.
 */
export function railCaptionProps(): { onDoubleClick: () => void } {
  return {
    onDoubleClick: () => {
      void windowAction('toggle_maximize');
    },
  };
}
