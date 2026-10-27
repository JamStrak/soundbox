import {FolderCog, LoaderCircle, Settings2} from 'lucide-react';

export type SearchSource = 'catalog' | 'openverse' | 'freesound' | 'library' | 'game';
const sources: {id: SearchSource; name: string; hint: string}[] = [
  {id:'catalog',name:'精选开放音效',hint:'CC0 · 免密钥'},
  {id:'openverse',name:'Openverse',hint:'公开搜索 · 免密钥'},
  {id:'freesound',name:'Freesound',hint:'需 API Key'},
  {id:'library',name:'我的素材',hint:'收藏 / 导入 / 本地包'},
  {id:'game',name:'游戏音效包',hint:'已安装 / 已索引'},
];

export default function SearchSources({active,onSelect,onManage,busy,keyReady,onSettings,localContext}:{active:SearchSource;onSelect:(source:SearchSource)=>void;onManage:()=>void;busy:boolean;keyReady:boolean;onSettings:()=>void;localContext:string}) {
  const description = active==='catalog'?'精选多作者游戏音效，保留 CC0 来源；首次试听会缓存所在官方包。':active==='openverse'?'搜索更广的公开音效，每条结果显示其使用许可。':active==='freesound'?(keyReady?'直接搜索 Freesound；授权账户后可下载原文件。':'连接 Freesound 后可直连检索，也可以先使用两个免密钥来源。'):active==='game'?'搜索本机已安装、已索引或已保存的游戏素材包；每条声音保留原包许可。':`正在搜索${localContext}。已保存的声音与本地 ZIP 索引可在「全部音效」中查看。`;
  return <section className="search-sources" aria-label="选择搜索来源">
    <div className="source-heading"><span>搜索来源</span><button type="button" className="text-button" onClick={onManage} aria-label="管理音效包">{busy?<LoaderCircle size={15} className="spin"/>:<FolderCog size={15}/>}管理音效包{busy&&<small>任务进行中</small>}</button></div>
    <div className="source-choices" role="group" aria-label="搜索范围">{sources.map(source=><button key={source.id} type="button" aria-label={`搜索来源：${source.name}`} aria-pressed={active===source.id} className={`source-choice ${active===source.id?'active':''}`} onClick={()=>onSelect(source.id)}><strong>{source.name}</strong><small>{source.id==='freesound'&&keyReady?'已连接':source.hint}</small></button>)}</div>
    <div className="source-description"><p>{description}</p>{active==='freesound'&&!keyReady&&<button type="button" className="text-button" onClick={onSettings}><Settings2 size={13}/>配置 API Key</button>}</div>
  </section>;
}
