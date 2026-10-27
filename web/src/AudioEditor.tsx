import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import type {CSSProperties, ReactNode} from 'react';
import {ArrowDown, ArrowUp, AudioLines, Check, ChevronRight, Copy, Download, Folder, Headphones, Info, LoaderCircle, Pause, Play, Plus, Scissors, Search, SlidersHorizontal, Trash2, X} from 'lucide-react';
import {downloadEditorPreview, errorText, fileDownload, post, request} from './api';
import type {AudioClipRecipe, AudioEditPreview, AudioEditRecipe, Folder as SoundFolder, Sound} from './api';
import './audio-editor.css';
import ShortcutHelp from './ShortcutHelp';
import FadeEnvelope from './FadeEnvelope';
import {isSpace, nativeNavigationTarget, nativeSpaceTarget, typingTarget} from './keyboard';

type EditClip = AudioClipRecipe & {key: string; sound: Sound};
type PreviewState = {result: AudioEditPreview; recipeKey: string};
type Props = {
  initialSounds: Sound[];
  folders: SoundFolder[];
  prepareSound: (sound: Sound) => Promise<Sound>;
  onClose: () => void;
  onSaved: (sound: Sound) => void;
};
const MAX_CLIPS=12;
const MAX_DURATION=240;
const minimumClipLength=0.02;
const round=(value:number)=>Math.round(value*1000)/1000;
const clamp=(value:number,min:number,max:number)=>Math.max(min,Math.min(max,value));
const finite=(value:string)=>Number.isFinite(Number(value))?Number(value):0;
const durationText=(value:number)=>`${Number.isFinite(value)?value.toFixed(2):'0.00'} 秒`;

function asClip(sound:Sound):EditClip {
  if(!sound.downloaded||!Number.isFinite(sound.duration)||sound.duration<=0)throw new Error(`「${sound.name}」没有可读取的音频或时长，请重新下载后再剪辑。`);
  if(sound.duration<minimumClipLength)throw new Error(`「${sound.name}」短于 0.02 秒，无法剪辑。请使用至少 0.02 秒的音效。`);
  return {key:crypto.randomUUID(),sound,sound_id:sound.id,start:0,end:Math.min(sound.duration,MAX_DURATION),gain_db:0,fade_in:0,fade_out:0,fade_in_curve:'linear',fade_out_curve:'linear'};
}
async function discardPreview(id:string){await request(`/editor/previews/${encodeURIComponent(id)}`,{method:'DELETE'}).catch(()=>undefined);}
function FolderOptions({folders}:{folders:SoundFolder[]}) {
  const options:ReactNode[]=[];
  function walk(parent:string|null,depth:number){for(const folder of folders.filter(item=>item.parent_id===parent)){options.push(<option key={folder.id} value={folder.id}>{'　'.repeat(depth)}{folder.name}</option>);walk(folder.id,depth+1);}}
  walk(null,0);return <>{options}</>;
}
function AudioWave({waveform}:{waveform:number[]}) {
  const bars=useMemo(()=>{
    if(!waveform?.length)return [];
    const count=Math.min(waveform.length,110);
    const points=Array.from({length:count},(_,index)=>{const start=Math.floor(index*waveform.length/count);const end=Math.max(start+1,Math.floor((index+1)*waveform.length/count));return Math.max(...waveform.slice(start,end).map(value=>Math.abs(Number(value)||0)));});
    const max=Math.max(...points,.001);return points.map(value=>Math.max(1,value/max*55));
  },[waveform]);
  if(!bars.length)return <div className="editor-no-wave"><AudioLines size={18}/><span>暂无波形，可使用起止秒数精确截取</span></div>;
  return <svg viewBox={`0 0 ${bars.length*4} 60`} preserveAspectRatio="none" className="editor-wave-svg" role="img" aria-label="原音频实际波形">{bars.map((height,index)=><rect key={index} x={index*4} y={(60-height)/2} width="2" height={height} rx="1"/>)}</svg>;
}

