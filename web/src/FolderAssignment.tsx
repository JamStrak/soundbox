import {useRef, useState} from 'react';
import type {FormEvent, ReactNode} from 'react';
import {Check, FolderPlus, LoaderCircle, Plus} from 'lucide-react';
import {errorText, patch, post} from './api';
import type {Folder, Sound} from './api';

export default function FolderAssignment({sounds,folders,onSound,onFolder,onComplete,onCancel,onBusy}:{sounds:Sound[];folders:Folder[];onSound:(sound:Sound)=>void;onFolder:(folder:Folder)=>void;onComplete:()=>void;onCancel:()=>void;onBusy:(busy:boolean)=>void}) {
  const [pending,setPending]=useState(sounds);
  const [target,setTarget]=useState(sounds.length===1?sounds[0].folder_id||'':'');
  const [creating,setCreating]=useState(false);
  const [name,setName]=useState('');
  const [parent,setParent]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const running=useRef(false);
  function options():ReactNode[] {
    const result:ReactNode[]=[];
    function walk(id:string|null,depth:number){for(const folder of folders.filter(item=>item.parent_id===id)){result.push(<option key={folder.id} value={folder.id}>{'　'.repeat(depth)}{folder.name}</option>);walk(folder.id,depth+1);}}
    walk(null,0);return result;
  }
  async function submit(event:FormEvent) {
    event.preventDefault();if(running.current||!pending.length||creating&&!name.trim())return;
    running.current=true;setBusy(true);onBusy(true);setError('');
    let destination=target;const remaining=[...pending];
    try {
      if(creating){const folder=await post<Folder>('/folders',{name:name.trim(),parent_id:parent||null});onFolder(folder);destination=folder.id;setTarget(folder.id);setCreating(false);setName('');}
      while(remaining.length){const sound=remaining[0];const saved=sound.saved?sound:await post<Sound>('/sounds/save',{sound});remaining[0]=saved;if(!sound.saved)onSound(saved);const updated=await patch<Sound>(`/sounds/${encodeURIComponent(saved.id)}`,{folder_id:destination||null});onSound(updated);remaining.shift();}
      onComplete();
    } catch(cause) {
      setPending(remaining);
      const done=pending.length-remaining.length;
      setError(`${done?`已加入 ${done} 条，剩余 ${remaining.length} 条可重试。 `:''}${errorText(cause)}`);
    } finally {running.current=false;setBusy(false);onBusy(false);}
  }
  return <form className="simple-form folder-assignment" onSubmit={submit}>
    <div className="assignment-summary"><FolderPlus size={20}/><div><b>{pending.length===1?pending[0].name:`已选 ${pending.length} 条音效`}</b><p>选择文件夹即可归档。收藏状态保留，在线音效会同时加入我的素材。</p></div></div>
    {!creating?<label className="field-label">目标文件夹<select aria-label="目标文件夹" value={target} disabled={busy} onChange={event=>setTarget(event.target.value)}><option value="">未分类</option>{options()}</select></label>:<div className="assignment-create"><label className="field-label">新文件夹名称<input aria-label="新文件夹名称" autoFocus value={name} maxLength={80} required disabled={busy} placeholder="例如：项目 A · 战斗" onChange={event=>setName(event.target.value)}/></label><label className="field-label">上级文件夹<select aria-label="新文件夹上级" value={parent} disabled={busy} onChange={event=>setParent(event.target.value)}><option value="">顶层文件夹</option>{options()}</select></label></div>}
    <button type="button" className="text-button assignment-new" disabled={busy} onClick={()=>{setCreating(value=>!value);setError('');}}><Plus size={14}/>{creating?'选择已有文件夹':'新建文件夹'}</button>
    {error&&<p className="assignment-error" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="button secondary" disabled={busy} onClick={onCancel}>取消</button><button type="submit" className="button" aria-label="确认加入" disabled={busy||creating&&!name.trim()}>{busy?<LoaderCircle size={15} className="spin"/>:<Check size={15}/>} {busy?'正在加入…':'确认加入'}</button></div>
  </form>;
}
