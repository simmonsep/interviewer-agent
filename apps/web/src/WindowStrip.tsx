import {useEffect,useState} from 'react';
import {dragSurfaceProps,hasWindowControl,windowAction,windowState} from './windowControl.ts';

/**
 * Window chrome for the screens that run before the workbench (booting and login).
 *
 * F07Z-105: the product window has no native title bar, and those two screens used to
 * render no top bar at all — so at the login screen the window could neither be moved nor
 * closed. This strip is the smallest thing that restores both without inventing a second
 * full layout. It renders nothing in a browser tab.
 */
export function WindowStrip(){
 const [available,setAvailable]=useState(hasWindowControl());
 const [maximized,setMaximized]=useState(false);
 useEffect(()=>{
  let alive=true;
  const tick=async()=>{
   const state=await windowState();
   if(!alive)return;
   if(state){setAvailable(true);setMaximized(state.maximized);}
  };
  void tick();
  const timer=setInterval(()=>void tick(),1500);
  return()=>{alive=false;clearInterval(timer);};
 },[]);
 if(!available)return null;
 return <div className="desktop-window-strip" {...dragSurfaceProps()}>
  <span className="desktop-window-strip-title">求职工作台</span>
  <div className="d-window-controls" role="group" aria-label="窗口控制">
   <button type="button" data-testid="win-minimize" title="最小化" aria-label="最小化"
    onClick={()=>void windowAction('minimize')}>─</button>
   <button type="button" data-testid="win-maximize" data-maximized={maximized?'true':'false'}
    title={maximized?'还原窗口':'最大化'} aria-label={maximized?'还原窗口':'最大化'}
    aria-pressed={maximized}
    onClick={()=>void windowAction('toggle_maximize')}>{maximized?'❐':'▢'}</button>
   <button type="button" className="is-close" data-testid="win-close" title="关闭" aria-label="关闭"
    onClick={()=>void windowAction('close')}>✕</button>
  </div>
 </div>;
}