export default function AudioEditor({initialSounds,folders,prepareSound,onClose,onSaved}:Props) {
  const [clips,setClips]=useState<EditClip[]>([]);
  const [preparing,setPreparing]=useState(true);
  const [prepareMessage,setPrepareMessage]=useState('正在准备剪辑素材…');
  const [error,setError]=useState('');
  const [masterGain,setMasterGain]=useState(0);
  const [gap,setGap]=useState(0);
  const [format,setFormat]=useState<'wav'|'mp3'>('wav');
  const [name,setName]=useState(initialSounds.length===1?`${initialSounds[0].name.slice(0,195)} · 剪辑`:'音效拼接');
  const [folderId,setFolderId]=useState(initialSounds.length===1?initialSounds[0].folder_id||'':'');
  const [preview,setPreview]=useState<PreviewState|null>(null);
  const [rendering,setRendering]=useState(false);
  const [saving,setSaving]=useState(false);
  const [savedSound,setSavedSound]=useState<Sound|null>(null);
  const [savedRecipeKey,setSavedRecipeKey]=useState('');
  const [playing,setPlaying]=useState(false);
  const [playTime,setPlayTime]=useState(0);
  const [confirmClose,setConfirmClose]=useState(false);
  const [pickerOpen,setPickerOpen]=useState(false);
  const [libraryQuery,setLibraryQuery]=useState('');
  const [libraryItems,setLibraryItems]=useState<Sound[]>([]);
  const [libraryLoading,setLibraryLoading]=useState(false);
  const [libraryError,setLibraryError]=useState('');
  const [addingId,setAddingId]=useState('');
  const dialogRef=useRef<HTMLDivElement>(null);
  const audioRef=useRef<HTMLAudioElement|null>(null);
  const aliveRef=useRef(true);
  const previewIdRef=useRef<string|null>(null);
  const renderController=useRef<AbortController|null>(null);
  const renderVersion=useRef(0);
  const initialPreparation=useRef<Promise<EditClip[]>|null>(null);
  const onCloseRef=useRef(onClose);
  const keyboardRef=useRef<(event:KeyboardEvent)=>void>(()=>{});
  onCloseRef.current=onClose;

  const recipe=useMemo<AudioEditRecipe>(()=>({clips:clips.map(({sound_id,start,end,gain_db,fade_in,fade_out,fade_in_curve='linear',fade_out_curve='linear'})=>({sound_id,start,end,gain_db,fade_in,fade_out,fade_in_curve,fade_out_curve})),master_gain_db:masterGain,gap,format}),[clips,masterGain,gap,format]);
  const recipeKey=JSON.stringify(recipe);
  const totalDuration=clips.reduce((total,clip)=>total+clip.end-clip.start,0)+Math.max(0,clips.length-1)*gap;
  const stale=!!preview&&preview.recipeKey!==recipeKey;
  const alreadySaved=!!savedSound&&savedRecipeKey===recipeKey;
  const busy=preparing||rendering||saving||!!addingId;
  const validation=useMemo(()=>{
    if(!clips.length)return '请至少添加一个音效片段。';
    if(clips.length>MAX_CLIPS)return `一次最多拼接 ${MAX_CLIPS} 个片段。`;
    for(let index=0;index<clips.length;index++){
      const clip=clips[index];const length=clip.end-clip.start;const prefix=`第 ${index+1} 段`;
      if(!Number.isFinite(length)||clip.start<0||length<minimumClipLength-.00001||clip.end>clip.sound.duration+.0001)return `${prefix}的起止时间应在原音频内，且片段长度至少为 0.02 秒。`;
      if(clip.gain_db< -60||clip.gain_db>12)return `${prefix}音量应在 -60 到 +12 dB 之间。`;
      if(clip.fade_in<0||clip.fade_out<0||clip.fade_in+clip.fade_out>length+.0001)return `${prefix}的淡入与淡出时间之和不能超过片段长度（${durationText(length)}）。`;
    }
    if(totalDuration>MAX_DURATION+.0001)return `成品总时长为 ${durationText(totalDuration)}，请缩短到 ${MAX_DURATION} 秒以内。`;
    return '';
  },[clips,totalDuration]);

  useEffect(()=>{
    if(!confirmClose)return;
    dialogRef.current?.querySelector<HTMLButtonElement>('[role="alertdialog"] button')?.focus();
    return()=>{dialogRef.current?.focus();};
  },[confirmClose]);

  useEffect(()=>{
    let active=true;
    if(!initialPreparation.current){
      initialPreparation.current=(async()=>{
        if(initialSounds.length>MAX_CLIPS)throw new Error(`一次最多编辑 ${MAX_CLIPS} 个音效，请减少选择后重试。`);
        const ready:EditClip[]=[];
        for(let index=0;index<initialSounds.length;index++){
          if(aliveRef.current)setPrepareMessage(`正在准备 ${index+1} / ${initialSounds.length}：${initialSounds[index].name}`);
          ready.push(asClip(await prepareSound(initialSounds[index])));
          if(!aliveRef.current)break;
        }
        return ready;
      })();
    }
    initialPreparation.current.then(result=>{if(active){setClips(result);setPreparing(false);}}).catch(reason=>{if(active){setError(errorText(reason));setPreparing(false);}});
    return()=>{active=false;};
  },[]);

  useEffect(()=>{
    aliveRef.current=true;
    const audio=new Audio();audio.preload='metadata';audioRef.current=audio;
    const time=()=>setPlayTime(audio.currentTime||0);
    const play=()=>setPlaying(true);
    const pause=()=>setPlaying(false);
    const fail=()=>{if(aliveRef.current){setPlaying(false);setError('试听音频暂时无法播放。请重新生成试听，或保存后下载文件检查。');}};
    audio.addEventListener('timeupdate',time);audio.addEventListener('play',play);audio.addEventListener('pause',pause);audio.addEventListener('ended',pause);audio.addEventListener('error',fail);
    return()=>{
      aliveRef.current=false;renderVersion.current++;renderController.current?.abort();
      audio.pause();audio.removeEventListener('error',fail);audio.removeAttribute('src');audio.load();audioRef.current=null;
      if(previewIdRef.current)void discardPreview(previewIdRef.current);
    };
  },[]);

  useEffect(()=>{
    audioRef.current?.pause();setPlaying(false);setPlayTime(0);
  },[recipeKey]);

  useEffect(()=>{
    if(!pickerOpen)return;
    const controller=new AbortController();setLibraryLoading(true);setLibraryError('');
    const timer=setTimeout(()=>{request<{items:Sound[];total:number}>(`/library?${new URLSearchParams({q:libraryQuery,sort:'name'})}`,{signal:controller.signal}).then(data=>{if(!controller.signal.aborted)setLibraryItems(data.items);}).catch(reason=>{if(!controller.signal.aborted)setLibraryError(errorText(reason));}).finally(()=>{if(!controller.signal.aborted)setLibraryLoading(false);});},160);
    return()=>{clearTimeout(timer);controller.abort();};
  },[pickerOpen,libraryQuery]);

  const close=useCallback(()=>{
    if(saving)return;
    if(rendering){setConfirmClose(true);return;}
    onCloseRef.current();
  },[rendering,saving]);
  const closeRef=useRef(close);closeRef.current=close;
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    dialogRef.current?.focus();
    const keyboard=(event:KeyboardEvent)=>keyboardRef.current(event);
    document.addEventListener('keydown',keyboard);
    return()=>{document.removeEventListener('keydown',keyboard);previous?.focus();};
  },[]);

  function changeClip(key:string,patch:Partial<AudioClipRecipe>){
    setError('');
    setClips(current=>current.map(clip=>{
      if(clip.key!==key)return clip;
      const next={...clip,...patch};
      const length=Math.max(0,next.end-next.start);
      const fades=next.fade_in+next.fade_out;
      // Keep the user's balance when a shorter crop can no longer fit both fades.
      // Round down to milliseconds so the sum never exceeds the selected audio.
      if(('start' in patch||'end' in patch)&&fades>length&&fades>0){
        next.fade_in=Math.floor(next.fade_in*length/fades*1000)/1000;
        next.fade_out=Math.min(length-next.fade_in,Math.floor(next.fade_out*length/fades*1000)/1000);
      }
      return next;
    }));
  }
  function changeStart(clip:EditClip,value:number){const start=clamp(value,0,Math.max(0,clip.sound.duration-minimumClipLength));changeClip(clip.key,{start,end:Math.min(clip.sound.duration,Math.max(clip.end,start+minimumClipLength))});}
  function changeEnd(clip:EditClip,value:number){const end=clamp(value,minimumClipLength,clip.sound.duration);changeClip(clip.key,{end,start:Math.max(0,Math.min(clip.start,end-minimumClipLength))});}
  function moveClip(index:number,direction:number){setClips(current=>{const next=[...current];[next[index],next[index+direction]]=[next[index+direction],next[index]];return next;});}
  function duplicateClip(index:number){if(clips.length>=MAX_CLIPS)return;setClips(current=>{const next=[...current];next.splice(index+1,0,{...current[index],key:crypto.randomUUID()});return next;});}
  async function addSound(sound:Sound){
    if(busy||clips.length>=MAX_CLIPS)return;
    setAddingId(sound.id);setError('');
    try{const ready=await prepareSound(sound);if(aliveRef.current){setClips(current=>[...current,asClip(ready)]);setLibraryItems(current=>current.map(item=>item.id===ready.id?ready:item));}}
    catch(reason){if(aliveRef.current)setError(errorText(reason));}
    finally{if(aliveRef.current)setAddingId('');}
  }
  async function playPreview(result:AudioEditPreview,saved:Sound|null=null){
    const audio=audioRef.current;if(!audio)return;
    const url=saved?`/api/audio/${encodeURIComponent(saved.id)}`:result.preview_url;
    if(audio.src!==new URL(url,location.origin).href){audio.pause();audio.src=url;setPlayTime(0);}
    if(!audio.paused){audio.pause();return;}
    try{await audio.play();}catch(reason){if(aliveRef.current&&!(reason instanceof DOMException&&reason.name==='AbortError'))setError('试听已生成。浏览器暂未开始播放，请点击「播放试听」再试。');}
  }
  async function renderPreview(){
    if(busy||validation)return;
    if(preview&&!stale){await playPreview(preview.result,alreadySaved?savedSound:null);return;}
    audioRef.current?.pause();setRendering(true);setError('');
    const controller=new AbortController();renderController.current=controller;
    const version=++renderVersion.current;const submittedKey=recipeKey;
    try{
      const result=await request<AudioEditPreview>('/editor/render',{method:'POST',body:JSON.stringify(recipe),signal:controller.signal});
      if(!aliveRef.current||controller.signal.aborted||version!==renderVersion.current){await discardPreview(result.id);return;}
      const previousId=previewIdRef.current;previewIdRef.current=result.id;
      setPreview({result,recipeKey:submittedKey});setSavedSound(null);setSavedRecipeKey('');setPlayTime(0);
      if(previousId&&previousId!==result.id)void discardPreview(previousId);
      await playPreview(result);
    }catch(reason){if(aliveRef.current&&!(reason instanceof DOMException&&reason.name==='AbortError'))setError(`${errorText(reason)} 原始素材仍保留，可以调整后重新试听。`);}
    finally{if(aliveRef.current&&version===renderVersion.current){setRendering(false);renderController.current=null;}}
  }
  async function save(){
    if(!preview||stale||busy||validation||!name.trim()||alreadySaved)return;
    setSaving(true);setError('');
    try{
      const sound=await post<Sound>('/editor/save',{preview_id:preview.result.id,name:name.trim(),folder_id:folderId||null});
      if(!aliveRef.current)return;
      audioRef.current?.pause();setSavedSound(sound);setSavedRecipeKey(recipeKey);onSaved(sound);
    }catch(reason){if(aliveRef.current)setError(errorText(reason));}
    finally{if(aliveRef.current)setSaving(false);}
  }

  keyboardRef.current=(event:KeyboardEvent)=>{
    if(event.defaultPrevented||event.isComposing||event.keyCode===229)return;
    if(event.key==='Escape'){
      event.preventDefault();event.stopPropagation();if(event.repeat)return;
      if(confirmClose)setConfirmClose(false);else if(pickerOpen)setPickerOpen(false);else closeRef.current();
      return;
    }
    if(event.key==='Tab'){
      const scope=confirmClose?dialogRef.current?.querySelector('[role="alertdialog"]'):dialogRef.current;
      const controls=Array.from(scope?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary')||[]).filter(element=>element.getClientRects().length>0);
      const first=controls[0],last=controls.at(-1);
      if(!scope?.contains(document.activeElement)||document.activeElement===scope){event.preventDefault();(event.shiftKey?last:first)?.focus();}
      else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
      return;
    }
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'&&!event.altKey){
      event.preventDefault();event.stopPropagation();
      if(event.repeat||confirmClose||pickerOpen||busy)return;
      if(!preview||stale){setError('请先生成当前剪辑的试听，再按 Ctrl / ⌘ + S 保存。');return;}
      if(!name.trim()){setError('请先填写新音效名称。');return;}
      void save();return;
    }
    if(event.ctrlKey||event.metaKey||event.altKey||typingTarget(event.target))return;
    if(isSpace(event)||event.key.toLowerCase()==='k'){
      if(isSpace(event)&&nativeSpaceTarget(event.target))return;
      event.preventDefault();event.stopPropagation();
      if(!event.repeat&&!confirmClose&&!pickerOpen)void renderPreview();
      return;
    }
    if(['ArrowLeft','ArrowRight','Home'].includes(event.key)&&!nativeNavigationTarget(event.target)){
      event.preventDefault();event.stopPropagation();
      const audio=audioRef.current;
      if(!audio||!preview||stale||busy||confirmClose||pickerOpen||!Number.isFinite(audio.duration))return;
      audio.currentTime=event.key==='Home'?0:clamp(audio.currentTime+(event.key==='ArrowLeft'?-1:1)*(event.shiftKey?5:1),0,audio.duration);
      setPlayTime(audio.currentTime);
    }
  };

  return <div className="audio-editor-backdrop"><div className="audio-editor-dialog" role="dialog" aria-modal="true" aria-label="音频剪辑" tabIndex={-1} ref={dialogRef}>
    <header className="audio-editor-header"><div className="editor-title-icon"><Scissors size={23}/></div><div><span className="eyebrow">SOUND VAULT / AUDIO EDITOR</span><h2>让声音，刚刚好。</h2><p>精确截取 · 顺序拼接 · 音量与淡入淡出</p></div><button className="icon-button" aria-label="关闭音频剪辑" disabled={saving} onClick={close}><X size={20}/></button></header>
    <div className="audio-editor-scroll"><div className="audio-editor-layout"><section className="editor-clips-section" aria-label="剪辑片段列表">
      <div className="editor-shortcut-strip"><span><kbd>空格</kbd> 播放 / 暂停 · <kbd>← →</kbd> 移动试听位置</span><ShortcutHelp editor/></div>
      {initialSounds[0]?.source==='video'&&<div className="editor-video-source"><b>视频音轨已提取 · {initialSounds[0].original_filename||initialSounds[0].name}</b><p>{(initialSounds[0].audio_track_count||1)>1?`视频有 ${initialSounds[0].audio_track_count} 条音轨，当前使用第一条。`:''}拖动波形两端或填写起止秒数，点击「生成试听」，满意后命名并选择文件夹保存。{initialSounds[0].duration>240?'已先选中前 4 分钟，可移动起止点截取后面的声音。':''}</p><p>未保存的音轨是临时草稿，关闭窗口后清理；原视频保持不变。</p></div>}
      <div className="editor-section-title"><h3><Scissors size={16}/>音频片段</h3><span>{clips.length} / {MAX_CLIPS} 段</span></div>
      {preparing?<div className="editor-preparing" role="status"><LoaderCircle size={27} className="spin"/><b>{prepareMessage}</b><p>在线音效会先保存公开音频，再进入剪辑。</p></div>:<>
        {clips.map((clip,index)=>{const clipDuration=clip.end-clip.start;const max=clip.sound.duration;return <fieldset key={clip.key} className="editor-clip-card" disabled={busy} aria-label={`第 ${index+1} 段音效`}><div className="editor-clip-heading"><span className="clip-number">{String(index+1).padStart(2,'0')}</span><div><h4 title={clip.sound.name}>{clip.sound.name}</h4><p>原长 {durationText(max)} <ChevronRight size={10}/> 选中 <b>{durationText(clipDuration)}</b></p></div><div className="clip-order-buttons"><button type="button" className="icon-button" disabled={index===0||busy} aria-label={`上移第 ${index+1} 段`} title="上移片段" onClick={()=>moveClip(index,-1)}><ArrowUp size={14}/></button><button type="button" className="icon-button" disabled={index===clips.length-1||busy} aria-label={`下移第 ${index+1} 段`} title="下移片段" onClick={()=>moveClip(index,1)}><ArrowDown size={14}/></button><button type="button" className="icon-button" disabled={clips.length>=MAX_CLIPS||busy} aria-label={`复制第 ${index+1} 段`} title="重复使用这段音效" onClick={()=>duplicateClip(index)}><Copy size={14}/></button><button type="button" className="icon-button" disabled={busy} aria-label={`删除第 ${index+1} 段`} title="移除片段" onClick={()=>setClips(current=>current.filter(item=>item.key!==clip.key))}><Trash2 size={14}/></button></div></div>
          <div className="clip-trim-wave" style={{'--clip-start':`${clip.start/max*100}%`,'--clip-end':`${clip.end/max*100}%`} as CSSProperties}><AudioWave waveform={clip.sound.waveform}/><div className="clip-wave-shade before"/><div className="clip-wave-shade after"/><div className="clip-wave-selection"/><input type="range" aria-label={`第 ${index+1} 段截取起点`} min={0} max={max} step={0.001} value={clip.start} onChange={event=>changeStart(clip,finite(event.target.value))}/><input type="range" aria-label={`第 ${index+1} 段截取终点`} min={0} max={max} step={0.001} value={clip.end} onChange={event=>changeEnd(clip,finite(event.target.value))}/></div>
          <div className="clip-wave-scale"><span>0.00s</span><span>拖动两端手柄截取</span><span>{max.toFixed(2)}s</span></div>
          <div className="clip-time-fields"><label>起点<input type="number" aria-label={`第 ${index+1} 段起点秒数`} min={0} max={max} step={0.001} value={round(clip.start)} onChange={event=>changeStart(clip,finite(event.target.value))}/><span>秒</span></label><span className="clip-time-arrow">→</span><label>终点<input type="number" aria-label={`第 ${index+1} 段终点秒数`} min={0} max={max} step={0.001} value={round(clip.end)} onChange={event=>changeEnd(clip,finite(event.target.value))}/><span>秒</span></label><button type="button" className="text-button" onClick={()=>changeClip(clip.key,{start:0,end:max})}>恢复整段</button></div>
          <FadeEnvelope index={index} waveform={clip.sound.waveform} sourceDuration={clip.sound.duration} start={clip.start} end={clip.end} fadeIn={clip.fade_in} fadeOut={clip.fade_out} fadeInCurve={clip.fade_in_curve??'linear'} fadeOutCurve={clip.fade_out_curve??'linear'} disabled={busy} onChange={values=>changeClip(clip.key,values)} onCurveChange={(side,curve)=>changeClip(clip.key,side==='in'?{fade_in_curve:curve}:{fade_out_curve:curve})}/>
          <div className="clip-gain-control"><label className="field-label">片段音量<div className="editor-unit-input"><input type="number" aria-label={`第 ${index+1} 段音量 dB`} min={-60} max={12} step={0.5} value={clip.gain_db} onChange={event=>changeClip(clip.key,{gain_db:clamp(finite(event.target.value),-60,12)})}/><span>dB</span></div></label></div>
        </fieldset>;})}
        {!clips.length&&<div className="editor-no-clips"><AudioLines size={29}/><h3>添加第一段声音</h3><p>从素材库选择音效，开始截取或拼接。</p></div>}
        <button className="editor-add-clip" type="button" disabled={busy||clips.length>=MAX_CLIPS} onClick={()=>setPickerOpen(open=>!open)} aria-expanded={pickerOpen}><Plus size={17}/>{pickerOpen?'收起素材选择':'添加音效片段'}<span>同一个音效可以重复添加</span></button>
        {pickerOpen&&<div className="editor-sound-picker"><div className="editor-picker-search"><Search size={15}/><input aria-label="搜索待添加音效" placeholder="搜索本机素材，中英文均可" value={libraryQuery} onChange={event=>setLibraryQuery(event.target.value)} disabled={busy}/></div><div className="editor-picker-items">{libraryLoading?<p className="editor-picker-message"><LoaderCircle className="spin" size={16}/>正在读取素材库…</p>:libraryError?<p className="editor-picker-message error">{libraryError}</p>:!libraryItems.length?<p className="editor-picker-message">没有找到音效，请试试其他关键词。</p>:libraryItems.slice(0,80).map(sound=><div className="editor-picker-row" key={sound.id}><AudioLines size={18}/><div><b>{sound.name}</b><span>{durationText(sound.duration)} · {sound.downloaded?'已在本机':'添加时下载公开音频'}</span></div><button type="button" className="button small secondary" aria-label={`添加 ${sound.name} 到剪辑`} disabled={busy||clips.length>=MAX_CLIPS} onClick={()=>addSound(sound)}>{addingId===sound.id?<LoaderCircle className="spin" size={13}/>:<Plus size={13}/>}添加</button></div>)}</div>{libraryItems.length>80&&<p className="editor-picker-message">显示前 80 个音效，请输入关键词缩小范围。</p>}</div>}
      </>}
    </section><aside className="editor-output-section"><div className="editor-section-title"><h3><SlidersHorizontal size={16}/>成品设置</h3><span>FINAL MIX</span></div><fieldset className="editor-output-controls" disabled={busy}><div className="editor-output-stat"><span>预计成品时长</span><strong className={totalDuration>MAX_DURATION?'over-limit':''}>{durationText(totalDuration)}</strong><small>按列表顺序拼接 · 最长 {MAX_DURATION} 秒</small></div><label className="field-label">总音量<div className="editor-unit-input"><input type="number" aria-label="总音量 dB" min={-24} max={12} step={0.5} value={masterGain} onChange={event=>setMasterGain(clamp(finite(event.target.value),-24,12))}/><span>dB</span></div></label><label className="field-label">片段之间的静音间隔<div className="editor-unit-input"><input type="number" aria-label="片段间隔秒数" min={0} max={5} step={0.01} value={gap} onChange={event=>setGap(clamp(finite(event.target.value),0,5))}/><span>秒</span></div></label><label className="field-label">导出格式<select aria-label="导出格式" value={format} onChange={event=>setFormat(event.target.value as 'wav'|'mp3')}><option value="wav">WAV</option><option value="mp3">MP3</option></select></label></fieldset>
      {!preparing&&validation&&<div className="editor-inline-message error" role="alert"><Info size={14}/><span>{validation}</span></div>}
      {error&&<div className="editor-inline-message error" role="alert"><Info size={14}/><span>{error}</span></div>}
      <div className={`editor-preview-panel ${stale?'stale':''}`}><header><Headphones size={15}/><b>成品试听</b><span>{stale?'需要重新试听':preview?'已生成':'尚未生成'}</span></header>{preview&&<><div className="editor-preview-wave"><AudioWave waveform={preview.result.waveform}/></div><div className="editor-preview-progress"><span>{playTime.toFixed(1)}s</span><input aria-label="剪辑试听进度" type="range" min={0} max={preview.result.duration||1} step={0.01} value={Math.min(playTime,preview.result.duration||1)} disabled={stale||busy} onChange={event=>{if(audioRef.current){audioRef.current.currentTime=finite(event.target.value);setPlayTime(finite(event.target.value));}}}/><span>{preview.result.duration.toFixed(1)}s</span></div></>}{stale&&<p className="editor-stale-message" role="status">剪辑已修改，旧试听已过期。请重新生成试听后再保存。</p>}<button type="button" className="button full" aria-label={rendering?'正在生成试听':preview&&!stale?(playing?'暂停试听':'播放试听'):'生成试听'} disabled={busy||!!validation} onClick={renderPreview}>{rendering?<LoaderCircle className="spin" size={16}/>:playing&&!stale?<Pause size={15}/>:<Play size={15}/>} {rendering?'正在生成试听…':preview&&!stale?(playing?'暂停试听':'播放试听'):stale?'重新生成试听':'生成试听'}</button>{preview&&!stale&&<button type="button" className="button secondary full daw-export-button" aria-label="下载 WAV 给 DAW" disabled={busy||preview.result.format!=='wav'} onClick={()=>downloadEditorPreview(preview.result.id,name.trim()||'声匣音效')}><Download size={15}/>下载 WAV 给 DAW</button>}<p className="editor-render-note">{rendering?'正在本机处理音频，编辑已暂时锁定。':preview&&!stale&&preview.result.format==='wav'?'WAV · 44.1 kHz · 16 位 PCM；下载后可拖入 DAW 时间线，不必先存入素材库。':'点击后在本机处理音频，不调用 AI。选择 WAV 后可下载给 DAW。'}</p></div>
      {preview&&!stale&&preview.result.warnings?.length>0&&<div className="editor-render-warnings">{preview.result.warnings.map((warning,index)=><p key={index}><Info size={12}/>{warning}</p>)}</div>}
      <fieldset className="editor-save-fields" disabled={busy||alreadySaved}><label className="field-label">新音效名称<input aria-label="剪辑音效名称" maxLength={200} value={name} onChange={event=>setName(event.target.value)} placeholder="给这个声音起个名字"/></label><label className="field-label">保存到文件夹<select aria-label="剪辑保存文件夹" value={folderId} onChange={event=>setFolderId(event.target.value)}><option value="">未分类</option><FolderOptions folders={folders}/></select></label></fieldset>
      {alreadySaved&&savedSound?<div className="editor-saved-result" role="status"><div><Check size={17}/><b>已保存到素材库</b></div><p>{savedSound.name}</p><button type="button" className="button secondary full" aria-label="下载剪辑音效" onClick={()=>fileDownload(savedSound.id)}><Download size={15}/>下载 {savedSound.format?.toUpperCase()||format.toUpperCase()}</button></div>:<button type="button" className="button secondary full editor-save-button" aria-label="保存新音效" disabled={busy||!!validation||!preview||stale||!name.trim()} onClick={save}>{saving?<LoaderCircle className="spin" size={15}/>:<Folder size={15}/>} {saving?'正在保存…':'保存新音效'}</button>}
    </aside></div></div><footer className="audio-editor-footer"><span><Check size={13}/>原素材保留，新音效单独保存</span><span>本机剪辑 · 无 AI 费用</span><button className="button small secondary" disabled={saving} onClick={close}>完成</button></footer>
    {confirmClose&&<div className="editor-cancel-overlay" role="alertdialog" aria-label="取消正在生成的试听"><div><h3>放弃这份剪辑草稿？</h3><p>试听仍在生成。关闭将取消等待，当前未保存的剪辑设置会丢弃，原始素材仍然保留。</p><div><button className="button secondary" onClick={()=>setConfirmClose(false)}>继续等待</button><button className="button" onClick={()=>{renderVersion.current++;renderController.current?.abort();onCloseRef.current();}}>放弃并关闭</button></div></div></div>}
  </div></div>;
}
