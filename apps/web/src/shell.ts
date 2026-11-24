// Which entry the current window belongs to. The desktop host (Tauri) opens
// `?shell=desktop` once; the identity is then kept in sessionStorage for this window
// so a reload, an opened session or a rewritten `/?goal=…` URL cannot silently drop it.
// On this branch the browser default is also the desktop workbench: a normal tab with no
// parameter shows the full shell, and the practice entry is reached with `?shell=web`.
export type Shell='web'|'desktop';
const KEY='py-shell';
export function resolveShell():Shell{
 // F07Z-138: an explicit `?shell=` must win even when sessionStorage throws (blocked storage in
 // a hardened profile). Before this split the whole body shared one try block, so a storage
 // error also discarded the parameter and a user could not reach the practice entry at all.
 try{
  const param=new URLSearchParams(window.location.search).get('shell');
  if(param==='desktop'||param==='web'){
   try{sessionStorage.setItem(KEY,param);}catch{/* not fatal: the explicit choice still applies */}
   return param;
  }
 }catch{/* no window/location: fall through to the stored value, then the workbench */}
 try{
  const saved=sessionStorage.getItem(KEY);
  if(saved==='desktop'||saved==='web')return saved;
 }catch{/* storage may be blocked; fall back to the workbench */}
 return 'desktop';
}

// Appearance for the desktop shell only. Stored in localStorage because it is a user
// preference that must survive closing the window; the web entry never reads it.
export type Theme='light'|'dark'|'auto';
export const THEME_KEY='py-desktop-theme';
export function resolveTheme():Theme{
 try{
  const saved=localStorage.getItem(THEME_KEY);
  if(saved==='light'||saved==='dark'||saved==='auto')return saved;
 }catch{/* storage may be blocked; the default below is still correct */}
 return 'light';
}
export function storeTheme(theme:Theme){
 try{localStorage.setItem(THEME_KEY,theme);}catch{/* not fatal: the in-memory value still applies */}
}
export function systemPrefersDark():boolean{
 try{return window.matchMedia('(prefers-color-scheme: dark)').matches;}catch{return false;}
}
export function paintTheme(theme:Theme,systemDark:boolean):'light'|'dark'{
 return theme==='auto'?(systemDark?'dark':'light'):theme;
}
