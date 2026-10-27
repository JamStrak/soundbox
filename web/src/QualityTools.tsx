import {useCallback, useEffect, useRef, useState} from 'react';
import {AudioLines, Check, Eye, Info, LoaderCircle, RefreshCw, ScanLine, X} from 'lucide-react';
import {errorText, request} from './api';
import type {AudioFilter, AudioTask, Sound, VisibilityFilter} from './api';

const activeTask = (task: AudioTask | null) => !!task && ['queued', 'running'].includes(task.status);
const flagNames: Record<string,string> = {quiet:'电平偏低',long_edges:'首尾低电平较长',peak_risk:'峰值需留意',strong_transient:'短促瞬态',silent:'数字全静音',decode_error:'无法解码'};

/** One explicit, cancellable local task. Failed polling never starts a second task. */
export function useAudioTask(onUpdate: (task:AudioTask)=>void) {
  const [task,setTask]=useState<AudioTask|null>(null);
  const [starting,setStarting]=useState(false);
  const [cancelling,setCancelling]=useState(false);
  const [error,setError]=useState('');
  const latest=useRef<AudioTask|null>(null);
  const callback=useRef(onUpdate); callback.current=onUpdate;
  const controller=useRef<AbortController|null>(null);
  const cancelRequested=useRef(false);
  const running=useRef(false);
  const lastUpdate=useRef('');
  useEffect(()=>()=>controller.current?.abort(),[]);

  const apply=useCallback((next:AudioTask)=>{
    if(latest.current?.id===next.id&&!activeTask(latest.current)&&activeTask(next))return latest.current;
    latest.current=next;setTask(next);
    const signature=`${next.id}:${next.status}:${next.completed}:${next.items.length}:${next.errors.length}`;
    if(signature!==lastUpdate.current){lastUpdate.current=signature;callback.current(next);}
    return next;
  },[]);
  const watch=useCallback(async(initial:AudioTask,signal:AbortSignal)=>{
    let value=apply(initial);
    while(activeTask(value)&&!signal.aborted){
      await new Promise<void>(resolve=>{const finish=()=>{clearTimeout(timer);signal.removeEventListener('abort',finish);resolve();};const timer=setTimeout(finish,900);signal.addEventListener('abort',finish,{once:true});});
      if(signal.aborted)throw new DOMException('Aborted','AbortError');
      value=apply(await request<AudioTask>(`/audio-tasks/${encodeURIComponent(value.id)}`,{signal}));
    }
    return value;
  },[apply]);
  const run=useCallback(async(path:string,body?:unknown)=>{
    if(running.current||activeTask(latest.current))throw new Error('请先等待当前任务完成，或取消当前任务。');
    running.current=true;
    controller.current?.abort();const attempt=new AbortController();controller.current=attempt;
    cancelRequested.current=false;setError('');setStarting(true);setCancelling(false);
    try{
      let created=await request<AudioTask>(path,{method:'POST',body:body===undefined?undefined:JSON.stringify(body),signal:attempt.signal});
      apply(created);setStarting(false);
      if(cancelRequested.current&&activeTask(created))created=await request<AudioTask>(`/audio-tasks/${encodeURIComponent(created.id)}/cancel`,{method:'POST',signal:attempt.signal});
      return await watch(created,attempt.signal);
    }catch(cause){if(!attempt.signal.aborted)setError(`${errorText(cause)}${activeTask(latest.current)?' 可重新读取状态，不会重复创建任务。':''}`);throw cause;}
    finally{running.current=false;if(!attempt.signal.aborted){setStarting(false);setCancelling(false);}}
  },[apply,watch]);
  const cancel=useCallback(async()=>{
    cancelRequested.current=true;setCancelling(true);
    if(!activeTask(latest.current))return;
    try{apply(await request<AudioTask>(`/audio-tasks/${encodeURIComponent(latest.current!.id)}/cancel`,{method:'POST'}));setError('');}
    catch(cause){setError(`取消未确认：${errorText(cause)} 请重新读取状态。`);}
    finally{setCancelling(false);}
  },[apply]);
  const retry=useCallback(async()=>{
    const id=latest.current?.id;if(!id)return;
    controller.current?.abort();const attempt=new AbortController();controller.current=attempt;setError('');
    try{await watch(await request<AudioTask>(`/audio-tasks/${encodeURIComponent(id)}`,{signal:attempt.signal}),attempt.signal);}
    catch(cause){if(!attempt.signal.aborted)setError(errorText(cause));}
  },[watch]);
  return {task,starting,busy:starting||activeTask(task),cancelling,error,run,cancel,retry};
}
export type AudioTaskControl = ReturnType<typeof useAudioTask>;

