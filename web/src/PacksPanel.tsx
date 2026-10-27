import {useCallback, useEffect, useRef, useState} from 'react';
import {ArrowDownToLine, Check, FolderDown, LoaderCircle, Package, RefreshCw, ShieldCheck} from 'lucide-react';
import {errorText, request} from './api';
import type {AudioTask, GamePack} from './api';
import {AudioTaskProgress, useAudioTask} from './QualityTools';

function bytesLabel(value:number) {return value>=1024*1024?`${(value/1024/1024).toFixed(1)} MiB`:`${Math.ceil(value/1024)} KiB`;}

export function useGamePacks(onUpdate:(task:AudioTask)=>void) {
  const [items,setItems]=useState<GamePack[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [installing,setInstalling]=useState(false);
  const [queueName,setQueueName]=useState('');
  const stopQueue=useRef(false);
  const installingRef=useRef(false);
  const mounted=useRef(true);
  const control=useAudioTask(onUpdate);
  const load=useCallback(async()=>{
    setLoading(true);setError('');
    try{const data=await request<{items:GamePack[]}>('/game-packs');if(mounted.current)setItems(data.items);}
    catch(cause){if(mounted.current)setError(errorText(cause));}
    finally{if(mounted.current)setLoading(false);}
  },[]);
  useEffect(()=>{mounted.current=true;void load();return()=>{mounted.current=false;stopQueue.current=true;};},[load]);
  useEffect(()=>{if(control.task&&['complete','failed','cancelled'].includes(control.task.status))void load();},[control.task?.id,control.task?.status,load]);
  async function install(ids:string[]){
    if(installingRef.current||control.busy||!ids.length)return;
    installingRef.current=true;stopQueue.current=false;setInstalling(true);setError('');
    try{
      for(let index=0;index<ids.length;index++){
        if(stopQueue.current||!mounted.current)break;
        const pack=items.find(item=>item.id===ids[index]);
        setQueueName(`${pack?.name||'游戏音效包'}${ids.length>1?` · ${index+1} / ${ids.length}`:''}`);
        const result=await control.run(`/game-packs/${encodeURIComponent(ids[index])}/install`);
        if(mounted.current)await load();
        if(result.status!=='complete')break;
      }
    }catch(cause){if(mounted.current&&!(cause instanceof DOMException&&cause.name==='AbortError'))setError(errorText(cause));}
    finally{installingRef.current=false;if(mounted.current){setInstalling(false);setQueueName('');}}
  }
  const cancel=async()=>{stopQueue.current=true;await control.cancel();};
  return {items,loading,error,busy:installing||control.busy,queueName,load,install,control:{...control,cancel}};
}
type PacksControl=ReturnType<typeof useGamePacks>;

export default function PacksPanel({packs}:{packs:PacksControl}) {
  const remaining=packs.items.filter(pack=>pack.installed_count<pack.count);
  const totalBytes=remaining.reduce((sum,pack)=>sum+pack.bytes,0);
  return <section className="game-packs-panel expanded" aria-label="离线游戏音效包">
    <header><div className="game-packs-icon"><Package size={23}/></div><div><span className="eyebrow">GAME AUDIO / OFFLINE READY</span><h2>离线游戏音效包</h2><p>官方 Kenney 游戏音效包 · 安装后可离线中英文搜索 · CC0</p></div></header>
    <div className="game-packs-grid">{packs.loading&&!packs.items.length?<p className="pack-loading"><LoaderCircle size={17} className="spin"/>正在读取官方音效包…</p>:packs.items.map(pack=><article className="game-pack-card" key={pack.id}><div className="game-pack-card-heading"><AudioPackIcon installed={pack.installed_count>=pack.count}/><span>CC0 · KENNEY</span></div><h3>{pack.name}</h3><p>{pack.description}</p><div className="game-pack-meta"><span>{pack.count} 个音效</span><span>{bytesLabel(pack.bytes)}</span></div><button type="button" className={`button small ${pack.installed_count>=pack.count?'secondary':''}`} aria-label={`安装 ${pack.name}`} disabled={packs.busy||pack.installed_count>=pack.count} onClick={()=>void packs.install([pack.id])}>{pack.installed_count>=pack.count?<Check size={14}/>:<ArrowDownToLine size={14}/>} {pack.installed_count>=pack.count?'已安装':pack.installed_count>0?`继续安装（${pack.installed_count}/${pack.count}）`:'安装音效包'}</button></article>)}</div>{remaining.length>0&&<div className="game-packs-actions"><span><ShieldCheck size={14}/>来源与许可随素材保留；适合与否仍需试听。</span><button type="button" className="button small" aria-label="安装全部游戏音效包" disabled={packs.busy||packs.loading} onClick={()=>void packs.install(remaining.map(pack=>pack.id))}>{packs.busy?<LoaderCircle size={15} className="spin"/>:<FolderDown size={15}/>}安装全部{remaining.length>1?` ${remaining.length} 个包`:''}<small>{bytesLabel(totalBytes)}</small></button></div>}
    {packs.busy&&packs.queueName&&<p className="pack-current-name">正在准备 {packs.queueName}</p>}
    <AudioTaskProgress control={packs.control} title="游戏包安装"/>
    {packs.error&&<div className="audio-task-error" role="alert"><span>{packs.error}</span><button type="button" className="text-button" disabled={packs.busy} onClick={()=>void packs.load()}><RefreshCw size={13}/>重新读取包列表</button></div>}
  </section>;
}
function AudioPackIcon({installed}:{installed:boolean}){return <span className={`pack-status-icon ${installed?'installed':''}`}>{installed?<Check size={19}/>:<Package size={19}/>}</span>;}
