import {useEffect, useState} from 'react';
import {ArrowUpRight, ChevronDown, ChevronRight, FolderSearch, Library, LoaderCircle, Search, ShieldCheck} from 'lucide-react';
import {errorText, request} from './api';
import type {AudioTask, CatalogPack, LocalPack} from './api';
import {AudioTaskProgress, useAudioTask} from './QualityTools';

export function bytesLabel(value:number) {return value>=1024*1024?`${(value/1024/1024).toFixed(1)} MiB`:`${Math.ceil(value/1024)} KiB`;}

export default function CatalogPanel({active,selectedPack,onSearch,onTaskUpdate,onLocalComplete,refresh}:{active:boolean;selectedPack:string;onSearch:(packId:string)=>void;onTaskUpdate:(task:AudioTask)=>void;onLocalComplete:()=>void;refresh:number}) {
  const [packs,setPacks]=useState<CatalogPack[]>([]);
  const [localPacks,setLocalPacks]=useState<LocalPack[]>([]);
  const [localOpen,setLocalOpen]=useState(true);
  const [path,setPath]=useState('');
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const task=useAudioTask(onTaskUpdate);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');
    Promise.all([request<{items:CatalogPack[]}>('/catalog/packs',{signal:controller.signal}),request<{items:LocalPack[]}>('/local-packs',{signal:controller.signal})])
      .then(([catalog,local])=>{if(!controller.signal.aborted){setPacks(catalog.items);setLocalPacks(local.items);}})
      .catch(cause=>{if(!controller.signal.aborted)setError(errorText(cause));})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[refresh]);
  async function indexZip(){
    if(!path.trim()||task.busy)return;setError('');
    try{const result=await task.run('/local-packs/index',{path:path.trim()});if(result.status==='complete')onLocalComplete();}
    catch(cause){if(!(cause instanceof DOMException&&cause.name==='AbortError'))setError(errorText(cause));}
  }
  const count=packs.reduce((sum,pack)=>sum+pack.count,0);
  const authors=new Set(packs.map(pack=>pack.author)).size;
  return <section className="catalog-panel" aria-label="精选开放音效包">
    <header><div className="catalog-icon"><Library size={23}/></div><div><span className="eyebrow">OPEN GAME AUDIO / VERIFIED SOURCES</span><h2>精选开放音效包</h2><p>{loading?'正在读取开放音效目录…':`${count.toLocaleString()} 条音效 · ${packs.length} 个开放包 · ${authors} 位作者`} · 中英文关键词检索</p></div><button type="button" className="button small" onClick={()=>onSearch('')}><Search size={14}/>浏览全部精选音效</button></header>
    {active&&<label className="catalog-pack-filter">音效包筛选<select aria-label="音效包筛选" value={selectedPack} onChange={event=>onSearch(event.target.value)}><option value="">所有开放音效包</option>{packs.map(pack=><option key={pack.id} value={pack.id}>{pack.name} · {pack.author} · {pack.count} 条</option>)}</select></label>}
    <p className="catalog-note"><ShieldCheck size={14}/>来源与 CC0 许可已核实。首次试听缓存所在官方包并提取选中音频；收藏或加入文件夹可保存到个人素材库。适合游戏的听感仍请实际试听。</p>
    <div className="local-pack-section"><button type="button" className="text-button" aria-expanded={localOpen} onClick={()=>setLocalOpen(value=>!value)}><FolderSearch size={15}/>把已下载的音效 ZIP 加入搜索{localPacks.length>0&&<small>已索引 {localPacks.length} 个包</small>}{localOpen?<ChevronDown size={14}/>:<ChevronRight size={14}/>}</button>{localOpen&&<div className="local-pack-form"><p>适合免费赠品和你已有的正版音效包。输入这台电脑上的 ZIP 路径，只在本机建立目录，试听时再解压选中音频。沿用原包许可，不会自动变成 CC0。</p><form onSubmit={event=>{event.preventDefault();void indexZip();}}><label htmlFor="local-audio-pack-path" className="sr-only">本地音效 ZIP 路径</label><input id="local-audio-pack-path" aria-label="本地音效 ZIP 路径" value={path} onChange={event=>setPath(event.target.value)} placeholder={'例如 E:\\音效素材\\免费音效包.zip'} disabled={task.busy}/><button type="submit" className="button small" disabled={task.busy||!path.trim()}>{task.busy?<LoaderCircle size={14} className="spin"/>:<FolderSearch size={14}/>}建立搜索目录</button></form>{localPacks.length>0&&<details className="local-pack-indexed"><summary>查看已索引的 {localPacks.length} 个音效包</summary><div className="local-pack-list">{localPacks.map(pack=><div key={pack.id}><b>{pack.name}</b><span>{pack.count} 条 · {pack.license||'沿用原包许可'}</span></div>)}</div></details>}</div>}<AudioTaskProgress control={task} title="本地音效包索引"/></div>
    <div className="catalog-grid">{packs.map(pack=><article key={pack.id} className={`catalog-card ${selectedPack===pack.id?'selected':''}`}><div className="catalog-card-heading"><span>{pack.license} · {pack.author}</span><a href={pack.source_url} target="_blank" rel="noreferrer" aria-label={`查看 ${pack.name} 来源`}>来源<ArrowUpRight size={12}/></a></div><h3>{pack.name}</h3><p>{pack.description}</p><div className="catalog-card-meta"><span>{pack.count} 条</span><span>首次缓存 {bytesLabel(pack.bytes)}</span></div><button type="button" className="button small secondary" onClick={()=>onSearch(pack.id)}><Search size={13}/>浏览这个音效包</button></article>)}</div>
    {error&&<p className="catalog-error" role="alert">{error}</p>}
  </section>;
}