export function AudioTaskProgress({control,title}:{control:AudioTaskControl;title:string}) {
  const {task,busy,starting,cancelling,error}=control;
  if(!task&&!starting&&!error)return null;
  const percentage=task?.total?Math.min(100,task.completed/task.total*100):0;
  return <div className={`audio-task-progress ${task?.status==='failed'?'failed':''}`} aria-label={title}>
    <div className="audio-task-title">{busy?<LoaderCircle size={15} className="spin"/>:task?.status==='complete'?<Check size={15}/>:<Info size={15}/>}<b>{title}</b><span>{starting?'正在创建任务…':task?.status==='cancelled'?'已取消':task?.status==='failed'?'未完成':task?.status==='complete'?'已完成':`${task?.completed||0} / ${task?.total||0}`}</span>{busy&&<button type="button" className="text-button" aria-label={`取消${title}`} disabled={cancelling} onClick={()=>void control.cancel()}>{cancelling?'正在取消…':'取消'}<X size={12}/></button>}</div>
    {busy&&<div className="audio-task-meter" role="progressbar" aria-label={`${title}进度`} aria-valuemin={0} aria-valuemax={task?.total||100} aria-valuenow={task?.completed||0}><span style={{width:`${percentage}%`}}/></div>}
    <p role="status">{task?.message||'正在准备，原素材始终保留。'}</p>
    {!!task?.errors.length&&<details className="audio-task-errors"><summary>{task.errors.length} 项未完成，查看原因</summary><ul>{task.errors.map((message,index)=><li key={index}>{message}</li>)}</ul></details>}
    {error&&<div className="audio-task-error" role="alert"><span>{error}</span>{task&&<button type="button" className="text-button" onClick={()=>void control.retry()}><RefreshCw size={12}/>重新读取状态</button>}</div>}
  </div>;
}

export function CheckBadge({sound}:{sound:Sound}) {
  const check=sound.audio_check;
  if(sound.hidden)return <span className="audio-check-badge hidden">已隐藏</span>;
  if(!check)return <span className={`audio-check-badge ${sound.audio_check_error?'warning':'unchecked'}`} title={sound.audio_check_error||'尚未进行音频检测'}>{sound.audio_check_error?'检测失败':'未检测'}</span>;
  const unusable=check.status==='unusable';
  return <span className={`audio-check-badge ${unusable?'unusable':check.flags.length?'warning':'ready'}`} title={check.message}>{unusable?'不可播放':check.flags.includes('quiet')?'电平偏低':check.flags.some(flag=>['peak_risk','long_edges'].includes(flag))?'检测提示':'已检测'}</span>;
}

