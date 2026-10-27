import {useCallback, useEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {AudioLines, FileVideo, LoaderCircle, Upload, X} from 'lucide-react';
import {errorText, request} from './api';
import type {Sound} from './api';
import './video-import.css';

const VIDEO_ACCEPT='.mp4,.mov,.m4v,.webm,.mkv,.avi';
export async function discardVideoSource(id:string) {
  await request(`/video-sources/${encodeURIComponent(id.replace(/^video-/,''))}`,{method:'DELETE'}).catch(()=>undefined);
}

/** A temporary source enters the editor; only its saved edit enters the library. */
export default function VideoImport({onReady,notify}:{onReady:(sound:Sound)=>void;notify:(message:string,kind:'error')=>void}) {
  const [open,setOpen]=useState(false);
  const [dragging,setDragging]=useState(false);
  const [filename,setFilename]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const input=useRef<HTMLInputElement>(null);
  const dialog=useRef<HTMLDivElement>(null);
  const trigger=useRef<HTMLButtonElement>(null);
  const job=useRef<{id:string;controller:AbortController}|null>(null);
  const ready=useRef(onReady);ready.current=onReady;
  const notifyRef=useRef(notify);notifyRef.current=notify;
  const dragDepth=useRef(0);

  const cancel=useCallback(()=>{
    const current=job.current;job.current=null;
    if(current){current.controller.abort();void discardVideoSource(current.id);}
    setBusy(false);setOpen(false);setDragging(false);setError('');
    trigger.current?.focus();
  },[]);

  const importFiles=useCallback(async(files:File[])=>{
    if(job.current)return;
    setOpen(true);setDragging(false);setError('');setFilename('');
    if(files.length!==1){setError('请一次拖入一个视频，完成剪辑后再添加下一个。');return;}
    const file=files[0];
    if(!/\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(file.name)){setError('请选择 MP4、MOV、M4V、WebM、MKV 或 AVI 视频。音频文件请使用「导入音效」。');return;}
    if(!file.size||file.size>512*1024*1024){setError('请选择非空且不超过 512 MB 的视频。');return;}
    const current={id:crypto.randomUUID().replaceAll('-',''),controller:new AbortController()};
    job.current=current;setFilename(file.name);setBusy(true);
    const body=new FormData();body.append('file',file);
    try {
      const sound=await request<Sound>(`/video-sources/${current.id}`,{method:'POST',body,signal:current.controller.signal});
      if(job.current!==current){void discardVideoSource(current.id);return;}
      job.current=null;setBusy(false);setOpen(false);ready.current(sound);
    } catch(reason) {
      void discardVideoSource(current.id);
      if(job.current!==current)return;
      job.current=null;setBusy(false);setError(errorText(reason));
    }
  },[]);

  useEffect(()=>{
    const otherDialog=()=>Array.from(document.querySelectorAll('[role="dialog"]:not(.video-import-dialog)')).some(element=>element.getClientRects().length>0);
    const hasFiles=(event:DragEvent)=>Array.from(event.dataTransfer?.types||[]).includes('Files');
    const enter=(event:DragEvent)=>{if(!hasFiles(event))return;event.preventDefault();dragDepth.current++;if(!job.current&&!otherDialog())setDragging(true);};
    const over=(event:DragEvent)=>{if(!hasFiles(event))return;event.preventDefault();if(event.dataTransfer)event.dataTransfer.dropEffect=job.current||otherDialog()?'none':'copy';};
    const leave=(event:DragEvent)=>{if(!hasFiles(event))return;dragDepth.current=Math.max(0,dragDepth.current-1);if(!dragDepth.current)setDragging(false);};
    const drop=(event:DragEvent)=>{
      if(!hasFiles(event))return;event.preventDefault();dragDepth.current=0;setDragging(false);
      if(job.current){notifyRef.current('正在提取视频音轨，请完成或取消后再添加视频。','error');return;}
      if(otherDialog()){notifyRef.current('请先完成并关闭当前窗口，再拖入视频。','error');return;}
      void importFiles(Array.from(event.dataTransfer?.files||[]));
    };
    const reset=()=>{dragDepth.current=0;setDragging(false);};
    window.addEventListener('dragenter',enter);window.addEventListener('dragover',over);window.addEventListener('dragleave',leave);window.addEventListener('drop',drop);window.addEventListener('blur',reset);window.addEventListener('dragend',reset);
    return()=>{
      window.removeEventListener('dragenter',enter);window.removeEventListener('dragover',over);window.removeEventListener('dragleave',leave);window.removeEventListener('drop',drop);window.removeEventListener('blur',reset);window.removeEventListener('dragend',reset);
      const current=job.current;job.current=null;if(current){current.controller.abort();void discardVideoSource(current.id);}
    };
  },[importFiles]);

  useEffect(()=>{
    if(!open)return;
    if(!dialog.current?.contains(document.activeElement))dialog.current?.focus();
    const keys=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){event.preventDefault();cancel();}
      if(event.key==='Tab'){
        const controls=Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')||[]);
        const first=controls[0],last=controls.at(-1);
        if(!dialog.current?.contains(document.activeElement)||document.activeElement===dialog.current){event.preventDefault();(event.shiftKey?last:first)?.focus();}
        else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
      }
    };
    document.addEventListener('keydown',keys);return()=>document.removeEventListener('keydown',keys);
  },[open,busy,cancel]);

  return <>
    <button ref={trigger} className="button small secondary video-import-trigger" onClick={()=>{setError('');setFilename('');setOpen(true);}} title="也可直接把视频拖到页面上"><FileVideo size={16}/>从视频提取</button>
    <input ref={input} type="file" accept={VIDEO_ACCEPT} hidden aria-label="选择要提取音轨的视频" onChange={event=>{const files=Array.from(event.target.files||[]);event.target.value='';if(files.length)void importFiles(files);}}/>
    {open&&createPortal(<div className="modal-backdrop"><div className="modal video-import-dialog" role="dialog" aria-modal="true" aria-label="从视频提取音效" tabIndex={-1} ref={dialog}>
      <header className="modal-header"><div><span className="eyebrow">VIDEO TO GAME AUDIO</span><h2>把视频里的好声音，留下来。</h2><p>拖入视频 → 截取和调整 → 保存到素材库</p></div><button className="icon-button" aria-label={busy?'取消视频提取':'关闭视频提取'} onClick={cancel}><X size={20}/></button></header>
      <div className="video-import-content">
        {busy?<div className="video-extract-progress" role="status"><LoaderCircle size={35} className="spin"/><h3>正在提取音轨…</h3><p className="video-filename">{filename}</p><p>在本机处理，完成后自动进入剪辑器。</p><div className="video-progress-track"/></div>:<button className="video-drop-zone" onClick={()=>input.current?.click()}><FileVideo size={38}/><strong>拖入视频，或点击选择</strong><span>MP4 · MOV · M4V · WebM · MKV · AVI</span><small>单个视频不超过 512 MB，音轨不超过 10 分钟</small></button>}
        {error&&<p className="video-import-error" role="alert">{error}</p>}
        <div className="video-import-note"><AudioLines size={18}/><p>提取视频的第一条音轨，保留其中已有的音效、音乐和人声。进入剪辑器后可裁剪、拼接、调节音量和淡入淡出。</p></div>
        <p className="video-import-local">本机处理 · 不消耗 AI 额度 · 保存前不加入素材库</p>
      </div>
      <footer className="modal-footer"><span>原视频保持不变</span><button className="button secondary" onClick={cancel}>{busy?'取消提取':'返回素材库'}</button>{!busy&&<button className="button" onClick={()=>input.current?.click()}><Upload size={15}/>{error?'重新选择视频':'选择视频'}</button>}</footer>
    </div></div>,document.body)}
    {dragging&&createPortal(<div className="video-page-drop" aria-live="polite"><div><FileVideo size={44}/><strong>松开，提取视频音轨</strong><span>接着裁剪、试听，保存成你的游戏音效</span></div></div>,document.body)}
  </>;
}