export function CheckDetails({sound}:{sound:Sound}) {
  const check=sound.audio_check;
  const level=(value:number|null)=>typeof value==='number'&&Number.isFinite(value)?`${value.toFixed(1)} dBFS`:'—';
  return <div className="audio-check-details"><div className="audio-check-heading"><ScanLine size={14}/><b>音频检查</b><CheckBadge sound={sound}/></div>{check?<><p>{check.message||'已完成客观电平检查，适合与否请结合试听判断。'}</p><div className="audio-check-metrics"><span>峰值<b>{level(check.peak_dbfs)}</b></span><span>整段 RMS<b>{level(check.rms_dbfs)}</b></span></div>{check.flags.length>0&&<div className="audio-check-flags">{check.flags.filter(flag=>flagNames[flag]).map(flag=><span key={flag}>{flagNames[flag]}</span>)}</div>}<small>客观电平不等于音质评分。低电平尾部可能包含有效混响。</small></>:<p>{sound.audio_check_error||'尚未检测。点击「检查本页」可了解电平、首尾低电平和可播放状态，不会调用 AI。'}</p>}</div>;
}

export default function QualityTools({filter,visibility,onFilter,onVisibility,onCheck,disabled,online,game,collapse,onCollapse,control,source}:{filter:AudioFilter;visibility:VisibilityFilter;onFilter:(value:AudioFilter)=>void;onVisibility:(value:VisibilityFilter)=>void;onCheck:()=>void;disabled:boolean;online:boolean;game:boolean;collapse:boolean;onCollapse:(value:boolean)=>void;control:AudioTaskControl;source?:string}) {
  const filterLabels:Record<AudioFilter,string>={all:'',checked:'只看已检测',quiet:'只看电平偏低',issues:'只看检测提示',unchecked:'只看未检测'};
  const visibilityLabels:Record<VisibilityFilter,string>={visible:'',all:'含隐藏音效',hidden:'只看隐藏 / 不可播放'};
  const activeSettings=[filterLabels[filter],visibilityLabels[visibility],game&&!collapse?'展开相似变体':''].filter(Boolean);
  const summaryStatus=[control.busy?'正在检查':null,...activeSettings].filter(Boolean).join(' · ');
  return <section className="quality-tools" aria-label="音频检查与高级筛选"><details className="quality-disclosure"><summary><ScanLine size={14}/><span>音频检查与高级筛选</span>{summaryStatus&&<span className="quality-summary-status">{summaryStatus}</span>}</summary><div className="quality-tool-controls"><div className="quality-filter"><ScanLine size={14}/><select aria-label="音频检查筛选" value={filter} onChange={e=>onFilter(e.target.value as AudioFilter)}><option value="all">全部检测状态</option><option value="checked">已检测</option><option value="quiet">电平偏低</option><option value="issues">有检测提示</option><option value="unchecked">未检测</option></select></div><div className="quality-filter"><Eye size={14}/><select aria-label="隐藏状态筛选" value={visibility} onChange={e=>onVisibility(e.target.value as VisibilityFilter)}><option value="visible">默认显示</option><option value="all">全部（含隐藏 / 不可播放）</option><option value="hidden">已隐藏 / 不可播放</option></select></div>{game&&<label className="variant-toggle"><input type="checkbox" aria-label="折叠相似变体" checked={collapse} onChange={e=>onCollapse(e.target.checked)}/>折叠相似变体</label>}<button type="button" className="button small secondary quality-check-button" aria-label="检查本页音频" disabled={disabled||control.busy} onClick={onCheck}>{control.busy?<LoaderCircle className="spin" size={14}/>:<AudioLines size={14}/>}检查本页 <span>最多 10 条</span></button></div><p className="quality-budget-note">{source==='catalog'?'只检查已下载或已试听的本机音频。未缓存条目请先试听；检查不会下载官方整包。':source==='localpack'?'只检查已下载或已试听的本机音频。未缓存条目请先试听；检查不会解压其他音效。':game?'在本机分析声音，不改变原文件。精选素材和本地包条目请先试听完成缓存。':online?'联网检查会读取公开音频：本次最多 30 MiB，单条最多 5 MiB，可随时取消。':'在本机分析声音，不改变原文件。精选素材和本地包请先试听；其他未下载在线音频最多读取 30 MiB。'}不调用 AI，不消耗 Token。</p></details><AudioTaskProgress control={control} title="音频检查"/></section>;
}
